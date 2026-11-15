"""Deprecated alias for :mod:`backend.services.log_service`.

Before 2026-11 this module owned a second store (`app_events.jsonl`) that kept
only warnings/errors and could not carry package or browser attribution. The
unified store now lives in ``log_service`` (docs/logs.md); this module stays for
one release so out-of-tree imports and old tests keep working.

Do not add logic here — call ``backend.services.log_service`` directly.
"""

from pathlib import Path

import logging

from backend.services import log_service as _log_service

logger = logging.getLogger("ethos-nas.services.app_log_service")  # shim, kept for the naming guard

# Historic constant kept for callers that imported it.
MAX_ENTRIES = 500
PERSIST_KEEP = 1000


def init(persist_path: Path):
    """Map the legacy `app_events.jsonl` path onto the unified store."""
    path = Path(persist_path)
    if path.suffix == ".jsonl":
        return _log_service.init(path.parent / "logs", legacy_path=path)
    return _log_service.init(path)


get_service = _log_service.get_service
record = _log_service.record
AppLogHandler = _log_service.AppLogHandler
attach_to_logging = _log_service.attach_to_logging

