"""EthOS NAS — Firewall & Access Control Router

REST:
- GET    /api/security/status               — aggregate security status
- GET    /api/security/ufw/rules            — list UFW rules
- POST   /api/security/ufw/rules            — add a UFW rule
- DELETE /api/security/ufw/rules/{num}      — delete a UFW rule by number
- POST   /api/security/ufw/enable           — enable UFW
- POST   /api/security/ufw/disable          — disable UFW
- GET    /api/security/failed-logins        — failed logins (?hours=&limit=)
- GET    /api/security/firewall-log         — UFW BLOCK events (?hours=&limit=&external=)
- POST   /api/security/ufw/logging          — enable/disable UFW logging (body {enabled})
- POST   /api/security/bans                 — manual ban (body {ip})
- POST   /api/security/bans/bulk            — ban multiple IPs (body {ips: [...]})
- GET    /api/security/bans                 — list manual bans
- DELETE /api/security/bans/{ip}            — remove manual ban
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.routers.auth import require_admin
from backend.services.access_control_service import (
    SUDO_HINT,
    ban_ip,
    ban_ips,
    get_failed_logins,
    list_bans,
    unban_ip,
    get_firewall_blocks,
    ufw_add_rule,
    ufw_delete_rule,
    ufw_installed,
    ufw_set_enabled,
    ufw_set_logging,
    ufw_status,
)

logger = logging.getLogger("ethos-nas.routers.access_control")
router = APIRouter(dependencies=[Depends(require_admin)])


class UfwRuleCreate(BaseModel):
    action: str = "allow"
    protocol: str = "tcp"
    port: str = ""
    source: str = ""
    comment: str = ""
    model_config = {"extra": "forbid"}


class BanCreate(BaseModel):
    ip: str
    model_config = {"extra": "forbid"}


def _error_detail(e: Exception) -> str:
    detail = str(e)
    low = detail.lower()
    if ("password is required" in low or "interactive authentication" in low) \
            and SUDO_HINT not in detail:
        detail = f"{detail} {SUDO_HINT}"
    return detail


def _raise_http(e: Exception) -> None:
    if isinstance(e, HTTPException):
        raise e
    detail = _error_detail(e)
    logger.error("access_control: %s", detail, exc_info=not isinstance(e, RuntimeError))
    if isinstance(e, RuntimeError):
        raise HTTPException(status_code=400, detail=detail)
    raise HTTPException(status_code=500, detail=detail)


def _notify(kind: str, title: str, message: str = "") -> None:
    """Publish a durable Notification Center event (must never break the request)."""
    try:
        from backend.services.notifications_service import emit_event
        emit_event(kind, title, message, source="firewall")
    except Exception:  # noqa: BLE001 — notifications are best-effort
        logger.warning("firewall: could not emit notification %r", title, exc_info=True)


@router.get("/status")
async def security_status() -> dict:
    def _load() -> dict:
        status: dict = {"ufw": None, "failed_logins_24h": 0,
                        "blocked_packets_24h": 0, "bans_count": 0}
        ufw_state: dict | None = None
        try:
            ufw_state = ufw_status()
            status["ufw"] = {
                "installed": ufw_installed(),
                "enabled": ufw_state["enabled"],
                "logging": ufw_state["logging"],
                "defaults": ufw_state["defaults"],
                "rules_count": len(ufw_state["rules"]),
            }
        except Exception as e:
            logger.error("security status ufw: %s", e)
            status["ufw"] = {"installed": ufw_installed(), "error": _error_detail(e)}
        try:
            status["failed_logins_24h"] = get_failed_logins(hours=24, limit=0)["total_events"]
        except Exception as e:
            logger.error("security status failed_logins: %s", e)
        try:
            # Reuse the rules already fetched above; parses the (cached) kernel log.
            if ufw_state is not None:
                status["bans_count"] = len(list_bans(ufw_state["rules"]))
            status["blocked_packets_24h"] = get_firewall_blocks(
                hours=24, limit=0, ufw_state=ufw_state)["total_events"]
        except Exception as e:
            logger.error("security status firewall log: %s", e)
        return status

    try:
        return await asyncio.to_thread(_load)
    except Exception as e:  # pragma: no cover — defensive
        _raise_http(e)


@router.get("/ufw/rules")
async def list_ufw_rules() -> dict:
    try:
        return await asyncio.to_thread(ufw_status)
    except Exception as e:
        _raise_http(e)


@router.post("/ufw/rules")
async def add_ufw_rule(body: UfwRuleCreate) -> dict:
    try:
        num = await asyncio.to_thread(
            ufw_add_rule,
            action=body.action,
            protocol=body.protocol,
            port=body.port,
            source=body.source,
            comment=body.comment,
        )
    except Exception as e:
        _raise_http(e)
    target = f"{body.port or 'any port'}/{body.protocol}" if body.protocol != "any" else (body.port or "any port")
    logger.info("firewall: rule %s added (%s %s from %s) as #%s",
                body.action, target, body.protocol, body.source or "anywhere", num)
    _notify("info", "Firewall rule added",
            f"{body.action} {target} from {body.source or 'anywhere'}"
            + (f" ({body.comment})" if body.comment else ""))
    return {"status": "added", "num": num, "rule": body.model_dump()}


@router.delete("/ufw/rules/{num}")
async def delete_ufw_rule(num: int) -> dict:
    try:
        await asyncio.to_thread(ufw_delete_rule, num)
    except Exception as e:
        _raise_http(e)
    logger.info("firewall: rule #%s deleted", num)
    _notify("info", "Firewall rule deleted", f"Rule #{num} removed")
    return {"status": "deleted", "num": num}


@router.post("/ufw/enable")
async def enable_ufw() -> dict:
    try:
        await asyncio.to_thread(ufw_set_enabled, True)
    except Exception as e:
        _raise_http(e)
    logger.info("firewall: UFW enabled")
    _notify("success", "Firewall enabled", "Incoming connections are now filtered by UFW.")
    return {"status": "enabled"}


@router.post("/ufw/disable")
async def disable_ufw() -> dict:
    try:
        await asyncio.to_thread(ufw_set_enabled, False)
    except Exception as e:
        _raise_http(e)
    logger.warning("firewall: UFW disabled")
    _notify("warning", "Firewall disabled", "Incoming connections are no longer filtered.")
    return {"status": "disabled"}


@router.get("/failed-logins")
async def failed_logins(
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(50, ge=0, le=500),
) -> dict:
    try:
        return await asyncio.to_thread(get_failed_logins, hours=hours, limit=limit)
    except Exception as e:
        _raise_http(e)


@router.get("/firewall-log")
async def firewall_log(
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(50, ge=0, le=500),
    external: bool = False,
) -> dict:
    try:
        return await asyncio.to_thread(
            get_firewall_blocks, hours=hours, limit=limit, external_only=external)
    except Exception as e:
        _raise_http(e)


class UfwLoggingBody(BaseModel):
    enabled: bool = True
    model_config = {"extra": "forbid"}


@router.post("/ufw/logging")
async def set_ufw_logging(body: UfwLoggingBody) -> dict:
    try:
        await asyncio.to_thread(ufw_set_logging, body.enabled)
    except Exception as e:
        _raise_http(e)
    logger.info("firewall: UFW packet logging %s", "enabled" if body.enabled else "disabled")
    _notify("info", "Firewall logging " + ("enabled" if body.enabled else "disabled"),
            "Blocked packets are recorded" if body.enabled else "Blocked packets are not recorded")
    return {"status": "logging", "enabled": body.enabled}


@router.post("/bans")
async def create_ban(body: BanCreate) -> dict:
    try:
        result = await asyncio.to_thread(ban_ip, body.ip)
    except Exception as e:
        _raise_http(e)
    if result.get("already_banned"):
        logger.info("firewall: ban for %s already present", body.ip)
    else:
        logger.warning("firewall: banned %s", body.ip)
        _notify("warning", "IP banned", f"{body.ip} is blocked by UFW.")
    return result


class BanBulkBody(BaseModel):
    ips: list[str] = Field(min_length=1, max_length=50)
    model_config = {"extra": "forbid"}


@router.post("/bans/bulk")
async def create_bans_bulk(body: BanBulkBody) -> dict:
    try:
        result = await asyncio.to_thread(ban_ips, body.ips)
    except Exception as e:
        _raise_http(e)
    banned = result.get("banned", 0)
    logger.warning("firewall: bulk ban — %s banned, %s already, %s failed",
                   banned, result.get("already", 0), len(result.get("errors") or []))
    if banned:
        _notify("warning", "IPs banned", f"{banned} address(es) blocked by UFW.")
    return result


@router.get("/bans")
async def get_bans() -> dict:
    def _load() -> dict:
        bans = list_bans()
        try:
            enabled = ufw_status()["enabled"]
        except Exception as e:  # noqa: BLE001 — bans are still useful
            logger.warning("firewall: could not read UFW state for bans: %s", e)
            enabled = None
        return {"bans": bans, "total": len(bans), "ufw_enabled": enabled}

    try:
        return await asyncio.to_thread(_load)
    except Exception as e:
        _raise_http(e)


@router.delete("/bans/{ip:path}")
async def delete_ban(ip: str) -> dict:
    """Remove a ban. `{ip:path}` so CIDR bans (a.b.c.0/24) can be unbanned."""
    try:
        removed = await asyncio.to_thread(unban_ip, ip)
    except Exception as e:
        _raise_http(e)
    logger.warning("firewall: unban %s — %s rule(s) removed", ip, removed)
    _notify("info", "IP unbanned", f"{ip} is no longer blocked ({removed} rule(s) removed).")
    return {"status": "unbanned", "ip": ip, "removed": removed}
