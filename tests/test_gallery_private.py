#!/usr/bin/env python3
"""Gallery Private vault — PIN derivation, AES-GCM at rest, session TTL.

The Private section used to keep its crypto inside ``routers/gallery.py``.
It now lives in ``services/gallery_private.py`` (TODO.md P2-02). This checks
the security-relevant contract without a server:

  * the PIN is only ever stored as a PBKDF2 hash; the AES key uses a different salt
  * encrypted files carry the ``ETHOS-ENC1`` header and fail closed on a wrong key
  * ``.ethos.enc`` round-trips byte-for-byte and the plaintext is removed
  * a PIN change re-keys existing ciphertext (no plaintext left on disk)
  * ``ps`` sessions are bound to a user and expire after the TTL
  * decrypted temp files are 0600 under ``<DATA_DIR>/.privtmp``

Run:
    .venv/bin/python tests/test_gallery_private.py
"""

import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="ethos-gallery-private-"))
os.environ["ETHOS_DATA_DIR"] = str(_TMP)

from backend.services import gallery_private as gp  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _raises(fn, *args) -> bool:
    try:
        fn(*args)
    except Exception:
        return True
    return False


def main() -> int:
    salt = os.urandom(16).hex()
    enc_salt = os.urandom(16).hex()

    # 1. PIN derivation: deterministic, salt-sensitive, hash ≠ key.
    h1 = gp.pin_hash("pluton2303", salt)
    check("pin hash is deterministic", h1 == gp.pin_hash("pluton2303", salt))
    check("pin hash depends on the salt", h1 != gp.pin_hash("pluton2303", enc_salt))
    check("pin hash depends on the pin", h1 != gp.pin_hash("pluton2304", salt))
    check("pin hash is 32 bytes hex", len(h1) == 64, h1)
    key = gp.enc_key("pluton2303", enc_salt)
    check("enc key is 32 raw bytes", len(key) == 32 and isinstance(key, bytes))
    check("enc key is not the stored hash", key.hex() != h1)

    # 2. Blob format + fail-closed decryption.
    blob = gp.encrypt_bytes(key, b"hello private")
    check("blob carries the magic header", blob.startswith(gp.ENC_MAGIC), blob[:12])
    check("nonce is 12 bytes", len(blob) == len(gp.ENC_MAGIC) + 12 + len("hello private") + 16,
          str(len(blob)))
    check("round-trip", gp.decrypt_bytes(key, blob) == b"hello private")
    wrong = gp.enc_key("pluton2304", enc_salt)
    check("wrong key fails closed", _raises(gp.decrypt_bytes, wrong, blob))
    check("non-EthOS blob rejected", _raises(gp.decrypt_bytes, key, b"plain text"))

    # 3. File encryption round-trip: original removed, suffix added.
    work = _TMP / "photos"
    work.mkdir(parents=True, exist_ok=True)
    photo = work / "IMG_0001.jpg"
    payload = b"\xff\xd8" + os.urandom(2048)
    photo.write_bytes(payload)
    gp.encrypt_file(str(photo), key)
    enc = str(photo) + gp.ENC_SUFFIX
    check("plaintext removed after encrypt", not photo.exists())
    check("ciphertext written", Path(enc).exists())
    check("ciphertext is not the plaintext", Path(enc).read_bytes() != payload)
    gp.decrypt_file(enc, key)
    check("decrypt restores the original bytes", photo.read_bytes() == payload)
    check("ciphertext removed after decrypt", not Path(enc).exists())
    check("no leftover temp files",
          sorted(p.name for p in work.iterdir()) == ["IMG_0001.jpg"],
          str(sorted(p.name for p in work.iterdir())))

    # 4. Oversize files are refused (constant is the documented 512 MiB guard).
    real_max = gp.MAX_ENCRYPT_SIZE
    gp.MAX_ENCRYPT_SIZE = 16
    try:
        big = work / "big.bin"
        big.write_bytes(b"\x00" * 64)
        check("oversize file refused", _raises(gp.encrypt_file, str(big), key))
        check("oversize file kept intact", big.exists() and big.stat().st_size == 64)
        check("oversize file not half-encrypted", not Path(str(big) + gp.ENC_SUFFIX).exists())
    finally:
        gp.MAX_ENCRYPT_SIZE = real_max

    # 5. PIN change re-keys existing ciphertext.
    photo.write_bytes(payload)
    gp.encrypt_file(str(photo), key)
    new_key = gp.enc_key("nowe-haslo", enc_salt)
    gp.reencrypt_file(enc, key, new_key)
    check("re-encrypted file opens with the new key",
          gp.decrypt_bytes(new_key, Path(enc).read_bytes()) == payload)
    check("old key no longer opens it",
          _raises(gp.decrypt_bytes, key, Path(enc).read_bytes()))

    # 6. Sessions: bound to the user, expire with the TTL.
    gp.SESSIONS.clear()
    token = gp.issue_session("marcin", new_key)
    check("session resolves for its user", gp.session_for(token, "marcin") is not None)
    check("session rejects another user", gp.session_for(token, "intruder") is None)
    check("unknown token rejected", gp.session_for("nope", "marcin") is None)
    gp.SESSIONS[token]["exp"] = time.time() - 1
    check("expired session rejected", gp.session_for(token, "marcin") is None)

    stale = gp.issue_session("marcin", new_key)
    gp.SESSIONS[stale]["exp"] = time.time() - 1
    fresh = gp.issue_session("marcin", new_key)
    check("issuing a token purges expired ones", stale not in gp.SESSIONS, str(gp.SESSIONS.keys()))
    check("fresh token valid", gp.session_for(fresh, "marcin") is not None)
    check("ttl is 30 minutes", gp.SESSION_TTL == 1800, str(gp.SESSION_TTL))

    # 7. Plaintext temp files are 0600 under <DATA_DIR>/.privtmp.
    photo.write_bytes(payload)
    gp.encrypt_file(str(photo), key)
    tmp = gp.decrypt_to_tmp(enc, key, str(photo))
    mode = stat.S_IMODE(os.stat(tmp).st_mode)
    check("temp file is 0600", mode == 0o600, oct(mode))
    check("temp file lives in .privtmp", Path(tmp).parent == gp.PRIV_TMP_DIR, tmp)
    check("temp file holds the plaintext", Path(tmp).read_bytes() == payload)
    gp.rm_quiet(tmp)
    check("rm_quiet removes the temp file", not Path(tmp).exists())
    gp.rm_quiet(tmp)  # idempotent
    check("rm_quiet is idempotent", True)

    # 8. Private image thumbnails are decrypt-in-memory only (no disk cache).
    from PIL import Image
    import io as _io
    src = _TMP / "shot.png"
    Image.new("RGB", (400, 300), (10, 120, 200)).save(src)
    src_bytes = src.read_bytes()
    gp.encrypt_file(str(src), key)
    thumb = gp.private_image_thumb(str(src) + gp.ENC_SUFFIX, key, 128)
    check("thumb is WebP", thumb[:4] == b"RIFF" and thumb[8:12] == b"WEBP", str(thumb[:12]))
    with Image.open(_io.BytesIO(thumb)) as img:
        check("thumb fits the requested box", max(img.size) <= 128, str(img.size))
    check("no plaintext next to the encrypted file",
          not src.exists() and src_bytes != thumb)

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
