"""EthOS NAS — SMART Disk Health Router

REST:
- GET /api/smart          — list all disks with SMART summary
- GET /api/smart/{device} — detailed SMART attributes for a device

Uses ``smartctl`` (smartmontools) to query disk health.  On permission errors
(predictable when the server runs as non-root) it falls back to returning
``{"healthy": null, "note": "smartctl requires root"}`` for each disk.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.routers.auth import require_admin
from backend.services import health_monitor_service as hms

logger = logging.getLogger("ethos-nas.routers.smart")

router = APIRouter()


def _run(cmd: list[str], timeout: int = 15) -> Optional[str]:
    """Run a command and return stdout, or None on error."""
    rc, out, _err = _run_result(cmd, timeout)
    return out if rc == 0 else None


def _run_result(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run a command and return ``(returncode, stdout, stderr)`` — never raises."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, (result.stdout or "").strip(), (result.stderr or "").strip()
    except FileNotFoundError:
        logger.warning("smartctl not found — is smartmontools installed?")
        return 127, "", "smartctl not installed"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 — callers report the detail
        logger.debug("smartctl error: %s", exc)
        return 1, "", str(exc)[:200]


def _device_name(device: str) -> str:
    """Normalise and validate a device name (short or /dev path)."""
    dev_name = device.replace("/dev/", "").strip()
    if not dev_name or not re.match(r"^[a-zA-Z][a-zA-Z0-9_]+$", dev_name):
        raise HTTPException(status_code=400, detail="Invalid device name")
    return dev_name


def _disks_from_lsblk() -> list[dict]:
    """Return a list of physical disk names (e.g. ``sda``) from lsblk."""
    out = _run(
        ["lsblk", "--json", "--bytes", "--output", "NAME,SIZE,TYPE,MODEL"],
        timeout=8,
    )
    if not out:
        return []
    try:
        data = json.loads(out)
        disks = []
        for dev in data.get("blockdevices", []):
            if dev.get("type") == "disk":
                disks.append({
                    "name": dev["name"],
                    "device": f"/dev/{dev['name']}",
                    "model": (dev.get("model") or "").strip(),
                    "size_bytes": int(dev.get("size", 0) or 0),
                })
        return disks
    except (json.JSONDecodeError, KeyError):
        return []


def _parse_smart_json(device: str) -> Optional[dict]:
    """Parse ``smartctl --json --all /dev/{device}`` output.

    Returns a dict with health info or ``None`` on failure.
    """
    out = _run(["smartctl", "--json", "--all", f"/dev/{device}"])
    if not out:
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    # Extract overall health status
    health = data.get("smart_status", {})
    passed = health.get("passed")

    # Temperature
    temp_info = data.get("temperature") or {}
    temp = temp_info.get("current") if isinstance(temp_info, dict) else None

    # Collect key SMART attributes
    attributes = []
    for attr in (data.get("ata_smart_attributes", {}).get("table") or []):
        att = {
            "id": attr.get("id"),
            "name": attr.get("name", ""),
            "value": attr.get("value"),
            "worst": attr.get("worst"),
            "thresh": attr.get("thresh"),
            "raw": attr.get("raw", {}).get("value") if isinstance(attr.get("raw"), dict) else attr.get("raw"),
            "when_failed": attr.get("when_failed"),
        }
        # Only include if the attribute is meaningful (not all-zeros)
        if att["value"] is not None:
            # Flag critical: Pre-fail type with when_failed set
            if att["when_failed"]:
                att["critical"] = True
            attributes.append(att)

    # Power-on hours
    power_on_hours = None
    for attr in attributes:
        if attr["name"] and "power_on" in attr["name"].lower().replace(" ", "_"):
            power_on_hours = attr["raw"]
            break

    return {
        "device": device,
        "healthy": passed,
        "temperature": temp,
        "power_on_hours": power_on_hours,
        "attributes": attributes,
        "model": data.get("model_name", ""),
        "serial": data.get("serial_number", ""),
        "firmware": data.get("firmware_version", ""),
        "interface": data.get("interface_speed", {}).get("current", {}).get("speed") if isinstance(data.get("interface_speed"), dict) else None,
    }


def _smartctl_brief(device: str) -> dict:
    """Fallback: basic SMART status via ``-H`` flag (less parsing).

    Used when --json output is not available or fails to parse.
    """
    out = _run(["smartctl", "-H", f"/dev/{device}"])
    healthy = None
    if out:
        m = re.search(r"SMART overall-health self-assessment test result: (\w+)", out)
        if m:
            healthy = m.group(1).lower() == "passed"
    return {
        "device": device,
        "healthy": healthy,
        "note": "Basic health only (--json parsing failed)"
    }


@router.get("")
async def smart_list() -> dict:
    """List all physical disks with SMART health summary.

    Returns a summary (healthy/temperature) for each disk without
    fetching full attribute tables (use ``GET /api/smart/{device}``
    for details).
    """
    disks = await asyncio.to_thread(_disks_from_lsblk)
    results = []

    for disk in disks:
        name = disk["name"]
        smart_data = await asyncio.to_thread(_parse_smart_json, name)

        if smart_data:
            results.append({
                "device": f"/dev/{name}",
                "name": name,
                "model": smart_data.get("model") or disk.get("model", ""),
                "size_bytes": disk["size_bytes"],
                "healthy": smart_data["healthy"],
                "temperature": smart_data["temperature"],
                "power_on_hours": smart_data["power_on_hours"],
                "serial": smart_data.get("serial", ""),
                "firmware": smart_data.get("firmware", ""),
            })
        else:
            # Fallback: report disk exists but SMART info unavailable
            brief = _smartctl_brief(name)
            results.append({
                "device": f"/dev/{name}",
                "name": name,
                "model": disk.get("model", ""),
                "size_bytes": disk["size_bytes"],
                "healthy": brief.get("healthy"),
                "temperature": None,
                "power_on_hours": None,
                "serial": "",
                "firmware": "",
                "note": brief.get("note", "smartctl unavailable or permission denied"),
            })

    return {"disks": results, "total": len(results)}


@router.get("/history")
async def smart_history(hours: int = 24, device: str = "") -> dict:
    """Persisted SMART samples (temperature + key attributes) for one or all disks.

    Read-only: the health monitor records one sample per check, so this never
    probes a disk on the request path.
    """
    dev = f"/dev/{_device_name(device)}" if device.strip() else None
    return await asyncio.to_thread(hms.smart_history, hours, dev)


class SelfTestRequest(BaseModel):
    """Self-test action: ``short``, ``long`` or ``abort``."""

    model_config = {"extra": "forbid"}
    mode: str = "short"


@router.post("/{device:str}/self-test", dependencies=[Depends(require_admin)])
async def start_self_test(device: str, body: SelfTestRequest) -> dict:
    """Start (or abort) a SMART self-test — drive-side, admin-only."""
    dev_name = _device_name(device)
    mode = (body.mode or "").strip().lower()
    if mode not in {"short", "long", "abort"}:
        raise HTTPException(status_code=422, detail="mode must be short, long or abort")
    rc, out, err = await asyncio.to_thread(
        _run_result, ["smartctl", "-t", mode, f"/dev/{dev_name}"], 30)
    logger.info("SMART self-test %s on /dev/%s (rc=%s)", mode, dev_name, rc)
    if rc != 0:
        raise HTTPException(status_code=500, detail=err or out or f"smartctl exited {rc}")
    return {"ok": True, "device": f"/dev/{dev_name}", "mode": mode, "output": out}


@router.get("/{device:str}/selftest-log")
async def self_test_log(device: str) -> dict:
    """The drive's own self-test log (``smartctl -l selftest``)."""
    dev_name = _device_name(device)
    rc, out, err = await asyncio.to_thread(
        _run_result, ["smartctl", "-l", "selftest", f"/dev/{dev_name}"], 15)
    if rc != 0 and not out:
        raise HTTPException(status_code=500, detail=err or f"smartctl exited {rc}")
    return {"device": f"/dev/{dev_name}", "output": out}


@router.get("/{device:str}")
async def smart_device(device: str) -> dict:
    """Return detailed SMART attributes for a single device.

    Args:
        device: Short name (e.g. ``sda``) or full path (``/dev/sda``).
    """
    # Normalise — strip leading /dev/ if present
    dev_name = _device_name(device)

    smart_data = await asyncio.to_thread(_parse_smart_json, dev_name)
    if smart_data:
        return smart_data

    # Fallback
    brief = _smartctl_brief(dev_name)
    if brief["healthy"] is not None:
        return brief

    raise HTTPException(
        status_code=503,
        detail=f"Unable to read SMART data for /dev/{dev_name} "
               f"(root privileges may be required)",
    )
