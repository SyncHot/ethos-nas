#!/usr/bin/env python3
"""Unit test — mirror / hoster failover for HTTP downloads.

No network: the transfer layer is stubbed, so the test only exercises the
engine contract (source switching, resume reset, retry budget, endpoints).
Run:
    .venv/bin/python tests/test_download_mirrors.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp(prefix="ethos-dl-mirror-data-")
WORK = tempfile.mkdtemp(prefix="ethos-dl-mirror-files-")
os.environ["ETHOS_DATA_DIR"] = DATA
os.environ.setdefault("ETHOS_JWT_SECRET", "download-mirror-test")
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from backend.routers import downloads  # noqa: E402
from backend.services import notifications_service  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def task(url: str, *, kind: str = "http", mirrors=None) -> dict:
    item = {
        "id": "dl_mirror", "url": url, "name": "file.bin", "path": str(Path(WORK) / "file.bin"),
        "kind": kind, "status": "downloading", "size": 100, "downloaded": 40, "speed": 10,
        "error": "", "_resume_pos": 40, "created_at": 1, "started_at": 1, "finished_at": 0,
    }
    if mirrors:
        item["_mirrors"] = list(mirrors)
    return item


def add_task_payload(**kw):
    return downloads.AddTaskRequest(**kw)


def main() -> int:
    cfg = {"download_dir": WORK, "auto_retry": True, "retry_max": 3, "retry_delay": 10,
           "duplicate_policy": "rename", "max_concurrent": 2}
    original = {
        "load_config": downloads._load_config,
        "save_tasks": downloads._save_tasks,
        "emit_event": notifications_service.emit_event,
        "is_admin": downloads._is_admin,
    }
    downloads._load_config = lambda: cfg
    downloads._save_tasks = lambda: None
    notifications_service.emit_event = lambda *a, **k: None
    downloads._is_admin = lambda *a, **k: True  # skip DNS lookups for the SSRF guard

    async def _noop_pump() -> None:
        return

    original_pump = downloads._pump
    downloads._pump = _noop_pump
    try:
        print("[source list]")
        t = task("https://a.example/file.bin", mirrors=["https://b.example/file.bin",
                                                        "https://a.example/file.bin",
                                                        "https://c.example/file.bin"])
        check("primary comes first, duplicates dropped",
              downloads._mirror_urls(t) == ["https://a.example/file.bin",
                                            "https://b.example/file.bin",
                                            "https://c.example/file.bin"])
        check("active source falls back to the primary",
              downloads._source_url(t) == "https://a.example/file.bin")

        print("[validation]")
        fresh = task("https://a.example/file.bin")
        kept = downloads._attach_mirrors(
            fresh, ["ftp://bad/file", "", "https://a.example/file.bin",
                    "https://b.example/file.bin", "https://b.example/file.bin"])
        check("only http(s), de-duplicated mirrors are stored",
              kept == 2 and fresh["_mirrors"] == ["https://b.example/file.bin"], str(fresh))
        many = task("https://a.example/file.bin")
        downloads._attach_mirrors(many, [f"https://m{i}.example/file.bin" for i in range(20)])
        check("mirror list is capped", downloads._mirror_urls(many) ==
              ["https://a.example/file.bin"] + [f"https://m{i}.example/file.bin" for i in range(10)])
        magnet = task("magnet:?xt=urn:btih:abc", kind="debrid")
        check("magnet tasks reject mirrors",
              downloads._attach_mirrors(magnet, ["https://b.example/f"]) == 0
              and "_mirrors" not in magnet)

        print("[failover]")
        part = Path(t["path"] + ".part")
        part.write_bytes(b"x" * 40)
        switched = downloads._try_next_mirror(t)
        check("switch moves to the second source", switched and downloads._source_url(t) ==
              "https://b.example/file.bin")
        check("switch resets the transfer offset",
              t["_resume_pos"] == 0 and t["downloaded"] == 0 and t["size"] == 0)
        check("switch drops the foreign partial file", not part.exists())
        check("switch re-queues with a fresh budget",
              t["status"] == "waiting" and t["_retry_count"] == 0 and t["_next_retry_at"] == 0)
        check("mirror index is visible", t["_mirror_index"] == 1)
        downloads._try_next_mirror(t)
        check("last mirror is reached", downloads._source_url(t) == "https://c.example/file.bin")
        check("no mirror left after the last one", downloads._try_next_mirror(t) is False)

        print("[_fail_task integration]")
        permanent = task("https://a.example/file.bin",
                         mirrors=["https://b.example/file.bin", "https://c.example/file.bin"])
        downloads._fail_task(permanent, "404 Not Found")
        check("permanent failure fails over instead of dying",
              permanent["status"] == "waiting"
              and downloads._source_url(permanent) == "https://b.example/file.bin"
              and permanent["error"] == "", str(permanent))
        check("failover does not consume the retry budget", permanent["_retry_count"] == 0)

        transient = task("https://a.example/file.bin", mirrors=["https://b.example/file.bin"])
        downloads._fail_task(transient, "connection reset by peer")
        check("transient failure retries the same source first",
              transient["status"] == "waiting"
              and downloads._source_url(transient) == "https://a.example/file.bin"
              and transient["_retry_count"] == 1)
        for _ in range(int(cfg["retry_max"])):
            downloads._fail_task(transient, "connection reset by peer")
        check("mirror is used once the retry budget is spent",
              downloads._source_url(transient) == "https://b.example/file.bin"
              and transient["_retry_count"] == 0, str(transient))

        last = task("https://a.example/file.bin")
        downloads._fail_task(last, "404 Not Found")
        check("no mirror → error as before",
              last["status"] == "error" and "404" in last["error"])

        print("[worker uses the active source]")
        seen: list[str] = []
        original_stream = downloads._stream_to_file

        def fake_stream(url, state, resume, stop_event, on_chunk, on_open,
                        limit_bps=0, global_bps=0):
            seen.append(url)
            if "a.example" in url:
                raise RuntimeError("HTTP 404 not found")
            on_open(200, 5, {}, 0)
            on_chunk(5)

        downloads._stream_to_file = fake_stream
        worker = task("https://a.example/file.bin", mirrors=["https://b.example/file.bin"])
        try:
            downloads._download_worker(worker, threading.Event())
        except RuntimeError as exc:  # the real caller turns this into _fail_task
            downloads._fail_task(worker, str(exc))
        downloads._download_worker(worker, threading.Event())
        check("second attempt fetches the mirror",
              seen == ["https://a.example/file.bin", "https://b.example/file.bin"], str(seen))
        check("task identity keeps the primary URL",
              worker["url"] == "https://a.example/file.bin")
        downloads._stream_to_file = original_stream

        print("[API surface]")
        created = asyncio.run(downloads.add_task(add_task_payload(
            url="https://a.example/file.bin", folder=WORK,
            mirrors=["https://b.example/file.bin", "https://c.example/file.bin"])))
        check("POST /tasks stores mirrors",
              created.get("mirror_count") == 3 and created.get("mirror_index") == 0, str(created))
        record = downloads._find(created["id"])
        more = asyncio.run(downloads.add_task_mirrors(
            created["id"], downloads.AddMirrorsRequest(urls=["https://d.example/file.bin"])))
        check("POST /tasks/{id}/mirrors appends",
              more.get("mirror_count") == 4, str(more))
        try:
            asyncio.run(downloads.add_task_mirrors(
                created["id"], downloads.AddMirrorsRequest(urls=["ftp://nope/file"])))
            check("unusable mirrors are rejected", False, "no exception")
        except HTTPException as exc:
            check("unusable mirrors are rejected", exc.status_code == 400)
        magnet_task = downloads._make_debrid_task(WORK, "magnet:?xt=urn:btih:abc", "magnet")
        downloads._tasks.append(magnet_task)
        try:
            asyncio.run(downloads.add_task_mirrors(
                magnet_task["id"], downloads.AddMirrorsRequest(urls=["https://b.example/f"])))
            check("magnet tasks cannot take mirrors", False, "no exception")
        except HTTPException as exc:
            check("magnet tasks cannot take mirrors", exc.status_code == 400)

        print("[reset]")
        record["_mirror_index"] = 2
        record["_active_url"] = "https://c.example/file.bin"
        asyncio.run(downloads.reset_task(created["id"]))
        check("reset returns to the primary source",
              record["_mirror_index"] == 0 and "_active_url" not in record, str(record))
    finally:
        downloads._load_config = original["load_config"]
        downloads._save_tasks = original["save_tasks"]
        notifications_service.emit_event = original["emit_event"]
        downloads._is_admin = original["is_admin"]
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
