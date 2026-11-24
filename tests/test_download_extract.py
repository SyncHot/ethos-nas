#!/usr/bin/env python3
"""Unit test — post-download archive extraction (auto and manual).

Uses a real ZIP so no external extractor is required. In-process, isolated
ETHOS_DATA_DIR, temp download folder. Run:
    .venv/bin/python tests/test_download_extract.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp(prefix="ethos-dl-extract-data-")
WORK = tempfile.mkdtemp(prefix="ethos-dl-extract-files-")
os.environ["ETHOS_DATA_DIR"] = DATA
os.environ.setdefault("ETHOS_JWT_SECRET", "download-extract-test")
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from backend.routers import downloads  # noqa: E402
from backend.services import download_config, notifications_service, path_scope, user_data  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def make_zip(path: Path, member: str = "hello.txt", content: bytes = b"hi") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, content)


def task(task_id: str, name: str, path: str, status: str = "completed", *, kind: str = "http",
         files=None) -> dict:
    item = {
        "id": task_id, "url": f"https://example.com/{name}", "name": name, "path": path,
        "kind": kind, "status": status, "size": 10, "downloaded": 10, "speed": 0,
        "error": "", "created_at": 1, "started_at": 1, "finished_at": 2, "category": "",
    }
    if kind in ("debrid", "debrid_link"):
        item["provider"] = "alldebrid"
        item["_files"] = files or []
    return item


async def run_auto(task: dict) -> None:
    await downloads._auto_extract_task(task)
    while downloads._EXTRACT_JOBS:
        await asyncio.gather(*list(downloads._EXTRACT_JOBS))


def main() -> int:
    print("[archive detection]")
    check("zip/rar/7z are archives",
          all(downloads._is_archive_path(p) for p in ("a.zip", "b.rar", "c.7z", "d.tar.gz")))
    check("plain files are not archives", not downloads._is_archive_path("movie.mkv"))

    single = Path(WORK) / "single.zip"
    make_zip(single)
    check("single archive is its own first volume", downloads._first_volume([str(single)]) == str(single))

    first = Path(WORK) / "Course.part01.rar"
    second = Path(WORK) / "Course.part02.rar"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    check("part01 is the first volume",
          downloads._first_volume([str(second), str(first)]) == str(first))

    link_dest = Path(WORK) / "hoster.zip"
    make_zip(link_dest)
    link_task = task("dl_link", "hoster.zip", WORK, kind="debrid_link",
                     files=[{"dest": str(link_dest), "downloaded": True}])
    check("debrid_link local dest is used",
          downloads._local_archive_paths(link_task) == [str(link_dest)])

    print("[defaults]")
    check("archives extract and sources are removed by default",
          download_config.DEFAULT_CONFIG["auto_extract"] is True
          and download_config.DEFAULT_CONFIG["auto_extract_delete"] is True)
    fresh = downloads._load_config()
    check("a new account gets both extraction defaults",
          fresh["auto_extract"] is True and fresh["auto_extract_delete"] is True, str(fresh))

    # An account that stored the old defaults (both off) is switched on once.
    store = user_data.user_store("downloads.json")
    store.save({"download_dir": WORK, "auto_extract": False, "auto_extract_delete": False})
    migrated = downloads._load_config()
    check("an existing off config is migrated to the new default",
          migrated["auto_extract"] is True and migrated["auto_extract_delete"] is True,
          str(migrated))
    check("the migration is recorded in the account slice",
          store.load().get("_extract_defaults_v2") is True)
    migrated["auto_extract"] = False
    migrated["auto_extract_delete"] = False
    downloads._save_config(migrated)
    respected = downloads._load_config()
    check("an explicit off after the migration is respected",
          respected["auto_extract"] is False and respected["auto_extract_delete"] is False,
          str(respected))

    def write_user(name: str, **values) -> None:
        with path_scope.system_scope(name):
            user_data.user_store("downloads.json").save({"_extract_defaults_v2": True, **values})

    write_user("alice", download_dir=WORK, auto_extract=True, auto_extract_delete=False)
    write_user("bob", download_dir=WORK, auto_extract=True, auto_extract_delete=True)
    # Link downloads created before the owner field existed belong to the admin.
    write_user("admin", download_dir=WORK, auto_extract=True, auto_extract_delete=False)
    with path_scope.system_scope():
        alice_cfg = downloads._config_for_tasks([{"owner": "alice"}])
        bob_cfg = downloads._config_for_tasks([{"owner": "bob"}])
        orphan_cfg = downloads._config_for_tasks([{"owner": ""}])
    check("the delete policy comes from the task owner's slice",
          alice_cfg["auto_extract_delete"] is False and bob_cfg["auto_extract_delete"] is True)
    check("an ownerless task uses the admin's extraction policy",
          orphan_cfg["auto_extract_delete"] is False, str(orphan_cfg))

    print("[auto extraction]")
    cfg = {"auto_extract": True, "auto_extract_delete": False, "download_dir": WORK}
    original_cfg = downloads._load_config
    original_save = downloads._save_tasks
    original_emit = notifications_service.emit_event
    downloads._load_config = lambda: cfg
    downloads._save_tasks = lambda: None
    notifications_service.emit_event = lambda *a, **k: None
    try:
        auto_zip = Path(WORK) / "auto.zip"
        make_zip(auto_zip, "inside.txt", b"payload")
        auto_task = task("dl_auto", "auto.zip", str(auto_zip))
        asyncio.run(run_auto(auto_task))
        dest = Path(str(auto_task.get("_extract_dest") or ""))
        check("auto-extract finishes", auto_task.get("_extract_state") == "done", str(auto_task))
        check("files are extracted next to the archive",
              (dest / "inside.txt").is_file() and (dest / "inside.txt").read_bytes() == b"payload",
              str(dest))
        check("archive is kept when deletion is off", auto_zip.exists())

        print("[delete after extraction]")
        cfg["auto_extract_delete"] = True
        delete_zip = Path(WORK) / "delete.zip"
        make_zip(delete_zip)
        delete_task = task("dl_delete", "delete.zip", str(delete_zip))
        asyncio.run(run_auto(delete_task))
        check("archive is removed with auto_extract_delete",
              delete_task.get("_extract_state") == "done" and not delete_zip.exists(),
              str(delete_task))

        empty_zip = Path(WORK) / "empty.zip"
        with zipfile.ZipFile(empty_zip, "w"):
            pass
        empty_task = task("dl_empty", "empty.zip", str(empty_zip))
        asyncio.run(run_auto(empty_task))
        check("an extractor run with no files keeps the source",
              empty_task.get("_extract_state") == "done" and empty_zip.exists(),
              str(empty_task))
        cfg["auto_extract_delete"] = False

        print("[package completeness]")
        p1 = Path(WORK) / "Pack.part01.rar"
        p2 = Path(WORK) / "Pack.part02.rar"
        p1.write_bytes(b"x")
        p2.write_bytes(b"x")
        part1 = task("dl_p1", "Pack.part01.rar", str(p1))
        part2 = task("dl_p2", "Pack.part02.rar", str(p2), status="downloading")
        downloads._tasks[:] = [part1, part2]
        asyncio.run(run_auto(part1))
        check("incomplete package is not extracted",
              not part1.get("_extract_state") and not part2.get("_extract_state"), str(part1))

        print("[manual extraction guards]")
        pending = task("dl_pending", "pending.zip", str(Path(WORK) / "pending.zip"),
                       status="downloading")
        try:
            downloads._manual_extract([pending])
            check("incomplete download cannot be extracted", False, "no exception")
        except HTTPException as exc:
            check("incomplete download cannot be extracted", exc.status_code == 409)

        no_archive = task("dl_plain", "movie.mkv", str(Path(WORK) / "movie.mkv"))
        Path(no_archive["path"]).write_bytes(b"x")
        try:
            downloads._manual_extract([no_archive])
            check("download without an archive is rejected", False, "no exception")
        except HTTPException as exc:
            check("download without an archive is rejected", exc.status_code == 400)

        manual_zip = Path(WORK) / "manual.zip"
        make_zip(manual_zip)
        manual = task("dl_manual", "manual.zip", str(manual_zip))

        async def _manual_scenario() -> dict:
            started = downloads._manual_extract([manual])
            while downloads._EXTRACT_JOBS:
                await asyncio.gather(*list(downloads._EXTRACT_JOBS))
            return started

        started = asyncio.run(_manual_scenario())
        check("manual extraction starts a job", started.get("status") == "extracting")
        check("manual extraction completes", manual.get("_extract_state") == "done", str(manual))
    finally:
        downloads._load_config = original_cfg
        downloads._save_tasks = original_save
        notifications_service.emit_event = original_emit
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
