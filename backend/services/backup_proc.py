"""Subprocess plumbing shared by the backup engine, Docker and rclone helpers.

Argument lists only (never `shell=True`), one cancellable operation at a time.
`OperationHandle` is the single mutable state the API returns, and it also owns
the currently running child so `POST /api/backup/cancel` can stop it.
"""

import asyncio
import logging
import re
import time
from typing import Callable, Optional

logger = logging.getLogger("ethos-nas.services.backup_proc")

# rsync progress updates are separated by \r, log lines by \n.
_LINE_BREAK_RE = re.compile(r"[\r\n]")


class OpCancelled(Exception):
    """Raised when the operator cancels a backup/restore/verify operation."""


class OperationHandle:
    """Progress + cancellation for one queued backup operation."""

    def __init__(self, op_id: str, kind: str, schedule: dict, trigger: str) -> None:
        self.op_id = op_id
        self.kind = kind
        self.schedule_id = schedule.get("id")
        self.schedule_name = schedule.get("name", "")
        self.trigger = trigger
        self.started_at = _now()
        self.status = "queued"
        self.phase = "queued"
        self.detail = ""
        self.percent: Optional[float] = None
        self.bytes_done = 0
        self.bytes_total = 0
        self.files_done = 0
        self.files_total = 0
        self.generation: Optional[str] = None
        self.error = ""
        self.cancel_requested = False
        self.proc: Optional[asyncio.subprocess.Process] = None

    # ── progress ─────────────────────────────────────────────────────────
    def set_phase(self, phase: str, detail: str = "") -> None:
        self.phase = phase
        self.detail = (detail or "")[:300]
        self.status = "running"

    def set_progress(self, percent=None, bytes_done=None, bytes_total=None,
                     files_done=None, files_total=None) -> None:
        if percent is not None:
            self.percent = max(0.0, min(100.0, float(percent)))
        if bytes_done is not None:
            self.bytes_done = max(0, int(bytes_done))
        if bytes_total is not None:
            self.bytes_total = max(0, int(bytes_total))
        if files_done is not None:
            self.files_done = max(0, int(files_done))
        if files_total is not None:
            self.files_total = max(0, int(files_total))

    def snapshot(self) -> dict:
        return {
            "op_id": self.op_id,
            "kind": self.kind,
            "schedule_id": self.schedule_id,
            "schedule_name": self.schedule_name,
            "trigger": self.trigger,
            "status": self.status,
            "phase": self.phase,
            "detail": self.detail,
            "percent": self.percent,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "files_done": self.files_done,
            "files_total": self.files_total,
            "generation": self.generation,
            "error": self.error,
            "started_at": self.started_at,
            "cancel_requested": self.cancel_requested,
        }

    # ── cancellation ─────────────────────────────────────────────────────
    def attach(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        if self.cancel_requested:
            _terminate(proc)

    def detach(self, proc: asyncio.subprocess.Process) -> None:
        if self.proc is proc:
            self.proc = None

    def check(self) -> None:
        if self.cancel_requested:
            raise OpCancelled("Cancelled by the operator")

    def cancel(self) -> None:
        self.cancel_requested = True
        if self.proc is not None:
            _terminate(self.proc)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _terminate(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:  # intentional: process already exited
        return


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    _terminate(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:  # intentional: process already exited
            pass


async def run(args: list[str], *, op: Optional[OperationHandle] = None,
              capture: bool = True, stdout_null: bool = False) -> tuple[int, bytes, bytes]:
    """Run a child process; returns (returncode, stdout, stderr)."""
    if op is not None:
        op.check()
    kwargs = {
        "stdout": asyncio.subprocess.DEVNULL if stdout_null or not capture else asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
    }
    logger.debug("backup: exec %s", " ".join(args))
    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
    if op is not None:
        op.attach(proc)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        await _stop(proc)
        raise
    finally:
        if op is not None:
            op.detach(proc)
    return proc.returncode or 0, out or b"", err or b""


async def run_streaming(args: list[str], *, op: OperationHandle,
                        on_line: Callable[[str], None]) -> tuple[int, str]:
    """Run a child and feed every stdout/stderr line to `on_line` as it arrives.

    Used for `rsync --info=progress2`, where the UI needs live percentages.
    Returns (returncode, combined output).
    """
    op.check()
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    op.attach(proc)
    chunks: list[str] = []

    async def pump(stream: Optional[asyncio.StreamReader]) -> None:
        if stream is None:
            return
        buffer = ""
        while True:
            raw = await stream.read(4096)
            if not raw:
                break
            buffer += raw.decode(errors="replace")
            # rsync --info=progress2 terminates updates with \r, not \n.
            parts = _LINE_BREAK_RE.split(buffer)
            buffer = parts.pop()
            for line in parts:
                if not line.strip():
                    continue
                chunks.append(line)
                try:
                    on_line(line)
                except Exception:  # intentional: a parser bug must not kill the backup
                    logger.debug("backup: progress parser failed for %r", line, exc_info=True)
        if buffer.strip():
            chunks.append(buffer)
            try:
                on_line(buffer)
            except Exception:  # intentional: a parser bug must not kill the backup
                logger.debug("backup: progress parser failed for %r", buffer, exc_info=True)

    try:
        await asyncio.gather(pump(proc.stdout), pump(proc.stderr))
        await proc.wait()
    except asyncio.CancelledError:
        await _stop(proc)
        raise
    finally:
        op.detach(proc)
    return proc.returncode or 0, "\n".join(chunks)
