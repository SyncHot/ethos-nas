#!/usr/bin/env python3
"""Unit checks for services/video_engine.py (playback mode, ffmpeg builders, probe)."""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services import video_engine as ve  # noqa: E402
from backend.services.video_hls import rewrite_playlist, segment_path  # noqa: E402

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


HEVC = {"width": 3840, "height": 2160, "video_codec": "hevc", "audio_codec": "eac3",
        "audio_tracks": [{"codec": "eac3", "language": "eng"},
                         {"codec": "aac", "language": "pol"}]}
HEVC_10BIT = {"width": 1920, "height": 1080, "pix_fmt": "yuv420p10le",
              "video_codec": "hevc", "audio_codec": "eac3",
              "audio_tracks": [{"codec": "eac3", "language": "eng"}]}
H264 = {"width": 1920, "height": 1080, "video_codec": "h264", "pix_fmt": "yuv420p",
        "audio_codec": "aac",
        "audio_tracks": [{"codec": "aac", "language": "eng"}]}
H264_AC3 = {"width": 1920, "height": 1080, "video_codec": "h264", "pix_fmt": "yuv420p",
            "audio_codec": "ac3",
            "audio_tracks": [{"codec": "ac3", "language": "eng"}]}
H264_10BIT = {"width": 1920, "height": 1080, "video_codec": "h264",
              "pix_fmt": "yuv420p10le", "audio_codec": "aac",
              "audio_tracks": [{"codec": "aac", "language": "eng"}]}


def test_modes() -> None:
    check("h264+aac mp4 is direct",
          ve.select_playback_mode(H264, "/x/movie.mp4") == "direct")
    check("h264+ac3 mkv is remux",
          ve.select_playback_mode(H264_AC3, "/x/movie.mkv") == "remux")
    check("hevc mkv is transcode",
          ve.select_playback_mode(HEVC, "/x/movie.mkv") == "transcode")
    check("no ffmpeg falls back to direct",
          ve.select_playback_mode(HEVC, "/x/movie.mkv", ffmpeg_available=False) == "direct")


def test_bitrate() -> None:
    check("4K downscaled to 720p gets 4M",
          ve.bitrate_for(3840, 2160, 720) == "4M")
    check("1080p target gets 6M",
          ve.bitrate_for(3840, 2160, 1080) == "6M")
    check("480p target gets 2.5M",
          ve.bitrate_for(1920, 1080, 480) == "2.5M")
    check("360p target gets 1.5M",
          ve.bitrate_for(640, 360, 0) == "1.5M")


def test_video_args() -> None:
    pre, video, filters = ve.build_video_args(
        HEVC, "transcode", "vaapi", "balanced", decode_hw=True, device="/dev/dri/renderD128")
    check("VA-API decode pins the device", "-vaapi_device" in pre and "/dev/dri/renderD128" in pre)
    check("VA-API decode uses -hwaccel vaapi", "-hwaccel" in pre and "vaapi" in pre)
    check("VA-API GPU downscale",
          any(f.startswith("scale_vaapi=w=-2:h=1080") for f in filters), str(filters))
    check("VA-API decode forces nv12 for H.264",
          any("format=nv12" in f for f in filters), str(filters))
    check("VA-API encoder selected", "h264_vaapi" in video)

    pre, video, filters = ve.build_video_args(
        HEVC_10BIT, "transcode", "vaapi", "balanced", decode_hw=True,
        device="/dev/dri/renderD128")
    check("10-bit HEVC decode is converted to nv12",
          filters == ["scale_vaapi=format=nv12"], str(filters))
    check("10-bit HEVC still uses h264_vaapi", "h264_vaapi" in video)

    pre, video, filters = ve.build_video_args(
        HEVC, "transcode", "vaapi", "balanced", decode_hw=False, device="/dev/dri/renderD128")
    check("software decode uploads frames", "format=nv12" in filters and "hwupload" in filters)
    check("software decode still uses VA-API encode", "h264_vaapi" in video)

    pre, video, filters = ve.build_video_args(
        HEVC, "transcode", "qsv", "balanced", decode_hw=True, device="/dev/dri/renderD128")
    check("QSV GPU downscale", "scale_qsv=w=-2:h=1080" in filters, str(filters))
    check("QSV encoder selected", "h264_qsv" in video)

    pre, video, filters = ve.build_video_args(HEVC, "transcode", None, "fast")
    check("libx264 gets CRF", "libx264" in video and "-crf" in video)
    check("libx264 downscales to 720p", "scale=-2:720" in filters, str(filters))

    pre, video, filters = ve.build_video_args(H264_AC3, "remux", None, "balanced")
    check("remux copies the video", video == ["-c:v", "copy"] and filters == [])


def test_audio_args() -> None:
    args = ve.build_audio_args(H264, 0, "remux", "192k")
    check("AAC remux copies audio", args[-2:] == ["-c:a", "copy"], str(args))
    args = ve.build_audio_args(H264_AC3, 0, "remux", "192k")
    check("AC3 remux encodes to AAC", "aac" in args and "192k" in args)
    args = ve.build_audio_args(HEVC, 1, "transcode", "128k")
    check("selected audio track is mapped", "0:a:1?" in args, str(args))


def test_aac_channel_layout() -> None:
    # Chrome rejects AAC muxed from a non-MPEG-4 layout (PCE): every E-AC-3
    # "5.1(side)" WEB-DL played a black screen with CHUNK_DEMUXER_ERROR.
    side = {"audio_tracks": [{"codec": "eac3", "channels": 6,
                              "channel_layout": "5.1(side)"}]}
    args = ve.build_audio_args(side, 0, "transcode", "192k")
    check("5.1(side) is normalised to 5.1",
          args[-2:] == ["-af", "aformat=channel_layouts=5.1"], str(args))

    unknown = {"audio_tracks": [{"codec": "eac3", "channels": 6}]}
    args = ve.build_audio_args(unknown, 0, "transcode", "192k")
    check("unknown 6-channel layout is normalised too",
          "aformat=channel_layouts=5.1" in " ".join(args), str(args))

    standard = {"audio_tracks": [{"codec": "ac3", "channels": 6,
                                  "channel_layout": "5.1"}]}
    args = ve.build_audio_args(standard, 0, "transcode", "192k")
    check("standard 5.1 adds no filter", "-af" not in args, str(args))

    stereo = {"audio_tracks": [{"codec": "aac", "channels": 2,
                                "channel_layout": "stereo"}]}
    check("standard stereo adds no filter", ve.aac_layout_filter(stereo["audio_tracks"][0]) == [])

    side_aac = {"audio_tracks": [{"codec": "aac", "channels": 6,
                                  "channel_layout": "5.1(side)"}]}
    args = ve.build_audio_args(side_aac, 0, "remux", "192k")
    check("non-standard AAC is re-encoded instead of copied",
          "copy" not in args and "aac" in args, str(args))
    check("AAC re-encode still gets the 5.1 filter",
          "aformat=channel_layouts=5.1" in " ".join(args), str(args))

    cmd = ve.build_hls_cmd(
        "ffmpeg", "/x/movie.mkv", side, 0, "vaapi", "transcode", 0,
        "balanced", "192k", "/tmp/out", decode_hw=True, device="/dev/dri/renderD128")
    check("HLS command carries the layout filter",
          "aformat=channel_layouts=5.1" in " ".join(cmd), " ".join(cmd))


def test_audio_codec_negotiation() -> None:
    # Browsers without AAC (some Chromium builds) get Opus, or video only.
    ac3 = {"audio_tracks": [{"codec": "ac3", "channels": 6, "channel_layout": "5.1"}]}
    args = ve.build_audio_args(ac3, 0, "transcode", "192k", "opus")
    check("opus fallback uses libopus", "libopus" in args and "192k" in args, str(args))
    check("opus is downmixed to stereo", "-ac" in args and "2" in args, str(args))

    args = ve.build_audio_args(ac3, 0, "transcode", "192k", "none")
    check("no-audio mode drops the track", args == ["-an"], str(args))

    args = ve.build_audio_args(ac3, 0, "transcode", "192k", "bogus")
    check("unknown acodec falls back to aac", "aac" in args, str(args))

    cmd = ve.build_hls_cmd(
        "ffmpeg", "/x/movie.mkv", ac3, 0, "vaapi", "transcode", 0,
        "balanced", "192k", "/tmp/out", acodec="none")
    check("HLS command can drop audio", "-an" in cmd, " ".join(cmd))


def test_commands() -> None:
    cmd = ve.build_transcode_cmd(
        "ffmpeg", "/x/movie.mkv", HEVC, 12.5, "vaapi", "transcode", 0,
        "balanced", "192k", decode_hw=True, device="/dev/dri/renderD128")
    joined = " ".join(cmd)
    for token in ("-hwaccel vaapi", "h264_vaapi", "-g 30", "-bf 0",
                  "-movflags +frag_keyframe+empty_moov", "pipe:1", "-sn"):
        check(f"pipe cmd contains {token}", token in joined, joined)

    cmd = ve.build_hls_cmd(
        "ffmpeg", "/x/movie.mkv", HEVC, 0, "vaapi", "transcode", 0,
        "balanced", "192k", "/tmp/out", decode_hw=True, device="/dev/dri/renderD128")
    joined = " ".join(cmd)
    for token in ("-re", "-f hls", "-hls_segment_type fmp4", "delete_segments",
                  "independent_segments", "index.m3u8", "seg%05d.m4s"):
        check(f"HLS cmd contains {token}", token in joined, joined)


def test_render_command() -> None:
    cmd = ve.build_render_cmd(
        "ffmpeg", "/x/movie.mkv", HEVC, "vaapi", 0, "fast", "160k",
        "/data/video_exports/files/job1.mp4.part",
        decode_hw=True, device="/dev/dri/renderD128")
    joined = " ".join(cmd)
    for token in ("-hwaccel vaapi", "h264_vaapi", "scale_vaapi=w=-2:h=720",
                  "-progress pipe:1", "-nostats", "-movflags +faststart",
                  "-f mp4", "/data/video_exports/files/job1.mp4.part", "-sn"):
        check(f"render cmd contains {token}", token in joined, joined)
    check("render cmd is finite (no -re)", "-re" not in cmd, joined)
    check("render cmd writes a file", cmd[-1].endswith("job1.mp4.part"), cmd[-1])

    silent = ve.build_render_cmd(
        "ffmpeg", "/x/movie.mkv", HEVC, "libx264", 0, "balanced", "192k",
        "/tmp/o.mp4", progress=False)
    check("progress can be disabled", "-progress" not in silent, " ".join(silent))
    check("software path scales to 1080p",
          "scale=-2:1080" in " ".join(silent), " ".join(silent))

    noaudio = ve.build_render_cmd(
        "ffmpeg", "/x/movie.mkv", HEVC, "vaapi", 0, "fast", "160k",
        "/tmp/o.mp4", acodec="none")
    check("render can drop audio", "-an" in noaudio, " ".join(noaudio))

    opus = ve.build_render_cmd(
        "ffmpeg", "/x/movie.mkv", HEVC, "vaapi", 1, "fast", "160k",
        "/tmp/o.mp4", acodec="opus")
    opus_joined = " ".join(opus)
    check("render can encode opus", "libopus" in opus_joined and "0:a:1" in opus_joined,
          opus_joined)


def test_download_mode() -> None:
    check("copy-safe detection needs 8-bit H.264",
          ve.copy_video_ok(H264) and not ve.copy_video_ok(H264_10BIT)
          and not ve.copy_video_ok(HEVC), "")

    mode, note = ve.download_mode(H264, "original", ".mp4")
    check("original MP4 is served as is", mode == "passthrough", mode)
    check("passthrough explains itself", "no conversion" in note, note)

    mode, _ = ve.download_mode(H264_AC3, "original", ".mkv")
    check("original MKV remuxes the video", mode == "remux", mode)

    mode, note = ve.download_mode(HEVC, "original", ".mkv")
    check("original falls back for HEVC", mode == "transcode", mode)
    check("fallback says it re-encodes", "1080p" in note, note)

    mode, _ = ve.download_mode(H264_10BIT, "original", ".mkv")
    check("10-bit is never copied", mode == "transcode", mode)

    mode, note = ve.download_mode(H264, "fast", ".mkv")
    check("a preset always encodes", mode == "transcode" and not note, mode)

    check("480p preset exists", ve.QUALITY_PRESETS["tiny"]["max_height"] == 480,
          str(ve.QUALITY_PRESETS.get("tiny")))
    check("480p is an export quality", "tiny" in ve.EXPORT_QUALITIES,
          str(ve.EXPORT_QUALITIES))
    _pre, _video, filters = ve.build_video_args(H264, "transcode", None, "tiny")
    check("480p downscales to 480", filters == ["scale=-2:480"], str(filters))

    cmd = ve.build_render_cmd(
        "ffmpeg", "/x/movie.mkv", H264_AC3, None, 0, "original", "192k",
        "/tmp/o.mp4", mode="remux")
    joined = " ".join(cmd)
    check("remux render copies the video", "-c:v copy" in joined, joined)
    check("remux render still writes a faststart MP4",
          "+faststart" in joined and "-sn" in joined, joined)
    check("remux render encodes AC3 to AAC", "aac" in joined, joined)


def test_playlist_and_segments() -> None:
    text = ('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4.0,\nseg00000.m4s\n')
    rewritten = rewrite_playlist(text, "tok en")
    check("init URI gets the token",
          'URI="init.mp4?token=tok%20en"' in rewritten, rewritten)
    check("segment gets the token", "seg00000.m4s?token=tok%20en" in rewritten, rewritten)
    check("no token leaves the playlist unchanged", rewrite_playlist(text, None) == text)

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "init.mp4").write_bytes(b"x")
        Path(tmp, "seg00000.m4s").write_bytes(b"y")
        session = type("S", (), {"directory": Path(tmp)})()
        check("init is served", segment_path(session, "init.mp4") is not None)
        check("segment is served", segment_path(session, "seg00000.m4s") is not None)
        check("traversal is blocked", segment_path(session, "../init.mp4") is None)
        check("playlist name is not a segment", segment_path(session, "index.m3u8") is None)


async def test_probe_without_ffmpeg() -> None:
    probe = ve.HardwareProbe(None, None)
    st = await probe.status()
    check("missing ffmpeg is reported", st["encoder"] is None and not st["tools"]["ffmpeg"])


async def test_limiter() -> None:
    limiter = ve.TranscodeLimiter(1)
    check("first slot acquired", await limiter.acquire(timeout=0.1))
    check("second slot is refused", not await limiter.acquire(timeout=0.2))
    await limiter.release()
    check("released slot is reusable", await limiter.acquire(timeout=0.1))
    await limiter.release()


def main() -> int:
    test_modes()
    test_bitrate()
    test_video_args()
    test_audio_args()
    test_aac_channel_layout()
    test_audio_codec_negotiation()
    test_commands()
    test_render_command()
    test_download_mode()
    test_playlist_and_segments()
    asyncio.run(test_probe_without_ffmpeg())
    asyncio.run(test_limiter())
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
