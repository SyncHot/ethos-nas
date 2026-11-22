#!/usr/bin/env python3
"""Watch-folder scanner — iteration, dedupe, move and error handling (no server).

Extracted from `routers/downloads.py` in P2-01.

Run:  .venv/bin/python tests/test_watch_folders.py
"""

import logging
import sys
import tempfile
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import watch_folders  # noqa: E402

FAILURES: list[str] = []
LOG = logging.getLogger("test.watch")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def _parse(data: bytes) -> dict:
    text = data.decode(errors="replace").strip()
    if text == "bad":
        raise ValueError("not a torrent")
    return {"hash": "h-" + text, "name": text, "magnet": "magnet:" + text, "size": 1}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ethos-watch-test-"))
    try:
        print("scan_folder")
        (tmp / "a.torrent").write_text("a")
        (tmp / "b.torrent").write_text("b")
        (tmp / "notes.txt").write_text("ignored")
        queued, calls = [], []

        def enqueue(task, info_hash):
            queued.append((task["id"], info_hash))
            calls.append("enqueue")

        count = watch_folders.scan_folder(
            str(tmp), parse_torrent=_parse, build_task=lambda info: {"id": info["hash"]},
            enqueue=enqueue, seen=set(), failed=set(), logger=LOG,
            on_queued=lambda: calls.append("pump"))
        check("queues every torrent", count == 2, str(count))
        check("enqueue called per torrent", sorted(item[0] for item in queued) == ["h-a", "h-b"],
              str(queued))
        check("pump scheduled per queued task", calls.count("pump") == 2, str(calls))
        check("non-torrent file untouched", (tmp / "notes.txt").exists())
        check("torrents moved out of the scan root",
              not (tmp / "a.torrent").exists() and (tmp / ".processed" / "a.torrent").exists())

        print("dedupe")
        seen = {"h-a", "h-b"}
        (tmp / "a.torrent").write_text("a")
        count = watch_folders.scan_folder(
            str(tmp), parse_torrent=_parse, build_task=lambda info: {"id": info["hash"]},
            enqueue=enqueue, seen=seen, failed=set(), logger=LOG)
        check("known infohash is skipped", count == 0, str(count))
        check("skipped file is left in place", (tmp / "a.torrent").exists())

        print("failures")
        (tmp / "a.torrent").unlink()
        (tmp / "broken.torrent").write_text("bad")
        failed = set()
        count = watch_folders.scan_folder(
            str(tmp), parse_torrent=_parse, build_task=lambda info: {"id": info["hash"]},
            enqueue=enqueue, seen=set(), failed=failed, logger=LOG)
        check("unparseable torrent is not queued", count == 0)
        check("filename remembered as failed", "broken.torrent" in failed)

        print("move collisions")
        (tmp / ".processed" / "c.torrent").write_text("old")
        (tmp / "c.torrent").write_text("c")
        moved = watch_folders.move_processed(str(tmp), "c.torrent", LOG)
        check("collision gets a suffix", moved is not None and moved.endswith("c.1.torrent"),
              str(moved))

        print("bounded dedupe set")
        bucket = set()
        for index in range(10):
            watch_folders.remember(bucket, f"x{index}", cap=5)
        check("set stays bounded", len(bucket) == 5, str(len(bucket)))
        check("newest entries survive", "x9" in bucket)

        check("missing folder returns 0", watch_folders.scan_folder(
            str(tmp / "nope"), parse_torrent=_parse, build_task=lambda info: {},
            enqueue=lambda *_: None, seen=set(), failed=set(), logger=LOG) == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("All watch-folder checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
