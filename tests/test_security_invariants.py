#!/usr/bin/env python3
"""Security invariants — regression guard for TODO.md P0-01/02/03/06.

Boots its own isolated instance (throwaway port + ETHOS_DATA_DIR + test JWT
secret), so it never touches production data and needs no external fixture.

Run:
    .venv/bin/python tests/test_security_invariants.py
"""

import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "invariants-test-secret"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(role: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "1", "username": "invariants", "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, method: str, path: str, token: str | None = None, body=None) -> int:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def _wait_ready(base: str, proc: subprocess.Popen, log_path: Path, timeout: float = 25.0) -> bool:
    """The server is ready once /api/system/status answers 401 (auth required)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _call(base, "GET", "/api/system/status") == 401:
            return True
        time.sleep(0.3)
    return False


def _source_checks() -> None:
    """Static guards that need no server."""
    print("\n[static]")
    # Credentials are never committed: a test may only read the password from
    # ETHOS_TEST_PASSWORD, never assign a literal.
    literal_pass = re.compile(r"^\s*PASS\s*=\s*[\"'][^\"']+[\"']", re.MULTILINE)
    offending = [
        str(p.relative_to(ROOT)) for p in (ROOT / "tests").glob("test_*.py")
        if literal_pass.search(p.read_text(errors="replace"))
    ]
    check("no hardcoded test password", not offending, ", ".join(offending))

    main_src = (ROOT / "backend/main.py").read_text()
    check("public_paths is an exact set", "public_paths = {" in main_src)
    check("public_paths is not prefix-matched",
          "any(path.startswith(p) for p in public_paths)" not in main_src)

    qb_src = (ROOT / "backend/services/qb_compat.py").read_text()
    check("qBittorrent login does not log the body", "body=%r" not in qb_src)
    check("qBittorrent login does not log the password match", "pw_len" not in qb_src)

    auth_src = (ROOT / "backend/routers/auth.py").read_text()
    check("require_admin dependency exists", "def require_admin(" in auth_src)


def main() -> int:
    _source_checks()

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-invariants-"))
    log_path = data_dir / "server.log"
    env = {
        **os.environ,
        "ETHOS_PORT": str(port),
        "ETHOS_DATA_DIR": str(data_dir),
        "ETHOS_JWT_SECRET": SECRET,
    }
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    try:
        print(f"\n[live] isolated instance on {base}")
        if not _wait_ready(base, proc, log_path):
            print(log_path.read_text()[-2000:])
            print("  [FAIL] isolated instance did not become ready")
            return 1

        user, admin = _token("user"), _token("admin")

        print("\n[public] unauthenticated")
        for path, expected in [
            ("/api/system/status", 401),
            ("/api/ethos-nas-service/status", 401),
            ("/api/smart", 401),
            ("/api/packages/desktop-icons", 200),
            ("/api/downloads/qb/app/version", 404),  # public route, no APK needed
        ]:
            got = _call(base, "GET", path)
            check(f"GET {path} -> {expected}", got == expected, f"got {got}")

        print("\n[user role] privileged endpoints")
        for method, path, body, expected in [
            ("DELETE", "/api/processes/1", None, 403),
            ("POST", "/api/system/restart", None, 403),
            ("GET", "/api/systemd", None, 403),
            ("GET", "/api/ethos-nas-service/status", None, 403),
            ("GET", "/api/cron", None, 403),
            ("GET", "/api/backup/status", None, 403),
            ("GET", "/api/disk-clean/scan", None, 403),
            ("GET", "/api/logs/files", None, 403),
        ]:
            got = _call(base, method, path, user, body)
            check(f"{method} {path} -> {expected}", got == expected, f"got {got}")

        print("\n[user role] filesystem scope")
        for method, path, body, expected in [
            ("GET", "/api/files?path=/etc", None, 403),
            ("GET", "/api/files?path=/tmp", None, 403),
            ("DELETE", "/api/files/delete", {"paths": ["/etc/hostname"]}, 403),
            ("POST", "/api/files/copy",
             {"sources": ["/etc/hostname"], "dest_dir": "/tmp"}, 403),
            ("POST", "/api/sync/mkdir", {"path": "/etc/ethos-invariants"}, 403),
            ("GET", "/api/editor/git/status?repo=/etc", None, 403),
            ("GET", "/api/editor/search?q=root&repo=/etc", None, 403),
        ]:
            got = _call(base, method, path, user, body)
            check(f"{method} {path} -> {expected}", got == expected, f"got {got}")

        print("\n[admin role] same endpoints allowed")
        for method, path, expected in [
            ("GET", "/api/systemd", 200),
            ("GET", "/api/files?path=/etc", 200),
            ("GET", "/api/ethos-nas-service/status", 200),
            ("GET", "/api/backup/status", 200),
            ("GET", "/api/logs/files", 200),
            ("GET", "/api/system/storage-usage", 200),
        ]:
            got = _call(base, method, path, admin)
            check(f"{method} {path} -> {expected}", got == expected, f"got {got}")
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
        for name, _ok, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
