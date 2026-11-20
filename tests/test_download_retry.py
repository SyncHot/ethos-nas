#!/usr/bin/env python3
"""Unit test — auto-retry backoff and duplicate handling.

Calls the downloads router in-process; no server and no ETHOS_TEST_PASSWORD.
Uses an isolated ETHOS_DATA_DIR and a temp download folder.

Run: .venv/bin/python tests/test_download_retry.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp(prefix="ethos-dl-retry-data-")
WORK = tempfile.mkdtemp(prefix="ethos-dl-retry-files-")
os.environ["ETHOS_DATA_DIR"] = DATA
os.environ.setdefault("ETHOS_JWT_SECRET", "download-retry-test")
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from backend.routers import downloads  # noqa: E402
from backend.services import download_config  # noqa: E402
from backend.services import path_scope  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def task(url: str, name: str, path: str, status: str = "waiting") -> dict:
    return {
        "id": f"dl_{abs(hash((url, path))) % 10**8}",
        "url": url,
        "name": name,
        "path": path,
        "kind": "http",
        "status": status,
        "size": 10,
        "downloaded": 0,
        "speed": 0,
        "error": "",
        "created_at": 1,
        "started_at": 0,
        "finished_at": 0,
        "category": "",
    }


def main() -> int:
    # Engine-level test: retries run in the background as the system identity.
    path_scope.CURRENT_ROLE.set("admin")
    path_scope.CURRENT_USER.set("system")
    print("[config defaults]")
    defaults = download_config.DEFAULT_CONFIG
    check("auto-retry is on by default", defaults.get("auto_retry") is True)
    check("three retries by default", defaults.get("retry_max") == 3)
    check("base retry delay is 60 s", defaults.get("retry_delay") == 60)
    check("duplicate policy defaults to rename", defaults.get("duplicate_policy") == "rename")

    print("[retry classification]")
    check("connection reset is retryable", downloads._is_retryable("Connection reset by peer"))
    check("HTTP 502 is retryable", downloads._is_retryable("HTTP 502: Bad Gateway"))
    check("429 is retryable", downloads._is_retryable("HTTP 429: Too Many Requests"))
    check("dead hoster link is permanent",
          not downloads._is_retryable(
              "AllDebrid: This link is not available on the file hoster website"))
    check("bad debrid key is permanent", not downloads._is_retryable("Invalid API key (bad_token)"))
    check("missing provider is permanent",
          not downloads._is_retryable("Debrid provider is no longer configured"))
    check("backoff doubles", [downloads._retry_delay({"retry_delay": 60}, n) for n in (1, 2, 3)]
          == [60, 120, 240])
    check("backoff is capped", downloads._retry_delay({"retry_delay": 3600}, 10) <= 6 * 3600)

    print("[failure / backoff]")
    wakes: list[str] = []
    original_cfg = downloads._load_config
    original_wake = downloads._schedule_retry_wakeup
    original_notify = downloads._notify_status_change
    original_save = downloads._save_tasks
    cfg = {
        "auto_retry": True, "retry_max": 2, "retry_delay": 60,
        "download_dir": WORK, "categories": [], "debrid": {},
    }
    downloads._load_config = lambda: cfg
    downloads._schedule_retry_wakeup = lambda: wakes.append("wake")
    downloads._notify_status_change = lambda *a, **k: None
    downloads._save_tasks = lambda: None
    try:
        dying = task("https://example.com/a.bin", "a.bin", os.path.join(WORK, "a.bin"),
                     status="downloading")
        downloads._fail_task(dying, "Connection reset by peer")
        check("transient failure is re-queued",
              dying["status"] == "waiting" and dying["_retry_count"] == 1, str(dying))
        check("retry time is in the future",
              dying.get("_next_retry_at", 0) > time.time(), str(dying.get("_next_retry_at")))
        check("a wake-up was scheduled", wakes == ["wake"], str(wakes))

        dying["status"] = "downloading"
        downloads._fail_task(dying, "HTTP 502: Bad Gateway")
        check("second transient failure counts up", dying["_retry_count"] == 2)

        dying["status"] = "downloading"
        downloads._fail_task(dying, "HTTP 503: Service Unavailable")
        check("exhausted attempts end in error",
              dying["status"] == "error" and dying["_next_retry_at"] == 0, str(dying))

        permanent = task("https://example.com/dead.bin", "dead.bin",
                         os.path.join(WORK, "dead.bin"), status="downloading")
        downloads._fail_task(
            permanent, "AllDebrid: This link is not available on the file hoster website")
        check("permanent failure is not retried",
              permanent["status"] == "error" and permanent.get("_retry_count", 0) == 0,
              str(permanent))

        cfg["auto_retry"] = False
        disabled = task("https://example.com/b.bin", "b.bin",
                        os.path.join(WORK, "b.bin"), status="downloading")
        downloads._fail_task(disabled, "Connection reset")
        check("auto-retry can be switched off", disabled["status"] == "error")
    finally:
        downloads._load_config = original_cfg
        downloads._schedule_retry_wakeup = original_wake
        downloads._notify_status_change = original_notify
        downloads._save_tasks = original_save

    print("[duplicate helpers]")
    downloads._tasks[:] = [
        task("https://example.com/f.bin", "f.bin", os.path.join(WORK, "f.bin")),
        task("https://example.com/f.bin", "f.bin", os.path.join(WORK, "other", "f.bin")),
        task("https://example.com/g.bin", "g.bin", os.path.join(WORK, "g.bin"), status="completed"),
    ]
    check("same URL + folder is a duplicate",
          (downloads._find_duplicate("https://example.com/f.bin", WORK) or {}).get("status")
          == "waiting")
    check("another folder is not a duplicate",
          downloads._find_duplicate("https://example.com/f.bin", os.path.join(WORK, "nope")) is None)
    check("finished tasks are not duplicates",
          downloads._find_duplicate("https://example.com/g.bin", WORK) is None)

    print("[add_task duplicate policy]")
    original_pump = downloads._pump
    original_private = downloads._reject_private_url
    cfg = {
        "download_dir": WORK, "categories": [], "max_concurrent": 3,
        "auto_download_debrid": True, "duplicate_policy": "rename", "debrid": {},
        "auto_retry": True, "retry_max": 3, "retry_delay": 60,
    }

    async def _noop() -> None:
        return None

    downloads._load_config = lambda: cfg
    downloads._pump = _noop
    downloads._reject_private_url = lambda url: None
    downloads._notify_status_change = lambda *a, **k: None
    try:
        downloads._tasks[:] = []
        payload = downloads.AddTaskRequest(url="https://example.com/same.bin")
        first = asyncio.run(downloads.add_task(payload))
        second = asyncio.run(downloads.add_task(payload))
        check("re-adding the same URL returns the existing task",
              second.get("duplicate") is True and second["id"] == first["id"], str(second))
        check("no duplicate record was created", len(downloads._tasks) == 1)

        existing = Path(WORK) / "same.bin"
        existing.write_bytes(b"old")
        cfg["duplicate_policy"] = "skip"
        downloads._tasks[:] = []
        try:
            asyncio.run(downloads.add_task(payload))
            check("skip policy refuses an existing file", False, "no exception")
        except HTTPException as exc:
            check("skip policy refuses an existing file", exc.status_code == 409)

        cfg["duplicate_policy"] = "overwrite"
        downloads._tasks[:] = []
        overwritten = asyncio.run(downloads.add_task(payload))
        check("overwrite keeps the existing name", overwritten["path"] == str(existing),
              overwritten["path"])
    finally:
        downloads._pump = original_pump
        downloads._reject_private_url = original_private
        downloads._load_config = original_cfg
        downloads._notify_status_change = original_notify
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
