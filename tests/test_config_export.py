#!/usr/bin/env python3
"""Config export/import round-trip (TODO.md P5-04).

Boots an isolated instance, saves a per-user wallpaper, exports the config zip,
resets the wallpaper, imports the archive and checks the choice is back. Also
asserts the archive reader rejects path traversal.

Run:
    .venv/bin/python tests/test_config_export.py
"""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from base64 import urlsafe_b64encode
from hashlib import sha256
from hmac import new as hmac_new
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "export-test-secret"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(data: bytes) -> str:
    return urlsafe_b64encode(data).decode().rstrip("=")


def _token() -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"sub": "1", "username": "admin", "role": "admin",
                               "exp": int(time.time()) + 3600, "iat": int(time.time())}).encode())
    sig = _b64(hmac_new(SECRET.encode(), f"{header}.{payload}".encode(), sha256).digest())
    return f"{header}.{payload}.{sig}"


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-export-"))
    # A pre-existing gallery index must travel with the archive.
    import sqlite3
    with sqlite3.connect(data_dir / "gallery_index.db") as conn:
        conn.execute("CREATE TABLE marker (v TEXT)")
        conn.execute("INSERT INTO marker VALUES ('before-export')")
    log = open(data_dir / "server.log", "w")
    env = {**os.environ, "ETHOS_PORT": str(port), "ETHOS_DATA_DIR": str(data_dir),
           "ETHOS_JWT_SECRET": SECRET}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    token = _token()

    def call(method: str, path: str, data: bytes | None = None, ctype: str | None = None):
        req = urllib.request.Request(base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError:
            return 0, b""  # not listening yet

    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                print((data_dir / "server.log").read_text()[-2000:])
                return 1
            if call("GET", "/api/system/status")[0] == 200:
                break
            time.sleep(0.3)

        status, _ = call("POST", "/api/appearance/wallpaper",
                         json.dumps({"wallpaper": "dusk"}).encode(), "application/json")
        check("saved a per-user wallpaper", status == 200, str(status))

        status, archive = call("GET", "/api/system/config-export")
        check("export returns a zip", status == 200 and archive[:2] == b"PK", f"{status}, {archive[:2]}")

        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            names = zf.namelist()
        check("archive carries the gallery index", "gallery_index.db" in names, str(names))
        check("archive carries a manifest", "manifest.json" in names, str(names))

        status, snapshot = call("POST", "/api/system/config-snapshots")
        check("manual snapshot accepted", status == 200, f"{status}, {snapshot[:120]}")
        status, listing = call("GET", "/api/system/config-snapshots")
        snaps = json.loads(listing)["snapshots"] if status == 200 else []
        check("snapshot listed", status == 200 and len(snaps) >= 1, f"{status}, {snaps}")
        if snaps:
            mode = (data_dir / "snapshots" / snaps[0]["name"]).stat().st_mode & 0o777
            check("snapshot file is 0600", mode == 0o600, oct(mode))

        wallpaper = json.loads(call("GET", "/api/appearance/wallpaper")[1]).get("wallpaper")
        call("DELETE", "/api/appearance/wallpaper")
        status, _ = call("GET", "/api/appearance/wallpaper")
        check("wallpaper reset before import", status == 404, str(status))

        boundary = "----ethos" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"cfg.zip\"\r\n"
                f"Content-Type: application/zip\r\n\r\n").encode() + archive + f"\r\n--{boundary}--\r\n".encode()
        status, payload = call("POST", "/api/system/config-import", body,
                               f"multipart/form-data; boundary={boundary}")
        check("import succeeds", status == 200, f"{status}, {payload[:120]}")
        restored = json.loads(payload).get("imported", []) if status == 200 else []
        check("import restored the gallery index", "gallery_index.db" in restored, str(restored))
        status, body = call("GET", "/api/appearance/wallpaper")
        restored_wallpaper = json.loads(body).get("wallpaper") if status == 200 else None
        check("wallpaper restored by import", restored_wallpaper == wallpaper,
              f"{status}, {restored_wallpaper}")

        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("../evil.json", "{}")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"bad.zip\"\r\n"
                f"Content-Type: application/zip\r\n\r\n").encode() + bad.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        status, _ = call("POST", "/api/system/config-import", body,
                         f"multipart/form-data; boundary={boundary}")
        check("path traversal in archive is rejected", status == 400, str(status))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
