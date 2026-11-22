"""Camera stream credentials — split, mask and encrypt at rest.

A camera URL may embed `user:password@`; storing that verbatim in
`surveillance.json` leaked the password to every reader of the state file and
to `GET /api/surveillance/cameras`. Credentials are now kept separately,
encrypted with AES-256-GCM (same primitive as PhotoStation Private), and the
URL returned to clients is always masked.

The key is derived from the appliance JWT secret (already 0600 under
`<DATA_DIR>/jwt_secret`), so no new key material has to be provisioned. A
missing/rotated secret only makes the stored password unreadable — it never
takes the app down; the operator re-enters the password.
"""

import base64
import hashlib
import logging
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.camera_secrets")

_MAGIC = b"ETHOS-CAMCRED1\x00"
_KEY_INFO = b"ethos-camera-credentials:"
# Same expression as camera_service used before the split: `scheme://user:pass@host`.
CRED_URL_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^/\s:@]+):(?P<pw>[^/\s@]+)@")


def _key() -> bytes:
    return hashlib.sha256(_KEY_INFO + str(settings.JWT_SECRET).encode("utf-8")).digest()


def encrypt_secret(plain: str) -> str:
    """Encrypt a stream password. Empty input returns an empty string."""
    value = str(plain or "")
    if not value:
        return ""
    from os import urandom

    nonce = urandom(12)
    blob = _MAGIC + nonce + AESGCM(_key()).encrypt(nonce, value.encode("utf-8"), None)
    return base64.urlsafe_b64encode(blob).decode("ascii")


def decrypt_secret(stored: str) -> str:
    """Decrypt a stored password. Returns '' when the blob is unreadable."""
    raw = str(stored or "")
    if not raw:
        return ""
    try:
        blob = base64.urlsafe_b64decode(raw.encode("ascii"))
        if not blob.startswith(_MAGIC):
            return ""
        nonce, ciphertext = blob[len(_MAGIC):len(_MAGIC) + 12], blob[len(_MAGIC) + 12:]
        return AESGCM(_key()).decrypt(nonce, ciphertext, None).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 — wrong/rotated key or corrupt blob
        logger.warning("Stored camera credential could not be decrypted: %s", type(exc).__name__)
        return ""


def split_credentials(url: str) -> tuple[str, str, str]:
    """Return `(url_without_credentials, user, password)` for a stream URL."""
    text = str(url or "").strip()
    if not text:
        return "", "", ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text, "", ""
    user = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if not user and not password:
        return text, "", ""
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parts.port:
        netloc = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)), user, password


def merge_credentials(url: str, user: str = "", password: str = "") -> str:
    """Add credentials to a URL for ffmpeg (percent-encoded)."""
    text = str(url or "").strip()
    if not text or not user:
        return text
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def mask_url(url: str) -> str:
    """Remove embedded credentials from a URL (for logs and API responses)."""
    return CRED_URL_RE.sub(r"\g<scheme>***:***@", str(url or ""))


def mask_text(text: object) -> str:
    """Mask any `scheme://user:pass@` occurrence in free-form text (ffmpeg errors)."""
    return CRED_URL_RE.sub(r"\g<scheme>***:***@", str(text))
