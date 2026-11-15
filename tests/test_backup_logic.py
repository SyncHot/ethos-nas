#!/usr/bin/env python3
"""Backup Manager v2 — schedule validation and normalization units.

Run: .venv/bin/python tests/test_backup_logic.py
"""
import os
import sys
import tempfile

_DATA = tempfile.mkdtemp(prefix="ethos-backup-logic-")
os.environ.update({
    "ETHOS_DATA_DIR": _DATA,
    "ETHOS_JWT_SECRET": "backup-logic-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.services import backup_engine, backup_store  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def main() -> int:
    source = tempfile.mkdtemp(prefix="bm-valid-src-")
    destination = tempfile.mkdtemp(prefix="bm-valid-dst-")

    def schedule(**overrides):
        base = {
            "name": "Test", "type": "folder", "source": source,
            "destination": destination, "cron": "0 2 * * *",
        }
        base.update(overrides)
        return backup_store.normalize_schedule(base)

    print("\n[1] path validation")
    check("valid schedule accepted", backup_store.validate_schedule(schedule()) is None)
    check("same source/destination rejected",
          bool(backup_store.validate_schedule(schedule(destination=source))))
    check("destination inside source rejected", bool(backup_store.validate_schedule(
        schedule(destination=os.path.join(source, "nested")))))
    check("missing source rejected", bool(backup_store.validate_schedule(
        schedule(source=os.path.join(source, "missing")))))
    check("missing destination parent rejected", bool(backup_store.validate_schedule(
        schedule(destination=os.path.join(destination, "nope", "deeper")))))
    check("root source rejected", bool(backup_store.validate_schedule(schedule(source="/"))))

    print("\n[2] cron / retention validation")
    check("bad cron rejected", bool(backup_store.validate_schedule(schedule(cron="bad"))))
    check("cron macro accepted", backup_store.validate_schedule(schedule(cron="@daily")) is None)
    check("zero retention rejected", bool(backup_store.validate_schedule(
        schedule(retention={"keep_last": 0, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0}))))
    retention = backup_store.norm_retention({"keep_daily": 999, "keep_weekly": -3})
    check("retention clamped", retention["keep_daily"] == 365 and retention["keep_weekly"] == 0,
          str(retention))

    print("\n[3] option normalization")
    options = backup_store.norm_options({"exclude": "a, b ,,", "include_images": "yes"}, "docker-compose")
    check("exclude split", options["exclude"] == ["a", "b"], str(options["exclude"]))
    check("docker flags kept", options["include_images"] is True)
    check("folder drops docker options",
          "include_volumes" not in backup_store.norm_options({"include_volumes": False}, "folder"))
    check("projects auto is None",
          backup_store.norm_options({"projects": "auto"}, "docker-compose")["projects"] is None)
    bad_project = schedule(type="docker-compose",
                           options={"projects": ["ok-name", "bad name!"]})
    check("invalid project name rejected", bool(backup_store.validate_schedule(bad_project)))

    print("\n[4] remote validation")
    remote = schedule(remote={"enabled": True, "remote": "", "path": "x"})
    check("remote without name rejected", bool(backup_store.validate_schedule(remote)))
    remote = schedule(remote={"enabled": True, "remote": "gdrive", "path": "backups", "bwlimit": "10M"})
    check("valid remote accepted", backup_store.validate_schedule(remote) is None)
    remote = schedule(remote={"enabled": True, "remote": "gdrive", "path": "../escape"})
    check("remote path traversal rejected", bool(backup_store.validate_schedule(remote)))

    print("\n[5] public shape")
    public = backup_store.public_schedule(schedule(id="abc123"))
    check("id preserved", public["id"] == "abc123")
    check("next_run present", public.get("next_run") is not None)
    check("target path derived", public["target_path"].endswith(os.path.basename(source)))

    print("\n[6] generation selection protects the legacy copy")
    legacy = [{"generation": "legacy", "created_at": "2026-01-01T00:00:00Z", "status": "legacy"}]
    newest = [{"generation": "2026-11-14T02-00-00", "created_at": "2026-11-14T02:00:00Z",
               "status": "complete"}]
    keep = backup_engine.select_generations(legacy + newest,
                                            {"keep_last": 1, "keep_daily": 0,
                                             "keep_weekly": 0, "keep_monthly": 0})
    check("legacy kept while only one native generation exists", "legacy" in keep, str(keep))

    print(f"\n{len(FAILURES)} failure(s)")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
