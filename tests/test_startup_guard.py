#!/usr/bin/env python3
"""Unclean-stop detection + in-memory cache bounds.

services/startup_guard.py must turn "the previous run was killed" into exactly
one Notification Center event, and every long-lived module cache must stay
bounded (a per-path/per-track dict that only ever grows is a slow leak).

Run:
    .venv/bin/python tests/test_startup_guard.py
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import notifications_service, startup_guard  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def test_unclean_stop() -> None:
    events: list[tuple[str, str]] = []
    original_emit = notifications_service.emit_event
    original_marker = startup_guard._marker
    try:
        notifications_service.emit_event = (
            lambda kind, title, message, **kw: events.append((kind, title)))
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "started.json"
            startup_guard._marker = lambda: marker

            check("first run reports nothing", startup_guard.check_previous_run() is None)
            check("first run leaves the marker behind", marker.is_file())
            check("first run emits no event", events == [], str(events))

            info = startup_guard.check_previous_run()
            check("a surviving marker means the last run was killed", info is not None)
            check("the kill is reported once",
                  events == [("warning", "EthOS was not shut down cleanly")], str(events))

            startup_guard.mark_clean()
            check("a clean stop removes the marker", not marker.exists())
            startup_guard.check_previous_run()
            check("no new event after a clean stop", len(events) == 1, str(events))
    finally:
        notifications_service.emit_event = original_emit
        startup_guard._marker = original_marker


# (file, cache symbol, bound marker that must appear in that file)
_BOUND_CACHES = (
    ("backend/routers/gallery.py", "_priv_thumb_cache", "len(_priv_thumb_cache) >"),
    ("backend/routers/gallery.py", "_DURATION_CACHE", "len(_DURATION_CACHE) >"),
    ("backend/routers/share_links.py", "_THUMB_CACHE", "_THUMB_MAX_ENTRIES"),
    ("backend/routers/music.py", "_search_cache", "SEARCH_CACHE_MAX"),
    ("backend/routers/music.py", "_stream_cache", "len(_stream_cache) >"),
    ("backend/services/sharing_service.py", "_PATH_CACHE", "len(_PATH_CACHE) >"),
    ("backend/services/sharing_service.py", "_INCOMING_CACHE", "len(_INCOMING_CACHE) >"),
    ("backend/routers/video_station.py", "_LIB_CACHE", "_LIB_CACHE_TTL"),
)


def test_cache_bounds() -> None:
    for rel, symbol, marker in _BOUND_CACHES:
        source = (ROOT / rel).read_text(encoding="utf-8")
        check(f"{rel}: {symbol} is bounded", marker in source, f"missing {marker!r}")


def main() -> int:
    test_unclean_stop()
    test_cache_bounds()

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
