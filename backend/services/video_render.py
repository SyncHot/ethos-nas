"""Finite MP4 renders for offline downloads (Videos PWA / Video Station).

One job produces one complete, seekable MP4 under
``<DATA_DIR>/video_exports/files/<job>.mp4``: the phone downloads it once and
stores it in Cache Storage, so playback works without the NAS.

Design notes:

* **Own limiter** (:class:`TranscodeLimiter` with one slot) — a background
  render must never starve live playback, which uses the engine's `LIMITER`.
* **Copy when possible** — ``mode="remux"`` copies an H.264 video stream and
  ``mode="passthrough"`` serves an already-playable MP4 as is, so a download
  usually costs seconds instead of a full re-encode.
* **Real queue** — a user may queue many films; they render one at a time and
  the UI sees a ``queue_position``. Only a rolling window of prepared files is
  kept on disk (``PREPARED_WINDOW``); older copies are reaped.
* **Dedupe per owner** — asking twice for the same file/quality reuses the
  finished (or still running) copy instead of re-encoding.
* **Bounded** — queue size, total area, free-space preflight and a 7-day TTL
  swept with the other retention areas. A stored copy is deleted once the
  device confirms it stored the file (``consume()``).
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
from backend.services import json_store, shutdown_state, task_registry
from backend.services.video_engine import TranscodeLimiter

logger = logging.getLogger("ethos-nas.services.video_render")

MAX_JOBS_PER_USER = 60          # rows kept per owner (history + queue)
MAX_QUEUED_PER_USER = 40        # queued + rendering at any time
PREPARED_WINDOW = 5             # finished files kept per owner
DELIVERED_GRACE = 2 * 3600      # never reap a file served in the last 2 h
TTL_SECONDS = 7 * 86400
CONSUMED_TTL = 86400            # "saved on device" rows drop after a day
MAX_TOTAL_BYTES = 50 * 1024 ** 3  # 50 GiB of prepared downloads
MIN_FREE_BYTES = 5 * 1024 ** 3    # refuse to start below 5 GiB free
WORKER_TIMEOUT = 6 * 3600
MAX_JOBS_KEPT = 300
QUALITY_LABELS = {"tiny": "480p", "fast": "720p", "balanced": "1080p",
                  "high": "source", "original": "Original"}


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
    status: str = "queued"  # queued | rendering | done | failed | cancelled | consumed
    mode: str = "transcode"  # passthrough | remux | transcode
    note: str = ""
    percent: float = 0.0
    duration: float = 0.0
    size: int = 0
    error: str = ""
    queue_position: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    delivered_at: float = 0.0
    consumed_at: float = 0.0

    @property
    def output_name(self) -> str:
        return f"{self.id}.mp4"

    @property
    def active(self) -> bool:
        return self.status in ("queued", "rendering")

    @property
    def passthrough(self) -> bool:
        """The source file is served as is — never delete or overwrite it."""
        return self.mode == "passthrough"

    def public(self) -> dict:
        """Shape returned by /api/videos/exports (owner's own data only)."""
        return {
            "id": self.id,
            "name": Path(self.path).name,
            # The owner already knows this path (`/library` returns it); both
            # UIs need it to match a job to a library item after a reload.
            "path": self.path,
            "quality": self.quality,
            "quality_label": QUALITY_LABELS.get(self.quality, self.quality),
            "mode": self.mode,
            "note": self.note,
            "audio_idx": self.audio_idx,
            "status": self.status,
            "percent": round(float(self.percent or 0.0), 1),
            "duration": self.duration,
            "size": self.size,
            "error": self.error,
            "queue_position": self.queue_position,
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
                 max_per_user: int = MAX_JOBS_PER_USER,
                 max_queued: int = MAX_QUEUED_PER_USER,
                 prepared_window: int = PREPARED_WINDOW):
        self.base_dir = Path(base_dir) if base_dir else settings.DATA_DIR / "video_exports"
        self.files_dir = self.base_dir / "files"
        self.limiter = limiter or TranscodeLimiter(1)
        self.ttl = ttl
        self.max_total_bytes = max_total_bytes
        self.min_free_bytes = min_free_bytes
        self.max_per_user = max_per_user
        self.max_queued = max_queued
        self.prepared_window = max(1, int(prepared_window))
        self._store = json_store.get(self.base_dir / "jobs.json", version=1)
        self._jobs: dict[str, RenderJob] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}
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
            elif job.status == "done" and not job.passthrough:
                # The render completed but the file vanished (manual cleanup,
                # volume unmounted). Make it visible instead of a dead link.
                if not self._output_path(job).is_file():
                    job.status = "failed"
                    job.error = "The rendered file is gone — download it again"
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
            # A passthrough job serves the original file — it uses no extra area.
            if job.status == "done" and not job.passthrough:
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
        path = self._served_path(job)
        if not path.is_file():
            job.status = "failed"
            job.error = "The rendered file is gone — download it again"
            self._persist()
            raise RenderNotFound(job.error)
        return path

    def _served_path(self, job: RenderJob) -> Path:
        """The file `/download` streams: the source for a passthrough job."""
        return Path(job.path) if job.passthrough else self._output_path(job)

    # ── queue helpers (call with the lock held) ───────────────────────────

    def _refresh_positions_locked(self) -> None:
        owners: dict[str, list[RenderJob]] = {}
        for job in self._jobs.values():
            if job.active:
                owners.setdefault(job.owner, []).append(job)
            else:
                job.queue_position = 0
        for jobs in owners.values():
            jobs.sort(key=lambda j: (j.created_at, j.id))
            for position, job in enumerate(jobs, start=1):
                job.queue_position = position

    def _owner_count(self, owner: str) -> int:
        # Terminal failures do not block the queue; only live/ready work counts.
        return sum(1 for j in self._jobs.values()
                   if j.owner == owner and j.status in ("queued", "rendering", "done"))

    def _discard_locked(self, job: RenderJob) -> None:
        """Drop a job row and its files (never the original of a passthrough)."""
        if not job.passthrough:
            with contextlib.suppress(OSError):
                self._output_path(job).unlink()
        with contextlib.suppress(OSError):
            self._partial_path(job).unlink()
        self._jobs.pop(job.id, None)

    def _reap_prepared_locked(self, owner: str) -> int:
        """Keep only the newest ``prepared_window`` ready files for an owner."""
        prepared = sorted((j for j in self._jobs.values()
                           if j.owner == owner and j.status == "done"),
                          key=lambda j: (j.created_at, j.id))
        removed = 0
        now = time.time()
        while len(prepared) > self.prepared_window:
            job = prepared.pop(0)
            if job.delivered_at and now - job.delivered_at < DELIVERED_GRACE:
                continue  # being transferred right now
            self._discard_locked(job)
            removed += 1
        if removed:
            logger.info("Reaped %d old download(s) for %s", removed, owner)
        return removed

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self, *, owner: str, abs_path: str, info: dict, quality: str,
                    audio_idx: int, source_key: str, cmd_factory,
                    mode: str = "transcode", note: str = "") -> RenderJob:
        """Queue a render, or reuse the copy already made (or running) for it."""
        if shutdown_state.is_set():
            raise RenderBusy("The server is shutting down — try again after the restart")
        lock = self._ensure_lock()
        async with lock:
            self.sweep_locked()
            for job in self._jobs.values():
                if job.owner != owner or job.source_key != source_key:
                    continue
                if job.active:
                    return job
                if job.status == "done" and self._served_path(job).is_file():
                    return job
            queued = sum(1 for j in self._jobs.values() if j.owner == owner and j.active)
            if queued >= self.max_queued:
                raise RenderLimit(
                    f"The download queue is full ({self.max_queued}) — remove an item first")
            if self._owner_count(owner) >= self.max_per_user:
                raise RenderLimit(
                    f"You already have {self.max_per_user} downloads — remove one first")
            self._reap_prepared_locked(owner)
            if self._stored_bytes() >= self.max_total_bytes:
                raise RenderDiskError("The download area is full — delete an older download")
            if self._free_bytes() < self.min_free_bytes:
                raise RenderDiskError("Not enough free space on the NAS data volume")
            if mode != "passthrough":
                self._ensure_dirs()
            job = RenderJob(
                id=uuid.uuid4().hex, owner=owner, path=abs_path, quality=quality,
                audio_idx=max(0, int(audio_idx or 0)), source_key=source_key,
                duration=float(info.get("duration") or 0), mode=mode, note=note,
            )
            self._jobs[job.id] = job
            self._refresh_positions_locked()
            self._persist()
        task = task_registry.spawn(self._run(job, abs_path, info, cmd_factory),
                                   name=f"video-render-{job.id}")
        if task is not None:
            self._tasks[job.id] = task
        logger.info("Render queued | id=%s owner=%s q=%s mode=%s pos=%d input=%s",
                    job.id, owner, quality, mode, job.queue_position, abs_path)
        return job

    async def _run(self, job: RenderJob, abs_path: str, info: dict, cmd_factory) -> None:
        out = self._output_path(job)
        part = self._partial_path(job)
        acquired = False
        proc: asyncio.subprocess.Process | None = None
        stderr_tail: deque[bytes] = deque(maxlen=64)
        try:
            if job.passthrough:
                # The source is already a phone-playable MP4: nothing to do.
                job.status = "rendering"
                job.started_at = time.time()
                if not os.path.isfile(abs_path):
                    raise RenderError("The original file is gone")
                job.size = os.stat(abs_path).st_size
                job.percent = 100.0
                job.status = "done"
                job.finished_at = time.time()
                logger.info("Render passthrough | id=%s owner=%s size=%d input=%s",
                            job.id, job.owner, job.size, abs_path)
                return
            if not await self.limiter.acquire(timeout=WORKER_TIMEOUT):
                raise RenderBusy("The renderer stayed busy")
            acquired = True
            if job.status == "cancelled":
                # Cancelled while waiting in the queue — never start ffmpeg.
                return
            if shutdown_state.is_set():
                # Shutdown began while this job waited for the renderer slot:
                # do not spawn a process the unit stop is about to kill.
                raise asyncio.CancelledError
            cmd = cmd_factory(str(part))
            job.status = "rendering"
            job.started_at = time.time()
            logger.info("Render start | id=%s q=%s mode=%s input=%s",
                        job.id, job.quality, job.mode, abs_path)
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
            if job.status == "cancelled" or shutdown_state.is_set():
                # Deliberate stop (cancel or shutdown) — not a failure, no event.
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
            logger.info("Render done | id=%s owner=%s mode=%s size=%d input=%s",
                        job.id, job.owner, job.mode, job.size, abs_path)
        except asyncio.CancelledError:
            job.status = "cancelled"
            raise
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)[:600] or "Render failed"
            job.finished_at = time.time()
            logger.error("Render failed | id=%s input=%s: %s", job.id, abs_path, exc,
                         exc_info=not isinstance(exc, RenderError))
            self._notify_failed(job, abs_path)
            if not job.passthrough:
                with contextlib.suppress(OSError):
                    part.unlink()
        finally:
            self._procs.pop(job.id, None)
            self._tasks.pop(job.id, None)
            if acquired:
                with contextlib.suppress(Exception):
                    await self.limiter.release()
            if job.status == "cancelled":
                with contextlib.suppress(OSError):
                    part.unlink()
            self._refresh_positions_locked()
            if job.status == "done":
                self._reap_prepared_locked(job.owner)
                self._notify_ready(job)
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

    # ── notifications ─────────────────────────────────────────────────────

    def _notify_ready(self, job: RenderJob) -> None:
        """One batched event per owner instead of a toast for every film."""
        try:
            from backend.services.notifications_service import emit_event
            queued = any(j.active for j in self._jobs.values() if j.owner == job.owner)
            ready = sum(1 for j in self._jobs.values()
                        if j.owner == job.owner and j.status == "done")
            if queued:
                return  # more work in the queue — tell the user when it drains
            title = "Download ready" if ready <= 1 else f"{ready} downloads ready"
            message = (f"{Path(job.path).name} ({job.quality}) is ready to save offline."
                       if ready <= 1 else
                       f"{ready} videos are ready to save offline in the Videos app.")
            emit_event("success", title, message, source="videos", users=[job.owner],
                       action={"label": "Open Videos", "href": "/videos"},
                       dedupe_key=f"video-exports-{job.owner}")
        except Exception:
            logger.debug("Could not emit render notification", exc_info=True)

    def _notify_failed(self, job: RenderJob, abs_path: str) -> None:
        try:
            from backend.services.notifications_service import emit_event
            emit_event("error", "Download failed", f"{Path(abs_path).name}: {job.error}",
                       source="videos", users=[job.owner],
                       action={"label": "Open Videos", "href": "/videos"},
                       dedupe_key=f"video-export-{job.id}")
        except Exception:
            logger.debug("Could not emit render notification", exc_info=True)

    # ── user actions ──────────────────────────────────────────────────────

    async def cancel(self, job_id: str, owner: str) -> RenderJob:
        job = self.get(job_id, owner)
        proc = self._procs.get(job.id)
        job.status = "cancelled"
        job.finished_at = time.time()
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        task = self._tasks.get(job.id)
        if task is not None and not task.done():
            # Queued job still waiting for the renderer slot: drop it silently.
            task.cancel()
        if not job.passthrough:
            with contextlib.suppress(OSError):
                self._output_path(job).unlink()
        with contextlib.suppress(OSError):
            self._partial_path(job).unlink()
        self._refresh_positions_locked()
        self._persist()
        logger.info("Render cancelled | id=%s owner=%s", job.id, owner)
        return job

    def delete(self, job_id: str, owner: str) -> None:
        """Forget a finished/failed job and remove its file."""
        job = self.get(job_id, owner)
        self._discard_locked(job)
        self._refresh_positions_locked()
        self._persist()

    def mark_delivered(self, job_id: str, owner: str) -> None:
        """Record that the file was served — protects it from the reaper."""
        job = self.get(job_id, owner)
        job.delivered_at = time.time()
        self._persist()

    def consume(self, job_id: str, owner: str) -> RenderJob:
        """The device stored the file: free the NAS copy, keep a short history row."""
        job = self.get(job_id, owner)
        if job.status != "done":
            raise RenderNotFound("The download is not ready")
        with contextlib.suppress(OSError):
            self._partial_path(job).unlink()
        if not job.passthrough:
            with contextlib.suppress(OSError):
                self._output_path(job).unlink()
        job.status = "consumed"
        job.consumed_at = time.time()
        self._persist()
        logger.info("Render consumed | id=%s owner=%s mode=%s", job.id, owner, job.mode)
        return job

    # ── housekeeping ──────────────────────────────────────────────────────

    def sweep_locked(self) -> int:
        """Delete expired jobs and orphan files. Call with the lock held."""
        now = time.time()
        removed = 0
        for job in list(self._jobs.values()):
            if job.active:
                continue
            expired = now - (job.finished_at or job.created_at) > self.ttl
            consumed = (job.status == "consumed"
                        and job.consumed_at and now - job.consumed_at > CONSUMED_TTL)
            if job.status == "cancelled" or expired or consumed:
                self._discard_locked(job)
                removed += 1
        for owner in {job.owner for job in self._jobs.values()}:
            removed += self._reap_prepared_locked(owner)
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
            # intentional: an unreadable/vanished file is already cleaned up;
            # the sweep must never fail because of one orphan entry.
            logger.debug("Orphan sweep skipped an entry", exc_info=True)
        if removed:
            self._persist()
        return removed

    def sweep(self) -> int:
        """Expire old downloads. Used by the retention sweeper."""
        return self.sweep_locked()

    async def stop_all(self) -> None:
        # Mark first: `_run` re-checks the job status and the shutdown flag after
        # the process exits, so a deliberate stop must not become a failure with
        # a Notification Center toast on every restart.
        for job in list(self._jobs.values()):
            if job.active:
                job.status = "cancelled"
                job.error = "Interrupted by shutdown"
                job.finished_at = time.time()
        for job_id, proc in list(self._procs.items()):
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._procs.clear()
        self._tasks.clear()
        self._persist()

    def snapshot(self) -> dict:
        jobs = list(self._jobs.values())
        return {
            "jobs": len(jobs),
            "active": sum(1 for j in jobs if j.active),
            "queued": sum(1 for j in jobs if j.status == "queued"),
            "done": sum(1 for j in jobs if j.status == "done"),
            "bytes": self._stored_bytes(),
            "free_bytes": self._free_bytes(),
        }


MANAGER = RenderManager()

__all__ = [
    "CONSUMED_TTL",
    "MANAGER",
    "MAX_JOBS_PER_USER",
    "MAX_QUEUED_PER_USER",
    "MIN_FREE_BYTES",
    "PREPARED_WINDOW",
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
