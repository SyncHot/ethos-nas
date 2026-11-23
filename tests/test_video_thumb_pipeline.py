#!/usr/bin/env python3
"""Video thumbnail pipeline — master frame, 202 queue, failure backoff.

Service-level (no server, no ffmpeg): the file manager's video thumbnail
endpoint used to run ffmpeg inline per request and per size, so a grid of
videos answered 202 for most tiles and the client gave up — the thumbnails
simply never appeared. This checks the replacement contract:

  * a cache miss answers 202 + Retry-After and enqueues exactly one job
  * one master frame serves every size (640 = master, 320 = PIL slot)
  * a changed source file invalidates the master and its slots
  * repeated failures back off exponentially, then become permanent
    (permanent = static SVG instead of endless ffmpeg retries)
  * replacing the source resets the failure marker
  * /warm-thumbnails goes through the same queue and skips cached files

Run:
    .venv/bin/python tests/test_video_thumb_pipeline.py
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
_TMP = Path(tempfile.mkdtemp(prefix="ethos-video-thumb-test-"))
os.environ["ETHOS_DATA_DIR"] = str(_TMP)

from backend.routers import files as fm  # noqa: E402
from backend.services import media_cache, path_scope  # noqa: E402
from backend.services import thumbnails as thumbs  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _age_source(path: Path, seconds: float) -> None:
    stamp = time.time() + seconds
    os.utime(path, (stamp, stamp))


def main() -> int:
    # Service-level test: no request scope is involved, so run as the system
    # identity (the fail-closed default would reject the temp fixtures).
    path_scope.CURRENT_ROLE.set("admin")
    path_scope.CURRENT_USER.set("system")
    root = _TMP / "media"
    root.mkdir(parents=True, exist_ok=True)
    video = root / "clip.mp4"
    video.write_bytes(b"\x00" * 4096)

    # The pipeline lives in services/thumbnails.py — relocate its cache root
    # (and the router-side alias) so nothing touches the real data dir.
    media_cache.set_cache_root(_TMP)
    fm.THUMB_DIR = media_cache.THUMB_DIR
    fm._ensure_thumb_dir()

    real_queue = fm._queue_video_thumb
    queued: list[tuple[str, bool]] = []
    fm._queue_video_thumb = lambda p, face=True: (queued.append((p, face)) or True)
    try:
        # 1. Cache miss → 202 + one queued job (no ffmpeg in the request path).
        resp = asyncio.run(fm.video_thumbnail(path=str(video), size=320))
        check("cache miss returns 202", resp.status_code == 202, str(resp.status_code))
        check("cache miss enqueues exactly one job", queued == [(str(video), True)], str(queued))
        check("cache miss sets Retry-After", resp.headers.get("Retry-After") == "2",
              str(resp.headers.get("Retry-After")))

        # 2. A generated master serves every size; smaller sizes are PIL slots.
        from PIL import Image
        master = thumbs._video_master_file(str(video))
        Image.new("RGB", (640, 360), (30, 60, 90)).save(master, "WEBP")
        thumbs._write_thumb_meta(master, str(video))
        check("master reported fresh", thumbs._video_master_is_fresh(str(video)))

        resp = asyncio.run(fm.video_thumbnail(path=str(video), size=640))
        check("640 serves the master", Path(resp.path) == master, str(resp.path))

        resp = asyncio.run(fm.video_thumbnail(path=str(video), size=320))
        slot = thumbs._video_slot_file(str(video), 320)
        check("320 renders a slot", Path(resp.path) == slot and slot.exists(), str(resp.path))
        if slot.exists():
            with Image.open(slot) as img:
                check("slot width matches request", img.width == 320, str(img.width))

        # 2b. face=0 (File Manager) has its own master/slot cache and never
        # shares the face-aware artifact used by Photos.
        queued.clear()
        resp = asyncio.run(fm.video_thumbnail(path=str(video), size=320, face=0))
        check("face=0 cache miss returns 202", resp.status_code == 202, str(resp.status_code))
        check("face=0 queues a flat job", queued == [(str(video), False)], str(queued))
        flat_master = thumbs._video_master_file(str(video), face=False)
        check("flat master has its own cache slot", flat_master != master, str(flat_master))
        Image.new("RGB", (640, 360), (90, 30, 30)).save(flat_master, "WEBP")
        thumbs._write_thumb_meta(flat_master, str(video))
        resp = asyncio.run(fm.video_thumbnail(path=str(video), size=640, face=0))
        check("face=0 640 serves the flat master", Path(resp.path) == flat_master, str(resp.path))
        resp = asyncio.run(fm.video_thumbnail(path=str(video), size=640))
        check("default 640 still serves the face master", Path(resp.path) == master, str(resp.path))

        # 3. A changed source invalidates the master and the derived slot.
        video.write_bytes(b"\x01" * 8192)
        _age_source(video, 5)
        check("changed source invalidates master", not thumbs._video_master_is_fresh(str(video)))
        check("changed source invalidates slot",
              not thumbs._video_slot_is_fresh(slot, master, str(video)))

        # 4. Failures back off exponentially and end as a permanent marker.
        delays: list[int] = []
        marker: dict = {}
        for _ in range(thumbs._VIDEO_THUMB_FAIL_MAX_ATTEMPTS):
            marker = thumbs._record_video_thumb_failure(str(video), "no_frame")
            delays.append(round(float(marker["next_try"]) - time.time()))
        check("backoff grows between attempts", delays[0] < delays[1] < delays[2], str(delays))
        check("last attempt is permanent", bool(marker.get("permanent")), str(marker))
        check("backoff marker is active", thumbs._video_thumb_backoff(str(video)) is not None)

        resp = asyncio.run(fm.video_thumbnail(path=str(video), size=320))
        check("permanent failure serves the static SVG",
              resp.status_code == 200 and resp.media_type == "image/svg+xml",
              f"{resp.status_code} {resp.media_type}")

        # 5. Replacing the source resets the failure history.
        video.write_bytes(b"\x02" * 4096)
        _age_source(video, 10)
        check("replaced source resets the marker",
              thumbs._video_thumb_backoff(str(video)) is None)

        # 6. warm-thumbnails queues video paths only and skips cached ones.
        second = root / "second.mkv"
        second.write_bytes(b"\x00" * 2048)
        cached = root / "cached.mp4"
        cached.write_bytes(b"\x00" * 2048)
        cached_master = thumbs._video_master_file(str(cached))
        Image.new("RGB", (640, 360), (10, 10, 10)).save(cached_master, "WEBP")
        thumbs._write_thumb_meta(cached_master, str(cached))

        queued.clear()
        out = asyncio.run(fm.warm_thumbnails(paths=f"{second},{cached},/etc/passwd"))
        check("warm queues only uncached videos", queued == [(str(second), True)], str(queued))
        check("warm reports accepted count", out.get("accepted") == 1, str(out))
    finally:
        fm._queue_video_thumb = real_queue
        shutil.rmtree(_TMP, ignore_errors=True)

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
