"""Bounded HLS sessions for Video Station.

ffmpeg writes short fMP4 HLS segments into a per-session temp directory while
reading the source in real time (`-re`). The playlist keeps a small sliding
window and deletes old segments, so disk use stays bounded. Sessions are owned
by the authenticated user, expire after inactivity and are killed on shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from backend.config import settings
from backend.services import task_registry
from backend.services.video_engine import LIMITER, TranscodeLimiter

logger = logging.getLogger("ethos-nas.services.video_hls")

MAX_SESSIONS = 2
MAX_PER_USER = 1
IDLE_TTL = 120.0
DIR_TTL = 600.0
START_TIMEOUT = 15.0
SEGMENT_NAME = re.compile(r"^(init\.mp4|seg\d{5}\.m4s)$")
_MAP_URI = re.compile(r'(URI=")([^"]+)(")')


class HlsError(Exception):
    """Base class for HLS session failures."""


class HlsLimitError(HlsError):
    """No session slot is available right now."""


class HlsStartError(HlsError):
    """ffmpeg did not produce a playable playlist."""


@dataclass
class HlsSession:
    sid: str
    owner: str
    path: str
    directory: Path
    playlist: Path
    process: asyncio.subprocess.Process
    mode: str
    encoder: str | None
    decode_hw: bool
    created_at: float
    last_access: float
    stderr: deque = field(default_factory=lambda: deque(maxlen=64))
    drain_task: asyncio.Task | None = None
    limiter_held: bool = True

    def tail(self, limit: int = 2000) -> str:
        return b"".join(self.stderr).decode("utf-8", "replace")[-limit:]


class HlsManager:
    def __init__(self, limiter: TranscodeLimiter = LIMITER, *,
                 max_sessions: int = MAX_SESSIONS, max_per_user: int = MAX_PER_USER,
                 idle_ttl: float = IDLE_TTL, dir_ttl: float = DIR_TTL,
                 start_timeout: float = START_TIMEOUT):
        self.limiter = limiter
        self.max_sessions = max_sessions
        self.max_per_user = max_per_user
        self.idle_ttl = idle_ttl
        self.dir_ttl = dir_ttl
        self.start_timeout = start_timeout
        self._sessions: dict[str, HlsSession] = {}
        self._lock: asyncio.Lock | None = None
        self._sweeper: asyncio.Task | None = None

    def _ensure_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def base_dir(self) -> Path:
        path = settings.DATA_DIR / "video_hls"
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def start(self, owner: str, abs_path: str, cmd_factory: Callable[[str], list[str]],
                    *, mode: str, encoder: str | None, decode_hw: bool = False) -> HlsSession:
        lock = self._ensure_lock()
        async with lock:
            self._prune_locked()
            if len(self._sessions) >= self.max_sessions:
                raise HlsLimitError("Too many active video streams — stop one and retry")
            if sum(1 for s in self._sessions.values() if s.owner == owner) >= self.max_per_user:
                raise HlsLimitError("You already have an active video stream")

        if not await self.limiter.acquire(timeout=5.0):
            raise HlsLimitError("Transcoder is busy — try again in a moment")

        sid = uuid.uuid4().hex
        directory = self.base_dir() / sid
        try:
            directory.mkdir(parents=True, exist_ok=False)
            cmd = cmd_factory(str(directory))
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            shutil.rmtree(directory, ignore_errors=True)
            await self.limiter.release()
            logger.error("HLS start failed | input=%s: %s", abs_path, exc, exc_info=True)
            raise HlsStartError(f"Could not start stream: {exc}") from exc

        now = time.monotonic()
        session = HlsSession(
            sid=sid, owner=owner, path=abs_path, directory=directory,
            playlist=directory / "index.m3u8", process=proc, mode=mode,
            encoder=encoder, decode_hw=decode_hw, created_at=now, last_access=now,
        )
        session.drain_task = asyncio.create_task(self._drain(session))
        try:
            await self._wait_playlist(session)
        except HlsStartError:
            await self._terminate(session, remove=True)
            raise

        async with lock:
            self._sessions[sid] = session
        self._ensure_sweeper()
        logger.info(
            "HLS session started | id=%s owner=%s mode=%s encoder=%s decode=%s input=%s",
            sid, owner, mode, encoder or "none", "hw" if decode_hw else "sw", abs_path,
        )
        return session

    async def _drain(self, session: HlsSession) -> None:
        assert session.process.stderr is not None
        try:
            while True:
                chunk = await session.process.stderr.read(1 << 16)
                if not chunk:
                    break
                session.stderr.append(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("HLS stderr drain failed", exc_info=True)

    async def _wait_playlist(self, session: HlsSession) -> None:
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            text = ""
            try:
                text = session.playlist.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if text.startswith("#EXTM3U") and "#EXTINF" in text:
                return
            if session.process.returncode is not None:
                # ffmpeg may exit (short file) just as the playlist is renamed;
                # give the rename a moment before declaring failure.
                await asyncio.sleep(0.25)
                try:
                    text = session.playlist.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if text.startswith("#EXTM3U") and "#EXTINF" in text:
                    return
                await asyncio.sleep(0.05)
                raise HlsStartError(
                    f"ffmpeg exited before the first segment: {session.tail(1200) or 'unknown error'}")
            await asyncio.sleep(0.1)
        raise HlsStartError(f"Timed out preparing the stream: {session.tail(1200) or 'no playlist'}")

    async def get(self, sid: str, owner: str) -> HlsSession | None:
        lock = self._ensure_lock()
        async with lock:
            session = self._sessions.get(sid)
            if not session or session.owner != owner:
                return None
            session.last_access = time.monotonic()
            return session

    async def stop(self, sid: str, owner: str) -> bool:
        lock = self._ensure_lock()
        async with lock:
            session = self._sessions.get(sid)
            if not session or session.owner != owner:
                return False
            self._sessions.pop(sid, None)
        await self._terminate(session, remove=True)
        logger.info("HLS session stopped | id=%s owner=%s", sid, owner)
        return True

    async def stop_all(self) -> None:
        lock = self._ensure_lock()
        async with lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await self._terminate(session, remove=True)
        if self._sweeper and not self._sweeper.done():
            self._sweeper.cancel()
        self._sweeper = None

    async def _terminate(self, session: HlsSession, *, remove: bool) -> None:
        if session.drain_task and not session.drain_task.done():
            session.drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await session.drain_task
        if session.process.returncode is None:
            with contextlib.suppress(Exception):
                session.process.kill()
        with contextlib.suppress(Exception):
            await session.process.wait()
        if session.limiter_held:
            session.limiter_held = False
            with contextlib.suppress(Exception):
                await self.limiter.release()
        if remove:
            await asyncio.to_thread(shutil.rmtree, session.directory, ignore_errors=True)

    def _prune_locked(self) -> None:
        """Drop sessions whose ffmpeg exited and that sat idle past the TTL."""
        if not self._sessions:
            return
        now = time.monotonic()
        for sid, session in list(self._sessions.items()):
            if session.process.returncode is None:
                continue
            if now - session.last_access > self.idle_ttl:
                self._sessions.pop(sid, None)
                task_registry.spawn(self._terminate(session, remove=True),
                                    name=f"video-hls-stop-{sid}")

    def _ensure_sweeper(self) -> None:
        if self._sweeper and not self._sweeper.done():
            return
        self._sweeper = task_registry.spawn(self._sweep_forever(), name="video-hls-sweeper")

    async def _sweep_forever(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("HLS sweeper pass failed", exc_info=True)

    async def sweep_once(self) -> None:
        now = time.monotonic()
        lock = self._ensure_lock()
        async with lock:
            expired = [
                session for session in self._sessions.values()
                if now - session.last_access > self.idle_ttl
            ]
            for session in expired:
                self._sessions.pop(session.sid, None)
        for session in expired:
            await self._terminate(session, remove=True)
            logger.info("HLS session expired | id=%s owner=%s", session.sid, session.owner)
        await self._sweep_orphans(now)

    async def _sweep_orphans(self, now: float) -> None:
        base = self.base_dir()

        def _prune() -> None:
            try:
                entries = list(base.iterdir())
            except OSError:
                return
            for entry in entries:
                try:
                    if entry.is_dir() and entry.name not in self._sessions:
                        age = now - entry.stat().st_mtime
                        if age > self.dir_ttl:
                            shutil.rmtree(entry, ignore_errors=True)
                except OSError:
                    continue

        await asyncio.to_thread(_prune)


class _NullSuppress:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, Exception)


def contextlib_suppress() -> _NullSuppress:
    return _NullSuppress()


def rewrite_playlist(text: str, token: str | None) -> str:
    """Append ``?token=`` to segment/init URIs when the caller authenticated by query.

    Same-origin cookie auth needs no rewrite; API-key clients (Android) request
    the playlist with ``?token=`` and their segment requests must carry it too.
    """
    if not token:
        return text
    suffix = f"?token={quote(token, safe='')}"
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#EXT-X-MAP:"):
            line = _MAP_URI.sub(lambda m: f"{m.group(1)}{m.group(2)}{suffix}{m.group(3)}", line)
        elif stripped and not stripped.startswith("#"):
            line = f"{line}{suffix}"
        out.append(line)
    return "\n".join(out) + "\n"


def segment_path(session: HlsSession, name: str) -> Path | None:
    """Resolve an allowlisted segment/init filename inside the session dir."""
    if not SEGMENT_NAME.match(name or ""):
        return None
    target = session.directory / name
    try:
        if target.resolve().parent != session.directory.resolve():
            return None
    except OSError:
        return None
    return target if target.is_file() else None


MANAGER = HlsManager(LIMITER)

__all__ = [
    "HlsError",
    "HlsLimitError",
    "HlsManager",
    "HlsSession",
    "HlsStartError",
    "MANAGER",
    "rewrite_playlist",
    "segment_path",
]
