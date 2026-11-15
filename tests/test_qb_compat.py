#!/usr/bin/env python3
"""qBittorrent compatibility layer — contract tests (Radarr/Sonarr shim).

Boots its own isolated instance, obtains the generated qBittorrent API key as an
admin, and walks the exact endpoints NzbDrone calls.

Run:  .venv/bin/python tests/test_qb_compat.py
"""

import base64
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "qb-compat-test-secret"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _admin_token() -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "1", "username": "qb-admin", "role": "admin",
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _request(base, method, path, token=None, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = raw
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw
        return exc.code, body, dict(exc.headers)


def _q(base, method, path, sid="", form=None):
    headers = {"Cookie": f"SID={sid}"} if sid else {}
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(base + "/api/downloads/qb/api" + path, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            try:
                body = json.loads(raw) if raw else None
            except ValueError:
                body = raw
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw
        return exc.code, body, dict(exc.headers)


def _login(base, password):
    status, body, headers = _q(base, "POST", "/auth/login", form={"username": "admin", "password": password})
    sid = ""
    # Header casing from the ASGI server is lowercase; accept any casing.
    cookie = next((v for k, v in headers.items() if k.lower() == "set-cookie"), "")
    for part in cookie.split(";"):
        if part.strip().startswith("SID="):
            sid = part.strip()[4:]
    return status, sid


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-qb-test-"))
    log_path = data_dir / "server.log"
    env = {**os.environ, "ETHOS_PORT": str(port), "ETHOS_DATA_DIR": str(data_dir), "ETHOS_JWT_SECRET": SECRET}
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    admin = _admin_token()
    try:
        deadline = time.time() + 25
        ready = False
        while time.time() < deadline and proc.poll() is None:
            try:
                status = _request(base, "GET", "/api/system/status")[0]
            except OSError:
                status = 0
            if status == 401:
                ready = True
                break
            time.sleep(0.3)
        if not ready:
            print("Instance failed to start. Log tail:")
            print(log_path.read_text()[-3000:])
            return 1

        print("Public version endpoints")
        status, body, _h = _q(base, "GET", "/app/version")
        check("app/version is public → 200", status == 200)
        check("app/version returns a version string", str(body).strip().startswith("4."), str(body))
        status, body, _h = _q(base, "GET", "/app/webapiVersion")
        check("webapiVersion is public and returns 2.0", status == 200 and str(body).strip() == "2.0", str(body))
        status, body, _h = _q(base, "GET", "/v2/app/webapiVersion")
        check("v2 mount answers webapiVersion", status == 200 and str(body).strip() == "2.0", str(body))

        print("\nSID auth")
        check("preferences without auth → 403", _q(base, "GET", "/app/preferences")[0] == 403)
        status, _sid = _login(base, "wrong-key")
        check("login with a wrong key → 403", status == 403, f"HTTP {status}")

        status, cfg, _h = _request(base, "GET", "/api/downloads/config", admin)
        key = (cfg or {}).get("qb_api_key", "") if isinstance(cfg, dict) else ""
        check("admin can read the generated key", key.startswith("ethos_"))
        status, sid = _login(base, key)
        check("login with the key → 200 + SID", status == 200 and bool(sid), f"HTTP {status}")

        print("\nNzbDrone endpoints")
        status, prefs, _h = _q(base, "GET", "/app/preferences", sid)
        check("preferences → 200", status == 200)
        check("preferences report dht=true (magnet support)",
              isinstance(prefs, dict) and prefs.get("dht") is True)
        check("preferences expose download_path", bool(isinstance(prefs, dict) and prefs.get("download_path")))

        status, info, _h = _q(base, "GET", "/torrents/info", sid)
        check("torrents/info → array", status == 200 and isinstance(info, list))
        status, info2, _h = _q(base, "GET", "/torrents/info?hashes=deadbeef&category=movies", sid)
        check("torrents/info accepts hashes+category", status == 200 and info2 == [])

        status, sync, _h = _q(base, "GET", "/sync/maindata?rid=4", sid)
        check("sync/maindata → {rid,torrents,transfer_info}",
              status == 200 and isinstance(sync, dict)
              and sync.get("rid") == "5"
              and isinstance(sync.get("torrents"), list)
              and isinstance(sync.get("transfer_info"), dict), f"HTTP {status}")
        status, transfer, _h = _q(base, "GET", "/transfer/info", sid)
        check("transfer/info → rate fields",
              status == 200 and isinstance(transfer, dict) and "download_payload_rate" in transfer)

        status, cats, _h = _q(base, "GET", "/torrents/categories", sid)
        check("torrents/categories → dict", status == 200 and isinstance(cats, dict))

        status, body, _h = _q(base, "POST", "/torrents/createCategory", sid,
                              {"category": "movies", "savePath": "/mnt/media/movies"})
        check("createCategory → 200", status == 200, f"HTTP {status}: {str(body)[:80]}")
        status, cats, _h = _q(base, "GET", "/torrents/categories", sid)
        check("created category is listed with its savePath",
              status == 200 and isinstance(cats, dict) and "movies" in cats
              and cats["movies"].get("savePath") == "/mnt/media/movies", str(cats)[:120])
        status, cfg_after, _h = _request(base, "GET", "/api/downloads/config", admin)
        persisted = (cfg_after or {}).get("categories") if isinstance(cfg_after, dict) else None
        check("createCategory persists in downloads_config.json",
              any(c.get("name") == "movies" for c in (persisted or [])), str(persisted)[:120])
        status, _body, _h = _q(base, "DELETE", "/torrents/deleteCategory", sid, {"category": "movies"})
        check("deleteCategory → 200", status == 200, f"HTTP {status}")
        status, cfg_after2, _h = _request(base, "GET", "/api/downloads/config", admin)
        persisted2 = (cfg_after2 or {}).get("categories") if isinstance(cfg_after2, dict) else None
        check("deleteCategory removes the persisted category",
              not any(c.get("name") == "movies" for c in (persisted2 or [])), str(persisted2)[:120])

        status, body, _h = _q(base, "GET", "/torrents/properties?hash=deadbeef", sid)
        check("properties for an unknown hash → 404", status == 404, f"HTTP {status}")
        status, body, _h = _q(base, "GET", "/torrents/files?hash=deadbeef", sid)
        check("files for an unknown hash → 404", status == 404, f"HTTP {status}")

        for path, form in (
            ("/torrents/topPrio", {"hashes": "deadbeef"}),
            ("/torrents/setShareLimits", {"hashes": "deadbeef", "ratioLimit": "1"}),
            ("/torrents/setForceStart", {"hashes": "deadbeef", "value": "true"}),
        ):
            status, _body, _h = _q(base, "POST", path, sid, form)
            check(f"{path} is a 200 no-op", status == 200, f"HTTP {status}")

        print("\nSession lifecycle")
        status, _body, _h = _q(base, "POST", "/auth/logout", sid)
        check("logout → 200", status == 200)
        status, _body, _h = _q(base, "GET", "/torrents/info", sid)
        check("logged-out SID → 403", status == 403, f"HTTP {status}")

        failed = [r for r in _results if not r[1]]
        print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
