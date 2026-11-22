#!/usr/bin/env python3
"""OS image API surface (isolated instance, no network, no disk writes).

Run: .venv/bin/python tests/test_os_image_api.py
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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET = "os-image-api-secret"
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
        "sub": "1", "username": "os-image-api", "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base, method, path, token=None):
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except OSError:
        return 0, b""


def _json(body):
    try:
        return json.loads(body or b"{}")
    except ValueError:
        return {}


def main() -> int:
    source = (ROOT / "backend/routers/system_os_image.py").read_text(encoding="utf-8")
    check("check endpoint is admin-only",
          '@router.post("/check", dependencies=[Depends(require_admin)])' in source)
    check("download endpoint is admin-only",
          '@router.post("/download", dependencies=[Depends(require_admin)])' in source)
    check("install endpoint is admin-only",
          '@router.post("/install", dependencies=[Depends(require_admin)])' in source)
    check("delete endpoint is admin-only",
          '@router.delete("/download", dependencies=[Depends(require_admin)])' in source)
    check("status endpoints exist",
          '@router.get("")' in source and '@router.get("/status")' in source)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-os-image-api-"))
    helper = data_dir / "fake-os-image"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    helper.chmod(0o755)
    log_path = data_dir / "server.log"
    env = {**os.environ, "ETHOS_PORT": str(port), "ETHOS_DATA_DIR": str(data_dir),
           "ETHOS_JWT_SECRET": SECRET, "ETHOS_OS_IMAGE_HELPER": str(helper),
           "ETHOS_OS_IMAGE_INDEX_URL": "https://127.0.0.1:9/index.json",
           "ETHOS_PACKAGES_DEFAULT_SOURCE": "0"}
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                print(log_path.read_text()[-2000:])
                check("isolated instance ready", False)
                return 1
            if _call(base, "GET", "/api/system/status")[0] == 401:
                break
            time.sleep(0.3)
        check("isolated instance ready", True)

        user, admin = _token("user"), _token("admin")
        status, _ = _call(base, "GET", "/api/system/os-image")
        check("anonymous status is 401", status == 401, str(status))

        status, body = _call(base, "GET", "/api/system/os-image", user)
        payload = _json(body)
        check("user can read the status", status == 200, str(status))
        for key in ("enabled", "helper", "identity", "layout", "ab_supported",
                    "latest", "downloaded", "download", "install", "slots", "error"):
            check(f"status has {key}", key in payload, str(sorted(payload)))
        check("single-slot layout is reported", payload.get("ab_supported") is False)
        check("helper is detected", payload.get("helper") is True)

        for method, path in (("POST", "/api/system/os-image/check"),
                             ("POST", "/api/system/os-image/download"),
                             ("POST", "/api/system/os-image/install"),
                             ("DELETE", "/api/system/os-image/download")):
            status, _ = _call(base, method, path, user)
            check(f"user cannot {method} {path} (403)", status == 403, str(status))

        status, body = _call(base, "POST", "/api/system/os-image/check", admin)
        checked = _json(body)
        check("admin check returns 200", status == 200, str(status))
        check("check surfaces the fetch error", bool(checked.get("error")), str(checked.get("error")))

        status, _ = _call(base, "POST", "/api/system/os-image/download", admin)
        check("download without an image is 409", status == 409, str(status))
        status, _ = _call(base, "POST", "/api/system/os-image/install", admin)
        check("install without an image is 409", status == 409, str(status))
        status, body = _call(base, "DELETE", "/api/system/os-image/download", admin)
        check("delete with nothing downloaded returns 200", status == 200, str(status))

        status, body = _call(base, "GET", "/api/system/os-image/status", admin)
        jobs = _json(body)
        check("job status returns 200", status == 200, str(status))
        check("job status has download and install",
              "download" in jobs and "install" in jobs, str(sorted(jobs)))
        status, _ = _call(base, "GET", "/api/system/os-image/status")
        check("anonymous job status is 401", status == 401, str(status))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        shutil.rmtree(data_dir, ignore_errors=True)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
