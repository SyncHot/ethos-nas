#!/usr/bin/env python3
"""Unified Logs WebSocket — one socket, JSON frames, admin-only (live instance).

Covers docs/logs.md:
  • 4001/4003 handshake rejection (no token / non-admin)
  • `source=app` pushes an `entry` frame after an ingest
  • file sources validate the `/var/log` allowlist and report errors as frames
  • unknown sources and invalid container names fail with a JSON `error` frame
  • the legacy `/ws/logs/system` path uses the same handler

    .venv/bin/python tests/test_logs_ws.py
"""

import asyncio
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
SECRET = "logs-ws-test-secret"
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


def _ws_checks(base: str, admin: str, user: str) -> None:
    try:
        import websockets
    except ImportError:
        print("  [SKIP] websockets not installed")
        return

    ws_base = base.replace("http://", "ws://")

    async def connect_rejected(url: str) -> bool:
        try:
            async with websockets.connect(url, open_timeout=5):
                return False
        except Exception:
            return True

    async def first_frames(url: str, count: int = 1) -> list[dict]:
        frames = []
        async with websockets.connect(url, open_timeout=5) as socket:
            for _ in range(count):
                frames.append(json.loads(await asyncio.wait_for(socket.recv(), 5)))
        return frames

    async def scenario() -> None:
        check("no token → handshake rejected", await connect_rejected(f"{ws_base}/ws/logs?source=app"))
        check("non-admin → handshake rejected",
              await connect_rejected(f"{ws_base}/ws/logs?source=app&token={user}"))
        check("non-admin file source rejected",
              await connect_rejected(f"{ws_base}/ws/logs?source=file:/var/log/syslog&token={user}"))

        # Live app stream: ingest must produce a JSON `entry` frame.
        async with websockets.connect(
            f"{ws_base}/ws/logs?source=app&token={admin}", open_timeout=5,
        ) as socket:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: _call(
                base, "POST", "/api/logs/ingest", user,
                {"level": "warning", "message": "ws live probe", "source": "browser:logs"}))
            matched = None
            # The socket also carries uvicorn access/error INFO rows — read until
            # our probe shows up (or the timeout proves the push is broken).
            for _ in range(12):
                frame = json.loads(await asyncio.wait_for(socket.recv(), 5))
                if (frame.get("type") == "entry"
                        and (frame.get("entry") or {}).get("message") == "ws live probe"):
                    matched = frame
                    break
            check("app stream pushes an entry frame", matched is not None, str(matched)[:160])
            check("entry carries the browser app",
                  (matched or {}).get("entry", {}).get("app") == "browser", str(matched)[:160])

        frames = await first_frames(
            f"{ws_base}/ws/logs?source=file:/var/log/ethos-does-not-exist.log&token={admin}", 1)
        check("missing file reports an error frame", frames[0].get("type") == "error", str(frames)[:160])

        frames = await first_frames(f"{ws_base}/ws/logs?source=file:/etc/passwd&token={admin}", 1)
        check("file outside the allowlist reports an error frame",
              frames[0].get("type") == "error" and "outside" in str(frames[0].get("detail", "")).lower(),
              str(frames)[:160])

        frames = await first_frames(f"{ws_base}/ws/logs?source=container:-bad&token={admin}", 1)
        check("invalid container name reports an error frame",
              frames[0].get("type") == "error", str(frames)[:160])

        frames = await first_frames(f"{ws_base}/ws/logs?source=nope&token={admin}", 1)
        check("unknown source reports an error frame",
              frames[0].get("type") == "error", str(frames)[:160])

        frames = await first_frames(
            f"{ws_base}/ws/logs/system?path=/var/log/ethos-does-not-exist.log&token={admin}", 1)
        check("legacy /ws/logs/system uses the same JSON contract",
              frames[0].get("type") == "error", str(frames)[:160])

    asyncio.run(scenario())


def main() -> int:
    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="ethos-logs-ws-"))
    base = f"http://127.0.0.1:{port}"
    log_path = tmp / "server.log"
    env = {
        **os.environ,
        "ETHOS_DATA_DIR": str(tmp),
        "ETHOS_JWT_SECRET": SECRET,
        "ETHOS_PORT": str(port),
        "ETHOS_DEBUG": "0",
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
        _ws_checks(base, _token("admin", "ws-admin"), _token("user", "ws-guest"))

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
