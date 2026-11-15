#!/usr/bin/env python3
"""WebSocket contract (TODO.md P3-08).

Static guard — no server needed:

    .venv/bin/python tests/test_ws_contract.py

Behaviour verified separately in a browser against an isolated instance: the
controller opens `/ws/files/events`, reports `readyState === OPEN`, and after
`close()` reports `CLOSED` with `closed === true` and no reconnect.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "frontend" / "static"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    ws = (STATIC / "core" / "ws.js").read_text()

    for needle, why in [
        ("export function connectWs", "exports connectWs()"),
        ("export function _backoffDelay", "exposes the backoff calculation"),
        ("MAX_DELAY_MS = 30000", "caps the retry delay"),
        ("Math.random()", "jitters the delay (no thundering herd)"),
        ("AUTH_CLOSE_CODES", "stops on auth close codes"),
        ("stopOnCodes", "lets a caller declare permanent codes"),
        ("onReconnectScheduled", "reports the delay back to the UI"),
        ("onAppClose", "closes with the owning window"),
        ("get readyState()", "returns a controller, not a raw socket"),
        ("get closed()", "exposes a closed flag"),
        ("function close(", "exposes close()"),
        ("function send(", "exposes send()"),
    ]:
        check(f"ws {why}", needle in ws, needle)

    check("no per-app websocket construction left",
          not any("new WebSocket" in p.read_text()
                  for p in list((STATIC / "apps").glob("*.js")) + list((STATIC / "core").glob("*.js"))
                  if p.name != "ws.js"),
          "an app still calls new WebSocket()")

    manual = [p.name for p in list((STATIC / "apps").glob("*.js")) + [STATIC / "app.js"]
              if re.search(r"['\"`]ws(s)?://", p.read_text()) or "wss: //" in p.read_text()]
    check("no app builds a ws:// URL by hand", not manual, ", ".join(manual))

    # HTTP download links legitimately carry ?token= (they cannot send headers);
    # socket URLs must not — the helper owns that.
    socket_token = []
    for path in (STATIC / "apps").glob("*.js"):
        for line in path.read_text().split("\n"):
            if "/ws/" in line and "token=" in line:
                socket_token.append(f"{path.name}: {line.strip()[:60]}")
    check("no app appends ?token= to a socket URL itself", not socket_token, "; ".join(socket_token))

    check("token is attached in one place", ws.count("token=${encodeURIComponent(token)}") == 1)
    # Regression: the PTY sends Uint8Array frames; JSON.stringify made the shell
    # print `{"0":101,…}` instead of running the command.
    check("send() passes binary frames through", "ArrayBuffer.isView(data)" in ws)
    check("send() does not stringify typed arrays",
          "typeof data === 'string' || isBinary ? data : JSON.stringify(data)" in ws)

    adopters = sorted(
        p.name for p in (STATIC / "apps").glob("*.js")
        if "core/ws.js" in p.read_text() or "core/log-stream.js" in p.read_text()
    )
    check("every socket consumer uses a shared helper", len(adopters) >= 4, ", ".join(adopters))

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
