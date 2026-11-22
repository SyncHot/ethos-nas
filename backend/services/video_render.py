"""Finite MP4 renders for offline downloads (Videos PWA).

One job produces one complete, seekable MP4 under
``<DATA_DIR>/video_exports/files/<job>.mp4``: the phone downloads it once and
stores it in Cache Storage, so playback works without the NAS.

Design notes:

* **Own limiter** (:class:`TranscodeLimiter` with one slot) — a background
  render must never starve live playback, which uses the engine's `LIMITER`.
* **Dedupe per owner** — asking twice for the same file/quality reuses the
  finished copy instead of re-encoding.
* **Bounded** — one active job and three stored jobs per user, a total size cap,
  a free-space preflight and a 7-day TTL swept with the other retention areas.
* Failures keep the ffmpeg stderr tail so the UI can say *why*.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from backend.config import settings
from backend.services import json_store, task_registry
from backend.services.video_engine import TranscodeLimiter

logger = logging.getLogger("ethos-nas.services.video_render")

MAX_JOBS_PER_USER = 3
MAX_ACTIVE_PER_USER = 1
TTL_SECONDS = 7 * 86400
MAX_TOTAL_BYTES = 20 * 1024 ** 3  # 20 GiB of prepared downloads
MIN_FREE_BYTES = 2 * 1024 ** 3   # refuse to start below 2 GiB free
WORKER_TIMEOUT = 6 * 3600
MAX_JOBS_KEPT = 200
QUALITY_LABELS = {"fast": "720p", "balanced": "1080p", "high": "source"}


class RenderError(Exception):
    """Base class for render failures the router can map to HTTP."""


class RenderBusy(RenderError):
    """This user already has a render in progress."""


class RenderLimit(RenderError):
    """Too many stored renders for this user."""


class RenderDiskError(RenderError):
    """Not enough free space on the data volume."""


class RenderNotFound(RenderError):
    """Unknown job id (or it belongs to someone else)."""


@dataclass
class RenderJob:
    id: str
    owner: str
    path: str
    quality: str
    audio_idx: int
    source_key: str
    status: str = "queued"  # queued | rendering | done | failed | cancelled
    percent: float = 0.0
    duration: float = 0.0
    size: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def output_name(self) -> str:
        return f"{self.id}.mp4"

    @property
    def active(self) -> bool:
        return self.status in ("queued", "rendering")

    def public(self) -> dict:
        """Shape returned by /api/videos/exports (no server paths)."""
        return {
            "id": self.id,
            "name": Path(self.path).name,
            "quality": self.quality,
            "quality_label": QUALITY_LABELS.get(self.quality, self.quality),
            "audio_idx": self.audio_idx,
            "status": self.status,
            "percent": round(float(self.percent or 0.0), 1),
            "duration": self.duration,
            "size": self.size,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "download_url": f"/api/videos/exports/{self.id}/download" if self.status == "done" else None,
        }


class RenderManager:
    """Queue, run and expire offline renders. One process-wide instance."""

    def __init__(self, *, limiter: TranscodeLimiter | None = None,
                 base_dir: Path | None = None, ttl: float = TTL_SECONDS,
                 max_total_bytes: int = MAX_TOTAL_BYTES,
                 min_free_bytes: int = MIN_FREE_BYTES,
                 max_per_user: int = MAX_JOBS_PER_USER):
        self.base_dir = Path(base_dir) if base_dir else settings.DATA_DIR / "video_exports"
        self.files_dir = self.base_dir / "files"
        self.limiter = limiter or TranscodeLimiter(1)
        self.ttl = ttl
        self.max_total_bytes = max_total_bytes
        self.min_free_bytes = min_free_bytes
        self.max_per_user = max_per_user
        self._store = json_store.get(self.base_dir / "jobs.json", version=1)
        self._jobs: dict[str, RenderJob] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._lock: asyncio.Lock | None = None
        self._load()

    # ── persistence ───────────────────────────────────────────────────────

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load(self) -> None:
        data = self._store.load()
        rows = (data or {}).get("jobs") if isinstance(data, dict) else None
        for row in rows or []:
            try:
                job = RenderJob(**row)
            except TypeError:
                continue
            if job.active:
                # The process died mid-render; the .part file is useless.
                job.status = "failed"
                job.error = "Interrupted by an EthOS restart"
                job.finished_at = time.time()
                with contextlib.suppress(OSError):
                    self._partial_path(job).unlink()
            self._jobs[job.id] = job
        if rows:
            self._persist()

    def _persist(self) -> None:
        # Keep the store bounded: newest first, finished jobs only trimmed.
        jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        if len(jobs) > MAX_JOBS_KEPT:
            keep = jobs[:MAX_JOBS_KEPT]
            for job in jobs[MAX_JOBS_KEPT:]:
                self._jobs.pop(job.id, None)
            jobs = keep
        try:
            self._store.save({"jobs": [asdict(job) for job in jobs]})
        except Exception:  # pragma: no cover - disk problems are logged elsewhere
            logger.warning("Could not persist render jobs", exc_info=True)

    # ── paths ─────────────────────────────────────────────────────────────

    def _output_path(self, job: RenderJob) -> Path:
        return self.files_dir / job.output_name

    def _partial_path(self, job: RenderJob) -> Path:
        return self.files_dir / f"{job.id}.mp4.part"

    def _ensure_dirs(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)

    def _free_bytes(self) -> int:
        try:
            self._ensure_dirs()
            return shutil.disk_usage(self.files_dir).free
        except OSError:
            return 0

    def _stored_bytes(self) -> int:
        total = 0
        for job in self._jobs.values():
            if job.status == "done":
                total += int(job.size or 0)
        return total

    # ── queries ───────────────────────────────────────────────────────────

    def get(self, job_id: str, owner: str) -> RenderJob:
        job = self._jobs.get(job_id)
        if not job or job.owner != owner:
            raise RenderNotFound("Download not found")
        return job

    def list_for(self, owner: str) -> list[RenderJob]:
        return sorted((j for j in self._jobs.values() if j.owner == owner),
                      key=lambda j: j.created_at, reverse=True)

    def download_path(self, job_id: str, owner: str) -> Path:
        job = self.get(job_id, owner)
        if job.status != "done":
            raise RenderNotFound("The download is not ready")
        path = self._output_path(job)
        if not path.is_file():
            job.status = "failed"
            job.error = "The rendered file is gone — download it again"
            self._persist()
            raise RenderNotFound(job.error)
        return path

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self, *, owner: str, abs_path: str, info: dict, quality: str,
                    audio_idx: int, source_key: str,
                    cmd_factory) -> RenderJob:
        """Queue a render (or return the finished copy for the same source)."""
        lock = self._ensure_lock()
        async with lock:
            self.sweep_locked()
            active = [j for j in self._jobs.values() if j.owner == owner and j.active]
            if len(active) >= MAX_ACTIVE_PER_USER:
                raise RenderBusy("Another download is being prepared — wait for it to finish")
            for job in self._jobs.values():
                if (job.owner == owner and job.source_key == source_key
                        and job.status == "done" and self._output_path(job).is_file()):
                    return job
            stored = [j for j in self._jobs.values() if j.owner == owner and j.status == "done"]
            if len(stored) >= self.max_per_user:
                raise RenderLimit(
                    f"You already keep {len(stored)} downloads — delete one first")
            if self._stored_bytes() >= self.max_total_bytes:
                raise RenderDiskError("The download area is full — delete an older download")
            if self._free_bytes() < self.min_free_bytes:
                raise RenderDiskError("Not enough free space on the NAS data volume")
            self._ensure_dirs()
            job = RenderJob(
                id=uuid.uuid4().hex, owner=owner, path=abs_path, quality=quality,
                audio_idx=max(0, int(audio_idx or 0)), source_key=source_key,
                duration=float(info.get("duration") or 0),
            )
            self._jobs[job.id] = job
            self._persist()
        task_registry.spawn(self._run(job, abs_path, info, cmd_factory),
                            name=f"video-render-{job.id}")
        logger.info("Render queued | id=%s owner=%s q=%s input=%s",
                    job.id, owner, quality, abs_path)
        return job

    async def _run(self, job: RenderJob, abs_path: str, info: dict, cmd_factory) -> None:
        out = self._output_path(job)
        part = self._partial_path(job)
        acquired = False
        proc: asyncio.subprocess.Process | None = None
        stderr_tail: deque[bytes] = deque(maxlen=64)
        try:
            if not await self.limiter.acquire(timeout=WORKER_TIMEOUT):
                raise RenderBusy("The renderer stayed busy")
            acquired = True
            cmd = cmd_factory(str(part))
            job.status = "rendering"
            job.started_at = time.time()
            logger.info("Render start | id=%s q=%s input=%s", job.id, job.quality, abs_path)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            self._procs[job.id] = proc
            drain = asyncio.create_task(self._drain(proc.stderr, stderr_tail))
            await self._consume_progress(job, proc.stdout)
            await proc.wait()
            drain.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drain
            if job.status == "cancelled":
                return
            if proc.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
                raise RenderError(
                    f"ffmpeg exited with {proc.returncode}: "
                    + (b"".join(stderr_tail).decode("utf-8", "replace")[-600:] or "no stderr"))
            os.replace(part, out)
            job.status = "done"
            job.percent = 100.0
            job.size = out.stat().st_size
            job.finished_at = time.time()
            logger.info("Render done | id=%s owner=%s size=%d input=%s",
                        job.id, job.owner, job.size, abs_path)
            self._notify(job, "success", "Download ready",
                         f"{Path(abs_path).name} ({job.quality}) is ready to download.")
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)[:600] or "Render failed"
            job.finished_at = time.time()
            logger.error("Render failed | id=%s input=%s: %s", job.id, abs_path, exc,
                         exc_info=not isinstance(exc, RenderError))
            self._notify(job, "error", "Download failed",
                         f"{Path(abs_path).name}: {job.error}")
            with contextlib.suppress(OSError):
                part.unlink()
        finally:
            self._procs.pop(job.id, None)
            if acquired:
                with contextlib.suppress(Exception):
                    await self.limiter.release()
            if job.status == "cancelled":
                with contextlib.suppress(OSError):
                    part.unlink()
            self._persist()

    async def _drain(self, stream: asyncio.StreamReader | None, sink: deque) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = await stream.read(1 << 16)
                if not chunk:
                    break
                sink.append(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.debug("Render stderr drain failed", exc_info=True)

    async def _consume_progress(self, job: RenderJob, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        duration = float(job.duration or 0)
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text or "=" not in text:
                    continue
                key, _, value = text.partition("=")
                if key in ("out_time_us", "out_time_ms") and duration > 0:
                    try:
                        seconds = int(value) / 1_000_000
                    except ValueError:
                        continue
                    job.percent = max(0.0, min(99.9, seconds / duration * 100))
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            logger.debug("Render progress parse failed", exc_info=True)

    def _notify(self, job: RenderJob, kind: str, title: str, message: str) -> None:
        try:
            from backend.services.notifications_service import emit_event
            emit_event(kind, title, message, source="videos", users=[job.owner],
                       action={"label": "Open Videos", "href": "/videos"})
        except Exception:
            logger.debug("Could not emit render notification", exc_info=True)

    async def cancel(self, job_id: str, owner: str) -> RenderJob:
        job = self.get(job_id, owner)
        proc = self._procs.get(job.id)
        job.status = "cancelled"
        job.finished_at = time.time()
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(OSError):
            self._output_path(job).unlink()
        with contextlib.suppress(OSError):
            self._partial_path(job).unlink()
        self._persist()
        logger.info("Render cancelled | id=%s owner=%s", job.id, owner)
        return job

    def delete(self, job_id: str, owner: str) -> None:
        """Forget a finished job and remove its file."""
        job = self.get(job_id, owner)
        with contextlib.suppress(OSError):
            self._output_path(job).unlink()
        with contextlib.suppress(OSError):
            self._partial_path(job).unlink()
        self._jobs.pop(job.id, None)
        self._persist()

    # ── housekeeping ──────────────────────────────────────────────────────

    def sweep_locked(self) -> int:
        """Delete expired jobs and orphan files. Call with the lock held."""
        now = time.time()
        removed = 0
        for job in list(self._jobs.values()):
            if job.active:
                continue
            if job.status == "cancelled" or now - (job.finished_at or job.created_at) > self.ttl:
                with contextlib.suppress(OSError):
                    self._output_path(job).unlink()
                with contextlib.suppress(OSError):
                    self._partial_path(job).unlink()
                self._jobs.pop(job.id, None)
                removed += 1
        # Orphan files (no job row) older than the TTL go too.
        try:
            known = {job.output_name for job in self._jobs.values()}
            for entry in self.files_dir.iterdir():
                if entry.name in known or entry.is_symlink():
                    continue
                if entry.is_file() and now - entry.stat().st_mtime > self.ttl:
                    entry.unlink()
                    removed += 1
        except OSError:
            pass
        if removed:
            self._persist()
        return removed

    def sweep(self) -> int:
        """Expire old downloads. Used by the retention sweeper."""
        return self.sweep_locked()

    async def stop_all(self) -> None:
        for job_id, proc in list(self._procs.items()):
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            job = self._jobs.get(job_id)
            if job and job.active:
                job.status = "failed"
                job.error = "Interrupted by shutdown"
                job.finished_at = time.time()
        self._procs.clear()
        self._persist()

    def snapshot(self) -> dict:
        jobs = list(self._jobs.values())
        return {
            "jobs": len(jobs),
            "active": sum(1 for j in jobs if j.active),
            "done": sum(1 for j in jobs if j.status == "done"),
            "bytes": self._stored_bytes(),
            "free_bytes": self._free_bytes(),
        }


MANAGER = RenderManager()

__all__ = [
    "MANAGER",
    "MAX_ACTIVE_PER_USER",
    "MAX_JOBS_PER_USER",
    "MIN_FREE_BYTES",
    "QUALITY_LABELS",
    "RenderBusy",
    "RenderDiskError",
    "RenderError",
    "RenderJob",
    "RenderLimit",
    "RenderManager",
    "RenderNotFound",
    "TTL_SECONDS",
]
