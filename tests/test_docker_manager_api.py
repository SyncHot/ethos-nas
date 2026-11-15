#!/usr/bin/env python3
"""Docker Manager API surface (isolated instance, no Docker install triggered).

Checks the engine status endpoint, its auth boundary and the job endpoint. The
install endpoint is only inspected statically: calling it on a host that has the
helper would really run apt.

Run: .venv/bin/python tests/test_docker_manager_api.py
"""
import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "docker-manager-api-secret"
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(role):
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "1", "username": "docker-api", "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base, method, path, token=None):
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError:
        return 0, b""


def _wait_ready(base, proc):
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _call(base, "GET", "/api/system/status")[0] == 401:
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    source = (ROOT / "backend/routers/docker.py").read_text(encoding="utf-8")
    check("engine install is admin-only",
          '@router.post("/docker/engine/install", dependencies=[Depends(require_admin)])' in source)
    check("engine status endpoint exists", '@router.get("/docker/engine")' in source)
    check("engine job endpoint exists", '@router.get("/docker/engine/jobs/{job_id}")' in source)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-docker-api-"))
    log_path = data_dir / "server.log"
    env = {**os.environ, "ETHOS_PORT": str(port), "ETHOS_DATA_DIR": str(data_dir),
           "ETHOS_JWT_SECRET": SECRET}
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        if not _wait_ready(base, proc):
            print(log_path.read_text()[-2000:])
            check("isolated instance ready", False)
            return 1
        check("isolated instance ready", True)

        user, admin = _token("user"), _token("admin")

        status, _ = _call(base, "GET", "/api/docker/engine")
        check("anonymous engine status is 401", status == 401, str(status))

        status, body = _call(base, "GET", "/api/docker/engine", user)
        payload = {}
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            check("engine status returns JSON", False, body[:200].decode("utf-8", "replace"))
        check("user can read engine status", status == 200, str(status))
        for key in ("daemon", "cli", "version", "compose", "socket", "helper", "restart_required"):
            check(f"engine status has {key}", key in payload, str(sorted(payload)))

        status, _ = _call(base, "POST", "/api/docker/engine/install", user)
        check("user cannot install the engine (403)", status == 403, str(status))

        status, _ = _call(base, "GET", "/api/docker/engine/jobs/job_doesnotexist", admin)
        check("unknown engine job is 404", status == 404, str(status))

        status, _ = _call(base, "GET", "/api/docker/engine", admin)
        check("admin can read engine status", status == 200, str(status))

        status, body = _call(base, "GET", "/api/system/doctor", admin)
        names = []
        try:
            names = [c.get("name") for c in json.loads(body or b"{}").get("checks", [])]
        except ValueError:
            check("doctor returns JSON", False, body[:200].decode("utf-8", "replace"))
        for expected in ("docker-engine", "docker-compose", "docker-group"):
            check(f"doctor reports {expected}", expected in names, str(names))
        check("doctor engine checks are optional",
              all(not c.get("required") for c in json.loads(body or b"{}").get("checks", [])
                  if c.get("name", "").startswith("docker-")), str(names))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
