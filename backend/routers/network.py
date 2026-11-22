"""EthOS NAS — Network Management Router

REST:
- GET /api/network/interfaces        — list interfaces with stats
- GET /api/network/status            — default gateway, DNS servers, default routes (read-only)
- GET /api/network/stats/history     — throughput history from the background sampler
"""

import json
import logging
import os
import socket
import subprocess
import time
from typing import Optional

import psutil
from fastapi import APIRouter, Query

from backend.config import settings

logger = logging.getLogger("ethos-nas.routers.network")
router = APIRouter()

RESOLV_CONF = "/etc/resolv.conf"


def _fmt_bytes(b: int) -> str:
    """Format bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b} PB"


def _get_interfaces() -> list[dict]:
    """Collect network interfaces with addresses and I/O stats."""
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    io = psutil.net_io_counters(pernic=True)

    interfaces = []
    for name, addr_list in addrs.items():
        ipv4 = next(
            (a.address for a in addr_list if a.family == socket.AF_INET), None
        )
        mac = next(
            (a.address for a in addr_list
             if a.family == psutil.AF_LINK), None  # type: ignore[attr-defined]
        )
        stat = stats.get(name)
        nic_io = io.get(name)
        interfaces.append({
            "name": name,
            "status": "up" if (stat and stat.isup) else "down",
            "speed_mbps": stat.speed if stat else 0,
            "ipv4": ipv4 or "",
            "mac": mac or "",
            "bytes_sent": nic_io.bytes_sent if nic_io else 0,
            "bytes_recv": nic_io.bytes_recv if nic_io else 0,
            "bytes_sent_human": _fmt_bytes(nic_io.bytes_sent) if nic_io else "0 B",
            "bytes_recv_human": _fmt_bytes(nic_io.bytes_recv) if nic_io else "0 B",
            "packets_sent": nic_io.packets_sent if nic_io else 0,
            "packets_recv": nic_io.packets_recv if nic_io else 0,
            "errors_in": nic_io.errin if nic_io else 0,
            "errors_out": nic_io.errout if nic_io else 0,
            "drops_in": nic_io.dropin if nic_io else 0,
            "drops_out": nic_io.dropout if nic_io else 0,
        })

    # Sort: up first, then alphabetical
    interfaces.sort(key=lambda i: (0 if i["status"] == "up" else 1, i["name"]))
    return interfaces


@router.get("/interfaces")
async def get_interfaces() -> dict:
    """Return all network interfaces with IP, MAC, and I/O stats."""
    import asyncio
    interfaces = await asyncio.to_thread(_get_interfaces)
    return {"interfaces": interfaces, "total": len(interfaces)}


# ── Connection status (gateway / DNS / routes) ───────────────────────────────

def _parse_dns_servers(path: str | None = None) -> list[str]:
    """Parse nameserver lines (IPv4/IPv6) from a resolv.conf-style file.

    Path resolved per call so RESOLV_CONF is overridable (tests). Missing/
    unreadable file yields [] — never raises.
    """
    if path is None:
        path = RESOLV_CONF
    servers: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0] == "nameserver":
                    servers.append(parts[1].strip())
    except OSError as exc:
        logger.debug("resolv.conf unreadable: %s", exc)
    return servers


def _default_routes() -> tuple[list[dict], str | None]:
    """Parse ``ip -j route show default`` → (routes, note). Never raises.

    A missing default route is a valid state (isolated network) and yields
    ([], None); only lookup/parse failures produce a human-readable note.
    """
    try:
        result = subprocess.run(
            ["ip", "-j", "route", "show", "default"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return [], "`ip` command not found — route info unavailable"
    except Exception as exc:  # noqa: BLE001 — status endpoint must stay resilient
        logger.debug("ip route error: %s", exc)
        return [], f"route lookup failed ({exc.__class__.__name__})"
    if result.returncode != 0:
        return [], "route lookup failed (non-zero exit)"
    try:
        raw = json.loads(result.stdout or "[]")
    except (json.JSONDecodeError, TypeError):
        return [], "could not parse route output"
    routes: list[dict] = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        entry: dict = {"dst": r.get("dst", ""), "dev": r.get("dev", "")}
        if r.get("gateway"):
            entry["gateway"] = r["gateway"]
        if r.get("prefsrc"):
            entry["source_ip"] = r["prefsrc"]
        routes.append(entry)
    return routes, None


def _collect_status() -> dict:
    """Assemble the connection status snapshot (pure reads)."""
    routes, note = _default_routes()
    gateway = None
    for r in routes:  # `ip` lists lowest-metric first — first gateway wins
        if r.get("gateway"):
            gateway = {"ip": r["gateway"], "interface": r.get("dev", ""),
                       "source_ip": r.get("source_ip", "")}
            break
    status: dict = {
        "gateway": gateway,
        "dns_servers": _parse_dns_servers(),
        "routes": routes,
    }
    if note:
        status["note"] = note
    return status


@router.get("/status")
async def get_status() -> dict:
    """Default gateway, DNS servers and default routes (read-only)."""
    import asyncio
    return await asyncio.to_thread(_collect_status)


@router.get("/stats/history")
async def stats_history(
    minutes: int = Query(default=60, ge=1, le=720),
) -> dict:
    """Throughput points {ts, rx_bps, tx_bps} for the last `minutes` minutes."""
    import asyncio
    from backend.services import net_stats_service as nss
    samples = await asyncio.to_thread(nss.get_history, minutes)
    return {
        "samples": samples,
        "interval_seconds": settings.NET_STATS_INTERVAL,
        "now": int(time.time()),
    }
