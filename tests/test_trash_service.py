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

        # ── Volume-local bin: a cross-filesystem delete is a rename ────────
        # `tmp` and DATA_DIR share a device here, so the real resolver must say
        # "no volume bin" (the DATA_DIR one is already local).
        real_volume_trash_dir = ts._volume_trash_dir
        check("same-device deletes keep using DATA_DIR trash",
              real_volume_trash_dir(str(tmp / "trash")) is None)

        volume_root = tmp / "volume" / ts.VOLUME_TRASH_NAME
        # Same device → real resolver puts this one in DATA_DIR/trash.
        same_vol = _make_file(tmp / "src" / "v", "film.mkv", "same")
        ts.move_to_trash(str(same_vol), "alice")

        # Force the volume bin to exercise the cross-device path.
        ts._volume_trash_dir = lambda src: volume_root
        try:
            movie = _make_file(tmp / "volume" / "movies", "film.mkv", "v" * 4096)
            moved = ts.move_to_trash(str(movie), "alice")
            check("volume delete removes the source", not movie.exists())
            check("volume delete lands in <mount>/.@Recycle/<user>",
                  (volume_root / "alice" / "film.mkv_1").is_file(), moved["name"])
            check("volume namespace is marked",
                  (volume_root / "alice" / ".namespace").is_file())
            check("volume bin is registered",
                  str(volume_root) in [str(v) for v in ts.registered_volumes()])
            check("usage reports the volume bin",
                  str(volume_root) in ts.usage().get("volumes", []), str(ts.usage()))
            check("names stay unique across namespaces",
                  moved["name"] == "film.mkv_1", str(moved))
            check("list spans DATA_DIR + volume bin", ts.list_items("alice")["total"] == 2,
                  str(ts.list_items("alice")))
            check("volume item resolves by name",
                  ts.resolve_item("alice", "film.mkv_1")[0] == volume_root / "alice" / "film.mkv_1")

            # Retention must see volume items too.
            old = _make_file(volume_root / "alice", "old.mkv", "old")
            old_stamp = time.time() - 40 * 86400
            os.utime(old, (old_stamp, old_stamp))
            swept = ts.sweep_retention(30 * 86400)
            check("retention sweeps volume items", not old.exists(), str(swept))
            check("retention keeps fresh volume items",
                  (volume_root / "alice" / "film.mkv_1").is_file())

            # An unwritable volume falls back to the DATA_DIR bin (never loses data).
            real_mark = ts._mark_namespace

            def _flaky(directory, owner, kind="user"):
                if str(directory).startswith(str(volume_root)):
                    raise OSError("read-only volume")
                return real_mark(directory, owner, kind)

            ts._mark_namespace = _flaky
            try:
                fallback_src = _make_file(tmp / "volume" / "movies", "fallback.mkv", "fb")
                fallback = ts.move_to_trash(str(fallback_src), "bob")
                check("unusable volume bin falls back to DATA_DIR",
                      (tmp / "trash" / "bob" / fallback["name"]).is_file(), str(fallback))
            finally:
                ts._mark_namespace = real_mark

            # Emptying a user's bin reaches every namespace it owns.
            check("empty covers DATA_DIR + volume items", ts.empty("alice") == (2, 0),
                  str(ts.usage()))
            check("volume namespace survives empty",
                  (volume_root / "alice" / ".namespace").is_file())
            check("only bob's fallback item remains", ts.usage()["files"] == 1, str(ts.usage()))
        finally:
            ts._volume_trash_dir = real_volume_trash_dir
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
