#!/usr/bin/env python3
"""Retention policy tests — services/retention_service.py (TODO.md P0-11).

Service-level only (no server): points settings.DATA_DIR at a temp directory.

Run:
    .venv/bin/python tests/test_retention.py
"""

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
from backend.services import retention_service as rs  # noqa: E402
from backend.services import trash_service as ts  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _touch(path: Path, age_seconds: float = 0.0, size: int = 1024) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ethos-retention-test-"))
    original_data_dir = settings.DATA_DIR
    original = {
        "TEMP_CHUNK_TTL_HOURS": settings.TEMP_CHUNK_TTL_HOURS,
        "TRASH_RETENTION_DAYS": settings.TRASH_RETENTION_DAYS,
        "THUMB_CACHE_MAX_FILES": settings.THUMB_CACHE_MAX_FILES,
        "VIDEO_THUMB_CACHE_MAX_FILES": settings.VIDEO_THUMB_CACHE_MAX_FILES,
    }
    try:
        settings.DATA_DIR = tmp
        settings.TEMP_CHUNK_TTL_HOURS = 24
        settings.TRASH_RETENTION_DAYS = 30
        settings.THUMB_CACHE_MAX_FILES = 3
        settings.VIDEO_THUMB_CACHE_MAX_FILES = 3

        # temp_chunks: one 2-day-old part, one fresh part
        _touch(tmp / "temp_chunks" / "old.part", age_seconds=2 * 86400)
        _touch(tmp / "temp_chunks" / "new.part")

        # transcodes: same TTL policy
        _touch(tmp / "transcodes" / "old.mp4", age_seconds=3 * 86400)

        # trash: an old directory and a fresh file (legacy flat layout)
        _touch(tmp / "trash" / "olddir" / "f.bin", age_seconds=40 * 86400)
        old_dir_stamp = time.time() - 40 * 86400
        os.utime(tmp / "trash" / "olddir", (old_dir_stamp, old_dir_stamp))
        _touch(tmp / "trash" / "fresh.bin")

        # per-user namespace: the age policy must apply per item, not to the
        # user's top-level trash directory
        ts.namespace_dir("alice", create=True)
        _touch(tmp / "trash" / "alice" / "old-user.bin", age_seconds=40 * 86400)
        _touch(tmp / "trash" / "alice" / "fresh-user.bin")

        # Old flat entries are adopted at startup in production; run the same
        # idempotent migration here so the assertions below see the final layout.
        check("legacy flat items migrated", ts.migrate_legacy()["moved"] == 2)

        # thumbnails: 5 files, staggered mtimes (newest = t0)
        for i in range(5):
            _touch(tmp / "thumbnails" / f"t{i}.webp", age_seconds=i * 60)

        # video_thumbs: 4 files
        for i in range(4):
            _touch(tmp / "video_thumbs" / f"v{i}.webp", age_seconds=i * 60)

        # video_exports: owned by RenderManager, retention only measures it
        _touch(tmp / "video_exports" / "files" / "old.mp4", age_seconds=40 * 86400)

        summary = rs.sweep()

        check("old chunk upload removed", not (tmp / "temp_chunks" / "old.part").exists())
        check("fresh chunk upload kept", (tmp / "temp_chunks" / "new.part").exists())
        check("old transcode removed", not (tmp / "transcodes" / "old.mp4").exists())
        check("old trash directory removed", not (tmp / "trash" / "_legacy" / "olddir").exists())
        check("fresh trash entry kept", (tmp / "trash" / "_legacy" / "fresh.bin").exists())
        check("old per-user trash item removed",
              not (tmp / "trash" / "alice" / "old-user.bin").exists())
        check("fresh per-user trash item kept",
              (tmp / "trash" / "alice" / "fresh-user.bin").exists())
        check("user trash namespace survives the sweep",
              (tmp / "trash" / "alice" / ".namespace").is_file())
        thumbs = sorted(p.name for p in (tmp / "thumbnails").glob("*.webp"))
        check("thumbnail cache capped at 3", len(thumbs) == 3, str(thumbs))
        check("thumbnail cache keeps the newest", thumbs == ["t0.webp", "t1.webp", "t2.webp"], str(thumbs))
        vthumbs = sorted(p.name for p in (tmp / "video_thumbs").glob("*.webp"))
        check("video thumb cache capped at 3", len(vthumbs) == 3, str(vthumbs))
        check("video exports are left to RenderManager",
              (tmp / "video_exports" / "files" / "old.mp4").exists())
        check("summary counts removals", summary["removed_files"] >= 5, str(summary))
        check("summary reports freed bytes", summary["freed_bytes"] > 0, str(summary))

        usage = rs.usage()
        check("usage lists every area", set(usage["areas"]) == {
            "temp_chunks", "transcodes", "trash", "thumbnails", "video_thumbs",
            "video_exports"})
        check("usage counts video exports", usage["areas"]["video_exports"]["files"] == 1,
              str(usage["areas"]["video_exports"]))
        check("usage counts files", usage["areas"]["thumbnails"]["files"] == 3,
              str(usage["areas"]["thumbnails"]))
        check("usage totals are positive", usage["total_bytes"] > 0 and usage["total_files"] > 0)

        # Second sweep with nothing to do must be a no-op and must not raise.
        again = rs.sweep()
        check("idle sweep removes nothing", again["removed_files"] == 0, str(again))

        # Missing directories are fine.
        shutil.rmtree(tmp / "trash")
        check("missing directories are tolerated", isinstance(rs.sweep(), dict))
    finally:
        settings.DATA_DIR = original_data_dir
        for key, value in original.items():
            setattr(settings, key, value)
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  FAILED: {name} {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
