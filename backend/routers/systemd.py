"""EthOS NAS — Systemd Service Manager Router

REST:
- GET  /api/systemd              — list all systemd services
- POST /api/systemd/{name}/action — start/stop/restart/enable/disable a service

Uses ``systemctl`` with strict whitelist validation on service names.
"""

import asyncio
import logging
import re
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.routers.auth import require_admin

logger = logging.getLogger("ethos-nas.routers.systemd")
router = APIRouter(dependencies=[Depends(require_admin)])

# Regex for validating service names — prevents shell injection
_SERVICE_RE = re.compile(r'^[a-zA-Z0-9_\-\.]+(\.service)?$')

# Whitelist of allowed actions
_ALLOWED_ACTIONS = {"start", "stop", "restart", "enable", "disable", "status"}

# Units owned by another feature: inspectable, but their lifecycle must not be
# changed through this generic router. `systemctl stop ufw.service` unloads the
# firewall without baseline rules, notifications or an audit trail — UFW changes
# go through /api/security (Firewall & Access Control) only.
_PROTECTED_UNITS = {"ufw.service"}


def _normalize_unit(name: str) -> str:
    """Return the canonical unit name (``cron`` → ``cron.service``)."""
    return name if "." in name else f"{name}.service"


class ServiceActionRequest(BaseModel):
    action: str
    model_config = {"extra": "forbid"}


def _run_systemctl(args: list[str], timeout: int = 15) -> Optional[str]:
    """Run systemctl with given args and return stdout, or None on error."""
    cmd = ["systemctl"] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout if result.returncode == 0 else None
    except FileNotFoundError:
        logger.warning("systemctl not found — is systemd available?")
        return None
    except Exception as exc:
        logger.debug("systemctl error: %s", exc)
        return None


def _list_services() -> list[dict]:
    """Parse ``systemctl list-units --type=service --all --no-legend`` output."""
    out = _run_systemctl([
        "list-units", "--type=service", "--all", "--no-legend",
        "--output=json",
    ])
    if not out:
        return []

    import json
    try:
        units = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        # Fallback to plain text parsing
        return _list_services_plain()

    services = []
    for unit in units:
        name = unit.get("unit", unit.get("Unit", ""))
        load = unit.get("load", unit.get("Load", ""))
        active = unit.get("active", unit.get("Active", ""))
        sub = unit.get("sub", unit.get("Sub", ""))
        description = unit.get("description", unit.get("Description", ""))

        services.append({
            "name": name,
            "description": description,
            "load_state": load,
            "active_state": active,
            "sub_state": sub,
        })

    return services


def _list_enabled_states() -> dict[str, str]:
    """Map unit name → boot-enabled state via a single ``list-unit-files`` call.

    Returns an empty dict when systemctl is unavailable or the output cannot be
    parsed — callers treat a missing entry as "unknown" rather than failing.
    """
    out = _run_systemctl([
        "list-unit-files", "--type=service", "--no-legend",
        "--output=json",
    ])
    if not out:
        return {}

    import json
    try:
        files = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return {}

    states: dict[str, str] = {}
    for f in files:
        name = f.get("unit_file", f.get("UnitFile", f.get("unit", "")))
        state = f.get("state", f.get("State", ""))
        if name:
            states[name] = state
    return states

def _list_services_plain() -> list[dict]:
    """Fallback: parse plain-text ``systemctl list-units`` output."""
    out = _run_systemctl([
        "list-units", "--type=service", "--all", "--no-legend",
    ])
    if not out:
        return []

    services = []
    for line in out.strip().split("\n"):
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        services.append({
            "name": parts[0],
            "description": parts[4] if len(parts) > 4 else "",
            "load_state": parts[1],
            "active_state": parts[2],
            "sub_state": parts[3],
        })
    return services


@router.get("")
async def list_services(limit: int = Query(0, ge=0, le=5000),
                        offset: int = Query(0, ge=0)) -> dict:
    """List systemd service units (paginated; `total` is the full count)."""
    services = await asyncio.to_thread(_list_services)
    enabled_map = await asyncio.to_thread(_list_enabled_states)
    for s in services:
        s["enabled_state"] = enabled_map.get(s["name"], "")
        s["protected"] = _normalize_unit(str(s.get("name") or "")) in _PROTECTED_UNITS
    total = len(services)
    return {
        "services": services[offset:offset + limit] if limit else services[offset:],
        "total": total,
        "limit": limit,
        "offset": offset,
        "loaded": sum(1 for s in services if s.get("load_state") == "loaded"),
        "active": sum(1 for s in services if s.get("active_state") == "active"),
    }


@router.post("/{name:str}/action")
async def service_action(name: str, body: ServiceActionRequest) -> dict:
    """Start, stop, restart, enable, or disable a systemd service.

    Args:
        name: Service name (e.g. ``cron`` or ``cron.service``).
        body: JSON with ``action`` field.
    """
    if not _SERVICE_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid service name: {name}",
        )

    action = body.action
    if action not in _ALLOWED_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action '{action}'. Must be one of: {', '.join(sorted(_ALLOWED_ACTIONS))}",
        )

    # For status action, just return current state
    if action == "status":
        # to_thread: systemctl status can take seconds — never block the event loop.
        out = await asyncio.to_thread(_run_systemctl, ["status", name], 10)
        return {"name": name, "action": action, "output": out[:500] if out else "No output"}

    # Protected units: read-only here. The defining feature owns the lifecycle.
    if _normalize_unit(name) in _PROTECTED_UNITS:
        logger.warning("systemd: rejected %s on protected unit %s", action, name)
        raise HTTPException(
            status_code=403,
            detail=(f"{name} is protected — manage the firewall through the "
                    "Firewall & Access Control app (UFW API), not systemd."),
        )

    # Perform the action
    result = await asyncio.to_thread(_run_systemctl, [action, name])

    if result is None:
        # Try to get error output
        try:
            err_result = await asyncio.to_thread(
                subprocess.run, ["systemctl", action, name],
                capture_output=True, text=True, timeout=10,
            )
            err_msg = err_result.stderr[:200] if err_result.stderr else "Unknown error"
        except Exception as exc:
            err_msg = str(exc)
        raise HTTPException(
            status_code=503,
            detail=f"Failed to {action} {name}: {err_msg}",
        )

    logger.info("systemctl %s %s — success", action, name)
    return {"ok": True, "name": name, "action": action}
