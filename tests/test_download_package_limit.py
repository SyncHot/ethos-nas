#!/usr/bin/env python3
"""Unit test — per-package speed limit (shared token bucket).

Run:
    .venv/bin/python tests/test_download_package_limit.py
"""
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = tempfile.mkdtemp(prefix="ethos-dl-pkglmit-data-")
WORK = tempfile.mkdtemp(prefix="ethos-dl-pkglmit-files-")
os.environ["ETHOS_DATA_DIR"] = DATA
os.environ.setdefault("ETHOS_JWT_SECRET", "download-package-limit-test")
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from backend.routers import downloads  # noqa: E402
from backend.services import download_config, download_transfer  # noqa: E402

FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def part(n: int, status: str = "waiting") -> dict:
    name = f"Course.part0{n}.rar"
    return {
        "id": f"dl_p{n}", "url": f"https://hoster.example/{name}", "name": name,
        "path": str(Path(WORK) / name), "kind": "http", "status": status,
        "size": 10, "downloaded": 0, "speed": 0, "error": "",
        "created_at": 1, "started_at": 0, "finished_at": 0,
    }


def main() -> int:
    print("[normalization]")
    norm = download_config.norm_package_limits
    check("empty / wrong type", norm(None) == {} and norm([]) == {} and norm("x") == {})
    check("non-hex keys dropped", norm({"../../etc": 1000, "zzzz": 5}) == {})
    check("zero and negative values dropped", norm({"abc123abc123": 0, "def456def456": -5}) == {})
    check("valid entries kept", norm({"abc123abc123": 1048576}) == {"abc123abc123": 1048576})
    huge = norm({"abc123abc123": 10 ** 15})
    check("absurd values are clamped",
          huge["abc123abc123"] == download_config.PACKAGE_LIMIT_MAX, str(huge))
    many = norm({f"{i:012x}": 1000 for i in range(80)})
    check("entry count is capped", len(many) == download_config.PACKAGE_LIMIT_ENTRIES, str(len(many)))

    print("[config]")
    cfg = download_config.load_config()
    check("defaults carry an empty map", cfg.get("package_limits") == {}, str(cfg.get("package_limits")))

    print("[_package_limit]")
    first, second = part(1), part(2)
    plain = {"package_limits": {}}
    check("no limit for a task without a package",
          downloads._package_limit({**first, "name": "movie.mkv"}, plain) == ("", 0))
    info = downloads._package_info(first)
    key = info["package_key"]
    check("limit is read per package key",
          downloads._package_limit(first, {"package_limits": {key: 500_000}}) == (key, 500_000))
    check("zero means unlimited", downloads._package_limit(first, {"package_limits": {key: 0}}) == ("", 0))

    print("[endpoint]")
    original = {
        "load_config": downloads._load_config,
        "save_config": downloads._save_config,
        "forget": downloads._forget_shared_limiter,
    }
    live = {"package_limits": {}}
    saved: list[dict] = []
    forgotten: list[str] = []
    downloads._load_config = lambda: dict(live)
    downloads._save_config = lambda cfg: (saved.append(dict(cfg)), live.update(cfg))
    downloads._forget_shared_limiter = lambda k: forgotten.append(k)
    downloads._tasks[:] = [first, second]
    import asyncio

    try:
        out = asyncio.run(downloads.set_package_limit(
            key, downloads.PackageLimitRequest(bps=2_000_000)))
        check("limit is stored", saved and saved[-1]["package_limits"] == {key: 2_000_000}, str(saved[-1:]))
        check("response echoes the cap", out["bps"] == 2_000_000 and out["tasks"] == 2, str(out))
        asyncio.run(downloads.set_package_limit(key, downloads.PackageLimitRequest(bps=0)))
        check("zero clears the entry", saved[-1]["package_limits"] == {}, str(saved[-1]))
        check("zero releases the shared bucket", forgotten == [f"pkg:{key}"], str(forgotten))
        try:
            asyncio.run(downloads.set_package_limit("ffffffffffff", downloads.PackageLimitRequest(bps=1)))
            check("unknown package is a 404", False, "no exception")
        except HTTPException as exc:
            check("unknown package is a 404", exc.status_code == 404)
        try:
            downloads.PackageLimitRequest(bps=-1)
            check("negative limit is rejected", False, "no exception")
        except ValidationError:
            check("negative limit is rejected", True)
    finally:
        downloads._load_config = original["load_config"]
        downloads._save_config = original["save_config"]
        downloads._forget_shared_limiter = original["forget"]
        downloads._tasks[:] = []

    print("[worker]")
    calls: list[dict] = []
    original_stream = downloads._stream_to_file
    original_load = downloads._load_config
    downloads._load_config = lambda: {"download_dir": WORK, "speed_limit": {"per_task_bps": 900_000},
                                      "package_limits": {key: 400_000}}
    downloads._stream_to_file = lambda url, *a, **kw: calls.append(kw) or 10
    try:
        downloads._download_worker(first, threading.Event())
        check("package cap overrides the per-task limit",
              calls and calls[-1].get("limit_bps") == 400_000, str(calls[-1:]))
        check("package parts share one bucket",
              calls and calls[-1].get("limit_key") == f"pkg:{key}", str(calls[-1:]))
        calls.clear()
        downloads._load_config = lambda: {"download_dir": WORK, "speed_limit": {"per_task_bps": 900_000},
                                          "package_limits": {}}
        downloads._download_worker(second, threading.Event())
        check("without a package cap the per-task limit applies",
              calls and calls[-1].get("limit_bps") == 900_000 and calls[-1].get("limit_key") == "",
              str(calls[-1:]))
    finally:
        downloads._stream_to_file = original_stream
        downloads._load_config = original_load

    print("[shared limiter]")
    bucket = download_transfer.shared_limiter("pkg:abc")
    check("same key → same bucket", bucket is download_transfer.shared_limiter("pkg:abc"))
    check("different key → different bucket",
          bucket is not download_transfer.shared_limiter("pkg:def"))
    download_transfer.forget_shared_limiter("pkg:abc")
    check("forget drops the bucket",
          bucket is not download_transfer.shared_limiter("pkg:abc"))

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
