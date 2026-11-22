"""Tell the operator when the previous run did not stop cleanly.

A killed service leaves no trace the next process can read from systemd: the
unit's ``Result`` is reset when it starts again, and ``dmesg`` needs privileges
the service user does not have. So the app keeps its own marker:

* written on every startup,
* removed only at the end of a clean lifespan teardown,
* also removed right before an in-place ``os.execv`` restart
  (Settings → Restart skips the teardown),

so a marker still present at startup means the previous process was killed
(systemd-oomd, kernel OOM killer, ``SIGKILL`` on a stuck stop) or the box lost
power. The reason is reported once through the Notification Center.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.startup_guard")

_RUN_DIRNAME = "run"
_MARKER_NAME = "started.json"


def _marker() -> Path:
    return Path(settings.DATA_DIR) / _RUN_DIRNAME / _MARKER_NAME


def _unit_restarts() -> Optional[int]:
    """``NRestarts`` of our own unit (best effort — no privileges needed)."""
    try:
        import subprocess
        out = subprocess.run(
            ["systemctl", "show", settings.SERVICE_UNIT, "--property=NRestarts"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        for line in out.stdout.splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "NRestarts":
                return int(value.strip())
    except Exception:  # noqa: BLE001 — diagnostics only, never fails startup
        logger.debug("Could not read NRestarts", exc_info=True)
    return None


def check_previous_run() -> Optional[dict]:
    """Write this run's marker and return info when the last stop was unclean."""
    marker = _marker()
    previous: Optional[dict] = None
    try:
        if marker.exists():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except ValueError:
                data = {"started_at": None}
            previous = data if isinstance(data, dict) else {"started_at": None}
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "started_at": time.time(),
            "pid": os.getpid(),
        }), encoding="utf-8")
    except OSError:
        logger.warning("Could not write the startup marker — unclean-stop detection off",
                       exc_info=True)
        return None

    if previous is None:
        return None  # first run on this data dir

    restarts = _unit_restarts()
    detail = {"previous_started_at": previous.get("started_at"),
              "n_restarts": restarts}
    logger.warning("Previous run did not shut down cleanly (started %s, NRestarts=%s)",
                   previous.get("started_at"), restarts)
    try:
        from backend.services.notifications_service import emit_event
        extra = f" (systemd restarts: {restarts})" if restarts else ""
        emit_event(
            "warning",
            "EthOS was not shut down cleanly",
            "The previous run ended without a clean stop — it was killed "
            "(out of memory or a stuck stop) or the box lost power" + extra + ".",
            source="system",
            dedupe_key="unclean-shutdown",
        )
    except Exception:  # noqa: BLE001 — a notification must not break startup
        logger.debug("Could not emit the unclean-stop event", exc_info=True)
    return detail


def mark_clean() -> None:
    """Remove the marker: the next start must not report a kill."""
    try:
        _marker().unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove the startup marker", exc_info=True)
