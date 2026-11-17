#!/usr/bin/env python3
"""Unit test — bulk removal of failed downloads (`POST /tasks/clear-failed`).

Calls the router function directly, so no server and no ETHOS_TEST_PASSWORD are
needed. Uses an isolated ETHOS_DATA_DIR; every fixture lives in a temp dir that
is removed at the end.

Run: .venv/bin/python tests/test_download_clear_failed.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp(prefix="ethos-dl-clear-data-")
os.environ["ETHOS_DATA_DIR"] = DATA
os.environ.setdefault("ETHOS_JWT_SECRET", "download-clear-failed-test")
sys.path.insert(0, str(ROOT))

from backend.routers import downloads  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def make_task(task_id: str, status: str, dest: str, *, kind: str = "http", files=None) -> dict:
    task = {
        "id": task_id,
        "url": f"https://example.com/{task_id}",
        "name": os.path.basename(dest) or task_id,
        "path": dest,
        "kind": kind,
        "status": status,
        "size": 100,
        "downloaded": 40,
        "speed": 0,
        "error": "connection reset" if status == "error" else "",
        "created_at": 1,
        "started_at": 2,
        "finished_at": 3,
        "_resume_pos": 0,
        "_name_fixed": False,
        "category": "",
    }
    if kind == "debrid_link":
        task.update({"provider": "alldebrid", "_files": files or []})
    return task


def main() -> int:
    work = tempfile.mkdtemp(prefix="ethos-dl-clear-files-")
    try:
        # Failed HTTP download: a .part exists, and even a finished-looking file
        # must survive the cleanup.
        failed_dest = os.path.join(work, "failed.bin")
        failed_part = downloads._part_path(failed_dest)
        Path(failed_part).write_bytes(b"partial")
        Path(failed_dest).write_bytes(b"finished")

        # Failed debrid_link: one destination per file, partial payload only.
        link_dest = os.path.join(work, "hoster.bin")
        link_part = downloads._part_path(link_dest)
        Path(link_part).write_bytes(b"partial")

        completed_dest = os.path.join(work, "done.bin")
        Path(completed_dest).write_bytes(b"done")

        failed = make_task("dl_failed", "error", failed_dest)
        failed_link = make_task(
            "dl_failed_link", "error", work, kind="debrid_link",
            files=[{"dest": link_dest, "name": "hoster.bin"}],
        )
        completed = make_task("dl_done", "completed", completed_dest)
        paused = make_task("dl_paused", "paused", os.path.join(work, "paused.bin"))
        downloads._tasks[:] = [failed, failed_link, completed, paused]

        result = asyncio.run(downloads.clear_failed())
        check("removes both failed records", result.get("removed") == 2, str(result))
        check("failed HTTP record is gone",
              all(t["id"] != "dl_failed" for t in downloads._tasks))
        check("failed debrid_link record is gone",
              all(t["id"] != "dl_failed_link" for t in downloads._tasks))
        check("completed record stays", any(t["id"] == "dl_done" for t in downloads._tasks))
        check("paused record stays", any(t["id"] == "dl_paused" for t in downloads._tasks))
        check("HTTP .part removed", not os.path.exists(failed_part))
        check("finished file untouched", os.path.exists(failed_dest))
        check("debrid_link .part removed", not os.path.exists(link_part))
        check("completed file untouched", os.path.exists(completed_dest))

        stored = json.loads((Path(DATA) / "downloads_tasks.json").read_text())
        ids = {t["id"] for t in stored}
        check("persisted list dropped the failed ids",
              "dl_failed" not in ids and "dl_failed_link" not in ids, str(ids))

        again = asyncio.run(downloads.clear_failed())
        check("second call is a no-op", again.get("removed") == 0, str(again))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(DATA, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
