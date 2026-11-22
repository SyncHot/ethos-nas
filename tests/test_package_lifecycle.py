#!/usr/bin/env python3
"""Package update notifications and the install disk preflight (no server).

Both behaviours live in services/ and are cheap to exercise directly: the update
checker reads the catalog through `package_repo.browse` and publishes one event
per package version that is newer than the installed one; the installer refuses
a filesystem below the configured reserve before it downloads anything.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-package-lifecycle-"))
os.environ["ETHOS_DATA_DIR"] = str(TMP / "data")
os.environ["ETHOS_PACKAGES_DEFAULT_SOURCE"] = "0"
sys.path.insert(0, str(ROOT))

from backend.services import package_installer, package_update_service as updates  # noqa: E402
from backend.services.package_schema import PackageError  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class _Usage:
    def __init__(self, free: int):
        self.free = free


def main() -> int:
    # ── Package update checker ──────────────────────────────────────────────
    events = []
    updates.emit_event = lambda kind, title, message, **kw: events.append(
        {"kind": kind, "title": title, "message": message, **kw})
    updates.packages_store.load_sources = lambda: [
        {"url": "https://repo.test/index.json", "enabled": True}]
    updates.packages_store.load_installed = lambda: [
        {"id": "demo", "name": "Demo", "version": "1.0.0"},
        {"id": "kernel", "name": "Kernel", "version": "0.9.0"},
    ]

    async def catalog(force: bool = False) -> dict:
        return {"packages": [
            {"id": "demo", "name": "Demo", "version": "1.1.0", "update_available": True},
            {"id": "kernel", "name": "Kernel", "version": "1.0.0", "update_available": True},
            # Not installed: a catalog entry alone must never notify.
            {"id": "other", "name": "Other", "version": "3.0.0", "update_available": True},
            {"id": "fresh", "name": "Fresh", "version": "2.0.0", "update_available": False},
        ]}

    updates.package_repo.browse = catalog
    found = asyncio.run(updates.check())
    check("one update per installed package",
          [u["id"] for u in found] == ["demo", "kernel"], str(found))
    check("only installed packages notify", len(events) == 2, str(events))
    check("event names the version transition",
          events[0]["message"] == "v1.0.0 → v1.1.0", str(events[0]))
    check("event source is packages", events[0]["kind"] == "info" and events[0]["source"] == "packages")
    check("dedupe key is per package version",
          events[0]["dedupe_key"] == "pkg-update:demo:1.1.0"
          and events[1]["dedupe_key"] == "pkg-update:kernel:1.0.0", str(events))
    check("event opens Package Center",
          events[0]["action"] == {"label": "Open Packages", "appId": "packages"})

    async def failing(force: bool = False) -> dict:
        raise PackageError("repo down", 502)

    updates.package_repo.browse = failing
    events.clear()
    check("repository failure is swallowed",
          asyncio.run(updates.check()) == [] and events == [], str(events))
    updates.packages_store.load_sources = lambda: []
    check("no sources means no check", asyncio.run(updates.check()) == [])

    # ── Install disk preflight ──────────────────────────────────────────────
    import shutil as _shutil

    real_usage = _shutil.disk_usage
    real_min = package_installer.settings.PACKAGES_MIN_FREE_MB
    try:
        package_installer.settings.PACKAGES_MIN_FREE_MB = 1024
        package_installer.shutil.disk_usage = lambda _path: _Usage(512 * 1024 * 1024)
        try:
            package_installer._preflight_disk("demo")
            low_rejected = False
        except PackageError as exc:
            low_rejected = exc.status == 507 and "512 MB" in exc.detail
        check("low disk space rejected with 507", low_rejected)

        package_installer.shutil.disk_usage = lambda _path: _Usage(8 * 1024 * 1024 * 1024)
        package_installer._preflight_disk("demo")
        check("enough space passes", True)

        package_installer.settings.PACKAGES_MIN_FREE_MB = 0
        package_installer.shutil.disk_usage = lambda _path: _Usage(0)
        package_installer._preflight_disk("demo")
        check("a disabled reserve never blocks", True)
    finally:
        package_installer.shutil.disk_usage = real_usage
        package_installer.settings.PACKAGES_MIN_FREE_MB = real_min
        _shutil.rmtree(TMP, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
