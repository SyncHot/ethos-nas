#!/usr/bin/env python3
"""Unit tests — core-owned package state (UI-only packages such as Cameras).

An uninstall used to leave `ethos-surveillance` configuration behind: the
engine kept restarting ffmpeg for a package that no longer existed. These tests
pin the inventory/purge contract (see `backend/services/package_state.py`).

No server needed; everything runs against a temp DATA_DIR.

Run:
    .venv/bin/python tests/test_package_core_state.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class _FakePipeline:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def join(self, timeout: float | None = None) -> None:
        pass


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="ethos-pkg-state-"))
    os.environ["ETHOS_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("ETHOS_ALLOW_INSECURE", "1")

    from backend.services import (camera_recordings, camera_service, camera_settings,
                                  camera_runner, package_state, packages_store)
    from backend.routers import packages as packages_router

    def seed_camera_state() -> None:
        camera_service.CAMERAS_PATH.write_text('{"version": 2, "cameras": []}', encoding="utf-8")
        camera_settings.STORE_PATH.write_text('{"version": 1, "quota_pct": 80}', encoding="utf-8")
        camera_service.EVENTS_PATH.write_text('{"version": 2, "events": []}', encoding="utf-8")
        camera_recordings.EXPORTS_PATH.write_text('{"version": 1, "exports": []}', encoding="utf-8")
        for path in (camera_service.SNAPS_DIR, camera_service.EVENTS_IMG_DIR,
                     camera_service.CLIPS_DIR, camera_recordings.EXPORTS_DIR):
            path.mkdir(parents=True, exist_ok=True)
            (path / "sample.bin").write_bytes(b"x" * 64)

    recordings = data_dir / "surveillance" / "recordings" / "cam_0123456789ab" / "2026-11-22" / "10-00-00"
    recordings.mkdir(parents=True, exist_ok=True)
    (recordings / "seg00000.m4s").write_bytes(b"r" * 128)
    seed_camera_state()

    print("\nPart A — describe")
    state = package_state.describe("ethos-surveillance")
    check("known package is described", bool(state), str(state))
    check("configuration counts as existing state", bool(state and state["exists"]))
    check("size covers the seeded files", bool(state and state["size_bytes"] >= 64 * 5),
          str(state and state["size_bytes"]))
    check("label explains what goes", bool(state and "camera" in state["label"].lower()),
          str(state and state["label"]))
    check("unknown package has no core state", package_state.describe("ethos-nope") is None)
    check("id is in the allowlist", "ethos-surveillance" in package_state.known_packages())

    print("\nPart B — purge stops the engine and keeps recordings")
    fake = _FakePipeline()
    camera_runner._PIPELINES["cam_0123456789ab"] = fake
    result = package_state.purge("ethos-surveillance")
    check("purge reports what it deleted", bool(result and result["size_bytes"] > 0), str(result))
    check("purge collects no errors", bool(result and not result["errors"]), str(result))
    check("pipelines are stopped first", fake.stopped)
    check("cameras store deleted", not camera_service.CAMERAS_PATH.exists())
    check("settings store deleted", not camera_settings.STORE_PATH.exists())
    check("motion events deleted", not camera_service.EVENTS_PATH.exists())
    check("snapshots deleted", not camera_service.SNAPS_DIR.exists())
    check("export index deleted", not camera_recordings.EXPORTS_PATH.exists())
    check("recordings are kept", (recordings / "seg00000.m4s").exists())
    check("nothing left to describe", package_state.describe("ethos-surveillance")["exists"] is False)

    print("\nPart C — uninstall preview merges package data and core state")
    seed_camera_state()
    pkg_data = packages_store.data_dir("ethos-surveillance")
    pkg_data.mkdir(parents=True, exist_ok=True)
    (pkg_data / "state.json").write_bytes(b"p" * 32)
    inventory = packages_router._data_inventory("ethos-surveillance")
    check("package data is reported", inventory.get("has_package_data") is True, str(inventory))
    check("core state is reported", bool(inventory.get("core")), str(inventory))
    check("sizes are added up",
          inventory["size_bytes"] == packages_store.dir_size(pkg_data) + inventory["core"]["size_bytes"],
          str(inventory))
    check("both roots are listed",
          str(pkg_data) in inventory["paths"]
          and str(camera_service.CAMERAS_PATH) in inventory["paths"],
          str(inventory["paths"]))
    check("label reaches the dialog", "camera" in str(inventory.get("label", "")).lower())

    print("\nPart D — orphan data reclaims the core state")
    orphans = asyncio.run(packages_router.list_orphan_data())["orphans"]
    entries = [item for item in orphans if item["id"] == "ethos-surveillance"]
    check("core state is listed as leftover", len(entries) == 1, str(orphans))
    entry = entries[0] if entries else None
    check("orphan entry says it is core state", bool(entry and entry.get("core")), str(entry))
    check("orphan total covers both roots",
          bool(entry and entry["size_bytes"] >= packages_store.dir_size(pkg_data)), str(entry))
    asyncio.run(packages_router.delete_orphan_data("ethos-surveillance"))
    check("cameras store is gone after the purge", not camera_service.CAMERAS_PATH.exists())
    check("package data is gone after the purge", not pkg_data.exists())
    check("recordings survive the orphan purge", (recordings / "seg00000.m4s").exists())

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
    shutil.rmtree(data_dir, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
