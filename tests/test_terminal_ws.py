"""End-to-end test — Terminal PTY WebSocket (/ws/terminal/{target}).

Verifies the hardened wire protocol against a running instance:
  - JWT auth enforced (missing/garbage token → close 4001)
  - target allowlist (invalid container name → close 4002)
  - host shell round-trip over binary frames
  - resize control frame actually resizes the PTY (stty size)
  - JSON-looking text is written to the PTY verbatim (old bug: killed session)
  - plain non-JSON text passthrough
  - `exit` → "[shell exited with code 0]" notice + clean close 1000
  - optional `cwd` param: valid dir → shell starts in it; nonexistent/relative → close 4003
  - no orphaned shell processes left under the server

Requires the `websockets` package (ships with uvicorn[standard]).
Self-cleaning: every session it opens is closed and reaped; nothing persists.

Run:  .venv/bin/python tests/test_terminal_ws.py   (server must be up)
      ETHOS_TEST_BASE=http://127.0.0.1:9002 to target an isolated instance.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from fixture_dir import service_readable_tmpdir
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
if urllib.parse.urlparse(BASE).scheme not in ("http", "https"):
    raise SystemExit(f"ETHOS_TEST_BASE must be http(s), got {BASE!r}")
WS_BASE = BASE.replace("https://", "wss://").replace("http://", "ws://")
USER = os.environ.get("ETHOS_TEST_USER", "marcin")
# Test credential resolved from the environment — never hardcode it here.
PASS = os.environ.get("ETHOS_TEST_PASS") or os.environ.get("ETHOS_TEST_PASSWORD") or ""
if not PASS:
    raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def login() -> str:
    req = urllib.request.Request(  # nosec: B310, S310 — BASE scheme validated at import
        BASE + "/api/auth/login",
        data=json.dumps({"username": USER, "password": PASS}).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec: B310, S310
        return json.loads(resp.read().decode())["token"]


async def wait_for(ws, needle: str, timeout: float = 8.0) -> str:
    """Read frames until `needle` appears in accumulated output."""
    texts, blob = [], bytearray()
    end = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = end - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(
                f"timeout waiting for {needle!r}; got: "
                f"{(''.join(texts) + blob.decode('utf-8', 'replace'))[-800:]!r}")
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except TimeoutError:
            raise AssertionError(
                f"timeout waiting for {needle!r}; got: "
                f"{(''.join(texts) + blob.decode('utf-8', 'replace'))[-800:]!r}") from None
        if isinstance(msg, str):
            texts.append(msg)
        else:
            blob.extend(msg)
        if needle in "".join(texts) + blob.decode("utf-8", "replace"):
            return "".join(texts) + blob.decode("utf-8", "replace")


async def expect_close(ws, code: int, what: str):
    """Drain any text notices until the close frame; assert its code."""
    try:
        while True:
            await ws.recv()
    except ConnectionClosed as e:
        got = e.rcvd.code if e.rcvd else None
        assert got == code, f"{what}: expected close {code}, got {got}"
    else:
        raise AssertionError(f"{what}: connection stayed open")


def _server_pid() -> str | None:
    """PID of the uvicorn server for BASE (python process only — pgrep -f also
    matches nohup wrapper shells whose cmdline contains the same text)."""
    port = urllib.parse.urlparse(BASE).port or 80
    out = subprocess.run(["pgrep", "-f", "uvicorn backend.main:app"],
                         capture_output=True, text=True).stdout.split()
    for pid in out:
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read()
            with open(f"/proc/{pid}/cmdline") as f:
                cmdline = f.read().replace("\0", " ")
        except OSError:
            continue
        if "python" in comm and f"--port {port}" in cmdline:
            return pid
    return None


async def run(token: str) -> int:
    failures = 0

    # ── Auth enforcement ────────────────────────────────────────────────────
    async with connect(f"{WS_BASE}/ws/terminal/host") as ws:
        await expect_close(ws, 4001, "missing JWT")
    if not check("rejects missing JWT (close 4001)", True):
        failures += 1

    async with connect(f"{WS_BASE}/ws/terminal/host?token=garbage.token.here") as ws:
        await expect_close(ws, 4001, "invalid JWT")
    if not check("rejects invalid JWT (close 4001)", True):
        failures += 1

    # ── Target allowlist ────────────────────────────────────────────────────
    async with connect(f"{WS_BASE}/ws/terminal/container:bad%20name!?token={token}") as ws:
        await expect_close(ws, 4002, "invalid target")
    if not check("rejects invalid container name (close 4002)", True):
        failures += 1

    # ── Host session: round-trip, resize, input survival, exit ─────────────
    async with connect(f"{WS_BASE}/ws/terminal/host?token={token}") as ws:
        await wait_for(ws, "$")  # login shell prompt
        if not check("host shell starts (prompt received)", True):
            failures += 1

        # The PTY must be the controlling terminal (TIOCSCTTY) — otherwise
        # interactive bash disables job control.
        await ws.send(b"echo JC$(set -o | grep monitor | awk '{print $2}')END\r")
        try:
            await wait_for(ws, "JConEND", timeout=5.0)
            check("interactive shell has job control", True)
        except AssertionError:
            if not check("interactive shell has job control", False, "monitor off"):
                failures += 1

        await ws.send(b"echo MARKER_$((6*7))\r")
        await wait_for(ws, "MARKER_42")
        if not check("command round-trip over binary frames", True):
            failures += 1

        # Resize control frame → stty size must reflect it.
        await ws.send(json.dumps({"type": "resize", "cols": 132, "rows": 40}))
        await asyncio.sleep(0.3)
        await ws.send(b"stty size\r")
        await wait_for(ws, "40 132")
        if not check("resize control frame applied (stty size → 40 132)", True):
            failures += 1

        # JSON-looking text must reach the PTY verbatim (old bug: killed session).
        await ws.send('{"not":"a resize"}\r')
        await asyncio.sleep(0.5)
        await ws.send(b"echo ALIVE_AFTER_JSON\r")
        await wait_for(ws, "ALIVE_AFTER_JSON", timeout=6.0)
        if not check("JSON-like text survives (written verbatim)", True):
            failures += 1

        # Plain non-JSON text path.
        await ws.send("echo PLAINTEXT_OK\r")
        await wait_for(ws, "PLAINTEXT_OK")
        if not check("plain text input passthrough", True):
            failures += 1

        # Shell exit → notice with real exit code + clean close 1000.
        await ws.send(b"exit\r")
        notices = ""
        closed_code = None
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=6.0)
                if isinstance(msg, str):
                    notices += msg
        except ConnectionClosed as e:
            closed_code = e.rcvd.code if e.rcvd else None
        except TimeoutError:
            pass
        if not check("shell exit notice sent (code 0)", "[shell exited with code 0]" in notices,
                     notices[-200:]):
            failures += 1
        if not check("clean close after exit (1000)", closed_code == 1000, f"got {closed_code}"):
            failures += 1

    # ── Orphan check: server must have no live shell children ───────────────
    # ── cwd param ─────────────────────────────────────────────────────────
    cwd_dir = service_readable_tmpdir(prefix="ethos-term-cwd-")
    try:
        async with connect(
            f"{WS_BASE}/ws/terminal/host?token={token}&cwd={urllib.parse.quote(cwd_dir)}") as ws:
            await wait_for(ws, "$")
            await ws.send(b"pwd\r")
            out = await wait_for(ws, cwd_dir, timeout=8.0)
            if not check("cwd= valid dir → shell starts in it (pwd)", cwd_dir in out, out[-200:]):
                failures += 1
            await ws.send(b"exit\r")
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=6.0)
            except (ConnectionClosed, TimeoutError):
                pass

        async with connect(
            f"{WS_BASE}/ws/terminal/host?token={token}&cwd=/nonexistent/ethos-test-dir") as ws:
            await expect_close(ws, 4003, "nonexistent cwd")
        if not check("cwd= nonexistent dir (close 4003)", True):
            failures += 1

        async with connect(
            f"{WS_BASE}/ws/terminal/host?token={token}&cwd=some/relative/dir") as ws:
            await expect_close(ws, 4003, "relative cwd")
        if not check("cwd= relative path (close 4003)", True):
            failures += 1
    finally:
        shutil.rmtree(cwd_dir, ignore_errors=True)

    await asyncio.sleep(1.0)
    pid = _server_pid()
    if not pid:
        if not check("no orphaned shell processes", False, "server PID not found"):
            failures += 1
    else:
        children = subprocess.run(["pgrep", "-P", pid], capture_output=True, text=True).stdout.split()
        detail = ""
        for cpid in children:
            try:
                with open(f"/proc/{cpid}/cmdline") as f:
                    cmd = f.read().replace("\0", " ").strip()
            except OSError:
                cmd = "?"
            if "bash" in cmd or "docker" in cmd:
                detail += f"{cpid} {cmd}\n"
        if not check("no orphaned shell processes (server children)", not detail, detail.strip()):
            failures += 1

    return failures


def main() -> int:
    print(f"\nTerminal WS E2E — {BASE}")
    try:
        token = login()
    except Exception as exc:
        print(f"  [FAIL] PAM login — {exc}")
        return 1
    t0 = time.time()
    failures = asyncio.run(run(token))
    total = 14
    print(f"\n{total - failures}/{total} passed in {time.time() - t0:.1f}s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
