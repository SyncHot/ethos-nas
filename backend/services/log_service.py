"""EthOS NAS — one unified diagnostic log store.

Every diagnostic message in EthOS lands here: in-process ``ethos-nas.*`` records,
browser errors reported by ``core/logger.js``, and native package messages sent
through ``ctx.log``. The store is a bounded in-memory ring plus an append-only
JSONL file with size-based rotation, so the Logs app can show the recent past
after a restart without ever growing without bound.

Design rules (docs/logs.md):
- ``record()`` never raises — reporting must not break the subsystem reporting it.
- One envelope: ``id, ts, level, app, source, message, detail, count``.
- ``app`` is derived by the caller (``core`` / ``browser`` / ``pkg:<id>``) and can
  never be set by an untrusted client — the router enforces that.
- INFO+ is persisted; DEBUG stays in memory only.
- Credentials are redacted before anything is stored or pushed.
"""

import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("ethos-nas.services.log_service")

LEVELS = ("debug", "info", "warning", "error")
SEVERITY = {"debug": 0, "info": 1, "warning": 2, "error": 3}
PERSIST_LEVELS = frozenset({"info", "warning", "error"})

_MAX_APP = 60
_MAX_SOURCE = 120
_MAX_MESSAGE = 500
_MAX_DETAIL = 4000

# Anything that looks like a credential is scrubbed before storage. The API
# token / password patterns are deliberately conservative: they only touch
# `key=value` pairs and bearer headers, so normal prose is left alone.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)((?:access_)?token\s*=\s*)[^\s&\"']+"), r"\1[redacted]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1[redacted]"),
    (re.compile(r"(?i)(password\s*[\"']?\s*[:=]\s*)[^\s,;\"'}]+"), r"\1[redacted]"),
    (re.compile(r"(?i)(api_key\s*[\"']?\s*[:=]\s*)[^\s,;\"'}]+"), r"\1[redacted]"),
    (re.compile(r"ethos_[a-f0-9]{48}"), "[redacted]"),
)


def redact(text: str) -> str:
    """Scrub credential-shaped substrings from a log message/detail."""
    out = str(text or "")
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out


def _clean(value: object, limit: int) -> str:
    """Strip NULs/control characters (keep tabs/newlines) and cap the length."""
    s = str(value if value is not None else "").replace("\x00", "")
    s = "".join(ch for ch in s if ch >= " " or ch in "\n\t")
    return s[:limit]


class LogService:
    """Bounded ring + rotating JSONL file, safe for worker threads."""

    def __init__(
        self,
        persist_dir: Optional[Path] = None,
        *,
        ring_size: int = 5000,
        max_bytes: int = 1024 * 1024,
        file_count: int = 5,
        dedupe_window: float = 30.0,
    ) -> None:
        self._lock = threading.RLock()
        self._ring_size = max(100, int(ring_size))
        self._max_bytes = max(4096, int(max_bytes))
        self._file_count = max(1, int(file_count))
        self._dedupe_window = max(0.0, float(dedupe_window))

        self._entries: deque[dict] = deque()
        self._ring_ids: set[int] = set()
        self._dedupe: dict[tuple[str, str, str, str], dict] = {}
        self._dropped = 0
        self._listeners: list[Callable[[dict], None]] = []

        self._dir: Optional[Path] = Path(persist_dir) if persist_dir else None
        self._active: Optional[Path] = (self._dir / "events.jsonl") if self._dir else None
        self._bytes = 0

        if self._dir is not None:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                self._load_persisted()
                if self._active is not None and self._active.exists():
                    self._bytes = self._active.stat().st_size
            except Exception:
                logger.warning("log store: cannot initialise at %s", self._dir, exc_info=True)

    # ── capture ────────────────────────────────────────────────────────────

    def record(
        self,
        level: str,
        source: str,
        message: str,
        detail: str = "",
        app: Optional[str] = None,
    ) -> Optional[dict]:
        """Append one event. Never raises; returns the stored entry (or None)."""
        try:
            lvl = level if level in LEVELS else "info"
            app_name = _clean(app or "core", _MAX_APP) or "core"
            src = _clean(source or "app", _MAX_SOURCE)
            msg = redact(_clean(message, _MAX_MESSAGE))
            det = redact(_clean(detail, _MAX_DETAIL))
            now = time.time()
            entry: Optional[dict] = None
            deduped = False

            with self._lock:
                key = (lvl, app_name, src, msg)
                prev = self._dedupe.get(key) if (self._dedupe_window > 0 and msg) else None
                if prev is not None and id(prev) in self._ring_ids and now - prev["ts"] <= self._dedupe_window:
                    prev["ts"] = round(now, 3)
                    prev["count"] = int(prev.get("count", 1)) + 1
                    entry = prev
                    deduped = True
                else:
                    entry = {
                        "id": f"log_{uuid.uuid4().hex[:12]}",
                        "ts": round(now, 3),
                        "level": lvl,
                        "app": app_name,
                        "source": src,
                        "message": msg,
                        "detail": det,
                        "count": 1,
                    }
                    self._append_locked(entry)
                    if self._dedupe_window > 0 and msg:
                        if len(self._dedupe) > self._ring_size * 2:
                            self._dedupe.clear()
                        self._dedupe[key] = entry
                    self._append_disk_locked(entry)

            if entry is not None and deduped and int(entry.get("count", 0)) % 10 == 0:
                # Refresh the persisted copy occasionally so a restart keeps the
                # repeat count without rewriting the file on every duplicate.
                with self._lock:
                    self._append_disk_locked(entry)
            if entry is not None:
                self._notify(entry)
            return dict(entry) if entry is not None else None
        except Exception:  # pragma: no cover — defensive
            logger.debug("log store: record failed", exc_info=True)
            return None

    def drop(self, count: int = 1) -> None:
        """Count events that were rejected (rate limit, oversized batch)."""
        with self._lock:
            self._dropped += max(0, int(count))

    def clear(self) -> int:
        """Empty the ring and the active file. Rotated history is kept."""
        with self._lock:
            cleared = len(self._entries)
            self._entries.clear()
            self._ring_ids.clear()
            self._dedupe.clear()
            if self._active is not None:
                try:
                    self._active.write_text("", encoding="utf-8")
                    self._bytes = 0
                except Exception:
                    logger.debug("log store: clear failed", exc_info=True)
            return cleared

    # ── query ──────────────────────────────────────────────────────────────

    def get(
        self,
        limit: int = 200,
        level: Optional[str] = None,
        app: Optional[str] = None,
        source: Optional[str] = None,
        search: str = "",
        since: Optional[float] = None,
    ) -> list[dict]:
        """Return events newest-first with hierarchical level filtering."""
        with self._lock:
            items = list(self._entries)
        if level in LEVELS:
            min_sev = SEVERITY[level]
            items = [e for e in items if SEVERITY.get(e["level"], 1) >= min_sev]
        if app:
            items = [e for e in items if e["app"] == app or e["app"].startswith(f"{app}:")]
        if source:
            items = [e for e in items if e["source"] == source]
        if since is not None:
            items = [e for e in items if e["ts"] >= float(since)]
        if search:
            q = search.lower()
            items = [
                e for e in items
                if q in (e["message"] + " " + e["detail"] + " " + e["source"] + " " + e["app"]).lower()
            ]
        limit = max(1, min(int(limit), self._ring_size))
        return [dict(e) for e in reversed(items[-limit:])]

    def apps(self) -> list[dict]:
        """Per-app summary for the viewer's source tree."""
        with self._lock:
            summary: dict[str, dict] = {}
            for entry in self._entries:
                item = summary.setdefault(
                    entry["app"],
                    {"app": entry["app"], "count": 0, "last_ts": entry["ts"],
                     "errors": 0, "warnings": 0},
                )
                item["count"] += 1
                item["last_ts"] = max(item["last_ts"], entry["ts"])
                if entry["level"] == "error":
                    item["errors"] += 1
                elif entry["level"] == "warning":
                    item["warnings"] += 1
        return sorted(summary.values(), key=lambda item: item["last_ts"], reverse=True)

    def counts(self) -> dict:
        with self._lock:
            counts = {level: 0 for level in LEVELS}
            for entry in self._entries:
                counts[entry["level"]] = counts.get(entry["level"], 0) + 1
        return counts

    def stats(self) -> dict:
        with self._lock:
            ring = len(self._entries)
            dropped = self._dropped
            disk_bytes = self._bytes
        return {
            "ring": ring,
            "ring_size": self._ring_size,
            "disk_bytes": disk_bytes,
            "max_bytes": self._max_bytes,
            "files": self._file_count,
            "dropped": dropped,
            "counts": self.counts(),
        }

    # ── push (WebSocket bridge) ────────────────────────────────────────────

    def register_listener(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def unregister_listener(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _notify(self, entry: dict) -> None:
        with self._lock:
            listeners = list(self._listeners)
        if not listeners:
            return
        payload = {"type": "entry", "entry": dict(entry)}
        for callback in listeners:
            try:
                callback(payload)
            except Exception:  # noqa: BLE001 — one bad listener cannot stop the rest
                logger.debug("log store: listener failed", exc_info=True)

    # ── persistence ────────────────────────────────────────────────────────

    def import_legacy(self, path: Path) -> int:
        """Import the pre-2026-11 `app_events.jsonl` once, then archive it."""
        legacy = Path(path)
        if not legacy.exists():
            return 0
        imported = 0
        try:
            lines = legacy.read_text(encoding="utf-8", errors="replace").splitlines()[-self._ring_size:]
            for line in lines:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if not isinstance(item, dict) or "message" not in item:
                    continue
                self.record(
                    str(item.get("level", "info")),
                    str(item.get("source", "legacy")),
                    str(item.get("message", "")),
                    str(item.get("detail", "")),
                    app=str(item.get("app") or "core"),
                )
                imported += 1
            archive = (self._dir or legacy.parent) / "legacy-app-events.jsonl"
            if archive.exists():
                archive.unlink()
            legacy.replace(archive)
            logger.info("log store: imported %d legacy app event(s)", imported)
        except Exception:
            logger.warning("log store: legacy import failed", exc_info=True)
        return imported

    def _append_locked(self, entry: dict) -> None:
        if len(self._entries) >= self._ring_size:
            evicted = self._entries.popleft()
            self._ring_ids.discard(id(evicted))
        self._entries.append(entry)
        self._ring_ids.add(id(entry))

    def _append_disk_locked(self, entry: dict) -> None:
        if self._active is None or entry["level"] not in PERSIST_LEVELS:
            return
        try:
            line = json.dumps(
                {key: entry[key] for key in ("ts", "level", "app", "source", "message", "detail", "count")},
                ensure_ascii=False,
            ) + "\n"
            size = len(line.encode("utf-8"))
            if self._bytes + size > self._max_bytes:
                self._rotate_locked()
            with open(self._active, "a", encoding="utf-8") as handle:
                handle.write(line)
            self._bytes += size
        except Exception:
            logger.debug("log store: persist failed", exc_info=True)

    def _rotate_locked(self) -> None:
        if self._dir is None or self._active is None:
            return
        try:
            if self._file_count > 1:
                for index in range(self._file_count, 1, -1):
                    src = self._dir / f"events.{index - 1}.jsonl"
                    dst = self._dir / f"events.{index}.jsonl"
                    if src.exists():
                        src.replace(dst)
            if self._active.exists():
                self._active.replace(self._dir / "events.1.jsonl")
        except Exception:
            logger.debug("log store: rotation failed", exc_info=True)
        finally:
            self._bytes = 0

    def _load_persisted(self) -> None:
        if self._dir is None:
            return
        files: list[Path] = []
        for index in range(self._file_count, 0, -1):
            path = self._dir / f"events.{index}.jsonl"
            if path.exists():
                files.append(path)
        if self._active is not None and self._active.exists():
            files.append(self._active)
        lines: list[str] = []
        for path in files:
            try:
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except Exception:
                logger.debug("log store: cannot read %s", path, exc_info=True)
        for line in lines[-self._ring_size:]:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if not isinstance(item, dict) or "message" not in item:
                continue
            level = str(item.get("level", "info"))
            entry = {
                "id": f"log_{uuid.uuid4().hex[:12]}",
                "ts": float(item.get("ts") or 0.0),
                "level": level if level in LEVELS else "info",
                "app": _clean(item.get("app") or "core", _MAX_APP) or "core",
                "source": _clean(item.get("source", ""), _MAX_SOURCE),
                "message": str(item.get("message", ""))[:_MAX_MESSAGE],
                "detail": str(item.get("detail", ""))[:_MAX_DETAIL],
                "count": int(item.get("count", 1) or 1),
            }
            self._append_locked(entry)


# ── singleton wiring ─────────────────────────────────────────────────────────

_service: Optional[LogService] = None
_service_lock = threading.Lock()


def init(persist_dir: Path, *, legacy_path: Optional[Path] = None, **options) -> LogService:
    """Create the singleton (called once from app startup)."""
    global _service
    with _service_lock:
        if _service is None:
            _service = LogService(persist_dir, **options)
            if legacy_path is not None:
                _service.import_legacy(Path(legacy_path))
        return _service


def get_service() -> LogService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                from backend.config import settings

                _service = LogService(
                    settings.DATA_DIR / "logs",
                    ring_size=settings.LOG_RING_SIZE,
                    max_bytes=settings.LOG_MAX_BYTES,
                    file_count=settings.LOG_FILES,
                    dedupe_window=settings.LOG_DEDUPE_WINDOW,
                )
    return _service


def record(
    level: str,
    source: str,
    message: str,
    detail: str = "",
    app: Optional[str] = None,
) -> Optional[dict]:
    """Convenience wrapper used by routers and services."""
    return get_service().record(level, source, message, detail, app=app)


class AppLogHandler(logging.Handler):
    """Forward Python log records into the unified store."""

    def __init__(self, min_level: int = logging.INFO, skip_prefixes: tuple[str, ...] = ()) -> None:
        super().__init__(min_level)
        self._skip = tuple(skip_prefixes)
        self.ethos_target = ""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < self.level:
                return
            if self._skip and record.name.startswith(self._skip):
                return
            if record.levelno >= logging.ERROR:
                level = "error"
            elif record.levelno >= logging.WARNING:
                level = "warning"
            else:
                level = "info"
            detail = ""
            if record.exc_info:
                import traceback

                detail = "".join(traceback.format_exception(*record.exc_info))[-_MAX_DETAIL:]
            get_service().record(level, record.name or "ethos-nas", record.getMessage(), detail, app="core")
        except Exception:  # pragma: no cover — never break logging
            pass


def attach_to_logging() -> None:
    """Attach the store handler to ethos-nas, uvicorn and root (idempotent)."""

    def _attach(name: str, level: int, skip: tuple[str, ...] = ()) -> None:
        target = logging.getLogger(name) if name else logging.getLogger()
        label = name or "root"
        if any(isinstance(h, AppLogHandler) and h.ethos_target == label for h in target.handlers):
            return
        handler = AppLogHandler(level, skip)
        handler.ethos_target = label
        handler.setFormatter(logging.Formatter("%(message)s"))
        target.addHandler(handler)

    _attach("ethos-nas", logging.INFO)
    _attach("uvicorn", logging.INFO)
    _attach("", logging.WARNING, skip=("ethos-nas", "uvicorn"))
