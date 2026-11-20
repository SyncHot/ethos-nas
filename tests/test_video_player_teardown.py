"""Static guards — File Manager video player teardown (sequential playback).

Pins the repair of "after three films the fourth one plays slowly":

  * the overlay is a fixed layer on <body>; opening a player must tear down a
    stray overlay anywhere — a window-scoped lookup missed orphans left by a
    closed window, and those kept streaming/decoding invisibly forever
  * switching films releases the previous stream before opening the next one
  * closing the player (or deleting the file) pauses and clears the source
  * the owning window's removal closes the player (MutationObserver) and the
    Files close listener is the last-resort teardown
  * thumbnails pause while a film plays (no 8K ffmpeg work competing for the
    disk) and resume on close
  * ``preload="metadata"`` — no aggressive multi-GB read-ahead per film

No server needed.

Run:  .venv/bin/python tests/test_video_player_teardown.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES_JS = "frontend/static/apps/files.js"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Body of a JS ``function name(...)`` (brace counting, strings ignored)."""
    match = re.search(rf"(?:async )?function {name}\s*\(", src)
    if not match:
        return ""
    idx = match.end() - 1
    depth = 0
    while idx < len(src):
        if src[idx] == "(":
            depth += 1
        elif src[idx] == ")":
            depth -= 1
            if depth == 0:
                break
        idx += 1
    start = src.index("{", idx)
    depth = 0
    for idx in range(start, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    return src[start:]


def main() -> int:
    src = _read(FILES_JS)
    open_player = _function_body(src, "fmOpenVideoPlayer")
    load_current = _function_body(src, "_videoLoadCurrent")
    close_player = _function_body(src, "fmCloseVideoPlayer")
    pump = _function_body(src, "_fmPumpThumbs")
    load_thumb = _function_body(src, "_fmLoadVideoThumb")
    watcher = _function_body(src, "_watchVideoOwnerWindow")
    delete_fn = _function_body(src, "fmVideoDelete")

    # 1. One player at a time, cleaned up globally (not just in the window).
    check("open tears down every stray overlay",
          "document.querySelectorAll('.fm-video-overlay')" in open_player)
    check("open pauses/clears stray <video> elements",
          "v.removeAttribute('src')" in open_player and "v.pause()" in open_player)
    check("open no longer looks for the overlay inside the window",
          "winEl.querySelector('.fm-video-overlay')" not in src)
    check("open watches the owning window", "_watchVideoOwnerWindow(root)" in open_player)

    # 2. Switching films releases the previous stream first.
    check("switch clears the previous source",
          "removeAttribute('src')" in load_current and "vidEl.pause()" in load_current)
    clear_at = load_current.find("removeAttribute('src')")
    assign_at = load_current.find("vidEl.src = ")
    check("teardown happens before the new src is assigned",
          0 <= clear_at < assign_at, f"clear={clear_at} assign={assign_at}")

    # 3. Close and delete fully release the media element.
    check("close clears the source", "removeAttribute('src')" in close_player)
    check("close removes every overlay",
          "document.querySelectorAll('.fm-video-overlay')" in close_player)
    check("close disconnects the window watcher", "_videoRootObserver.disconnect()" in close_player)
    check("close resumes thumbnails", "_fmPumpThumbs()" in close_player)
    check("delete clears the source", "removeAttribute('src')" in delete_fn)

    # 4. Owner-window teardown.
    check("watcher uses a MutationObserver", "new MutationObserver" in watcher)
    check("watcher closes when the root is gone",
          "root.isConnected" in watcher and "fmCloseVideoPlayer()" in watcher)
    check("Files app registers an onAppClose teardown", "onAppClose('files'," in src)
    check("onAppClose handler is imported", "onAppClose" in src.split("from '../core/window-manager.js'")[0])

    # 5. Thumbnails yield to playback.
    check("thumbnail pump pauses while playing",
          "if (_videoPlayer.open) return;" in pump)
    check("paused video tiles are re-queued",
          "img.dataset.vidSrc = url" in load_thumb)
    check("close re-observes thumbnails",
          "document.querySelectorAll('.fm-container').forEach(c => _fmObserveThumbs(c))" in close_player)

    # 6. No aggressive read-ahead on multi-GB files.
    check("video tag uses preload=metadata", 'preload="metadata"' in src)
    check("video tag no longer uses preload=auto", 'preload="auto"' not in src)

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
