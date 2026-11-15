"""EthOS NAS — raw iptables inspection router (read-only).

REST:
- GET    /api/network/firewall          — list raw iptables rules (inspection)
- GET    /api/network/firewall/status   — iptables/nftables service status

The *write* endpoints were removed: manipulating iptables policies directly
bypassed UFW and could leave the host unfiltered (or lock the UI out). All
firewall changes go through `/api/security/*` (UFW), see
`routers/access_control.py`.
"""

import asyncio
import logging
import subprocess

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.routers.auth import require_admin
from backend.services.firewall_service import (
    run_iptables_save, check_nftables, parse_rule_line,
)

logger = logging.getLogger("ethos-nas.routers.firewall")
router = APIRouter(dependencies=[Depends(require_admin)])


class FirewallRuleCreate(BaseModel):
    """Kept so the deprecated POST keeps a documented request body."""

    chain: str = "INPUT"
    protocol: str = "tcp"
    source: str = "0.0.0.0/0"
    destination: str = "0.0.0.0/0"
    dport: str = ""
    sport: str = ""
    action: str = "ACCEPT"
    comment: str = ""
    model_config = {"extra": "forbid"}


def _raise_http(e: Exception) -> None:
    if isinstance(e, HTTPException): raise e
    if isinstance(e, RuntimeError): raise HTTPException(status_code=400, detail=str(e))
    raise HTTPException(status_code=500, detail=str(e))


@router.get("/firewall")
async def list_rules() -> dict:
    try:
        ruleset = run_iptables_save()
        rules, policies = [], {}
        for line in ruleset.split("\n"):
            parsed = parse_rule_line(line)
            if parsed: rules.append(parsed)
            elif line.startswith(":"):
                parts = line.split()
                if len(parts) >= 2: policies[parts[0].lstrip(":")] = parts[1]
        return {"rules": rules, "total": len(rules), "policies": policies, "nftables_active": check_nftables()}
    except Exception as e:
        _raise_http(e)


@router.post("/firewall")
async def add_rule(body: FirewallRuleCreate) -> dict:
    """Deprecated — raw iptables writes conflict with UFW."""
    logger.warning("network firewall: rejected raw iptables rule add (UFW is authoritative)")
    raise HTTPException(
        status_code=409,
        detail="Raw iptables rules are not supported — use the UFW endpoints "
               "(/api/security/ufw/rules) in the Firewall & Access Control app.",
    )


@router.delete("/firewall/{rule_id}")
async def delete_rule(rule_id: str) -> dict:
    """Deprecated — raw iptables writes conflict with UFW."""
    logger.warning("network firewall: rejected raw iptables delete of %s", rule_id)
    raise HTTPException(
        status_code=409,
        detail="Raw iptables rules are not supported — use the UFW endpoints "
               "(/api/security/ufw/rules) in the Firewall & Access Control app.",
    )


@router.get("/firewall/status")
async def firewall_status() -> dict:
    status = {"active": False, "enabled": False, "using_nftables": check_nftables()}
    svc = "nftables" if check_nftables() else "iptables"
    # systemctl talks to systemd over D-Bus — a worker thread keeps the UI
    # responsive while it answers (and these used to fail completely silently).
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["systemctl", "is-active", svc],
            capture_output=True, text=True, timeout=5,
        )
        status["active"] = r.stdout.strip() == "active"
    except Exception as exc:
        logger.warning("firewall status: is-active %s failed: %s", svc, exc)
    try:
        r = await asyncio.to_thread(
            subprocess.run, ["systemctl", "is-enabled", svc],
            capture_output=True, text=True, timeout=5,
        )
        status["enabled"] = r.stdout.strip() == "enabled"
    except Exception as exc:
        logger.warning("firewall status: is-enabled %s failed: %s", svc, exc)
    try:
        rules = await asyncio.to_thread(run_iptables_save)
        status["rules_count"] = sum(1 for line in rules.split("\n") if line.startswith("-A "))
    except Exception as exc:
        # Previously `except Exception: pass` — the UI showed "0 rules" with no
        # explanation even when the real cause was a missing sudoers rule.
        logger.warning("firewall status: reading rules failed: %s", exc)
        status["rules_count"] = 0
        status["rules_error"] = str(exc)[:200]
    return status


@router.post("/firewall/enable")
async def enable_firewall() -> dict:
    """Deprecated — flipping INPUT/FORWARD to DROP bypassed UFW."""
    logger.warning("network firewall: rejected raw iptables policy change (enable)")
    raise HTTPException(
        status_code=409,
        detail="Use POST /api/security/ufw/enable (Firewall & Access Control app) — "
               "it installs the baseline rules first so the NAS stays reachable.",
    )


@router.post("/firewall/disable")
async def disable_firewall() -> dict:
    """Deprecated — flipping INPUT to ACCEPT bypassed UFW."""
    logger.warning("network firewall: rejected raw iptables policy change (disable)")
    raise HTTPException(
        status_code=409,
        detail="Use POST /api/security/ufw/disable (Firewall & Access Control app).",
    )
