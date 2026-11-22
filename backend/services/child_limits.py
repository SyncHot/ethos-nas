"""Limits for the media subprocesses EthOS spawns (ffmpeg, ffprobe).

A runaway 4K x265 encode can allocate several GiB. With swap full the kernel
then picks its OOM victim by size — usually the biggest process on the box, not
the one that misbehaved. Capping the *address space* of the child makes it fail
with a normal non-zero exit and a readable message instead, while ``nice``
keeps a background encode from starving the API.

The cap is applied by a tiny launcher (``-c`` program) instead of
``preexec_fn``: a fork + Python callback in a threaded server can deadlock,
while a plain list spawns through ``posix_spawn``. The launcher ``execs`` the
real binary, so the PID and the exit status still belong to ffmpeg.
"""

import logging
import sys
from typing import Sequence

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.child_limits")

_LAUNCHER = (
    "import os, resource, sys\n"
    "limit = int(sys.argv[1]) << 20\n"
    "resource.setrlimit(resource.RLIMIT_AS, (limit, limit))\n"
    "nice = int(sys.argv[2])\n"
    "if nice:\n"
    "    os.nice(nice)\n"
    "os.execvp(sys.argv[3], sys.argv[3:])\n"
)


def wrap(cmd: Sequence[str], *, mem_mb: int | None = None,
         nice: int | None = None) -> list[str]:
    """Return `cmd` prefixed with the RLIMIT_AS/nice launcher.

    ``mem_mb`` ≤ 0 (or no ``sys.executable``) returns the command unchanged, so
    every call site degrades to today's behaviour instead of failing.
    """
    argv = list(cmd)
    limit = int(mem_mb or 0)
    if limit <= 0 or not sys.executable:
        return argv
    level = settings.CHILD_NICE if nice is None else int(nice)
    return [sys.executable, "-c", _LAUNCHER, str(limit), str(max(0, level)), *argv]


def describe_exit(returncode: int | None, stderr_tail: str = "") -> str:
    """One-line reason for a failed child (signal, exit code, stderr tail)."""
    lines = [line for line in (stderr_tail or "").strip().splitlines() if line.strip()]
    detail = lines[-1][:300] if lines else ""
    if returncode is not None and returncode < 0:
        reason = f"killed by signal {-returncode}"
    else:
        reason = f"exit code {returncode}"
    return f"{reason}: {detail}" if detail else reason


def is_memory_exit(returncode: int | None) -> bool:
    """True when the child most likely hit the address-space limit.

    A refused allocation usually shows up as a normal exit after an ffmpeg
    "Cannot allocate memory" error, or as SIGKILL when the launcher could not
    start at all. The message check keeps the guess honest in logs.
    """
    return returncode is not None and returncode < 0
