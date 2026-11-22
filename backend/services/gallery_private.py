"""Gallery Private vault — PIN, AES-256-GCM at rest, short-lived sessions.

A user-chosen PIN (min 6 chars, PBKDF2-SHA256 600k iterations) protects the
Private section on top of the normal login. Only the verification hash is
persisted — never the PIN. The AES-256 key is derived from the same PIN with a
*separate* salt, so there is no stored key and no recovery path: forgetting the
PIN makes encrypted items unrecoverable (deliberate).

Flagged files are encrypted in place to ``<path>.ethos.enc``::

    ETHOS-ENC1\x00 || nonce(12) || AESGCM ciphertext+tag

Displaying them needs a ``ps`` session token (30 min, in memory only, bound to
the logged-in user). Downloads serve the ciphertext, so a copied blob cannot be
read without the PIN.

Extracted from ``routers/gallery.py`` (TODO.md P2-02): the router keeps the
request-bound glue (token decoding, config lookup, index lookup) and calls this
module for every crypto/session operation.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import secrets
import stat
import time
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from PIL import Image

from backend.config import settings
from backend.services.thumbnails import _extract_video_frame

logger = logging.getLogger("ethos-nas.services.gallery_private")

# ── Private section: PIN + at-rest encryption ───────────────────────────────
#
# A user-chosen PIN (min 6 chars) protects the Private section on top of the
# normal login. Only a PBKDF2 verification hash is persisted — never the PIN.
# The AES-256-GCM key for file encryption is derived from the same PIN with a
# separate salt, so there is no stored key and no recovery path: forgetting
# the PIN makes encrypted private items unrecoverable (deliberate).
#
# Files flagged private are re-encrypted in place to <path>.ethos.enc:
#   ETHOS-ENC1\x00 || nonce(12) || AESGCM ciphertext+tag
# Displaying them requires a short-lived Private session (ps_token, 30 min,
# in-memory only, bound to the logged-in user). Downloads of private items
# serve the ciphertext blob — a copied file cannot be shown without the PIN.

PIN_MIN_LEN = 6
PBKDF2_ITERATIONS = 600_000          # OWASP recommendation for PBKDF2-SHA256
ENC_MAGIC = b"ETHOS-ENC1\x00"
ENC_SUFFIX = ".ethos.enc"
MAX_ENCRYPT_SIZE = 512 * 1024 * 1024  # refuse to encrypt files larger than this
SESSION_TTL = 1800                    # Private session lifetime (seconds)

SESSIONS: dict[str, dict] = {}  # ps_token → {user, exp, key}
PRIV_TMP_DIR = settings.DATA_DIR / ".privtmp"
THUMB_CACHE: dict[tuple, tuple] = {}   # (path,size) → (mtime,size,webp bytes)


def pbkdf2(pin: str, salt_hex: str, dklen: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), bytes.fromhex(salt_hex),
        PBKDF2_ITERATIONS, dklen=dklen)


def pin_hash(pin: str, salt_hex: str) -> str:
    return pbkdf2(pin, salt_hex, 32).hex()


def enc_key(pin: str, enc_salt_hex: str) -> bytes:
    """AES-256 key for file encryption (separate derivation from the hash)."""
    return pbkdf2(pin, enc_salt_hex, 32)


def encrypt_bytes(key: bytes, data: bytes) -> bytes:
    nonce = os.urandom(12)
    return ENC_MAGIC + nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt_bytes(key: bytes, blob: bytes) -> bytes:
    if not blob.startswith(ENC_MAGIC):
        raise ValueError("not an EthOS-encrypted file")
    off = len(ENC_MAGIC)
    nonce, ct = blob[off:off + 12], blob[off + 12:]
    return AESGCM(key).decrypt(nonce, ct, None)   # InvalidTag on wrong key/corrupt


def encrypt_file(path: str, key: bytes) -> None:
    """Encrypt <path> in place → <path>.ethos.enc (atomic; original removed)."""
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("not a regular file")
    if st.st_size > MAX_ENCRYPT_SIZE:
        raise ValueError(f"file too large to encrypt (> {MAX_ENCRYPT_SIZE // (1024 * 1024)} MiB)")
    with open(path, "rb") as f:
        data = f.read()
    blob = encrypt_bytes(key, data)
    tmp = path + ENC_SUFFIX + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, path + ENC_SUFFIX)
    os.remove(path)


def decrypt_file(enc_path: str, key: bytes) -> None:
    """Decrypt <path>.ethos.enc back to the original (atomic)."""
    path = enc_path[: -len(ENC_SUFFIX)]
    with open(enc_path, "rb") as f:
        blob = f.read()
    data = decrypt_bytes(key, blob)
    tmp = path + ".dec.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    os.remove(enc_path)


def reencrypt_file(enc_path: str, old_key: bytes, new_key: bytes) -> None:
    """Re-key one encrypted file (PIN change)."""
    with open(enc_path, "rb") as f:
        blob = f.read()
    data = decrypt_bytes(old_key, blob)
    tmp = enc_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(encrypt_bytes(new_key, data))
    os.replace(tmp, enc_path)


def issue_session(username: str, key: bytes) -> str:
    now = time.time()
    for t in [t for t, v in SESSIONS.items() if v["exp"] < now]:
        del SESSIONS[t]
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"user": username, "exp": now + SESSION_TTL, "key": key}
    return token


def session_for(token: str, username: str) -> Optional[dict]:
    v = SESSIONS.get(token or "")
    if not v or v["user"] != username or v["exp"] < time.time():
        return None
    return v


def decrypt_to_tmp(enc: str, key: bytes, orig: str) -> str:
    """Decrypt an encrypted file to a 0600 temp file (Range-capable serving)."""
    PRIV_TMP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(PRIV_TMP_DIR, 0o700)
    with open(enc, "rb") as f:
        blob = f.read()
    data = decrypt_bytes(key, blob)   # may raise InvalidTag
    ext = os.path.splitext(orig)[1] or ".bin"
    tmp = PRIV_TMP_DIR / (hashlib.sha256(orig.encode()).hexdigest() + ext)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return str(tmp)


def rm_quiet(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass  # best-effort cleanup — nothing to report


def private_image_thumb(enc: str, key: bytes, size: int) -> bytes:
    """Decrypt an image in memory → square WebP thumbnail (no disk cache)."""
    with open(enc, "rb") as f:
        blob = f.read()
    data = decrypt_bytes(key, blob)
    with Image.open(io.BytesIO(data)) as img:
        if img.mode not in ("RGB", "RGBA", "P", "L"):
            img = img.convert("RGBA")
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=85)
        return buf.getvalue()


def private_video_thumb(enc: str, key: bytes, orig: str, size: int) -> Optional[bytes]:
    """Decrypt a video to a temp file, extract one frame via ffmpeg (files.py),
    read the WebP back into memory, then delete all plaintext artifacts."""
    tmp = decrypt_to_tmp(enc, key, orig)
    out = PRIV_TMP_DIR / (hashlib.sha256(f"vt:{orig}:{size}".encode()).hexdigest() + ".webp")
    try:
        ok = _extract_video_frame(tmp, size, out)
        if not ok or not out.exists():
            return None
        with open(out, "rb") as f:
            return f.read()
    finally:
        rm_quiet(tmp)
        rm_quiet(str(out))
