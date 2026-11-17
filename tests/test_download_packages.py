#!/usr/bin/env python3
"""Unit test — multi-part download packages (grouping + package actions).

The package fields are derived from task names/paths (`_package_info`), so no
server and no ETHOS_TEST_PASSWORD are needed. Uses an isolated ETHOS_DATA_DIR.

Run: .venv/bin/python tests/test_download_packages.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp(prefix="ethos-dl-packages-data-")
os.environ["ETHOS_DATA_DIR"] = DATA
os.environ.setdefault("ETHOS_JWT_SECRET", "download-packages-test")
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from backend.routers import downloads  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def make_task(task_id: str, name: str, status: str = "error", *, kind: str = "debrid_link",
              path: str = "/mnt/dl", error: str = "") -> dict:
    task = {
        "id": task_id,
        "url": f"https://rapidgator.net/file/abc/{name}",
        "name": name,
        "path": path,
        "kind": kind,
        "status": status,
        "size": 100,
        "downloaded": 0,
        "speed": 0,
        "error": error,
        "created_at": 1,
        "started_at": 0,
        "finished_at": 0,
        "category": "",
    }
    if kind == "debrid_link":
        task.update({
            "provider": "alldebrid", "_debrid_id": 0, "_debrid_status": "",
            "_debrid_progress": 0, "_files": [],
        })
    return task


def main() -> int:
    part1 = make_task("dl_a1", "Course.part01.rar.html")
    part2 = make_task("dl_a2", "Course.part02.rar.html")
    other = make_task("dl_b1", "Other.part01.rar")

    info = downloads._package_info
    first = info(part1) or {}
    second = info(part2) or {}
    third = info(other) or {}

    check("part number is parsed", first.get("part") == 1, str(first))
    check("parts share a package key", first.get("package_key") == second.get("package_key"),
          f"{first.get('package_key')} != {second.get('package_key')}")
    check("package name drops .partNN and .html", first.get("package") == "Course", str(first))
    check("different archive is a different package",
          first.get("package_key") != third.get("package_key"))
    check("plain files are not packaged",
          downloads._package_info(make_task("dl_x", "movie.mkv")) is None)
    check("legacy rar volume is detected",
          (info(make_task("dl_r", "Course.r00")) or {}).get("part") == 0)
    check("split zip volume is detected",
          (info(make_task("dl_z", "Course.zip.001")) or {}).get("part") == 1)
    check("different folders do not group",
          (info(make_task("dl_1", "Course.part01.rar", path="/mnt/one")) or {}).get("package_key")
          != (info(make_task("dl_2", "Course.part01.rar", path="/mnt/two")) or {}).get("package_key"))
    check("http destination file and debrid folder share a key",
          (info(make_task("dl_h", "Course.part01.rar", kind="http",
                          path="/mnt/dl/Course.part01.rar")) or {}).get("package_key")
          == first.get("package_key"))

    public = downloads._public(part1)
    check("_public exposes package fields",
          public.get("package_key") == first.get("package_key") and public.get("part") == 1,
          str(public.get("package_key")))

    # Package actions resolve every part through the same derived key.
    downloads._tasks[:] = [part1, part2, other]
    key = first.get("package_key") or ""

    async def _noop() -> None:
        return None

    original_pump = downloads._pump
    downloads._pump = _noop
    try:
        part1["status"] = "downloading"
        part2["status"] = "waiting"
        paused = asyncio.run(downloads.pause_package(key))
        check("pause package pauses every active part",
              paused.get("paused") == 2 and part1["status"] == part2["status"] == "paused",
              str(paused))

        part1["status"] = "error"
        part1["error"] = "AllDebrid: This link is not available on the file hoster website"
        part2["status"] = "paused"
        resumed = asyncio.run(downloads.resume_package(key))
        check("resume package re-queues failed and paused parts",
              resumed.get("resumed") == 2 and part1["status"] == "waiting" and not part1["error"],
              str(resumed))
        check("other package is untouched", other["status"] == "error")

        try:
            asyncio.run(downloads.pause_package("deadbeef0000"))
            check("unknown package returns 404", False, "no exception")
        except HTTPException as exc:
            check("unknown package returns 404", exc.status_code == 404, str(exc.status_code))

        deleted = asyncio.run(downloads.cancel_package(key))
        remaining = {t["id"] for t in downloads._tasks}
        check("cancel package removes every part",
              deleted.get("deleted") == 2 and not ({"dl_a1", "dl_a2"} & remaining), str(deleted))
        check("other package survives cancel", "dl_b1" in remaining, str(remaining))
    finally:
        downloads._pump = original_pump
        shutil.rmtree(DATA, ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
