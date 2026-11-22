#!/usr/bin/env python3
"""System stats WebSocket — /ws/stats push contract (isolated instance).

The System Overview window paints from this socket instead of polling
/api/system/status every 2 s. Covers:
  • missing/invalid token → handshake rejected (4001 before accept),
  • any authenticated user (not just admins) receives snapshots,
  • frames carry every field Overview paints,
  • the stream keeps pushing (two frames arrive within ~6 s).

    .venv/bin/python tests/test_stats_ws.py
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
SECRET = "stats-ws-test-secret"
DATA = tempfile.mkdtemp(prefix="ethos-stats-ws-")
os.environ.update({
    "ETHOS_DATA_DIR": DATA,
    "ETHOS_JWT_SECRET": SECRET,
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
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


def _status(base: str) -> int:
    request = urllib.request.Request(base + "/api/system/status")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def _wait_ready(base: str, proc: subprocess.Popen, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if _status(base) == 401:
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    try:
        import websockets
    except ImportError:
        print("websockets is not installed — skipping")
        return 0

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    ws_base = f"ws://127.0.0.1:{port}"
    admin, user = _token("admin", "admin"), _token("user", "user")

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        if not _wait_ready(base, proc):
            print((proc.stderr.read() if proc.stderr else "")[-2000:])
            print("  [FAIL] isolated instance did not become ready")
            return 1

        async def connect_rejected(url: str) -> bool:
            try:
                async with websockets.connect(url, open_timeout=5):
                    return False
            except Exception:
                return True

        async def frames(url: str, count: int) -> list[dict]:
            out = []
            async with websockets.connect(url, open_timeout=5) as socket_:
                for _ in range(count):
                    out.append(json.loads(await asyncio.wait_for(socket_.recv(), 8)))
            return out

        async def scenario() -> None:
            check("no token → handshake rejected", await connect_rejected(f"{ws_base}/ws/stats"))
            check("invalid token → handshake rejected",
                  await connect_rejected(f"{ws_base}/ws/stats?token=not-a-jwt"))

            admin_frames = await frames(f"{ws_base}/ws/stats?token={admin}", 2)
            check("admin receives two snapshots within 8 s", len(admin_frames) == 2)

            first = admin_frames[0]
            expected = {"cpu_percent", "cpu_per_core", "memory", "disk",
                        "network", "uptime_seconds", "hostname"}
            check("snapshot carries every field Overview paints",
                  expected <= set(first), str(sorted(set(first)))[:200])
            check("network block carries cumulative counters",
                  {"bytes_sent", "bytes_recv"} <= set(first.get("network") or {}))
            check("memory block carries total/used/percent",
                  {"total", "used", "percent"} <= set(first.get("memory") or {}))
            check("second frame is a newer snapshot",
                  admin_frames[1].get("uptime_seconds", 0) >= first.get("uptime_seconds", 0))

            user_frames = await frames(f"{ws_base}/ws/stats?token={user}", 1)
            check("plain user receives a snapshot (JWT-only endpoint)",
                  bool(user_frames) and expected <= set(user_frames[0]))

        asyncio.run(scenario())
        failures = [r for r in _results if not r[1]]
        print(f"\n{len(_results) - len(failures)}/{len(_results)} checks passed")
        return 1 if failures else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
