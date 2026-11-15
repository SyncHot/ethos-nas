#!/usr/bin/env python3
"""Notification Center — end-to-end contract (store v2 + REST + /ws/notifications).

Boots its own isolated instance (free port, temporary ETHOS_DATA_DIR, test-only
JWT secret), so it never touches production data and needs no
ETHOS_TEST_PASSWORD.

Covers:
  • v1 → v2 store migration, including the backup copy
  • new event fields (action, dedupe_key, count, read_by)
  • `POST /api/notifications`: admin-only custom sources, package_id policy
  • `dedupe_key` coalescing inside the window
  • `/ws/notifications`: token required, `sync` on connect, live `event`,
    `read-all` and `cleared` frames

    .venv/bin/python tests/test_notifications_ws.py
"""

import asyncio
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
SECRET = "notifications-ws-test-secret"
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


def _token(role: str, username: str = "notif-test") -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({
        "sub": "1", "username": username, "role": role,
        "exp": int(time.time()) + 3600, "iat": int(time.time()),
    }).encode())
    sig = _b64(hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def _call(base: str, method: str, path: str, token: str | None = None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, None
    except OSError:
        return 0, None


def _wait_ready(base: str, proc: subprocess.Popen, log_path: Path, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _call(base, "GET", "/api/system/status")[0] == 401:
            return True
        time.sleep(0.3)
    return False


def _ws_checks(base: str, admin: str) -> None:
    """Live WebSocket contract. Skips cleanly when `websockets` is unavailable."""
    try:
        import websockets
    except ImportError:
        print("  [SKIP] websockets not installed")
        return

    ws_base = base.replace("http://", "ws://")

    async def scenario() -> None:
        # 1. No token — the handshake must not complete.
        rejected = False
        try:
            async with websockets.connect(f"{ws_base}/ws/notifications", open_timeout=5):
                rejected = False
        except Exception:
            rejected = True
        check("WS rejects a connection without a token", rejected)

        # 2. Authenticated → sync frame, then live event / read-all / cleared.
        async with websockets.connect(
            f"{ws_base}/ws/notifications?token={admin}", open_timeout=5,
        ) as sock:
            frame = json.loads(await asyncio.wait_for(sock.recv(), 5))
            check("WS sends a sync frame on connect", frame.get("type") == "sync",
                  str(frame)[:120])

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: _call(
                base, "POST", "/api/notifications", admin,
                {"kind": "warning", "title": "WS probe", "message": "live", "source": "health"}))

            frame = json.loads(await asyncio.wait_for(sock.recv(), 5))
            check("WS pushes the new event", frame.get("type") == "event"
                  and frame.get("event", {}).get("title") == "WS probe", str(frame)[:160])
            check("WS frame carries the unread count", isinstance(frame.get("unread"), int))

            await loop.run_in_executor(None, lambda: _call(
                base, "POST", "/api/notifications/read-all", admin))
            frame = json.loads(await asyncio.wait_for(sock.recv(), 5))
            check("WS pushes read-all", frame.get("type") == "read-all"
                  and frame.get("unread") == 0, str(frame)[:120])

            await loop.run_in_executor(None, lambda: _call(
                base, "DELETE", "/api/notifications", admin))
            frame = json.loads(await asyncio.wait_for(sock.recv(), 5))
            check("WS pushes cleared", frame.get("type") == "cleared", str(frame)[:120])

    asyncio.run(scenario())


def main() -> int:
    try:
        port = _free_port()
        tmp = Path(tempfile.mkdtemp(prefix="ethos-notif-ws-"))
        base = f"http://127.0.0.1:{port}"
        log_path = tmp / "server.log"

        # Seed a v1 store so the boot path exercises the real migration.
        (tmp / "notifications.json").write_text(json.dumps({
            "version": 1,
            "events": [{
                "id": "evt_legacy0001",
                "kind": "success",
                "title": "Legacy event",
                "message": "from v1",
                "source": "downloads",
                "created_at": "2026-01-01T10:00:00+00:00",
                "read": False,
            }],
        }), encoding="utf-8")

        env = {
            **os.environ,
            "ETHOS_DATA_DIR": str(tmp),
            "ETHOS_JWT_SECRET": SECRET,
            "ETHOS_PORT": str(port),
            "ETHOS_DEBUG": "0",
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
                check("isolated instance became ready", False)
                return 1

            admin = _token("admin")
            user = _token("user", "notif-guest")

            print("\n[store] v1 → v2 migration")
            status, data = _call(base, "GET", "/api/notifications?limit=10", admin)
            events = (data or {}).get("events") or []
            legacy = next((e for e in events if e.get("id") == "evt_legacy0001"), None)
            check("legacy event survived the upgrade", legacy is not None, str(status))
            if legacy:
                check("legacy event gained v2 fields",
                      legacy.get("action") is None and legacy.get("read_by") == []
                      and legacy.get("count") == 1, str(legacy))
            stored = json.loads((tmp / "notifications.json").read_text(encoding="utf-8"))
            check("store file is version 2", stored.get("version") == 2, str(stored.get("version")))
            check("pre-migration backup kept",
                  any(p.name.startswith("notifications.v1-") for p in tmp.iterdir()))

            print("\n[REST] create + policy")
            status, data = _call(base, "POST", "/api/notifications", admin, {
                "kind": "info", "title": "Panel probe", "message": "hello",
                "source": "backup", "action": {"label": "Open", "appId": "backup"},
            })
            created = (data or {}).get("event") or {}
            check("admin can publish with a custom source", status == 200
                  and created.get("source") == "backup", f"{status} {data}")
            check("action is persisted", (created.get("action") or {}).get("appId") == "backup")

            status, _ = _call(base, "POST", "/api/notifications", user,
                              {"title": "nope", "source": "system"})
            check("non-admin cannot publish a custom source", status == 403, str(status))

            status, data = _call(base, "POST", "/api/notifications", admin,
                                 {"title": "pkg", "package_id": "does-not-exist"})
            check("unknown package_id is rejected", status == 404, str(status))

            status, data = _call(base, "POST", "/api/notifications", admin, {
                "kind": "warning", "title": "Repeated", "message": "same condition",
                "source": "health", "dedupe_key": "probe-repeat",
            })
            first_id = ((data or {}).get("event") or {}).get("id")
            status, data = _call(base, "POST", "/api/notifications", admin, {
                "kind": "warning", "title": "Repeated", "message": "same condition",
                "source": "health", "dedupe_key": "probe-repeat",
            })
            second = (data or {}).get("event") or {}
            check("dedupe_key coalesces into one row",
                  first_id and second.get("id") == first_id and second.get("count") == 2,
                  str(second)[:160])

            status, data = _call(base, "GET", "/api/notifications?kind=warning&limit=50", admin)
            kinds = {e.get("kind") for e in (data or {}).get("events") or []}
            check("kind filter returns warnings only", status == 200 and kinds <= {"warning"},
                  str(kinds))

            print("\n[WS] /ws/notifications")
            _ws_checks(base, admin)

            print("\n[REST] cleanup endpoints")
            _call(base, "POST", "/api/notifications", admin,
                  {"kind": "info", "title": "Cleanup probe", "source": "system"})
            status, data = _call(base, "DELETE", "/api/notifications", admin)
            check("clear-all reports what it removed", status == 200
                  and (data or {}).get("cleared", 0) >= 1, str(data))
            status, data = _call(base, "GET", "/api/notifications?limit=10", admin)
            check("history is empty afterwards", (data or {}).get("events") == [], str(data))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        pass

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
