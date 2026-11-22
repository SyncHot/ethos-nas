#!/usr/bin/env python3
"""Unit test — Resume repairs a task whose destination folder is not writable.

Queued tasks store an absolute path. After the download folder changes (or a
qBittorrent savePath from an old host layout is imported), the background worker
runs as the EthOS service user and cannot create e.g. `/home/qb-admin/Downloads`.
Resume moves a not-yet-downloaded task under the configured download dir.

Run:
    .venv/bin/python tests/test_download_rebase.py
"""
import os
import sys

os.environ.setdefault("ETHOS_DATA_DIR", "/tmp/ethos-dl-rebase-test")
os.environ.setdefault("ETHOS_JWT_SECRET", "download-rebase-test")
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.routers import downloads  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def make_task(path: str, kind: str = "debrid", **extra) -> dict:
    task = {
        "id": "dl_rebase", "kind": kind, "status": "error", "downloaded": 0,
        "path": path, "name": os.path.basename(path), "category": "movies",
        "_files": [],
    }
    task.update(extra)
    return task


def main() -> int:
    cfg = {"download_dir": "/mnt/SharedDrive/torrents/downloads"}
    original = downloads._usable_parent

    print("[rebase an unwritable old folder]")
    downloads._usable_parent = lambda _path: False
    try:
        t = make_task(
            "/home/qb-admin/Downloads/movies/Steps (2026)",
            _files=[{"dest": "/home/qb-admin/Downloads/movies/Steps (2026)/file.mkv"}],
        )
        check("returns True", downloads._rebase_task_to_download_dir(t, cfg))
        check("path is re-rooted",
              t["path"] == "/mnt/SharedDrive/torrents/downloads/movies/Steps (2026)", t["path"])
        check("file dest follows the task",
              t["_files"][0]["dest"] == "/mnt/SharedDrive/torrents/downloads/movies/Steps (2026)/file.mkv",
              t["_files"][0]["dest"])

        print("[debrid_link keeps its target folder]")
        link = make_task("/home/qb-admin/Downloads/movies", kind="debrid_link",
                         _files=[{"dest": "/home/qb-admin/Downloads/movies/movie.mkv"}])
        check("returns True", downloads._rebase_task_to_download_dir(link, cfg))
        check("path becomes the category folder",
              link["path"] == "/mnt/SharedDrive/torrents/downloads/movies", link["path"])
        check("file dest is re-rooted",
              link["_files"][0]["dest"] == "/mnt/SharedDrive/torrents/downloads/movies/movie.mkv",
              link["_files"][0]["dest"])

        print("[guards]")
        inside = make_task("/mnt/SharedDrive/torrents/downloads/movies/X")
        check("path already inside download_dir is untouched",
              not downloads._rebase_task_to_download_dir(inside, cfg) and inside["path"].endswith("/movies/X"))

        partial = make_task("/home/qb-admin/Downloads/movies/X", downloaded=1234)
        check("a task with downloaded bytes is untouched",
              not downloads._rebase_task_to_download_dir(partial, cfg) and partial["path"].startswith("/home/qb-admin"))

        active = make_task("/home/qb-admin/Downloads/movies/X", status="downloading")
        check("an active task is untouched", not downloads._rebase_task_to_download_dir(active, cfg))

        print("[writable external folder]")
        downloads._usable_parent = lambda _path: True
        external = make_task("/srv/media/movies/X")
        check("a working custom destination is untouched",
              not downloads._rebase_task_to_download_dir(external, cfg)
              and external["path"] == "/srv/media/movies/X")
    finally:
        downloads._usable_parent = original

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("OK: all rebase checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
