#!/usr/bin/env python3
"""Unified Logs API — auth matrix, ingest policy, legacy import (live instance).

Boots its own isolated instance (free port, temporary ETHOS_DATA_DIR, test-only
JWT secret), so it never touches production data and needs no
ETHOS_TEST_PASSWORD.

Covers docs/logs.md:
  • admin-only reads; any authenticated user may ingest
  • `package_id` policy (installed → pkg:<id>, unknown → 403)
  • batch cap, credential redaction, legacy `/api/logs/app` + `/client` aliases
  • host file path allowlist and the real HTTP error contract
  • `app_events.jsonl` import + archive on first boot

    .venv/bin/python tests/test_logs_api.py
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
SECRET = "logs-api-test-secret"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _token(role: str, username: str) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "1", "username": username, "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, method: str, path: str, token: str | None = None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, None
    except OSError:
        return 0, None


def _wait_ready(base: str, proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _call(base, "GET", "/api/system/status")[0] == 401:
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="ethos-logs-api-"))
    base = f"http://127.0.0.1:{port}"
    log_path = tmp / "server.log"

    (tmp / "packages").mkdir()
    (tmp / "packages" / "installed.json").write_text(json.dumps({
        "version": 1,
        "items": [{"id": "demo-pkg", "version": "1.0.0"}],
    }), encoding="utf-8")
    (tmp / "app_events.jsonl").write_text(json.dumps({
        "ts": 1763000000.0, "level": "error", "source": "ethos-nas.routers.files",
        "message": "legacy app event", "detail": "from the old store",
    }) + "\n", encoding="utf-8")

    env = {
        **os.environ,
        "ETHOS_DATA_DIR": str(tmp),
        "ETHOS_JWT_SECRET": SECRET,
        "ETHOS_PORT": str(port),
        "ETHOS_DEBUG": "0",
        "ETHOS_LOG_INGEST_PER_MIN": "1000",
        "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
    }
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        print(f"\n[live] isolated instance on {base}")
        if not _wait_ready(base, proc):
            print(log_path.read_text()[-2500:])
            check("isolated instance became ready", False)
            return 1

        admin = _token("admin", "logs-admin")
        user = _token("user", "logs-guest")

        print("\n[boot] legacy import")
        status, data = _call(base, "GET", "/api/logs?limit=50", admin)
        messages = [item.get("message") for item in (data or {}).get("entries") or []]
        check("legacy app event was imported", "legacy app event" in messages, str(status))
        check("legacy file was archived",
              (tmp / "logs" / "legacy-app-events.jsonl").exists() and not (tmp / "app_events.jsonl").exists())

        print("\n[auth matrix]")
        check("no token → 401", _call(base, "GET", "/api/logs")[0] == 401)
        check("user cannot read logs", _call(base, "GET", "/api/logs", user)[0] == 403)
        status, data = _call(base, "GET", "/api/logs?limit=10", admin)
        check("admin reads entries + counts", status == 200
              and isinstance(data.get("entries"), list)
              and isinstance(data.get("counts"), dict), str(status))

        print("\n[ingest]")
        status, data = _call(base, "POST", "/api/logs/ingest", user, {
            "level": "warning", "message": "browser probe", "source": "browser:files",
        })
        check("any user may ingest", status == 200 and data.get("accepted") == 1, f"{status} {data}")
        status, data = _call(base, "GET", "/api/logs?search=browser%20probe", admin)
        row = ((data or {}).get("entries") or [{}])[0]
        check("browser ingest is tagged browser", row.get("app") == "browser", str(row))
        check("browser source is preserved", row.get("source") == "browser:files", str(row))

        status, _ = _call(base, "POST", "/api/logs/ingest", user, {
            "level": "error", "message": "pkg probe", "package_id": "demo-pkg",
        })
        status, data = _call(base, "GET", "/api/logs?search=pkg%20probe", admin)
        row = ((data or {}).get("entries") or [{}])[0]
        check("installed package → pkg:<id>", row.get("app") == "pkg:demo-pkg", str(row))
        check("package source defaults to pkg-ui", row.get("source") == "pkg-ui", str(row))

        status, _ = _call(base, "POST", "/api/logs/ingest", user, {
            "level": "error", "message": "spoof", "package_id": "not-installed",
        })
        check("unknown package_id → 403", status == 403, str(status))

        status, _ = _call(base, "POST", "/api/logs/ingest", user, {
            "level": "error", "message": "token=supersecret password=hunter2",
        })
        status, data = _call(base, "GET", "/api/logs?search=supersecret", admin)
        check("credentials are redacted at rest", not (data or {}).get("entries"), str(data)[:160])

        batch = [{"level": "info", "message": f"batch {index}"} for index in range(60)]
        status, data = _call(base, "POST", "/api/logs/ingest", user, batch)
        check("batch is capped at 50", status == 200 and data.get("accepted") == 50
              and data.get("dropped") == 10, f"{status} {data}")

        status, data = _call(base, "POST", "/api/logs/client", user, {
            "level": "info", "message": "legacy alias",
        })
        check("legacy /client alias still accepts", status == 200 and data.get("accepted") == 1, str(status))

        status, data = _call(base, "GET", "/api/logs/app?limit=5", admin)
        check("legacy /app alias returns events", status == 200 and isinstance(data.get("events"), list),
              str(status))

        print("\n[host sources]")
        check("user cannot list host files", _call(base, "GET", "/api/logs/files", user)[0] == 403)
        status, data = _call(base, "GET", "/api/logs/files", admin)
        check("admin lists host files", status == 200 and isinstance(data.get("files"), list), str(status))
        check("path outside the allowlist → 403",
              _call(base, "GET", "/api/logs/tail?path=/etc/passwd", admin)[0] == 403)
        check("missing file → 404",
              _call(base, "GET", "/api/logs/tail?path=/var/log/ethos-does-not-exist.log", admin)[0] == 404)
        status, data = _call(base, "GET", "/api/logs/journal?lines=5", admin)
        check("journal endpoint answers or reports a clear failure", status == 200 or status >= 500,
              str(status))
        status, data = _call(base, "GET", "/api/logs/containers", admin)
        check("container listing answers or reports Docker unavailable",
              status == 200 or status == 503, str(status))

        status, data = _call(base, "GET", "/api/logs/stats", admin)
        check("stats expose ring + disk + dropped", status == 200
              and {"ring", "ring_size", "disk_bytes", "dropped"} <= set(data), str(status))

        print("\n[clear]")
        check("user cannot clear logs", _call(base, "DELETE", "/api/logs", user)[0] == 403)
        status, data = _call(base, "DELETE", "/api/logs", admin)
        check("admin can clear logs", status == 200 and data.get("cleared", 0) > 0, str(data))
        status, data = _call(base, "GET", "/api/logs?limit=50", admin)
        remaining = [item.get("message") for item in (data or {}).get("entries") or []]
        check("cleared rows are gone", "browser probe" not in remaining, str(remaining)[:160])

        failed = [item for item in _results if not item[1]]
        print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
        if failed:
            for name, _ok, detail in failed:
                print(f"  FAILED: {name} — {detail}")
            return 1
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
