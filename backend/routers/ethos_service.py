"""EthOS NAS — EthOS Service Manager (self-management)

REST:
- GET  /api/ethos-nas-service/status   — current status of ethos-nas.service
- POST /api/ethos-nas-service/action    — start|stop|restart the service
- POST /api/ethos-nas-service/toggle-enable — toggle auto-start on boot

Uses ``systemctl`` (both system-level and user-level) to manage the EthOS NAS
service. Detects whether running as a user unit or system unit automatically.
"""

import asyncio
import logging
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.config import settings
from backend.services.net_utils import port_is_listening
from backend.routers.auth import require_admin

logger = logging.getLogger("ethos-nas.routers.ethos_service")
# Self-management (start/stop/disable the NAS daemon) is admin-only — this
# router used to be reachable unauthenticated through a public_paths prefix.
router = APIRouter(dependencies=[Depends(require_admin)])

_SERVICE_NAME = settings.SERVICE_UNIT


def _detect_scope() -> str:
    """Detect if ethos-nas is running as a user-level or system-level service.

    Returns ``"user"`` or ``"system"`` based on which level has the unit active.
    Falls back to ``"user"`` since that's our default installation target.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", _SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip() == "active":
            return "user"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # intentional: a failure here must not break the caller

    try:
        result = subprocess.run(
            ["systemctl", "is-active", _SERVICE_NAME],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip() == "active":
            return "system"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # intentional: a failure here must not break the caller

    # Default: user-level (our standard installation)
    return "user"


def _get_systemctl_cmd(scope: str = None) -> list[str]:
    """Build systemctl command prefix based on scope."""
    if scope == "system":
        return ["systemctl"]
    else:
        return ["systemctl", "--user"]


async def _run_systemctl(args: list[str], timeout: int = 15,
                         scope: str = None) -> Optional[dict]:
    """Run systemctl and return {stdout, stderr, returncode}."""
    prefix = _get_systemctl_cmd(scope)
    cmd = prefix + args
    try:
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode,
        }
    except FileNotFoundError:
        logger.warning("systemctl not found")
        return None
    except Exception as exc:
        logger.debug("systemctl error: %s", exc)
        return {"stdout": "", "stderr": str(exc), "returncode": -1}


class ServiceActionRequest(BaseModel):
    action: str
    model_config = {"extra": "forbid"}


@router.get("/status")
async def get_status() -> dict:
    """Return the current status of the EthOS NAS service.

    Checks both user-level and system-level systemctl, plus whether the process
    is actually running on port 9000 as a fallback indicator.
    """
    scope = _detect_scope()
    result = await _run_systemctl(
        ["is-active", _SERVICE_NAME], timeout=5, scope=scope,
    )

    active_state = "inactive"
    if result and result["returncode"] == 0:
        state = result["stdout"].strip()
        if state in ("active", "activating"):
            active_state = "running"
        elif state == "deactivating":
            active_state = "stopping"
        else:
            active_state = state or "inactive"


    # Check if the service file exists at either level
    user_exists = (await asyncio.to_thread(
        subprocess.run,
        ["test", "-f", f"{__import__('os').path.expanduser('~/.config/systemd/user/ethos-nas.service')}"],
        capture_output=True, timeout=5,
    )).returncode == 0

    sys_exists = (await asyncio.to_thread(
        subprocess.run,
        ["test", "-f", "/etc/systemd/system/ethos-nas.service"],
        capture_output=True, timeout=5,
    )).returncode == 0

    # Check enabled state
    enable_result = await _run_systemctl(
        ["is-enabled", _SERVICE_NAME], timeout=5, scope=scope,
    )
    enabled_state = "unknown"
    if enable_result:
        enabled_state = enable_result["stdout"].strip() or "unknown"

    # Secondary indicator: is the service actually listening? The previous
    # `ss -tlnp | grep` call passed the pipe as a literal argv entry, always
    # failed, and the exception was swallowed — the UI reported "stopped" even
    # while the server was serving requests.
    port_9000_open = await asyncio.to_thread(port_is_listening, settings.PORT)

    return {
        "service": _SERVICE_NAME,
        "active_state": active_state,
        "port_9000_open": port_9000_open,
        "scope": scope,
        "user_service_file_exists": user_exists,
        "system_service_file_exists": sys_exists,
        "enabled_state": enabled_state,
    }


@router.post("/action")
async def service_action(body: ServiceActionRequest) -> dict:
    """Start, stop, or restart the EthOS NAS service."""
    if body.action not in ("start", "stop", "restart"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{body.action}'. Must be one of: start, stop, restart",
        )

    scope = _detect_scope()
    result = await _run_systemctl(
        [body.action, _SERVICE_NAME], timeout=15, scope=scope,
    )

    if not result or result["returncode"] != 0:
        err_msg = (result["stderr"] if result else "Command failed")[:300]
        raise HTTPException(
            status_code=503,
            detail=f"Failed to {body.action} {_SERVICE_NAME}: {err_msg}",
        )

    logger.info("systemctl %s %s — scope=%s", body.action, _SERVICE_NAME, scope)
    return {"ok": True, "action": body.action, "service": _SERVICE_NAME, "scope": scope}


@router.post("/toggle-enable")
async def toggle_enable() -> dict:
    """Toggle auto-start (enable/disable) for the EthOS NAS service."""
    scope = _detect_scope()

    # First check current state
    current = await _run_systemctl(
        ["is-enabled", _SERVICE_NAME], timeout=5, scope=scope,
    )
    is_enabled = current and "enabled" in (current["stdout"] or "").lower()

    action = "disable" if is_enabled else "enable"
    result = await _run_systemctl(
        [action, _SERVICE_NAME], timeout=15, scope=scope,
    )

    if not result or result["returncode"] != 0:
        err_msg = (result["stderr"] if result else "Command failed")[:300]
        raise HTTPException(
            status_code=503,
            detail=f"Failed to {action} {_SERVICE_NAME}: {err_msg}",
        )

    new_state = "enabled" if not is_enabled else "disabled"
    logger.info("systemctl %s %s — scope=%s", action, _SERVICE_NAME, scope)
    return {"ok": True, "action": action, "new_state": new_state, "scope": scope}
