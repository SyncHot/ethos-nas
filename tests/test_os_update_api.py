#!/usr/bin/env python3
"""OS package update API surface (isolated instance, fake root helper).

Checks the auth boundary, the status shape and the check/apply flow without
running apt: `ETHOS_OS_UPDATE_HELPER` points at a small script that writes the
same JSON files the real helper produces.

Run: .venv/bin/python tests/test_os_update_api.py
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
SECRET = "os-update-api-secret"
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
        "sub": "1", "username": "os-update-api", "role": role,
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


FAKE_HELPER = """#!/usr/bin/env python3
import json, os, sys

action = sys.argv[1] if len(sys.argv) > 1 else ""
state = os.path.join(os.environ["ETHOS_DATA_DIR"], "os-update")
os.makedirs(state, exist_ok=True)

if action == "check":
    payload = {
        "checked_at": "2026-11-22T10:00:00+00:00",
        "checked_at_epoch": __EPOCH__,
        "count": 2,
        "security_count": 1,
        "kept_back": 0,
        "packages": [
            {"name": "libc6", "from": "1.0", "to": "1.1", "origin": "resolute-security", "security": True},
            {"name": "vim", "from": "9.0", "to": "9.1", "origin": "resolute-updates", "security": False},
        ],
    }
    with open(os.path.join(state, "check.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
elif action == "apply":
    job = {"status": "ok", "step": "done", "percent": 100, "upgraded": 2,
           "reboot_required": False, "pid": os.getpid(), "at": "2026-11-22T10:01:00+00:00"}
    result = {"status": "ok", "step": "done", "upgraded": 2, "reboot_required": False,
              "detail": "2 package(s) upgraded", "at": "2026-11-22T10:01:00+00:00"}
    with open(os.path.join(state, "job.json"), "w", encoding="utf-8") as fh:
        json.dump(job, fh)
    with open(os.path.join(state, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh)
"""


def main() -> int:
    source = (ROOT / "backend/routers/system_os_update.py").read_text(encoding="utf-8")
    check("check endpoint is admin-only",
          '@router.post("/check", dependencies=[Depends(require_admin)])' in source)
    check("apply endpoint is admin-only",
          '@router.post("", dependencies=[Depends(require_admin)])' in source)
    check("status endpoints exist",
          '@router.get("")' in source and '@router.get("/status")' in source)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-os-update-api-"))
    helper = data_dir / "fake-os-update"
    helper.write_text(FAKE_HELPER.replace("__EPOCH__", str(int(time.time()))), encoding="utf-8")
    helper.chmod(0o755)
    log_path = data_dir / "server.log"
    env = {**os.environ, "ETHOS_PORT": str(port), "ETHOS_DATA_DIR": str(data_dir),
           "ETHOS_JWT_SECRET": SECRET, "ETHOS_OS_UPDATE_HELPER": str(helper),
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

        status, _ = _call(base, "GET", "/api/system/os-update")
        check("anonymous status is 401", status == 401, str(status))

        status, body = _call(base, "GET", "/api/system/os-update", user)
        payload = _json(body)
        check("user can read the status", status == 200, str(status))
        for key in ("enabled", "helper", "os", "auto_upgrades", "update_count",
                    "security_count", "packages", "reboot_required", "error"):
            check(f"status has {key}", key in payload, str(sorted(payload)))
        check("helper is detected", payload.get("helper") is True)
        check("os identity is present", bool((payload.get("os") or {}).get("kernel")))

        status, _ = _call(base, "POST", "/api/system/os-update/check", user)
        check("user cannot run a check (403)", status == 403, str(status))
        status, _ = _call(base, "POST", "/api/system/os-update", user)
        check("user cannot apply updates (403)", status == 403, str(status))

        status, body = _call(base, "POST", "/api/system/os-update/check", admin)
        checked = _json(body)
        check("admin check returns 200", status == 200, str(status))
        check("check reports two upgrades", checked.get("update_count") == 2, str(checked))
        check("check reports one security update", checked.get("security_count") == 1, str(checked))
        check("check has no error", checked.get("error") == "", str(checked.get("error")))
        check("check keeps the package rows", len(checked.get("packages") or []) == 2)

        status, body = _call(base, "GET", "/api/system/os-update", admin)
        cached = _json(body)
        check("cached status survives the request", status == 200 and cached.get("update_count") == 2,
              str(cached.get("update_count")))

        status, body = _call(base, "POST", "/api/system/os-update", admin)
        started = _json(body)
        check("admin apply returns 200", status == 200, str(status))
        check("apply reports updating", started.get("status") == "updating", str(started))

        result = {}
        deadline = time.time() + 8
        while time.time() < deadline:
            status, body = _call(base, "GET", "/api/system/os-update/status", admin)
            state = _json(body)
            if (state.get("result") or {}).get("status"):
                result = state
                break
            time.sleep(0.2)
        check("apply status returns 200", status == 200, str(status))
        for key in ("helper", "running", "stale", "job", "result"):
            check(f"apply status has {key}", key in result, str(sorted(result)))
        check("apply result is reported", (result.get("result") or {}).get("status") == "ok",
              str(result.get("result")))
        check("apply result counts upgrades", (result.get("result") or {}).get("upgraded") == 2)

        status, _ = _call(base, "GET", "/api/system/os-update/status")
        check("anonymous apply status is 401", status == 401, str(status))
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
