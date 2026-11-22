#!/usr/bin/env python3
"""Derived-media cache + transcode service (TODO.md P2-03).

The thumbnail/cover/transcode caches used to live inside
``routers/files.py``. They now share ``services/media_cache.py`` (sidecar
mtime+size freshness) and ``services/transcode.py`` (ffmpeg worker + job
registry). This checks the contract the router relies on — no server, no
ffmpeg:

  * freshness is mtime+size based; a touched or resized source goes stale
  * sidecar meta merges derived lineage (``extra``)
  * the orphan sweep drops stale artifacts and keeps fresh ones
  * transcode output paths encode source + height, and report
    done / running / queued / idle correctly
  * ``start()`` spawns exactly one job and finishes it

Run:
    .venv/bin/python tests/test_media_cache_transcode.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Isolate before importing the app modules (settings read env at import).
_TMP = Path(tempfile.mkdtemp(prefix="ethos-media-cache-test-"))
os.environ["ETHOS_DATA_DIR"] = str(_TMP)

from backend.services import media_cache, transcode  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _age(path: Path, seconds: float) -> None:
    stamp = time.time() + seconds
    os.utime(path, (stamp, stamp))


def main() -> int:
    media_cache.set_cache_root(_TMP)
    check("cache root relocated", media_cache.THUMB_DIR == _TMP / "thumbnails",
          str(media_cache.THUMB_DIR))
    check("transcode root relocated", media_cache.TRANSCODE_DIR == _TMP / "transcodes",
          str(media_cache.TRANSCODE_DIR))
    media_cache.ensure_thumb_dir()

    src = _TMP / "clip.mp4"
    src.write_bytes(b"\x00" * 1024)
    key_a = media_cache.make_thumb_key(str(src), 320)
    check("thumb key is deterministic", key_a == media_cache.make_thumb_key(str(src), 320))
    check("thumb key varies by size", key_a != media_cache.make_thumb_key(str(src), 640))

    cache = media_cache.THUMB_DIR / f"{key_a}.webp"
    cache.write_bytes(b"webp")

    # 1. Freshness requires a sidecar meta (no meta → stale, regenerate once).
    check("missing meta means stale", not media_cache.cache_is_fresh(cache, str(src)))

    media_cache.write_thumb_meta(cache, str(src), {"master_mtime": 42.0})
    check("meta makes it fresh", media_cache.cache_is_fresh(cache, str(src)))
    meta = media_cache.read_thumb_meta(cache)
    check("meta stores src size", meta.get("src_size") == 1024, str(meta))
    check("meta keeps derived lineage", meta.get("master_mtime") == 42.0, str(meta))

    # 2. A resized source invalidates the cache entry.
    src.write_bytes(b"\x00" * 2048)
    _age(src, 5)
    check("resized source is stale", not media_cache.cache_is_fresh(cache, str(src)))

    # 3. Touching the source invalidates as well.
    media_cache.write_thumb_meta(cache, str(src))
    _age(src, 30)
    check("touched source is stale", not media_cache.cache_is_fresh(cache, str(src)))
    media_cache.write_thumb_meta(cache, str(src))
    check("rewritten meta is fresh again", media_cache.cache_is_fresh(cache, str(src)))

    # 4. Orphan sweep: drop meta-less .webp + legacy _vid_v2, keep the rest.
    orphan = media_cache.THUMB_DIR / "deadbeef_320.webp"
    orphan.write_bytes(b"webp")
    legacy = media_cache.THUMB_DIR / "abc_vid_v2_640.jpg"
    legacy.write_bytes(b"jpg")
    other = media_cache.THUMB_DIR / "notes.txt"
    other.write_text("keep me")
    media_cache.cleanup_stale_thumb_artifacts()
    check("orphan artifact removed", not orphan.exists())
    check("legacy pipeline artifact removed", not legacy.exists())
    check("fresh artifact kept", cache.exists())
    check("non-webp file untouched", other.exists())

    # 5. Transcode cache paths + status reporting (no ffmpeg).
    out = transcode.output_file(str(src), 720)
    check("transcode path keeps the stem", out.name.endswith("_clip.mp4"), out.name)
    check("transcode path varies by height",
          transcode.output_file(str(src), 480) != out)
    check("no output means not fresh", not transcode.output_is_fresh(str(src), 720))
    check("idle when nothing is running",
          transcode.status_payload(str(src), 720)["status"] == "idle")

    media_cache.TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"\x00" * 64)
    media_cache.write_thumb_meta(out, str(src))
    check("finished output reported done",
          transcode.status_payload(str(src), 720)["status"] == "done")
    check("finished output is fresh", transcode.output_is_fresh(str(src), 720))

    check("start() on a fresh output is a no-op",
          asyncio.run(transcode.start(str(src), 720))["status"] == "done")

    # 6. A new source starts exactly one job that the worker completes.
    fresh = _TMP / "other.mp4"
    fresh.write_bytes(b"\x01" * 1024)
    calls: list[tuple[str, int]] = []

    def fake_worker(abs_path: str, height: int) -> None:
        calls.append((abs_path, height))
        target = transcode.output_file(abs_path, height)
        media_cache.TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x02" * 64)
        media_cache.write_thumb_meta(target, abs_path)
        transcode.job_state(abs_path)["status"] = "done"

    real_worker = transcode._transcode_worker
    transcode._transcode_worker = fake_worker
    try:
        async def run_job():
            first = await transcode.start(str(fresh), 720)
            second = await transcode.start(str(fresh), 720)
            for _ in range(200):
                await asyncio.sleep(0.01)
                if transcode.output_is_fresh(str(fresh), 720):
                    break
            return first, second

        first, second = asyncio.run(run_job())
        check("start() reports running", first["status"] == "running", str(first))
        check("duplicate start() does not re-queue", second["status"] in ("running", "queued"),
              str(second))
        check("worker ran exactly once", len(calls) == 1, str(calls))
        check("job finished and is cached", transcode.output_is_fresh(str(fresh), 720))
        check("no job reported after completion",
              transcode.active(str(fresh)) is None)
    finally:
        transcode._transcode_worker = real_worker

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
