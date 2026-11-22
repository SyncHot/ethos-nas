#!/usr/bin/env python3
"""HLS session checks: real ffmpeg start/stop, owner isolation, token rewrite."""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ethos-video-hls-test-")
_TMP_DATA = os.path.join(_TMP, "data")
os.environ["ETHOS_DATA_DIR"] = _TMP_DATA

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services import video_engine as ve  # noqa: E402
from backend.services import video_hls  # noqa: E402
from backend.routers.video_station import _ffprobe_sync  # noqa: E402

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"[PASS] {name}")
    else:
        _failed += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def make_sample(path: str) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=duration=8:size=320x180:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
        "-c:a", "aac", "-b:a", "64k", path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(proc.stderr[-800:])
    return proc.returncode == 0 and os.path.isfile(path)


async def run() -> int:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        print("[SKIP] ffmpeg/ffprobe not installed")
        return 0

    sample = os.path.join(_TMP, "sample.mkv")
    if not make_sample(sample):
        print("[SKIP] could not generate the HEVC sample")
        return 0

    info = _ffprobe_sync(sample)
    mode = ve.select_playback_mode(info, sample)
    check("HEVC sample needs transcode", mode == "transcode", mode)

    probe = ve.HardwareProbe(ffmpeg, ffprobe)
    status = await probe.status()
    print(f"[INFO] encoder={status.get('encoder')} decode={status.get('decode')}")
    decode_hw = False
    if status.get("encoder"):
        decode_hw, _ = await probe.can_decode_hw(
            sample, info, status.get("encoder"), status.get("device"))

    limiter = ve.TranscodeLimiter(2)
    mgr = video_hls.HlsManager(limiter=limiter, start_timeout=25)
    try:
        session = await mgr.start(
            "alice", sample,
            lambda out_dir: ve.build_hls_cmd(
                ffmpeg, sample, info, 0.0, status.get("encoder"), mode, 0,
                "balanced", "128k", out_dir, decode_hw=decode_hw,
                device=status.get("device"), list_size=4),
            mode=mode, encoder=status.get("encoder"), decode_hw=decode_hw,
        )
        playlist = session.playlist.read_text(encoding="utf-8")
        check("playlist is written", playlist.startswith("#EXTM3U"), playlist[:100])
        check("playlist has an init segment", "#EXT-X-MAP" in playlist, playlist[:200])
        segments = sorted(session.directory.glob("seg*.m4s"))
        check("at least one media segment exists", bool(segments))
        if segments:
            check("segment is non-empty", segments[0].stat().st_size > 0)
        check("other users cannot see the session", await mgr.get(session.sid, "bob") is None)
        check("owner can see the session", await mgr.get(session.sid, "alice") is not None)

        failed_second = False
        try:
            await mgr.start(
                "alice", sample,
                lambda out_dir: ve.build_hls_cmd(
                    ffmpeg, sample, info, 0.0, status.get("encoder"), mode, 0,
                    "balanced", "128k", out_dir, decode_hw=False,
                    device=status.get("device")),
                mode=mode, encoder=status.get("encoder"),
            )
        except video_hls.HlsLimitError:
            failed_second = True
        check("per-user session cap is enforced", failed_second)

        tokenized = video_hls.rewrite_playlist(playlist, "abc")
        check("token rewrite reaches segments", "seg00000.m4s?token=abc" in tokenized, tokenized[:300])
        check("other users cannot stop the session", not await mgr.stop(session.sid, "bob"))
        check("owner can stop the session", await mgr.stop(session.sid, "alice"))
        check("session directory is removed", not session.directory.exists())
    finally:
        await mgr.stop_all()

    # After a stop, the transcode slot must be reusable.
    check("limiter slot released after stop", await limiter.acquire(timeout=0.2))
    await limiter.release()
    return 0


def main() -> int:
    try:
        asyncio.run(run())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
