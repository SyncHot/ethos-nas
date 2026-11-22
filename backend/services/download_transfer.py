"""HTTP transfer engine for Downloads.

Extracted from `backend/routers/downloads.py` (TODO.md P2-01): part-file
handling, the token-bucket bandwidth limiter, the disk-space guard and the
range-resuming streamer. Everything here is blocking and runs inside the
router's worker threads; no router state is read.
"""

import http.client
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("ethos-nas.services.download_transfer")

MIN_FREE_BYTES = 256 * 1024 * 1024
# Bounds for the in-transfer filesystem check.
_MIN_RUNTIME_FREE_BYTES = 64 * 1024 * 1024
_DISK_CHECK_INTERVAL = 30.0
CHUNK_SIZE = 65536

# Transient network errors worth retrying with a Range resume. socket.timeout
# (an alias of TimeoutError since 3.10) is what "The read operation timed out"
# is; ConnectionError covers resets/broken pipes; HTTPException covers a
# malformed response mid-stream; URLError wraps connection-phase failures.
RETRYABLE_NET_ERRORS = (TimeoutError, ConnectionError, http.client.HTTPException,
                        urllib.error.URLError)
MAX_TRANSFER_RETRIES = 10   # per file, before the task is marked failed
RETRY_BACKOFF_CAP = 30.0    # seconds


def part_path(dest: str) -> str:
    return dest + ".part"


def finalize_part(dest: str) -> None:
    """Atomically move `<dest>.part` to its final name."""
    part = part_path(dest)
    if not os.path.exists(part):
        return
    try:
        os.replace(part, dest)
    except OSError as exc:
        logger.warning("Could not finalize %s: %s", dest, exc)


def resume_source(dest: str) -> int:
    """Bytes resumable for `dest`.

    New transfers write `<dest>.part`; older versions wrote the final name
    directly, so a legacy partial is renamed once and resumed from there.
    """
    part = part_path(dest)
    if not os.path.exists(part) and os.path.exists(dest):
        try:
            os.replace(dest, part)
        except OSError as exc:
            logger.warning("Could not adopt legacy partial %s: %s", dest, exc)
    return os.path.getsize(part) if os.path.exists(part) else 0


class RateLimiter:
    """Token-bucket style minimum spacing between chunk writes (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_at = 0.0

    def consume(self, nbytes: int, bps: int, stop: threading.Event) -> None:
        if bps <= 0 or nbytes <= 0:
            return
        with self._lock:
            now = time.time()
            start = max(now, self._next_at)
            self._next_at = start + nbytes / bps
            wait = self._next_at - now
        if wait > 0 and stop.wait(wait):
            return


GLOBAL_LIMITER = RateLimiter()

# Per-package limiters: every part of one package consumes from the same bucket,
# so a "package speed limit" caps the group instead of each connection.
_SHARED_LIMITERS: dict[str, RateLimiter] = {}
_SHARED_LOCK = threading.Lock()


def shared_limiter(key: str) -> RateLimiter:
    """The process-wide limiter for `key` (one bucket per package)."""
    with _SHARED_LOCK:
        limiter = _SHARED_LIMITERS.get(key)
        if limiter is None:
            limiter = _SHARED_LIMITERS[key] = RateLimiter()
        return limiter


def forget_shared_limiter(key: str) -> None:
    """Drop a package bucket (limit removed); a running part keeps its old one."""
    with _SHARED_LOCK:
        _SHARED_LIMITERS.pop(key, None)


def check_disk_space(task: dict, cfg: dict) -> str:
    """Return a human error when the task's target filesystem is too full."""
    raw = str(task.get("path") or "").strip()
    target = raw if raw and os.path.isdir(raw) else os.path.dirname(raw)
    if not target or not os.path.isdir(target):
        target = str(cfg.get("download_dir") or "")
    try:
        free = shutil.disk_usage(target).free
    except OSError:
        return ""  # unknown filesystem — let the write itself report the error
    need = int(task.get("size") or 0)
    if need and free < need + MIN_FREE_BYTES:
        return (f"Not enough disk space: need {need / 1e9:.1f} GB, "
                f"{free / 1e9:.1f} GB free in {target}")
    if free < MIN_FREE_BYTES:
        return f"Not enough disk space: only {free / 1e6:.0f} MB free in {target}"
    return ""


def stream_to_file(url: str, state: dict, resume_from: int, stop_event: threading.Event,
                   on_chunk=None, on_open=None, limit_bps: int = 0, global_bps: int = 0,
                   limit_key: str = "", headers: dict = None) -> int:
    """Stream `url` into `state["dest"]`, resuming from `resume_from` bytes on disk.

    `state` is a mutable dict holding at least {"dest": str}; callers may change
    state["dest"] from on_open() and the file follows the new path.

    Transient network errors (read timeouts, resets, dropped connections) are
    retried by reopening the connection with an HTTP Range header — a stalled
    multi-GB transfer no longer kills the whole task. Permanent errors (HTTP
    4xx/5xx) propagate immediately.

    on_open(status, content_length, headers, start_pos) is called before the
    file is opened for each attempt; on_chunk(pos) after every chunk written
    (pos = new absolute file position). Returns the final file size in bytes.

    `headers` carries optional provider-resolved request metadata (Referer,
    Cookie) — a multihoster link may require them; they never replace the
    Range/UA headers the engine owns.
    """
    pos = max(0, int(resume_from or 0))
    retries = 0
    last_disk_check = 0.0
    extra_headers = {str(k): str(v) for k, v in (headers or {}).items() if v}
    # A package limit is shared by every part; a per-task limit spaces only this file.
    own_limiter = shared_limiter(limit_key) if limit_key else RateLimiter()
    while True:
        if stop_event.is_set():
            return pos
        request_headers = {"User-Agent": "EthOS-NAS-DownloadStation/2.0", "Accept": "*/*", **extra_headers}
        if pos > 0:
            request_headers["Range"] = f"bytes={pos}-"
        try:
            req = urllib.request.Request(url, headers=request_headers)
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310 — scheme checked by caller
                status = getattr(resp, "status", 200)
                start_pos = pos
                if pos > 0 and status == 200:
                    pos = 0        # server ignored Range → restart from zero
                    start_pos = 0
                content_length = int(resp.headers.get("Content-Length") or 0)
                if on_open is not None:
                    on_open(status, content_length, resp.headers, start_pos)
                mode = "ab" if (start_pos > 0 and status == 206) else "wb"
                expected_total = (start_pos + content_length) if content_length > 0 else 0
                # Write to `<dest>.part` and rename on success, so a growing
                # download is never mistaken for a complete file by Gallery,
                # the file indexer or Radarr/Sonarr.
                part = part_path(state["dest"])
                with open(part, mode) as out:
                    while True:
                        if stop_event.is_set():
                            return pos
                        chunk = resp.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        out.write(chunk)
                        pos += len(chunk)
                        if on_chunk is not None:
                            on_chunk(pos)
                        # Effective bandwidth limits: per-task plus the shared global cap.
                        own_limiter.consume(len(chunk), limit_bps, stop_event)
                        GLOBAL_LIMITER.consume(len(chunk), global_bps, stop_event)
                        # A full disk must fail the task instead of silently
                        # filling the root filesystem. Checked every 30 s.
                        now = time.time()
                        if now - last_disk_check >= _DISK_CHECK_INTERVAL:
                            last_disk_check = now
                            try:
                                free = shutil.disk_usage(os.path.dirname(state["dest"]) or ".").free
                            except OSError:
                                free = None
                            if free is not None and free < _MIN_RUNTIME_FREE_BYTES:
                                raise RuntimeError("Not enough disk space to continue the download")
                # A clean EOF before the advertised length means the server
                # dropped the connection — retry with Range, don't call it done.
                if expected_total > 0 and pos < expected_total:
                    raise ConnectionError(
                        f"connection dropped after {pos - start_pos} of {expected_total - start_pos} bytes")
            finalize_part(state["dest"])
            return pos  # clean EOF at the expected length
        except urllib.error.HTTPError as e:
            if e.code == 416 and pos > 0:
                # Range beyond EOF — the file is already complete on disk.
                finalize_part(state["dest"])
                return pos
            raise  # permanent server-side error — retrying won't help
        except RETRYABLE_NET_ERRORS as exc:
            retries += 1
            if retries > MAX_TRANSFER_RETRIES:
                raise
            logger.warning("Transfer of %s stalled at %d bytes (%s) — retry %d/%d",
                           url, pos, exc, retries, MAX_TRANSFER_RETRIES)
            # Wait (not sleep) so pause/cancel takes effect during the backoff.
            if stop_event.wait(min(RETRY_BACKOFF_CAP, 2.0 * (2 ** (retries - 1)))):
                return pos
