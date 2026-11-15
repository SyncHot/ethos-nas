"""Per-user recycle bin — service-level regression test (no server).

Points settings.DATA_DIR at a temp directory and exercises
services/trash_service.py: ownership isolation, collision-free names, the
legacy flat-trash migration, path/name validation and retention.

Run:  .venv/bin/python tests/test_trash_service.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402
from backend.services import trash_service as ts  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _make_file(parent: Path, name: str, text: str = "x") -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / name
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ethos-trash-service-"))
    original_data_dir = settings.DATA_DIR
    try:
        settings.DATA_DIR = tmp

        # ── Legacy flat layout: one item owned by alice, one unknown ────────
        legacy = tmp / "trash"
        _make_file(legacy, "report.txt", "legacy")
        (legacy / "report.txt.meta.json").write_text(json.dumps({
            "original_path": "/home/alice/docs/report.txt",
            "trashed_name": "report.txt",
        }), encoding="utf-8")
        _make_file(legacy, "mystery.bin", "?")

        result = ts.migrate_legacy()
        check("migration moved both legacy items", result["moved"] == 2, str(result))
        check("alice's legacy item adopted by /home path",
              (tmp / "trash" / "alice" / "report.txt").is_file())
        check("unknown item parked in _legacy",
              (tmp / "trash" / "_legacy" / "mystery.bin").is_file())
        meta = json.loads((tmp / "trash" / "alice" / "report.txt.meta.json").read_text())
        check("migrated metadata records owner", meta.get("owner") == "alice", str(meta))
        check("migrated metadata records deleted_at", bool(meta.get("deleted_at")), str(meta))
        check("migration is idempotent", ts.migrate_legacy()["moved"] == 0)
        check("namespaces are marked", (tmp / "trash" / "alice" / ".namespace").is_file())

        check("no _legacy marker leaks into its item list",
              (tmp / "trash" / "_legacy" / ".namespace").is_file())
        check("_legacy items are addressable for the admin view",
              ts.resolve_item("_legacy", "mystery.bin")[0].is_file())

        # ── Same filename for two accounts: no global "_1" suffix ───────────
        file_a = _make_file(tmp / "src" / "a", "dup.txt", "alice")
        file_b = _make_file(tmp / "src" / "b", "dup.txt", "bob")
        moved_a = ts.move_to_trash(str(file_a), "alice")
        moved_b = ts.move_to_trash(str(file_b), "bob")
        check("alice keeps the original name", moved_a["name"] == "dup.txt", str(moved_a))
        check("bob keeps the original name", moved_b["name"] == "dup.txt", str(moved_b))
        check("alice namespace has 2 items", ts.list_items("alice")["total"] == 2)
        check("bob namespace has 1 item", ts.list_items("bob")["total"] == 1)
        check("bob cannot resolve alice's legacy item",
              _raises(FileNotFoundError, ts.resolve_item, "bob", "report.txt"))
        all_items = ts.list_items("bob", include_all=True)
        owners = {item["owner"] for item in all_items["items"]}
        check("admin view spans owners", {"alice", "bob", "_legacy"} <= owners, str(owners))

        # ── Validation ─────────────────────────────────────────────────────
        check("owner traversal rejected",
              _raises(ValueError, ts.normalize_owner, "../alice"))
        check("legacy owner is reserved",
              _raises(ValueError, ts.normalize_owner, "_legacy"))
        check("item traversal rejected",
              _raises(ValueError, ts.valid_item_name, "../x"))
        check("meta sidecar name rejected",
              _raises(ValueError, ts.valid_item_name, "x.meta.json"))

        # ── Retention: per item, never per user directory ──────────────────
        old_item = tmp / "trash" / "alice" / "dup.txt"
        old_stamp = time.time() - 40 * 86400
        os.utime(old_item, (old_stamp, old_stamp))
        swept = ts.sweep_retention(30 * 86400)
        check("old item removed by retention", not old_item.exists(), str(swept))
        check("old item's sidecar removed", not (tmp / "trash" / "alice" / "dup.txt.meta.json").exists())
        check("alice namespace survives", (tmp / "trash" / "alice" / ".namespace").is_file())
        check("bob's fresh item kept", (tmp / "trash" / "bob" / "dup.txt").is_file())
        check("usage counts trashed items only", ts.usage()["files"] == 3, str(ts.usage()))

        # ── Scoped delete / empty ──────────────────────────────────────────
        check("alice empty removes only her items", ts.empty("alice") == (1, 0))
        check("alice is empty", ts.list_items("alice")["total"] == 0)
        check("bob still owns an item", ts.list_items("bob")["total"] == 1)
        check("delete_item is owner-scoped",
              _raises(FileNotFoundError, ts.delete_item, "alice", "dup.txt"))
        check("bob can delete his item", ts.delete_item("bob", "dup.txt") == "dup.txt")
        check("all-users empty clears _legacy too",
              ts.empty(None, include_all=True) == (1, 0), str(ts.usage()))
        check("recycle bin is fully empty", ts.usage()["files"] == 0, str(ts.usage()))
    finally:
        settings.DATA_DIR = original_data_dir
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    return 0


def _raises(exc, fn, *args) -> bool:
    try:
        fn(*args)
    except exc:
        return True
    except Exception:
        return False
    return False


if __name__ == "__main__":
    sys.exit(main())
