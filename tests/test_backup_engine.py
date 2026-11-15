#!/usr/bin/env python3
"""Backup Manager v2 — engine, layout migration, retention and restore tests.

Standalone script (one file per process). No server, no login: the engine and
stores are exercised directly with isolated temp folders.

Run: .venv/bin/python tests/test_backup_engine.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

_DATA = tempfile.mkdtemp(prefix="ethos-backup-engine-")
os.environ.update({
    "ETHOS_DATA_DIR": _DATA,
    "ETHOS_JWT_SECRET": "backup-engine-test",
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.services import backup_engine, backup_layout, backup_store, cron_utils  # noqa: E402
from backend.services.backup_proc import OperationHandle  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def make_schedule(source, destination, **overrides):
    schedule = {
        "id": "test0001",
        "name": "Engine test",
        "type": "folder",
        "enabled": True,
        "cron": "0 2 * * *",
        "source": source,
        "destination": destination,
        "options": {},
        "retention": {"keep_last": 2, "keep_daily": 0, "keep_weekly": 0, "keep_monthly": 0},
        "verify_after_run": "quick",
        "skip_if_missing": True,
        "remote": {"enabled": False},
    }
    schedule.update(overrides)
    return backup_store.normalize_schedule(schedule)


async def run_backup(schedule):
    op = OperationHandle(f"op{int(time.time() * 1000) % 100000}", "backup", schedule, "manual")
    return await backup_engine.run_backup(schedule, op)


def main() -> int:
    if not shutil.which("rsync"):
        print("rsync is required for these tests")
        return 1

    print("\n[1] cron helpers")
    for expr in ("0 2 * * *", "@daily", "*/15 * * * *", "0 3 1 * *", "0 3 * * 0", "0 0 1 1 *"):
        check(f"cron valid: {expr}", cron_utils.is_valid(expr), expr)
    for expr in ("", "bad", "0 2 * *", "61 2 * * *"):
        check(f"cron invalid: {expr!r}", not cron_utils.is_valid(expr), expr)
    nxt = cron_utils.next_run("0 2 * * *")
    check("next_run is ISO", bool(nxt) and "T" in nxt, str(nxt))

    print("\n[2] v1 store migration")
    # The real 2026-09 store has no `version` key at all; migrate and persist it.
    v1 = {"schedules": [{
        "id": "legacy01", "name": "Legacy", "type": "folder",
        "source": _DATA, "destination": _DATA, "cron": "0 2 * * *",
        "enabled": True, "options": {}, "created_at": "2026-01-01T00:00:00Z",
    }]}
    with open(os.path.join(_DATA, "backup_schedules.json"), "w", encoding="utf-8") as handle:
        json.dump(v1, handle)
    backup_store._v2_persisted = False  # noqa: SLF001 — exercise the no-version path
    migrated = backup_store.load_schedules()
    check("schedule migrated", len(migrated) == 1 and migrated[0]["id"] == "legacy01")
    check("retention defaults added", migrated[0]["retention"]["keep_daily"] == 7)
    check("verify default added", migrated[0]["verify_after_run"] == "quick")
    with open(os.path.join(_DATA, "backup_schedules.json"), encoding="utf-8") as handle:
        persisted = json.load(handle)
    check("v2 shape persisted on disk", persisted.get("version") == 2,
          str(persisted.get("version")))
    check("defaults persisted", persisted["schedules"][0]["retention"]["keep_daily"] == 7)

    def _clear(data):
        data["schedules"] = []

    backup_store._SCHEDULES_STORE.update(_clear)  # noqa: SLF001

    print("\n[3] legacy layout migration")
    src = tempfile.mkdtemp(prefix="bm-src-")
    dst = tempfile.mkdtemp(prefix="bm-dst-")
    os.makedirs(os.path.join(src, "sub"))
    with open(os.path.join(src, "sub", "one.txt"), "w", encoding="utf-8") as handle:
        handle.write("hello\n")
    target_name = os.path.basename(src)
    os.makedirs(os.path.join(dst, target_name, "sub"))
    with open(os.path.join(dst, target_name, "sub", "old.txt"), "w", encoding="utf-8") as handle:
        handle.write("old\n")
    schedule = make_schedule(src, dst)
    layout = backup_layout.ensure_layout(schedule)
    check("layout migrated", layout["ok"] and layout["migrated"], str(layout))
    legacy = backup_layout.list_generations(schedule)
    check("legacy generation listed", len(legacy) == 1 and legacy[0]["status"] == "legacy")
    legacy_data = os.path.join(dst, target_name, "snapshots", legacy[0]["generation"], "data", "sub", "old.txt")
    check("legacy payload moved into snapshots/", os.path.isfile(legacy_data))
    check("legacy size indexed", legacy[0]["size_bytes"] == 4, str(legacy[0].get("size_bytes")))
    check("legacy file count indexed", legacy[0]["files_total"] == 1, str(legacy[0].get("files_total")))
    legacy_dir = os.path.join(dst, target_name, "snapshots", legacy[0]["generation"])
    legacy_manifest = backup_layout.read_manifest(legacy_dir)
    legacy_manifest.update({"size_bytes": None, "files_total": 0})
    backup_layout.write_manifest(legacy_dir, legacy_manifest)
    backup_layout.fill_missing_sizes(schedule)
    repaired = backup_layout.list_generations(schedule)[0]
    check("missing size is backfilled", repaired["size_bytes"] == 4 and repaired["files_total"] == 1,
          f"{repaired.get('size_bytes')}/{repaired.get('files_total')}")
    again = backup_layout.ensure_layout(schedule)
    check("migration is idempotent", again["migrated"] is False)

    print("\n[4] first backup + verify")
    result = __import__("asyncio").run(run_backup(schedule))
    check("backup succeeded", result["status"] == "success", result.get("error", ""))
    check("generation recorded", bool(result.get("generation")))
    generations = backup_layout.list_generations(schedule)
    complete = [g for g in generations if g["status"] == "complete"]
    check("complete generation exists", len(complete) >= 1)
    check("quick verify ran", complete[0]["verified"].get("status") == "ok",
          str(complete[0].get("verified")))
    first_data = os.path.join(dst, target_name, "snapshots", complete[0]["generation"], "data", "sub", "one.txt")
    check("file copied", os.path.isfile(first_data))
    check("manifest exists", os.path.isfile(os.path.join(
        dst, target_name, "snapshots", complete[0]["generation"], "manifest.json")))

    print("\n[5] incremental hardlink snapshot")
    time.sleep(1.1)
    before_inode = os.stat(first_data).st_ino
    with open(os.path.join(src, "sub", "two.txt"), "w", encoding="utf-8") as handle:
        handle.write("second\n")
    result = __import__("asyncio").run(run_backup(schedule))
    check("second backup succeeded", result["status"] == "success", result.get("error", ""))
    generations = [g for g in backup_layout.list_generations(schedule) if g["status"] == "complete"]
    newest = generations[0]["generation"]
    newest_one = os.path.join(dst, target_name, "snapshots", newest, "data", "sub", "one.txt")
    check("unchanged file is a hardlink", os.stat(newest_one).st_ino == before_inode)

    print("\n[6] deletion is reflected in the new snapshot only")
    time.sleep(1.1)
    os.unlink(os.path.join(src, "sub", "one.txt"))
    result = __import__("asyncio").run(run_backup(schedule))
    check("third backup succeeded", result["status"] == "success", result.get("error", ""))
    generations = [g for g in backup_layout.list_generations(schedule) if g["status"] == "complete"]
    newest = generations[0]["generation"]
    older = [g for g in generations if g["generation"] != newest]
    check("deleted file gone from newest", not os.path.exists(
        os.path.join(dst, target_name, "snapshots", newest, "data", "sub", "one.txt")))
    check("deleted file kept in older generation", any(os.path.exists(
        os.path.join(dst, target_name, "snapshots", g["generation"], "data", "sub", "one.txt")) for g in older))

    print("\n[7] retention prunes to policy")
    complete = [g for g in backup_layout.list_generations(schedule) if g["status"] == "complete"]
    check("retention kept 2 generations", len(complete) == 2, f"got {len(complete)}")
    check("newest generation kept", complete[0]["generation"] == newest)

    print("\n[8] restore to another folder")
    target = tempfile.mkdtemp(prefix="bm-restore-")
    op = OperationHandle("restore1", "restore", schedule, "manual")
    restored = __import__("asyncio").run(backup_engine.run_restore(
        schedule, newest, mode="to_folder", target=target, delete_extra=False, op=op))
    check("restore succeeded", restored["status"] == "success", restored.get("error", ""))
    check("restored latest content", os.path.isfile(os.path.join(target, "sub", "two.txt")))

    print("\n[9] in-place restore creates a pre-restore generation")
    with open(os.path.join(src, "sub", "two.txt"), "w", encoding="utf-8") as handle:
        handle.write("changed after backup\n")
    op = OperationHandle("restore2", "restore", schedule, "manual")
    restored = __import__("asyncio").run(backup_engine.run_restore(
        schedule, newest, mode="in_place", target=None, delete_extra=False, op=op))
    check("in-place restore succeeded", restored["status"] == "success", restored.get("error", ""))
    check("pre-restore generation recorded", bool(restored.get("pre_restore_generation")))
    with open(os.path.join(src, "sub", "two.txt"), "r", encoding="utf-8") as handle:
        content = handle.read()
    check("content restored", content == "second\n", content)

    print("\n[10] missing destination is a clean skip")
    missing = make_schedule(src, os.path.join(dst, "unplugged"), skip_if_missing=True)
    result = __import__("asyncio").run(run_backup(missing))
    check("status is skipped", result["status"] == "skipped", result.get("status"))
    check("reason names the destination", "not available" in result.get("error", ""))

    print("\n[11] retention selection units")
    now = "2026-11-14T02:00:00Z"
    gens = [
        {"generation": f"2026-11-{day:02d}T02-00-00", "created_at": f"2026-11-{day:02d}T02:00:00Z",
         "status": "complete"}
        for day in range(1, 15)
    ]
    keep = backup_engine.select_generations(gens, {"keep_last": 2, "keep_daily": 3,
                                                   "keep_weekly": 1, "keep_monthly": 1})
    check("keeps newest daily bucket", "2026-11-14T02-00-00" in keep)
    check("keeps three daily buckets", len([g for g in keep if g.startswith("2026-11-")]) >= 3)
    check("selection is bounded", len(keep) <= 6, str(sorted(keep)))

    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.rmtree(target, ignore_errors=True)

    print(f"\n{len(FAILURES)} failure(s)")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
