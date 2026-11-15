#!/usr/bin/env python3
"""One shared poller invariant (TODO.md P3-09).

Static guard, no server needed:

    .venv/bin/python tests/test_poller.py

The behaviour itself (pause on `document.hidden`, immediate catch-up when the tab
becomes visible again, no ticks after `stop()`, empty `activePollers()`) was
verified in a real browser against an isolated instance:

    ticksVisible 2 · ticksWhileHidden 0 · catchUpAfterVisible 1
    ticksAfterStop 0 · registeredAfterStop 0
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "frontend" / "static" / "core"
APPS = ROOT / "frontend" / "static" / "apps"
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    poller = CORE / "poller.js"
    check("core/poller.js exists", poller.is_file())
    src = poller.read_text() if poller.is_file() else ""

    for needle, why in [
        ("export function poll(", "exports poll()"),
        ("document.hidden", "pauses while the tab is hidden"),
        ("visibilitychange", "wakes up on tab return"),
        ("busy", "skips overlapping runs"),
        ("2 ** failures", "backs off on repeated errors"),
        ("onAppClose", "stops when the app's window closes"),
        ("export function activePollers", "exposes live pollers for tests"),
    ]:
        check(f"poller {why}", needle in src, needle)

    manager = (CORE / "window-manager.js").read_text()
    check("window-manager exports onAppClose", "export function onAppClose" in manager)
    check("window-manager notifies on close", "_notifyClosed(" in manager)
    check("close notification waits for the last window", "stillOpen" in manager)

    offenders = []
    for path in sorted(APPS.glob("*.js")):
        text = path.read_text()
        if re.search(r"\bsetInterval\(", text):
            offenders.append(path.name)
    check("no app rolls its own setInterval poller", not offenders, ", ".join(offenders))

    users = [p.name for p in APPS.glob("*.js") if "core/poller.js" in p.read_text()]
    # Twelve built-in apps used to poll; several UIs moved into packages
    # (editor, docker, kanban, photos, video, surveillance), so the core set is
    # smaller now. Keep the guard: any new polling app must adopt the helper.
    check("the shared poller is actually adopted", len(users) >= 5, f"{len(users)}: {', '.join(sorted(users))}")

    # Window lifecycle (P3-07): focus must not rebuild the taskbar.
    focus_body = manager.split("export function focusWindow")[1].split("\nexport function")[0]
    check("focusWindow uses the cheap taskbar update",
          "updateTaskbarActive();" in focus_body and "renderTaskbar();" not in focus_body,
          "focusWindow still calls renderTaskbar()")
    check("renderTaskbar no longer wipes the bar",
          "tb.innerHTML = ''" not in manager, "renderTaskbar still clears innerHTML")
    check("taskbar has a class-only refresh", "export function updateTaskbarActive" in manager)
    check("taskbar items are keyed by window id", "data-win-id" in manager)
    check("system entry has a stable class", "taskbar-system" in manager)
    check("close guards receive the window element",
          "appId → (winEl) => boolean" in manager or "winEl) => boolean" in manager)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
