"""EthOS NAS — Storage Management Router

REST:
- GET  /api/storage/disks      — physical disks (lsblk + SMART)
- GET  /api/storage/partitions — mounted partitions
- GET  /api/storage/usb        — USB storage devices (+ mountable partitions,
                                  supported filesystems for format ops)
- POST /api/storage/usb/mount    — persistent mount (fstab, nofail)
- POST /api/storage/usb/unmount  — unmount + remove fstab entry (permanent)
- POST /api/storage/usb/relabel          — change volume label (no reformat)
- POST /api/storage/usb/format           — reformat a partition/disk unit
- POST /api/storage/usb/partition        — add a partition in free space
- POST /api/storage/usb/delete-partition — delete a partition
- POST /api/storage/usb/init             — wipe disk + single full volume
"""

import asyncio
import json
import logging
import os
import subprocess
from typing import Optional

import psutil
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.routers.auth import require_admin

logger = logging.getLogger("ethos-nas.routers.storage")
router = APIRouter()


def _fmt_bytes(b: int) -> str:
    """Format bytes to human-readable string."""
    if b <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b} PB"


def _run(cmd: list[str], timeout: int = 8) -> Optional[str]:
    """Run a command and return stdout, or None on error."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None


def _lsblk() -> list[dict]:
    """Parse lsblk --json output into a list of disk dicts."""
    out = _run(["lsblk", "--json", "--bytes", "--output",
                "NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,LABEL,MODEL,VENDOR,TRAN,ROTA"])
    if not out:
        return []
    try:
        data = json.loads(out)
        disks = []
        for dev in data.get("blockdevices", []):
            if dev.get("type") != "disk":
                continue
            size = int(dev.get("size", 0) or 0)
            kids = [c for c in (dev.get("children") or []) if c]
            # A disk node carries no fstype/mountpoint of its own — those live
            # on its partitions. When a disk is a single volume (the common
            # case) surface the child's values so the table is actually useful.
            fstype = dev.get("fstype") or ""
            mount = dev.get("mountpoint") or ""
            label = dev.get("label") or ""
            if not (fstype or mount or label) and len(kids) == 1:
                kid = kids[0]
                fstype = kid.get("fstype") or ""
                mount = kid.get("mountpoint") or ""
                label = kid.get("label") or ""
            disks.append({
                "name": f"/dev/{dev['name']}",
                "model": (dev.get("model") or dev.get("vendor") or "").strip(),
                "size_bytes": size,
                "size_human": _fmt_bytes(size),
                "transport": dev.get("tran") or "unknown",
                # USB-SATA bridges report rota=0 even for spinning disks, which
                # made 5 TB HDDs show up as "SSD". ROTA is only trustworthy on
                # direct links; over USB the answer is "unknown" (null).
                "rotational": (str(dev.get("rota", "1")) == "1")
                              if (dev.get("tran") or "") != "usb" else None,
                "filesystem": fstype,
                "mountpoint": mount,
                "label": label,
                "partitions": len(kids),
                # No health claim here: SMART needs root/smartctl and lives in
                # the SMART Health app. Reporting a hard-coded True painted a
                # green "Healthy" badge on every disk, including failing ones.
                "smart": None,
            })
        return disks
    except (json.JSONDecodeError, KeyError):
        return []


def _usb_devices() -> list[dict]:
    """Return USB storage devices with their mountable partitions.

    Delegates to usb_mount_service for partition-level detail (fstype,
    stable identifiers, current mount state, managed fstab entries).
    """
    from backend.services import usb_mount_service

    devices = []
    for dev in usb_mount_service.list_usb_devices():
        partitions = dev["partitions"]
        # Disk-level convenience field: first mounted partition's mountpoint.
        mp = next((p["mountpoint"] for p in partitions if p["mounted"]), "")
        devices.append({
            "name": dev["device"],
            "model": dev["model"],
            "label": dev["label"],
            "size_bytes": dev["size_bytes"],
            "size_human": _fmt_bytes(dev["size_bytes"]),
            "mountpoint": mp,
            "partitions": partitions,
        })
    return devices


@router.get("/disks")
async def get_disks() -> dict:
    """List physical block devices with size and transport info."""
    disks = await asyncio.to_thread(_lsblk)
    return {"disks": disks, "total": len(disks)}


def _partitions_sync() -> list[dict]:
    """Mounted partitions + usage (blocking — always called in a thread).

    psutil.disk_usage() stats every mountpoint, and on a stale NFS/CIFS mount
    that can hang for seconds. Running it inline in the endpoint froze the whole
    event loop (every other API call stalled with it).
    """
    parts = []
    for p in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(p.mountpoint)
            parts.append({
                "device": p.device,
                "mountpoint": p.mountpoint,
                "fstype": p.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
                "total_human": _fmt_bytes(usage.total),
                "used_human": _fmt_bytes(usage.used),
                "free_human": _fmt_bytes(usage.free),
            })
        except (PermissionError, OSError):
            pass  # intentional: a failure here must not break the caller
    return parts


@router.get("/partitions")
async def get_partitions() -> dict:
    """List mounted partitions with usage stats via psutil."""
    parts = await asyncio.to_thread(_partitions_sync)
    return {"partitions": parts, "total": len(parts)}


@router.get("/usb")
async def get_usb_devices() -> dict:
    """List connected USB storage devices with mountable partitions."""
    from backend.services import usb_partition_service

    try:
        devices = await asyncio.to_thread(_usb_devices)
        filesystems = await asyncio.to_thread(usb_partition_service.supported_filesystems)
    except Exception as exc:  # noqa: BLE001 — surface a clean error to the UI
        logger.warning("USB device listing failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"usb_devices": devices, "total": len(devices), "supported_filesystems": filesystems}


class UsbMountRequest(BaseModel):
    device: str  # e.g. "/dev/sdb1"
    folder: Optional[str] = None  # optional mount folder name under /mnt (mount only)


@router.post("/usb/mount", dependencies=[Depends(require_admin)])
async def mount_usb_device(req: UsbMountRequest) -> dict:
    """Persistently mount a USB partition (fstab entry with nofail).

    The drive will be mounted automatically on every boot; if it is
    disconnected at boot time the system still starts normally.
    """
    from backend.services import usb_mount_service
    from backend.services.notifications_service import emit_event

    try:
        result = await asyncio.to_thread(usb_mount_service.mount_usb, req.device, req.folder)
    except usb_mount_service.UsbMountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("USB mount failed for %s", req.device)
        emit_event("error", "USB mount failed",
                   f"{req.device}: {exc}", source="storage")
        raise HTTPException(status_code=500, detail=f"Mount failed: {exc}")

    emit_event(
        "success", "USB drive mounted",
        f"{result['device']} → {result['mountpoint']} (persistent, nofail)",
        source="storage",
    )
    return result


@router.post("/usb/unmount", dependencies=[Depends(require_admin)])
async def unmount_usb_device(req: UsbMountRequest) -> dict:
    """Unmount a USB partition and remove its fstab entries (permanent).

    The drive will not be mounted automatically on the next boot.
    """
    from backend.services import usb_mount_service
    from backend.services.notifications_service import emit_event

    try:
        result = await asyncio.to_thread(usb_mount_service.unmount_usb, req.device)
    except usb_mount_service.UsbMountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("USB unmount failed for %s", req.device)
        emit_event("error", "USB unmount failed",
                   f"{req.device}: {exc}", source="storage")
        raise HTTPException(status_code=500, detail=f"Unmount failed: {exc}")

    msg = result["device"]
    if result["was_mounted"]:
        msg += f" (was at {result['mountpoint']})"
    removed = result["fstab_entries_removed"]
    if removed:
        msg += f", removed {removed} fstab entr{'y' if removed == 1 else 'ies'}"
    emit_event("success", "USB drive unmounted",
               f"{msg} — will not auto-mount on reboot", source="storage")
    return result

class UsbFormatRequest(BaseModel):
    device: str
    fstype: str = "ext4"
    label: str = ""

class UsbPartitionRequest(BaseModel):
    device: str  # the whole disk, e.g. /dev/sdb
    size_gb: Optional[float] = None  # None = all remaining free space
    fstype: str = "ext4"
    label: str = ""
    auto_mount: bool = True

class UsbDeviceRequest(BaseModel):
    device: str

def _usb_partition_error(kind: str, req_device: str, exc: Exception) -> HTTPException:
    from backend.services import usb_partition_service

    if isinstance(exc, usb_partition_service.UsbMountError):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("USB %s failed for %s", kind, req_device)
    from backend.services.notifications_service import emit_event
    emit_event("error", f"USB {kind} failed",
               f"{req_device}: {exc}", source="storage")
    return HTTPException(status_code=500, detail=f"{kind.capitalize()} failed: {exc}")

@router.post("/usb/format", dependencies=[Depends(require_admin)])
async def format_usb_device(req: UsbFormatRequest) -> dict:
    """Reformat a USB partition (or whole-disk unit) with the given filesystem.

    If the volume was mounted it is unmounted, reformatted and remounted —
    its persistent fstab entry survives. All existing data is destroyed.
    """
    from backend.services import usb_partition_service
    from backend.services.notifications_service import emit_event

    try:
        result = await asyncio.to_thread(
            usb_partition_service.format_usb, req.device, req.fstype, req.label,
        )
    except Exception as exc:  # noqa: BLE001
        raise _usb_partition_error("format", req.device, exc)

    msg = f"{result['device']} → {req.fstype}"
    if result.get("label"):
        msg += f" (label: {result['label']})"
    if result.get("mountpoint"):
        msg += f", remounted at {result['mountpoint']}"
    emit_event("success", "USB drive formatted", msg, source="storage")
    return result

class UsbRelabelRequest(BaseModel):
    device: str
    label: str = ""

@router.post("/usb/relabel", dependencies=[Depends(require_admin)])
async def relabel_usb_device(req: UsbRelabelRequest) -> dict:
    """Change a USB partition's volume label without reformatting.

    No data is touched. If the volume is mounted it is briefly unmounted and
    remounted at the same mount point; its fstab entry (keyed on PARTUUID/UUID)
    survives unchanged.
    """
    from backend.services import usb_partition_service
    from backend.services.notifications_service import emit_event

    try:
        result = await asyncio.to_thread(usb_partition_service.relabel_usb, req.device, req.label)
    except Exception as exc:
        raise _usb_partition_error("relabel", req.device, exc)

    msg = f"{result['device']}: {result['old_label'] or '(no label)'} → {result['new_label']}"
    emit_event("success", "USB drive renamed", msg, source="storage")
    return result

@router.post("/usb/partition", dependencies=[Depends(require_admin)])
async def create_usb_partition(req: UsbPartitionRequest) -> dict:
    """Create a new partition in the trailing free space of a USB disk.

    The partition is formatted with `fstype` and, by default, gets a
    persistent mount (fstab, nofail). On a raw stick without a partition
    table a GPT is created first, discarding any whole-disk layout.
    """
    from backend.services import usb_partition_service
    from backend.services.notifications_service import emit_event

    try:
        result = await asyncio.to_thread(
            usb_partition_service.create_partition,
            req.device, req.size_gb, req.fstype, req.label, req.auto_mount,
        )
    except Exception as exc:  # noqa: BLE001
        raise _usb_partition_error("partition", req.device, exc)

    msg = f"{result['fstype']} partition {result['partition']} on {result['disk']}"
    if result.get("mountpoint"):
        msg += f", mounted at {result['mountpoint']} (persistent)"
    emit_event("success", "USB partition created", msg, source="storage")
    return result

@router.post("/usb/delete-partition", dependencies=[Depends(require_admin)])
async def delete_usb_partition(req: UsbDeviceRequest) -> dict:
    """Delete a USB partition (unmounts it and removes its fstab entries)."""
    from backend.services import usb_partition_service
    from backend.services.notifications_service import emit_event

    try:
        result = await asyncio.to_thread(
            usb_partition_service.delete_partition, req.device,
        )
    except Exception as exc:  # noqa: BLE001
        raise _usb_partition_error("delete-partition", req.device, exc)

    emit_event("success", "USB partition deleted",
               f"{result['device']} removed from {result['disk']}", source="storage")
    return result

@router.post("/usb/init", dependencies=[Depends(require_admin)])
async def initialize_usb_disk(req: UsbPartitionRequest) -> dict:
    """Wipe every partition on a USB disk and recreate one full-size volume.

    Destructive "Initialize": all data on the disk is destroyed, a fresh GPT
    table is written, and a single partition spanning the whole disk is
    formatted (and by default mounted persistently).
    """
    from backend.services import usb_partition_service
    from backend.services.notifications_service import emit_event

    try:
        result = await asyncio.to_thread(
            usb_partition_service.initialize_disk,
            req.device, req.fstype, req.label, req.auto_mount,
        )
    except Exception as exc:  # noqa: BLE001
        raise _usb_partition_error("init", req.device, exc)

    msg = f"{result['disk']} re-initialized as {result['fstype']}"
    if result.get("mountpoint"):
        msg += f", mounted at {result['mountpoint']} (persistent)"
    emit_event("success", "USB disk initialized", msg, source="storage")
    return result
