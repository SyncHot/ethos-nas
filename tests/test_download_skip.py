#!/usr/bin/env python3
"""Unit test — per-part skip / unskip.

A broken volume in a multi-part package can be excluded without deleting the
record, so the rest of the package stays intact and visible.
Run:
    .venv/bin/python tests/test_download_skip.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp(prefix="ethos-dl-skip-data-")
WORK = tempfile.mkdtemp(prefix="ethos-dl-skip-files-")
os.environ["ETHOS_DATA_DIR"] = DATA
os.environ.setdefault("ETHOS_JWT_SECRET", "download-skip-test")
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from backend.routers import downloads  # noqa: E402
from backend.services import notifications_service, qb_compat  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def task(task_id: str, name: str, status: str) -> dict:
    return {
        "id": task_id, "url": f"https://example.com/{name}", "name": name,
        "path": str(Path(WORK) / name), "kind": "http", "status": status,
        "size": 100, "downloaded": 100 if status == "completed" else 40, "speed": 0,
        "error": "", "created_at": 1, "started_at": 1, "finished_at": 0,
    }


def main() -> int:
    cfg = {"download_dir": WORK, "auto_retry": True, "retry_max": 3, "retry_delay": 10,
           "auto_extract": False, "auto_extract_delete": False}
    original = {
        "load_config": downloads._load_config,
        "save_tasks": downloads._save_tasks,
        "emit_event": notifications_service.emit_event,
    }
    downloads._load_config = lambda: cfg
    downloads._save_tasks = lambda: None
    notifications_service.emit_event = lambda *a, **k: None

    async def _noop_pump() -> None:
        return

    original_pump = downloads._pump
    downloads._pump = _noop_pump
    try:
        print("[skip]")
        item = task("dl_p1", "Pack.part02.rar", "downloading")
        part = Path(item["path"] + ".part")
        part.write_bytes(b"x" * 40)
        item["_next_retry_at"] = 12345
        downloads._tasks.append(item)
        out = asyncio.run(downloads.skip_task("dl_p1"))
        check("status becomes skipped", out["status"] == "skipped", str(out))
        check("partial payload is dropped", not part.exists())
        check("retry schedule is cleared", item["_next_retry_at"] == 0)
        check("finished_at is stamped", out["finished_at"] > 0)
        check("skipping twice is harmless",
              asyncio.run(downloads.skip_task("dl_p1"))["status"] == "skipped")

        print("[guards]")
        done = task("dl_done", "Pack.part01.rar", "completed")
        downloads._tasks.append(done)
        try:
            asyncio.run(downloads.skip_task("dl_done"))
            check("completed downloads cannot be skipped", False, "no exception")
        except HTTPException as exc:
            check("completed downloads cannot be skipped", exc.status_code == 409)
        try:
            asyncio.run(downloads.skip_task("dl_missing"))
            check("unknown task is a 404", False, "no exception")
        except HTTPException as exc:
            check("unknown task is a 404", exc.status_code == 404)

        print("[unskip]")
        out = asyncio.run(downloads.unskip_task("dl_p1"))
        check("unskip returns to the queue", out["status"] == "waiting", str(out))
        check("unskip clears the finish stamp", out["finished_at"] == 0)
        check("unskip on a running task is a no-op",
              asyncio.run(downloads.unskip_task("dl_done"))["status"] == "completed")

        print("[package + qB semantics]")
        info = downloads._package_info(item)
        check("skipped part still groups", bool(info) and info["part"] == 2, str(info))
        group = downloads._package_tasks(info["package_key"])
        check("package still lists the record", any(t["id"] == "dl_p1" for t in group))
        asyncio.run(downloads.skip_task("dl_p1"))
        extracted: list[str] = []
        original_start = downloads._start_extract
        downloads._start_extract = lambda tasks, archive: extracted.append(archive)
        try:
            asyncio.run(downloads._auto_extract_task(done))
        finally:
            downloads._start_extract = original_start
        check("a skipped part blocks extraction", extracted == [], str(extracted))
        qb = qb_compat._qb_torrent_obj({**item, "status": "skipped", "hash": "a" * 40})
        check("qB shim reports skipped as pausedDL", qb.get("state") == "pausedDL", str(qb.get("state")))
        check("public payload keeps the row", downloads._public(item)["status"] == "skipped")
    finally:
        downloads._load_config = original["load_config"]
        downloads._save_tasks = original["save_tasks"]
        notifications_service.emit_event = original["emit_event"]
        downloads._pump = original_pump
        downloads._tasks[:] = []
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.rmtree(DATA, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
