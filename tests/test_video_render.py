#!/usr/bin/env python3
"""Offline render manager checks (Videos PWA).

Isolated instance, no server and no ffmpeg — every job is driven by a tiny
Python stand-in for ffmpeg so the queue, the caps, the dedupe, cancellation and
the sweeper can be asserted deterministically:

    .venv/bin/python tests/test_video_render.py
"""

import asyncio
import importlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import settings  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# Stand-ins for ffmpeg: argv is [python, -c, SCRIPT, out_path, mode].
_SCRIPT = r"""
import sys, time
out, mode = sys.argv[1], sys.argv[2]
if mode == "fail":
    sys.stderr.write("Invalid data found when processing input\n")
    sys.exit(3)
if mode == "slow":
    time.sleep(30)
for i in range(1, 5):
    print(f"out_time_us={i * 500000}", flush=True)
with open(out, "wb") as fh:
    fh.write(b"\0" * 2048)
"""


def fake_ffmpeg(mode: str = "ok"):
    def factory(part_path: str) -> list[str]:
        return [sys.executable, "-c", _SCRIPT, part_path, mode]
    return factory


async def _wait(job, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while job.active and time.monotonic() < deadline:
        await asyncio.sleep(0.02)


def _manager(tmp: Path, **kwargs):
    module = importlib.import_module("backend.services.video_render")
    return module, module.RenderManager(base_dir=tmp / "video_exports", **kwargs)


INFO = {"duration": 4.0, "streams": []}


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ethos-render-test-"))
    original_data_dir = settings.DATA_DIR
    settings.DATA_DIR = tmp
    try:
        module, mgr = _manager(tmp)

        # ── happy path ────────────────────────────────────────────────────
        job = await mgr.start(owner="alice", abs_path=str(tmp / "Movie.mkv"),
                              info=INFO, quality="fast", audio_idx=0,
                              source_key="movie|fast|0", cmd_factory=fake_ffmpeg())
        check("start returns a queued job", job.status in ("queued", "rendering"), job.status)
        await _wait(job)
        check("render finishes", job.status == "done", f"{job.status} {job.error}")
        check("render records the output size", job.size == 2048, str(job.size))
        check("render sets 100%", job.percent == 100.0, str(job.percent))
        path = mgr.download_path(job.id, "alice")
        check("download path exists", path.is_file(), str(path))
        check("download path is the job file", path.name == f"{job.id}.mp4", path.name)
        public = mgr.get(job.id, "alice").public()
        check("public shape has a download url",
              public["download_url"] == f"/api/videos/exports/{job.id}/download",
              str(public.get("download_url")))
        check("public shape exposes the file name",
              public.get("name") == "Movie.mkv", str(public))
        check("public shape lets the UI match library items",
              public.get("path") == str(tmp / "Movie.mkv"), str(public.get("path")))

        # ── dedupe ────────────────────────────────────────────────────────
        again = await mgr.start(owner="alice", abs_path=str(tmp / "Movie.mkv"),
                                info=INFO, quality="fast", audio_idx=0,
                                source_key="movie|fast|0", cmd_factory=fake_ffmpeg())
        check("same source reuses the finished render", again.id == job.id, again.id)

        # ── per-user caps ─────────────────────────────────────────────────
        store = json.loads((tmp / "video_exports" / "jobs.json").read_text() or "{}")
        check("jobs are persisted", any(r["id"] == job.id for r in store.get("jobs", [])),
              str(store)[:120])

        _module, tiny = _manager(tmp, max_per_user=1)
        try:
            await tiny.start(owner="alice", abs_path=str(tmp / "Other.mkv"), info=INFO,
                             quality="fast", audio_idx=0, source_key="other|fast|0",
                             cmd_factory=fake_ffmpeg())
            check("stored-download cap rejects a new job", False, "no RenderLimit")
        except module.RenderLimit:
            check("stored-download cap rejects a new job", True)

        _module, lowdisk = _manager(tmp, min_free_bytes=1 << 62)
        try:
            await lowdisk.start(owner="bob", abs_path=str(tmp / "Other.mkv"), info=INFO,
                                quality="fast", audio_idx=0, source_key="other|fast|0",
                                cmd_factory=fake_ffmpeg())
            check("free-space preflight rejects", False, "no RenderDiskError")
        except module.RenderDiskError:
            check("free-space preflight rejects", True)

        # ── queue: many jobs, rendered one after another ──────────────────
        first = await mgr.start(owner="carol", abs_path=str(tmp / "Slow.mkv"), info=INFO,
                                quality="fast", audio_idx=0, source_key="slow|fast|0",
                                cmd_factory=fake_ffmpeg("slow"))
        second = await mgr.start(owner="carol", abs_path=str(tmp / "Slow2.mkv"), info=INFO,
                                 quality="fast", audio_idx=0, source_key="slow2|fast|0",
                                 cmd_factory=fake_ffmpeg("slow"))
        check("a second job is queued instead of 429", second.status in ("queued", "rendering"),
              second.status)
        check("queue positions are FIFO",
              (first.queue_position, second.queue_position) == (1, 2),
              f"{first.queue_position},{second.queue_position}")
        await asyncio.sleep(0.2)
        check("one render at a time", second.status == "queued", second.status)

        # ── cancel ────────────────────────────────────────────────────────
        cancelled = await mgr.cancel(first.id, "carol")
        check("cancel marks the job cancelled", cancelled.status == "cancelled",
              cancelled.status)
        check("cancel removes the partial file",
              not (mgr.files_dir / f"{first.id}.mp4.part").exists())
        await asyncio.sleep(0.1)
        check("the queued render takes over", second.status in ("rendering", "done"), second.status)
        await mgr.cancel(second.id, "carol")
        mgr.delete(first.id, "carol")
        try:
            mgr.get(first.id, "carol")
            check("delete forgets the job", False, "still present")
        except module.RenderNotFound:
            check("delete forgets the job", True)

        # ── rolling window + consume ──────────────────────────────────────
        _module, windowed = _manager(tmp, prepared_window=1)
        keeper1 = await windowed.start(owner="frank", abs_path=str(tmp / "W1.mkv"), info=INFO,
                                       quality="fast", audio_idx=0, source_key="w1|fast|0",
                                       cmd_factory=fake_ffmpeg())
        await _wait(keeper1)
        keeper2 = await windowed.start(owner="frank", abs_path=str(tmp / "W2.mkv"), info=INFO,
                                       quality="fast", audio_idx=0, source_key="w2|fast|0",
                                       cmd_factory=fake_ffmpeg())
        await _wait(keeper2)
        check("rolling window keeps the newest copy",
              keeper2.status == "done" and (windowed.files_dir / f"{keeper2.id}.mp4").is_file(),
              keeper2.status)
        try:
            windowed.get(keeper1.id, "frank")
            check("rolling window reaps the oldest copy", False, "still present")
        except module.RenderNotFound:
            check("rolling window reaps the oldest copy", True)
        consumed = windowed.consume(keeper2.id, "frank")
        check("consume keeps the NAS copy for a re-download",
              consumed.status == "consumed"
              and (windowed.files_dir / f"{keeper2.id}.mp4").exists(), consumed.status)
        check("consumed job keeps a download url",
              consumed.public()["download_url"] is not None,
              str(consumed.public().get("download_url")))

        # ── passthrough (original MP4) ────────────────────────────────────
        source = tmp / "Original.mp4"
        source.write_bytes(b"\0" * 4096)
        _module, passthrough = _manager(tmp)
        direct = await passthrough.start(
            owner="heidi", abs_path=str(source), info=INFO, quality="original",
            audio_idx=0, source_key="orig|original|0|passthrough",
            cmd_factory=lambda _p: [], mode="passthrough", note="Original file")
        await _wait(direct)
        check("passthrough finishes without ffmpeg", direct.status == "done", direct.status)
        check("passthrough serves the original file",
              passthrough.download_path(direct.id, "heidi") == source, str(direct.path))
        passthrough.consume(direct.id, "heidi")
        check("consuming a passthrough keeps the source", source.is_file())

        # ── failures ──────────────────────────────────────────────────────
        bad = await mgr.start(owner="dave", abs_path=str(tmp / "Broken.mkv"), info=INFO,
                              quality="fast", audio_idx=0, source_key="bad|fast|0",
                              cmd_factory=fake_ffmpeg("fail"))
        await _wait(bad)
        check("ffmpeg failure marks the job failed", bad.status == "failed", bad.status)
        check("failure keeps the stderr tail", "Invalid data" in bad.error, bad.error)
        check("failure leaves no partial file",
              not (mgr.files_dir / f"{bad.id}.mp4.part").exists())

        # ── ownership ─────────────────────────────────────────────────────
        try:
            mgr.get(job.id, "mallory")
            check("another user cannot read the job", False, "leaked")
        except module.RenderNotFound:
            check("another user cannot read the job", True)
        try:
            mgr.download_path(job.id, "mallory")
            check("another user cannot download", False, "leaked")
        except module.RenderNotFound:
            check("another user cannot download", True)

        # ── expiry + restart recovery ─────────────────────────────────────
        sweep_removed = mgr.sweep()
        check("fresh downloads survive a sweep", sweep_removed == 0, str(sweep_removed))
        job.finished_at = time.time() - 8 * 86400
        mgr._persist()
        expired = mgr.sweep()
        check("expired download is swept", expired >= 1, str(expired))
        check("expired file is removed", not (mgr.files_dir / f"{job.id}.mp4").exists())

        _module, restarted = _manager(tmp)
        pending = await restarted.start(owner="erin", abs_path=str(tmp / "Later.mkv"),
                                       info=INFO, quality="fast", audio_idx=0,
                                       source_key="later|fast|0",
                                       cmd_factory=fake_ffmpeg("slow"))
        restarted._persist()
        _module, recovered = _manager(tmp)
        revived = recovered.get(pending.id, "erin")
        check("interrupted job is failed on restart", revived.status == "failed", revived.status)
        check("interrupted job explains itself", "Interrupted" in revived.error, revived.error)
        await restarted.cancel(pending.id, "erin")

        snap = recovered.snapshot()
        check("snapshot reports counters",
              {"jobs", "active", "done", "bytes", "free_bytes"} <= set(snap), str(snap))
        check("snapshot counts no active job", snap["active"] == 0, str(snap))

        failed = [r for r in _results if not r[1]]
        print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
        return 1 if failed else 0
    finally:
        settings.DATA_DIR = original_data_dir
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
