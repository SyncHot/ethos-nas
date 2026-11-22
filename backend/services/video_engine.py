"""Video playback engine: mode selection, ffmpeg builders and hardware probing.

Pure helpers live here so both the Videos router and the HLS session service use
one command shape. No router imports (``scripts/check_boundaries.py``).

Hardware probe cascade: Intel QuickSync (h264_qsv) → VA-API (h264_vaapi) →
software libx264. Every tier is validated with a real micro-encode and the
ffmpeg stderr tail is kept, so ``/api/videos/status`` can explain *why* a tier
failed instead of only saying "unavailable".
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import logging
import os
import time

from backend.config import settings
from backend.services import child_limits

logger = logging.getLogger("ethos-nas.services.video_engine")

BROWSER_AUDIO_OK = {"aac", "mp3", "opus"}
BROWSER_VIDEO_OK = {"h264", "vp8", "vp9"}
STREAMABLE_EXTS = {".mp4", ".m4v", ".webm", ".mov"}

# MPEG-4 channel configurations the AAC encoder can express without a Program
# Config Element (channelConfiguration 1-7). ffmpeg falls back to a PCE whenever
# the source layout has no MPEG-4 equivalent — notably E-AC-3 "5.1(side)", the
# usual layout of DD+ tracks in WEB-DL rips. Browsers refuse PCE audio in MSE
# (Chrome: CHUNK_DEMUXER_ERROR_APPEND_FAILED) so those movies played a black
# screen, while AC-3 "5.1" BluRay rips worked. Always normalise the layout.
AAC_STANDARD_LAYOUTS = {"mono", "stereo", "3.0", "4.0", "quad", "5.0", "5.1", "7.1"}
_AAC_LAYOUT_BY_CHANNELS = {1: "mono", 2: "stereo", 3: "3.0", 4: "4.0", 5: "5.0", 6: "5.1", 8: "7.1"}

# Audio codecs the player may ask for. ``aac`` is the default; Chromium builds
# without the proprietary AAC decoder (some distro/Electron builds report
# ``MediaSource.isTypeSupported('audio/mp4; codecs="mp4a.40.2") === false``)
# still decode Opus in MP4, and ``none`` keeps video playable without sound.
AUDIO_CODECS = ("aac", "opus", "none")

# Download ("offline export") presets: the live-playback encoders plus
# ``original`` — a stream copy when the source is already H.264 4:2:0 (seconds
# instead of an hour on a Celeron). "high"/"source" stays playback-only.
EXPORT_QUALITIES = ("tiny", "fast", "balanced", "original")

# H.264 bitstreams an MP4 can carry unchanged. 10-bit / 4:2:2 / 4:4:4 would
# play nowhere on phones, so those always re-encode.
COPY_SAFE_PIX_FMTS = {"yuv420p", "yuvj420p"}

QUALITY_PRESETS = {
    # Tuned for a 4-core Celeron-class CPU (Intel N150): libx264 veryfast does
    # ~8 fps at 1440p, so downscaling is what keeps live playback watchable.
    "tiny": {"preset": "ultrafast", "crf": 30, "max_height": 480},
    "fast": {"preset": "ultrafast", "crf": 28, "max_height": 720},
    "balanced": {"preset": "veryfast", "crf": 23, "max_height": 1080},
    "high": {"preset": "veryfast", "crf": 20, "max_height": 0},  # 0 = source res
}

AUDIO_BITRATES = {"96k", "128k", "160k", "192k", "256k"}

PROBE_VERSION = 2
HW_CACHE_TTL = 600.0
DECODE_CACHE_TTL = 600.0
PROBE_TIMEOUT = 25.0
DECODE_TIMEOUT = 20.0
MAX_REASON = 600


def copy_video_ok(info: dict) -> bool:
    """True when the video stream can be remuxed into MP4 without re-encoding.

    Conservative on purpose: only 8-bit H.264 4:2:0. A wrong guess would make
    the offline file unplayable on the phone, which is worse than an encode.
    """
    codec = (info.get("video_codec") or "").lower()
    pix_fmt = (info.get("pix_fmt") or "").lower()
    try:
        height = int(info.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    return codec == "h264" and pix_fmt in COPY_SAFE_PIX_FMTS and height > 0


def download_mode(info: dict, quality: str, ext: str, audio_idx: int = 0) -> tuple[str, str]:
    """Return ``(mode, note)`` for an offline export.

    passthrough — the source already is a phone-playable MP4: serve it as is
    remux       — copy the H.264 video, convert only the audio (~seconds)
    transcode   — full re-encode (HEVC/10-bit sources, or a downscaled preset)
    """
    if quality != "original":
        return "transcode", ""
    if not copy_video_ok(info):
        return "transcode", "Original needs a re-encode for this source — saved as 1080p"
    tracks = info.get("audio_tracks") or []
    idx = min(max(0, int(audio_idx or 0)), max(0, len(tracks) - 1)) if tracks else 0
    track = tracks[idx] if idx < len(tracks) else {}
    audio_codec = (track.get("codec") or info.get("audio_codec") or "").lower()
    layout = (track.get("channel_layout") or "").strip().lower()
    audio_copyable = audio_codec == "aac" and (not layout or layout in AAC_STANDARD_LAYOUTS)
    if ext.lower() in STREAMABLE_EXTS and audio_copyable:
        return "passthrough", "Original file — no conversion"
    return "remux", "Original video — audio converted to AAC"


def select_playback_mode(info: dict, abs_path: str, *, ffmpeg_available: bool = True) -> str:
    """How the browser should get this file.

    direct    — browser-native codecs + container, raw Range stream
    remux     — video codec OK, audio/container isn't: copy video, AAC audio
    transcode — video codec not browser-playable: full re-encode
    """
    if not ffmpeg_available:
        return "direct"  # nothing else we can do; the player surfaces the error
    audio_codec = (info.get("audio_codec") or "").lower()
    video_codec = (info.get("video_codec") or "").lower()
    ext = os.path.splitext(abs_path)[1].lower()
    video_ok = (not video_codec) or video_codec in BROWSER_VIDEO_OK
    audio_ok = (not audio_codec) or audio_codec in BROWSER_AUDIO_OK
    if video_ok and audio_ok and ext in STREAMABLE_EXTS:
        return "direct"
    if video_ok:
        return "remux"
    return "transcode"


def bitrate_for(width: int, height: int, max_height: int = 0) -> str:
    """Bitrate from the *output* height, not the source resolution.

    A 4K source downscaled to 720p must get the 720p bitrate; using the source
    dimensions wasted bandwidth in the previous implementation.
    """
    target_height = int(max_height or height or 0)
    if target_height >= 1080:
        return "6M"
    if target_height >= 720:
        return "4M"
    if target_height >= 480:
        return "2.5M"
    return "1.5M"


def build_video_args(
    info: dict,
    mode: str,
    encoder: str | None,
    quality: str,
    *,
    decode_hw: bool = False,
    device: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Return ``(input_options, video_options, filters)`` for one ffmpeg run."""
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)
    preset = QUALITY_PRESETS.get(quality) or QUALITY_PRESETS["balanced"]
    max_height = int(preset.get("max_height") or 0)
    downscale = bool(max_height and height and height > max_height)
    bitrate = bitrate_for(width, height, max_height)

    pre: list[str] = []
    filters: list[str] = []

    if mode == "remux":
        return pre, ["-c:v", "copy"], filters

    if encoder == "qsv":
        if device:
            pre += ["-qsv_device", device]
        if decode_hw and device:
            pre += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
            if downscale:
                filters.append(f"scale_qsv=w=-2:h={max_height}")
        else:
            if downscale:
                filters.append(f"scale=-2:{max_height}")
            filters.append("format=nv12")
        video = [
            "-c:v", "h264_qsv", "-preset", "veryfast", "-b:v", bitrate,
            "-g", "30", "-bf", "0", "-pix_fmt", "yuv420p",
        ]
    elif encoder == "vaapi":
        if device:
            pre += ["-vaapi_device", device]
        if decode_hw and device:
            pre += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
            # h264_vaapi only accepts 8-bit nv12. 10-bit HEVC sources otherwise
            # fail with "No usable encoding profile found" (seen on a real
            # 10-bit BD-Rip), so always force the encoder input format.
            if downscale:
                filters.append(f"scale_vaapi=w=-2:h={max_height}:format=nv12")
            else:
                filters.append("scale_vaapi=format=nv12")
        else:
            if downscale:
                filters.append(f"scale=-2:{max_height}")
            filters += ["format=nv12", "hwupload"]
        video = ["-c:v", "h264_vaapi", "-b:v", bitrate, "-g", "30", "-bf", "0"]
    else:
        video = [
            "-c:v", "libx264", "-preset", preset["preset"], "-crf", str(preset["crf"]),
            # Short GOP: with the default 250-frame GOP a slow CPU would take
            # ~a minute to emit the first fragment of the live pipe.
            "-g", "30", "-maxrate", bitrate, "-bufsize", "16M", "-pix_fmt", "yuv420p",
        ]
        if downscale:
            filters.append(f"scale=-2:{max_height}")

    return pre, video, filters


def aac_layout_filter(track: dict) -> list[str]:
    """``-af aformat=…`` that pins a standard MPEG-4 channel layout.

    Returns an empty list when the detected layout is already standard. An
    unknown layout is normalised from the channel count so the AAC encoder
    writes a channelConfiguration instead of a Program Config Element.
    """
    layout = (track.get("channel_layout") or "").strip().lower()
    if layout in AAC_STANDARD_LAYOUTS:
        return []
    try:
        channels = int(track.get("channels") or 0)
    except (TypeError, ValueError):
        channels = 0
    target = _AAC_LAYOUT_BY_CHANNELS.get(channels)
    return ["-af", f"aformat=channel_layouts={target}"] if target else []


def build_audio_args(info: dict, track_idx: int, mode: str, audio_bitrate: str,
                     acodec: str = "aac") -> list[str]:
    acodec = acodec if acodec in AUDIO_CODECS else "aac"
    tracks = info.get("audio_tracks") or []
    idx = min(max(0, int(track_idx)), max(0, len(tracks) - 1)) if tracks else 0
    track = tracks[idx] if idx < len(tracks) else {}
    if acodec == "none":
        return ["-an"]
    if acodec == "opus":
        # Browsers without AAC still decode Opus inside fMP4; downmix to stereo
        # because multichannel Opus in MSE is not portable.
        return ["-map", f"0:a:{idx}?", "-c:a", "libopus", "-b:a", audio_bitrate, "-ac", "2"]
    codec = (track.get("codec") or "").lower()
    layout = (track.get("channel_layout") or "").strip().lower()
    # Copy AAC only when it is already a standard layout — a PCE-based stream
    # would break playback in the browser just like an encoded one.
    if mode == "remux" and codec == "aac" and (not layout or layout in AAC_STANDARD_LAYOUTS):
        return ["-map", f"0:a:{idx}?", "-c:a", "copy"]
    return ["-map", f"0:a:{idx}?", "-c:a", "aac", "-b:a", audio_bitrate,
            *aac_layout_filter(track)]


def _av_args(
    info: dict,
    mode: str,
    encoder: str | None,
    track_idx: int,
    quality: str,
    audio_bitrate: str,
    *,
    decode_hw: bool,
    device: str | None,
    acodec: str = "aac",
) -> tuple[list[str], list[str], list[str]]:
    pre, video, filters = build_video_args(
        info, mode, encoder, quality, decode_hw=decode_hw, device=device,
    )
    audio = build_audio_args(info, track_idx, mode, audio_bitrate, acodec)
    return pre, video + (["-vf", ",".join(filters)] if filters else []), audio


def build_transcode_cmd(
    ffmpeg: str,
    abs_path: str,
    info: dict,
    start: float,
    encoder: str | None,
    mode: str,
    track_idx: int,
    quality: str,
    audio_bitrate: str,
    *,
    decode_hw: bool = False,
    device: str | None = None,
    loglevel: str = "error",
    acodec: str = "aac",
) -> list[str]:
    pre, video, audio = _av_args(
        info, mode, encoder, track_idx, quality, audio_bitrate,
        decode_hw=decode_hw, device=device, acodec=acodec,
    )
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", loglevel,
        *pre, "-ss", f"{start:.3f}", "-i", abs_path,
        "-map", "0:v:0", *video, *audio, "-sn",
        # Fragmented MP4: empty moov up front + one fragment per keyframe so
        # the browser starts playing immediately from a non-seekable pipe.
        "-f", "mp4", "-movflags", "+frag_keyframe+empty_moov", "pipe:1",
    ]


def build_hls_cmd(
    ffmpeg: str,
    abs_path: str,
    info: dict,
    start: float,
    encoder: str | None,
    mode: str,
    track_idx: int,
    quality: str,
    audio_bitrate: str,
    out_dir: str,
    *,
    decode_hw: bool = False,
    device: str | None = None,
    loglevel: str = "error",
    segment_seconds: int = 4,
    list_size: int = 10,
    acodec: str = "aac",
) -> list[str]:
    """Bounded live HLS: real-time read, fMP4 segments, small sliding window."""
    pre, video, audio = _av_args(
        info, mode, encoder, track_idx, quality, audio_bitrate,
        decode_hw=decode_hw, device=device, acodec=acodec,
    )
    return [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", loglevel,
        *pre, "-re", "-ss", f"{start:.3f}", "-i", abs_path,
        "-map", "0:v:0", *video, *audio, "-sn",
        "-f", "hls",
        "-hls_time", str(max(2, int(segment_seconds))),
        "-hls_list_size", str(max(3, int(list_size))),
        "-hls_flags", "delete_segments+independent_segments+temp_file",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", "init.mp4",
        "-hls_segment_filename", os.path.join(out_dir, "seg%05d.m4s"),
        os.path.join(out_dir, "index.m3u8"),
    ]


def build_render_cmd(
    ffmpeg: str,
    abs_path: str,
    info: dict,
    encoder: str | None,
    track_idx: int,
    quality: str,
    audio_bitrate: str,
    out_path: str,
    *,
    decode_hw: bool = False,
    device: str | None = None,
    acodec: str = "aac",
    loglevel: str = "error",
    progress: bool = True,
    mode: str = "transcode",
) -> list[str]:
    """Finite MP4 render used for offline downloads (seekable, +faststart).

    Unlike `/transcode` (a non-seekable pipe) and HLS (a sliding window), this
    writes one complete file: the phone can Range-seek it and the browser cache
    can store it. ``mode="remux"`` copies an already H.264 video stream (the
    common MKV case) so a two-hour film renders in seconds; ``transcode``
    re-encodes when the source codec cannot be copied.
    """
    pre, video, filters = build_video_args(
        info, mode, encoder, quality, decode_hw=decode_hw, device=device,
    )
    audio = build_audio_args(info, track_idx, mode, audio_bitrate, acodec)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", loglevel]
    if progress:
        # Machine-readable progress on stdout (`out_time_us=…`, `progress=end`).
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [*pre, "-i", abs_path, "-map", "0:v:0", *video]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    cmd += [*audio, "-sn", "-movflags", "+faststart", "-f", "mp4", out_path]
    return cmd


class TranscodeLimiter:
    """Small async limiter shared by the pipe and HLS paths."""

    def __init__(self, max_active: int = 2):
        self.max_active = max_active
        self._active = 0
        self._lock: asyncio.Lock | None = None

    def _ensure(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()

    async def acquire(self, timeout: float = 5.0) -> bool:
        self._ensure()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            async with self._lock:  # type: ignore[union-attr]
                if self._active < self.max_active:
                    self._active += 1
                    return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def release(self) -> None:
        self._ensure()
        async with self._lock:  # type: ignore[union-attr]
            self._active = max(0, self._active - 1)

    def snapshot(self) -> dict:
        return {"active": self._active, "max": self.max_active}


LIMITER = TranscodeLimiter(2)


def _list_dri_nodes() -> list[str]:
    try:
        return sorted(
            f"/dev/dri/{name}" for name in os.listdir("/dev/dri") if name.startswith("renderD")
        )
    except OSError:
        return []


def _openable(node: str) -> bool:
    """Open the render node the same way ffmpeg will (not just os.access)."""
    try:
        fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        return False
    os.close(fd)
    return True


class HardwareProbe:
    """Cached, serialised hardware encoder/decoder probe."""

    def __init__(self, ffmpeg_bin: str | None, ffprobe_bin: str | None = None):
        self.ffmpeg = ffmpeg_bin
        self.ffprobe = ffprobe_bin
        self._status: dict | None = None
        self._status_ts = 0.0
        self._lock: asyncio.Lock | None = None
        self._decode: dict[tuple[str, str, str], tuple[float, bool, str]] = {}
        self._hwaccels: list[str] = []
        self._hwaccels_ts = 0.0
        self._version = ""

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _run(self, cmd: list[str], timeout: float) -> tuple[int | None, str, str]:
        """Run a command; return ``(returncode, stdout, stderr_tail)``."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *child_limits.wrap(cmd, mem_mb=settings.CHILD_PROBE_MEM_LIMIT_MB),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:  # pragma: no cover - spawn failures are environmental
            return None, "", f"spawn failed: {exc}"
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return None, "", f"timed out after {timeout:.0f}s"
        out = (stdout or b"").decode("utf-8", "replace")
        tail = (stderr or b"").decode("utf-8", "replace").strip()
        return proc.returncode, out, tail[-MAX_REASON:]

    async def hwaccels(self, refresh: bool = False) -> list[str]:
        if not self.ffmpeg:
            return []
        now = time.monotonic()
        if not refresh and self._hwaccels and now - self._hwaccels_ts < HW_CACHE_TTL:
            return self._hwaccels
        cmd = [self.ffmpeg, "-hide_banner", "-hwaccels"]
        rc, out, _ = await self._run(cmd, timeout=10.0)
        if rc != 0:
            self._hwaccels = []
        else:
            self._hwaccels = [
                line.strip() for line in out.splitlines()
                if line.strip() and not line.strip().endswith(":")
            ]
        self._hwaccels_ts = now
        return self._hwaccels

    async def version(self) -> str:
        if self._version or not self.ffmpeg:
            return self._version
        rc, out, _ = await self._run([self.ffmpeg, "-hide_banner", "-version"], timeout=10.0)
        if rc == 0 and out:
            self._version = out.splitlines()[0].strip()[:160]
        return self._version

    async def _probe(self, args: list[str]) -> tuple[bool, str]:
        cmd = [
            self.ffmpeg or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc2=duration=0.4:size=640x360:rate=30",
            *args, "-f", "null", "-",
        ]
        rc, _, tail = await self._run(cmd, timeout=PROBE_TIMEOUT)
        return rc == 0, tail

    async def _probe_qsv(self, node: str) -> tuple[bool, str]:
        common = [
            "-c:v", "h264_qsv", "-preset", "veryfast", "-b:v", "2M",
            "-g", "30", "-bf", "0", "-pix_fmt", "nv12",
        ]
        variants = [
            ["-qsv_device", node, *common],
            ["-init_hw_device", f"qsv=hw:{node}", "-filter_hw_device", "hw", *common],
        ]
        reasons: list[str] = []
        for variant in variants:
            ok, reason = await self._probe(variant)
            if ok:
                return True, f"Intel QuickSync via {node}"
            reasons.append(reason or "probe exited non-zero")
        return False, " | ".join(r[-300:] for r in reasons if r)[-MAX_REASON:]

    async def _probe_vaapi(self, node: str) -> tuple[bool, str]:
        ok, reason = await self._probe([
            "-vaapi_device", node, "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-b:v", "2M", "-g", "30", "-bf", "0",
        ])
        return ok, (f"VA-API H.264 encode via {node}" if ok else (reason or "probe exited non-zero"))

    async def _probe_software(self) -> tuple[bool, str]:
        ok, reason = await self._probe(["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"])
        return ok, ("" if ok else (reason or "libx264 probe failed"))

    def cached_status(self) -> dict | None:
        """Last probe result without triggering a (slow) probe."""
        return self._status

    async def status(self, refresh: bool = False) -> dict:
        if not self.ffmpeg:
            return {
                "encoder": None, "device": None,
                "reason": "ffmpeg not found on PATH — start the service after installing ffmpeg",
                "probe_version": PROBE_VERSION, "probed_at": int(time.time()),
                "candidates": {}, "decode": {}, "tools": {"ffmpeg": False, "ffprobe": bool(self.ffprobe)},
            }
        now = time.monotonic()
        cached = self._status
        if not refresh and cached and now - self._status_ts < HW_CACHE_TTL:
            return cached
        async with self._ensure_lock():
            now = time.monotonic()
            if not refresh and self._status and now - self._status_ts < HW_CACHE_TTL:
                return self._status

            nodes = _list_dri_nodes()
            usable = [node for node in nodes if _openable(node)]
            hwaccels = await self.hwaccels(refresh=refresh)
            candidates: dict[str, dict] = {}
            result: dict = {
                "encoder": None, "device": usable[0] if usable else None,
                "reason": "", "probe_version": PROBE_VERSION, "probed_at": int(time.time()),
                "candidates": candidates,
                "decode": {
                    "vaapi": bool(usable and "vaapi" in hwaccels),
                    "qsv": bool(usable and "qsv" in hwaccels),
                    "hwaccels": hwaccels,
                },
                "tools": {"ffmpeg": True, "ffprobe": bool(self.ffprobe)},
                "ffmpeg_version": await self.version(),
            }

            if not nodes:
                result["reason"] = "no /dev/dri/renderD* node — no GPU is exposed to this host"
            elif not usable:
                user = getpass.getuser()
                result["reason"] = (
                    f"{nodes[0]} exists but user '{user}' cannot open it (root:render 0660) — run: "
                    f"sudo usermod -aG render,video {user} && sudo systemctl restart ethos-nas"
                )
            else:
                node = usable[0]
                ok, reason = await self._probe_qsv(node)
                candidates["qsv"] = {"ok": ok, "reason": reason, "device": node}
                if ok:
                    result.update(encoder="qsv", device=node, reason=reason)
                else:
                    ok, reason = await self._probe_vaapi(node)
                    candidates["vaapi"] = {"ok": ok, "reason": reason, "device": node}
                    if ok:
                        result.update(encoder="vaapi", device=node, reason=reason)
                    else:
                        result["reason"] = (
                            f"no working hardware H.264 encoder on {node} — using libx264"
                        )
                sw_ok, sw_reason = await self._probe_software()
                candidates["libx264"] = {"ok": sw_ok, "reason": sw_reason or "software fallback"}
                if not sw_ok and result["encoder"] is None:
                    result["reason"] = f"libx264 unavailable: {sw_reason}"

            self._status = result
            self._status_ts = time.monotonic()
            if result["encoder"]:
                logger.info("Videos media engine: %s", result["reason"])
            else:
                logger.info("Videos media engine: software libx264 (%s)", result["reason"])
            for name, candidate in candidates.items():
                if name != "libx264" and not candidate.get("ok"):
                    logger.info("Videos media engine: %s probe failed: %s", name,
                                (candidate.get("reason") or "")[-300:])
            return result

    async def can_decode_hw(self, abs_path: str, info: dict, encoder: str | None,
                            device: str | None) -> tuple[bool, str]:
        if not self.ffmpeg or not device or encoder not in ("vaapi", "qsv"):
            return False, "no hardware decoder"
        codec = (info.get("video_codec") or "").lower()
        key = (encoder, device, codec)
        now = time.monotonic()
        hit = self._decode.get(key)
        if hit and now - hit[0] < DECODE_CACHE_TTL:
            return hit[1], hit[2]
        if encoder == "vaapi":
            hw = ["-vaapi_device", device, "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
        else:
            hw = ["-qsv_device", device, "-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *hw,
            "-i", abs_path, "-t", "0.5", "-f", "null", "-",
        ]
        rc, _, tail = await self._run(cmd, timeout=DECODE_TIMEOUT)
        ok = rc == 0
        reason = "" if ok else (tail or f"decode probe exit {rc}")
        self._decode[key] = (now, ok, reason)
        if not ok:
            logger.info("HW decode unavailable for %s via %s — software decode (%s)",
                        codec or "?", encoder, reason[-300:])
        return ok, reason


def prune_decode_cache(probe: HardwareProbe, max_items: int = 64) -> None:
    """Bound the per-codec decode probe cache (oldest entries first)."""
    if len(probe._decode) <= max_items:
        return
    ordered = sorted(probe._decode.items(), key=lambda kv: kv[1][0])
    for key, _ in ordered[:len(probe._decode) - max_items]:
        probe._decode.pop(key, None)


__all__ = [
    "AAC_STANDARD_LAYOUTS",
    "AUDIO_BITRATES",
    "AUDIO_CODECS",
    "BROWSER_AUDIO_OK",
    "BROWSER_VIDEO_OK",
    "COPY_SAFE_PIX_FMTS",
    "EXPORT_QUALITIES",
    "HardwareProbe",
    "LIMITER",
    "QUALITY_PRESETS",
    "STREAMABLE_EXTS",
    "TranscodeLimiter",
    "aac_layout_filter",
    "bitrate_for",
    "build_audio_args",
    "build_hls_cmd",
    "build_render_cmd",
    "build_transcode_cmd",
    "build_video_args",
    "copy_video_ok",
    "download_mode",
    "prune_decode_cache",
    "select_playback_mode",
]
