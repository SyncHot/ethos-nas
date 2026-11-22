"""H.264 transcode cache (make HEVC/AV1 files playable in the browser).

Browsers cannot decode HEVC/AV1 8K streams; transcoding to H.264 ≤1080p makes
any video playable in the built-in player. One ffmpeg run at a time (a
semaphore serialises jobs — a second file waits as ``queued``), output cached
under ``<DATA_DIR>/transcodes`` with the same mtime+size sidecar meta as the
thumbnail cache, so a finished transcode survives a restart.

Extracted from ``routers/files.py`` (TODO.md P2-03); the router keeps the HTTP
endpoints and delegates here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import subprocess
from pathlib import Path

from backend.config import settings
from backend.services import media_cache, task_registry

logger = logging.getLogger("ethos-nas.services.transcode")

TRANSCODE_DIR = media_cache.TRANSCODE_DIR  # compat alias; see media_cache.set_cache_root
_cache_is_fresh = media_cache.cache_is_fresh
_write_thumb_meta = media_cache.write_thumb_meta
_probe_duration = media_cache.probe_duration

_SEM = asyncio.Semaphore(1)  # one transcode at a time on this CPU
_jobs: dict[str, dict] = {}   # abs_path → {status, progress, error}


def _transcode_key(abs_path: str, height: int) -> str:
    return hashlib.sha256(f"tc:{abs_path}:{height}".encode()).hexdigest()[:16]


def _transcoded_file(abs_path: str, height: int = 720) -> Path:
    stem = os.path.splitext(os.path.basename(abs_path))[0][:80]
    return media_cache.TRANSCODE_DIR / f"{_transcode_key(abs_path, height)}_{stem}.mp4"


def _transcode_is_fresh(out_file: Path, abs_path: str) -> bool:
    """True if the transcoded file matches the current source (mtime+size)."""
    return out_file.exists() and out_file.stat().st_size > 0 \
        and _cache_is_fresh(out_file, abs_path)


def _probe_codec(abs_path: str) -> str | None:
    """Return the first video codec name, or None."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", abs_path],
            capture_output=True, text=True, timeout=8,
        )
        line = (probe.stdout or "").strip().splitlines()
        return line[0].strip() if line else None
    except Exception:
        return None


def _probe_video_size(abs_path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream; (0, 0) on failure."""
    with contextlib.suppress(Exception):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "default=noprint_wrappers=1:nokey=1", abs_path],
            capture_output=True, text=True, timeout=8,
        )
        for line in (probe.stdout or "").strip().splitlines():
            if "," in line:
                w_s, h_s = line.split(",")
                return int(w_s), int(h_s)
    return 0, 0


def _transcode_worker(abs_path: str, height: int):
    """Run ffmpeg transcode in a worker thread. Updates job dict."""
    job = _jobs[abs_path]
    out_file = _transcoded_file(abs_path, height)
    tmp_file = out_file.with_suffix(".part.mp4")
    media_cache.TRANSCODE_DIR.mkdir(parents=True, exist_ok=True)

    # Duration for progress reporting (best-effort).
    duration = _probe_duration(abs_path) or 0.0

    # Compute output dimensions in Python (no ffmpeg expression escaping):
    # cap at `height` px tall and 1920 px wide, never upscale, keep even dims.
    iw, ih = _probe_video_size(abs_path)
    vf: str | None = None
    if iw > 0 and ih > 0:
        tw, th = iw, ih
        if ih > height:
            th = height
            tw = max(2, (iw * height // ih) & ~1)
        elif iw > 1920:
            tw = 1920
            th = max(2, (ih * 1920 // iw) & ~1)
        if (tw, th) != (iw, ih):
            vf = f"scale={tw}:{th}"

    cmd = [
        "nice", "-n", "19",
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
        "-i", abs_path,
        "-map", "0:v:0", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", "-maxrate", "8M", "-bufsize", "16M",
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-map", "0:a?", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-y", str(tmp_file),
    ]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            if "time=" in line and duration > 0:
                # Best effort — malformed ffmpeg output lines must not kill the job.
                with contextlib.suppress(Exception):
                    t = line.split("time=", 1)[1].split()[0]
                    h, m, s = (x.split(":") for x in [t])
                    cur = int(h[0]) * 3600 + int(m) * 60 + float(s)
                    job["progress"] = min(99.0, round(cur / duration * 100, 1))
        proc.wait()
        if proc.returncode == 0 and tmp_file.exists() and tmp_file.stat().st_size > 0:
            tmp_file.rename(out_file)
            _write_thumb_meta(out_file, abs_path)
            job["status"] = "done"
            job["progress"] = 100.0
            logger.info("Transcode complete: %s → %s", abs_path, out_file.name)
        else:
            job["status"] = "error"
            job["error"] = f"ffmpeg exited {proc.returncode}"
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        if tmp_file.exists():
            with contextlib.suppress(OSError):
                tmp_file.unlink()


# ── Service API (used by routers/files.py) ──────────────────────────────────

def output_file(abs_path: str, height: int = 720) -> Path:
    """Cache path of the transcode for ``abs_path`` at ``height``."""
    return _transcoded_file(abs_path, height)


def output_is_fresh(abs_path: str, height: int = 720) -> bool:
    """True when a finished transcode matches the current source."""
    return _transcode_is_fresh(_transcoded_file(abs_path, height), abs_path)


def job_state(abs_path: str) -> dict | None:
    """Current in-memory job for a source path (None when idle)."""
    return _jobs.get(abs_path)


def status_payload(abs_path: str, height: int = 720) -> dict:
    """Report transcode state: done (on disk) → running/queued → idle."""
    if output_is_fresh(abs_path, height):
        return {"status": "done", "progress": 100.0, "path": abs_path}

    job = _jobs.get(abs_path)
    if job is None or job.get("status") == "done":
        # No active job. A completed job with a stale output means the source
        # changed after transcoding — report idle so clients re-request.
        return {"status": "idle", "progress": 0.0, "path": abs_path}

    return {
        "status": job.get("status", "running"),
        "progress": job.get("progress", 0.0),
        "error": job.get("error"),
        "path": abs_path,
    }


def active(abs_path: str) -> str | None:
    """Return 'running'/'queued' while a job is in flight, else None."""
    job = _jobs.get(abs_path)
    if job is not None and job.get("status") in ("running", "queued"):
        return str(job["status"])
    return None


async def start(abs_path: str, height: int = 720) -> dict:
    """Start (or report) a transcode job for ``abs_path``."""
    if output_is_fresh(abs_path, height):
        return {"status": "done", "progress": 100.0, "path": abs_path}

    running = active(abs_path)
    if running is not None:
        job = _jobs[abs_path]
        return {"status": running, "progress": job.get("progress", 0.0),
                "path": abs_path}

    # (Re)start the worker; the semaphore serialises actual ffmpeg runs, so a
    # second file's job waits as "queued" until the first finishes.
    _jobs[abs_path] = {"status": "running", "progress": 0.0}

    async def _run():
        try:
            async with _SEM:
                await asyncio.to_thread(_transcode_worker, abs_path, height)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Transcode job failed: %s", exc, exc_info=True)
            j = _jobs.get(abs_path)
            if j is not None and j.get("status") != "done":
                j["status"] = "error"
                j["error"] = str(exc)

    task_registry.spawn(_run(), name="file-transcode")
    return {"status": "running", "progress": 0.0, "path": abs_path}
