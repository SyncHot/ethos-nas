#!/usr/bin/env python3
"""OS image service — index parsing, guards and job state (no network/sudo).

    .venv/bin/python tests/test_os_image_service.py
"""

import asyncio
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-os-image-test-"))
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
os.environ.setdefault("ETHOS_JWT_SECRET", "os-image-service-test")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.services import os_image_service  # noqa: E402

_results: list[tuple[str, bool, str]] = []
STATE = DATA_DIR / "os-image"
HELPER = DATA_DIR / "fake-os-image"
HELPER.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
HELPER.chmod(0o755)

IDENTITY = {"version": "0.1.0", "variant": "nas", "arch": "amd64", "layout": "ab1"}


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def _payload(entry: dict) -> bytes:
    return json.dumps({"schema": 2, "kind": "ethos-os", "generated_at": "2026-11-22T00:00:00Z",
                       "latest": {"amd64/nas": entry}, "releases": [entry]}).encode()


def _entry(version: str = "0.2.0", sha: str = "a" * 64) -> dict:
    return {
        "version": version, "build": 40, "git_sha": "abc1234", "layout": "ab1",
        "released_at": "2026-11-22T00:00:00Z", "notes": "test image",
        "min_ethos_version": "2.10.0",
        "rootfs": {"image": "ethos-os-0.2.0-amd64-nas.rootfs.img.xz",
                   "url": "https://example.invalid/rootfs.img.xz",
                   "sha256": sha, "size_bytes": 1234},
    }


def _expect(name: str, coro, status: int, needle: str = "") -> None:
    try:
        asyncio.run(coro)
        check(name, False, "no exception")
    except os_image_service.OsImageError as exc:
        check(name, exc.status == status and (needle in str(exc) if needle else True),
              f"status={exc.status} detail={exc}")


def _write_state(name: str, payload: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / name).write_text(json.dumps(payload), encoding="utf-8")


def main() -> int:
    settings.OS_IMAGE_ENABLED = True
    settings.OS_IMAGE_HELPER = str(HELPER)
    settings.OS_IMAGE_VIA_SUDO = False
    settings.OS_IMAGE_TTL_SECONDS = 3600
    original_identity = os_image_service.os_identity

    print("[index]")
    parsed = os_image_service.parse_index(_payload(_entry()))
    check("parses schema 2", parsed and parsed["version"] == "0.2.0", str(parsed))
    check("keeps the rootfs artifact", parsed["rootfs"]["sha256"] == "a" * 64)
    check("keeps the layout", parsed["layout"] == "ab1")
    check("marks a newer version", os_image_service.update_available(parsed, IDENTITY) is True)
    check("same version is not newer",
          os_image_service.update_available(parsed, {**IDENTITY, "version": "0.2.0"}) is False)
    check("no current version means unknown",
          os_image_service.update_available(parsed, {**IDENTITY, "version": ""}) is None)
    for bad in (b"not json", json.dumps({"schema": 1}).encode(),
                json.dumps({"schema": 2, "kind": "other"}).encode()):
        try:
            os_image_service.parse_index(bad)
            check(f"rejects bad index {bad[:12]!r}", False, "no exception")
        except os_image_service.OsImageError:
            check(f"rejects bad index {bad[:12]!r}", True)
    broken = _entry()
    broken["rootfs"]["sha256"] = "xyz"
    check("drops an invalid rootfs sha",
          not os_image_service.parse_index(_payload(broken))["rootfs"])

    print("\n[status]")
    os_image_service.os_identity = lambda: dict(IDENTITY)
    initial = os_image_service.status()
    check("reports the A/B layout", initial["ab_supported"] is True)
    check("detects the helper", initial["helper"] is True)
    check("no cached image yet", initial["latest"] is None)
    check("no error yet", initial["error"] == "")

    print("\n[download guards]")
    os_image_service.os_identity = lambda: {**IDENTITY, "layout": ""}
    _expect("refuses download without the A/B layout",
            os_image_service.start_download(), 409, "A/B")
    os_image_service.os_identity = lambda: dict(IDENTITY)
    _expect("refuses download before a check", os_image_service.start_download(), 409, "Check")
    os_image_service._save_state({**os_image_service._load_state(), "latest": _entry()})
    os_image_service.os_identity = lambda: {**IDENTITY, "version": "0.2.0"}
    _expect("refuses an up-to-date image", os_image_service.start_download(), 409, "up to date")
    os_image_service.os_identity = lambda: dict(IDENTITY)
    _write_state("job.json", {"status": "downloading", "pid": os.getpid(), "at": "2026-11-22T00:00:00+00:00"})
    _expect("refuses a second download", os_image_service.start_download(), 409, "already running")
    (STATE / "job.json").unlink()
    (STATE / "ethos-os-0.2.0-amd64-nas.rootfs.img.xz").write_bytes(b"x" * 1234)
    ready = asyncio.run(os_image_service.start_download())
    check("a ready download is reported", ready.get("status") == "downloaded", str(ready))

    print("\n[install guards]")
    os_image_service._save_state({**os_image_service._load_state(), "latest": _entry()})
    os_image_service.os_identity = lambda: {**IDENTITY, "layout": ""}
    _expect("refuses install without the A/B layout", os_image_service.install(), 409, "A/B")
    os_image_service.os_identity = lambda: dict(IDENTITY)
    # slots.json lives in DATA_DIR/system, not the image dir.
    (DATA_DIR / "system").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "system" / "slots.json").write_text(
        json.dumps({"active": "a", "slots": {"a": {"status": "candidate"},
                                             "b": {"status": "good"}}}), encoding="utf-8")
    _expect("refuses install while a candidate is pending",
            os_image_service.install(), 409, "candidate")
    (DATA_DIR / "system" / "slots.json").write_text(
        json.dumps({"active": "a", "slots": {"a": {"status": "good"},
                                             "b": {"status": "good"}}}), encoding="utf-8")
    settings.OS_IMAGE_HELPER = str(DATA_DIR / "missing")
    _expect("refuses install without the helper", os_image_service.install(), 409, "setup_scripts")
    settings.OS_IMAGE_HELPER = str(HELPER)

    print("\n[job state]")
    _write_state("job.json", {"status": "running", "step": "write", "percent": 40,
                              "pid": 999999, "at": "2026-11-22T00:00:00+00:00"})
    stale = os_image_service.install_status()
    check("dead helper pid is stale", stale["stale"] is True and stale["running"] is False)
    check("stale install is a failure", (stale["result"] or {}).get("status") == "failed")
    again = os_image_service.install_status()
    check("stale timestamp is stable",
          (again["result"] or {}).get("at") == (stale["result"] or {}).get("at"))
    _write_state("job.json", {"status": "running", "step": "write", "percent": 40,
                              "pid": os.getpid(),
                              "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
    live = os_image_service.install_status()
    check("live helper job is running", live["running"] is True and live["stale"] is False)

    print("\n[downloaded file]")
    latest = _entry()
    info = os_image_service._downloaded_file(latest)
    check("detects the downloaded image", info.get("ready") is True, str(info))
    (STATE / "job.json").unlink(missing_ok=True)
    removed = asyncio.run(os_image_service.delete_download())
    check("removes the downloaded image", removed["removed"] is True)
    check("file is gone", not (STATE / latest["rootfs"]["image"]).exists())

    os_image_service.os_identity = original_identity
    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
