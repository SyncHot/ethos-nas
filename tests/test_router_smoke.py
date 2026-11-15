#!/usr/bin/env python3
"""Router smoke test (TODO.md P4-05).

Boots an isolated instance, enumerates every documented `GET` route from
`/openapi.json`, and asserts none of them answers 5xx. This is a cheap safety
net for the routers that have no dedicated test — it catches import errors,
missing attributes, broken dependencies and unhandled exceptions at runtime.

Routes that need a path/query argument are skipped (they are covered by their
feature tests); slow filesystem scans are skipped explicitly.

Run:
    .venv/bin/python tests/test_router_smoke.py
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
SECRET = "smoke-test-secret"
os.environ.setdefault("ETHOS_PACKAGES_DEFAULT_SOURCE", "0")

# Expensive or host-state dependent; each has its own functional coverage.
SKIP = {
    "/api/disk-clean/scan",
    "/api/disk-clean/top-files",
    "/api/disk-clean/suggestions",
    "/api/system/shutdown",
}

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _token(role: str) -> str:
    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({
        "sub": "1", "username": "smoke", "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, path: str, token: str | None = None, timeout: float = 20.0) -> int:
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def main() -> int:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-smoke-"))
    log_path = data_dir / "server.log"
    env = {**os.environ, "ETHOS_PORT": str(port), "ETHOS_DATA_DIR": str(data_dir),
           "ETHOS_JWT_SECRET": SECRET}
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                print(log_path.read_text()[-2000:])
                return 1
            if _call(base, "/api/system/status") == 401:
                break
            time.sleep(0.3)
        else:
            print(log_path.read_text()[-2000:])
            return 1

        with urllib.request.urlopen(base + "/openapi.json", timeout=10) as resp:
            spec = json.load(resp)

        admin = _token("admin")
        checked = skipped = 0
        for path, operations in sorted(spec.get("paths", {}).items()):
            op = operations.get("get")
            if op is None or path in SKIP:
                continue
            required = [p for p in op.get("parameters", []) if p.get("required")]
            if any(p.get("in") in ("path", "query") for p in required):
                skipped += 1
                continue
            status = _call(base, path, admin)
            checked += 1
            check(f"GET {path} [{status}]", status not in (0, 500, 502, 503, 504),
                  "server error" if status >= 500 else "unreachable")

        # Every non-public route must reject an anonymous request.
        anonymous = _call(base, "/api/processes")
        check("anonymous /api/processes is rejected", anonymous == 401, str(anonymous))

        print(f"\n  ({checked} routes probed, {skipped} skipped for required params)")
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
