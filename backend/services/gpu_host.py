"""Host GPU facts for packages that ask for hardware transcoding.

A package can only *declare* that it wants a GPU (`capabilities: ["dri"]` for
Intel/AMD, `["nvidia"]` for a graphics card); the host decides whether that is
possible. This module answers that question without running ffmpeg:

  * ``dri`` — the render nodes under ``/dev/dri``, whether the EthOS service
    account can actually open them (root:render 0660 — a plain ``os.access``
    lies), which cards are present and the ``render``/``video`` group ids a
    container needs in ``group_add``;
  * ``nvidia`` — the ``/dev/nvidia*`` nodes, the driver binaries, the
    ``nvidia-ctk`` toolkit and whether the Docker daemon has the ``nvidia``
    runtime configured (without it ``runtime: nvidia`` fails the container
    start, so the installer must not inject it).

The Videos app owns the *fire and see* probe (``video_engine.HardwareProbe``
micro-encodes with ffmpeg and picks QSV/VAAPI/libx264). This module is the cheap
structural probe used by Packages and ``/api/system/status``; the DRI helpers are
shared so both agree on what "the render node is usable" means.

Results are cached briefly (``CACHE_SECONDS``) because ``/api/system/status`` is
polled and the NVIDIA path shells out to ``docker info``.
"""

from __future__ import annotations

import getpass
import grp
import json
import logging
import os
import stat
import subprocess
import threading
import time

from backend.services import system_deps

logger = logging.getLogger("ethos-nas.services.gpu_host")

DRI_DIR = "/dev/dri"
DEV_DIR = "/dev"
NVIDIA_PREFIX = "nvidia"
NVIDIA_RUNTIME = "nvidia"
CACHE_SECONDS = 60.0
DOCKER_INFO_TIMEOUT = 10

# PCI vendor id (as printed in /sys/class/drm/<card>/device/uevent) -> label.
_VENDORS = {"8086": "intel", "1002": "amd", "10de": "nvidia"}

_NVIDIA_HINT = (
    "sudo apt install nvidia-container-toolkit && "
    "sudo nvidia-ctk runtime configure --runtime=docker && "
    "sudo systemctl restart docker"
)

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


# ── DRI helpers (shared with video_engine) ─────────────────────────────────


def dri_nodes() -> list[str]:
    """Render nodes (``/dev/dri/renderD128`` …) present on the host."""
    try:
        names = sorted(name for name in os.listdir(DRI_DIR) if name.startswith("renderD"))
    except OSError:
        return []
    return [f"{DRI_DIR}/{name}" for name in names]


def dri_cards() -> list[str]:
    """Display cards (``/dev/dri/card0`` …) present on the host."""
    try:
        names = sorted(name for name in os.listdir(DRI_DIR) if name.startswith("card"))
    except OSError:
        return []
    return [f"{DRI_DIR}/{name}" for name in names]


def dri_openable(node: str) -> bool:
    """True when the service account can open `node` the way ffmpeg will.

    The render node is ``root:render 0660``; ``os.access`` follows the real
    credentials of the process, but an ``O_RDWR`` open additionally catches a
    node whose group the account is missing — the exact failure users hit after
    installing a GPU driver.
    """
    try:
        fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
    except OSError:
        return False
    os.close(fd)
    return True


def _group_gid(name: str) -> int | None:
    try:
        return int(grp.getgrnam(name).gr_gid)
    except (KeyError, OSError):
        return None


def _device_gid(path: str) -> int | None:
    try:
        return int(os.stat(path).st_gid)
    except OSError:
        return None


def _card_details() -> list[dict]:
    """Vendor/driver per card, read from sysfs — no subprocess, no lspci."""
    details: list[dict] = []
    try:
        names = sorted(name for name in os.listdir("/sys/class/drm")
                       if name.startswith("card") and "-" not in name)
    except OSError:
        return details
    for name in names:
        info = {"device": name, "vendor": "unknown", "vendor_id": "", "driver": ""}
        try:
            with open(f"/sys/class/drm/{name}/device/uevent", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            details.append(info)
            continue
        for line in raw.splitlines():
            key, _, value = line.partition("=")
            if key == "DRIVER":
                info["driver"] = value.strip()
            elif key == "PCI_ID":
                vendor_id = value.strip().split(":", 1)[0].lower()
                info["vendor_id"] = vendor_id
                info["vendor"] = _VENDORS.get(vendor_id, "unknown")
        details.append(info)
    return details


def _nvidia_nodes() -> list[str]:
    """Character devices of the NVIDIA driver (``/dev/nvidia0``, ``-ctl``, …)."""
    try:
        names = sorted(name for name in os.listdir(DEV_DIR) if name.startswith(NVIDIA_PREFIX))
    except OSError:
        return []
    nodes = []
    for name in names:
        path = f"{DEV_DIR}/{name}"
        try:
            if stat.S_ISCHR(os.stat(path).st_mode):
                nodes.append(path)
        except OSError:
            continue
    return nodes


def _docker_runtimes() -> list[str]:
    """Runtimes the Docker daemon advertises (``docker info``), best effort."""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True, text=True, timeout=DOCKER_INFO_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("docker info runtime probe failed", exc_info=True)
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout or "{}")
    except (TypeError, ValueError):
        return []
    return sorted(str(name) for name in (data or {})) if isinstance(data, dict) else []


# ── Probe ──────────────────────────────────────────────────────────────────


def _dri_status() -> dict:
    nodes = dri_nodes()
    cards = dri_cards()
    openable = [node for node in nodes if dri_openable(node)]
    render_gid = _group_gid("render")
    if render_gid is None and nodes:
        render_gid = _device_gid(nodes[0])
    video_gid = _group_gid("video")
    if video_gid is None and cards:
        video_gid = _device_gid(cards[0])
    details = _card_details()
    vendor = next((card["vendor"] for card in details if card.get("vendor") != "unknown"), "unknown")

    if not nodes:
        reason = "no /dev/dri/renderD* node — no GPU is exposed to this host"
    elif not openable:
        reason = (
            f"{nodes[0]} exists but user '{getpass.getuser()}' cannot open it "
            f"(root:render 0660) — run: sudo usermod -aG render,video "
            f"{getpass.getuser()} && sudo systemctl restart ethos-nas"
        )
    else:
        reason = ""

    return {
        "available": bool(openable),
        "nodes": nodes,
        "openable": openable,
        "cards": details,
        "vendor": vendor,
        "render_gid": render_gid,
        "video_gid": video_gid,
        "reason": reason,
    }


def _nvidia_status(runtimes: list[str] | None = None) -> dict:
    nodes = _nvidia_nodes()
    driver = system_deps.has_binary("nvidia-smi") and bool(
        {f"{DEV_DIR}/{NVIDIA_PREFIX}0", f"{DEV_DIR}/{NVIDIA_PREFIX}ctl"} & set(nodes)
    )
    toolkit = system_deps.has_binary("nvidia-ctk")
    runtime_configured = NVIDIA_RUNTIME in (runtimes or _docker_runtimes())
    available = bool(driver and runtime_configured)
    if available:
        reason = ""
    elif not driver and not nodes:
        reason = "no NVIDIA device on this host"
    elif not driver:
        reason = "NVIDIA device nodes exist but the driver binaries are missing"
    else:
        reason = ("the NVIDIA driver is installed but Docker has no nvidia runtime — "
                  f"run: {_NVIDIA_HINT}")
    return {
        "available": available,
        "nodes": nodes,
        "driver": bool(driver),
        "toolkit": bool(toolkit),
        "runtime_configured": runtime_configured,
        "reason": reason,
        "hint": "" if available else _NVIDIA_HINT,
    }


def probe(refresh: bool = False) -> dict:
    """Cached host GPU facts: ``{"dri": {...}, "nvidia": {...}}``."""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get("value")
        if not refresh and cached is not None and now - _CACHE.get("ts", 0.0) < CACHE_SECONDS:
            return cached
    runtimes = _docker_runtimes()
    value = {"dri": _dri_status(), "nvidia": _nvidia_status(runtimes)}
    with _CACHE_LOCK:
        _CACHE["value"] = value
        _CACHE["ts"] = time.monotonic()
    return value


def reset_cache() -> None:
    """Drop the cached probe (tests, and after a driver/toolkit change)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def compose_env() -> dict[str, str]:
    """GPU facts a signed compose file may interpolate.

    Only group ids of devices that really exist are exported, so a compose file
    can use ``group_add: ["${ETHOS_RENDER_GID}"]`` without a hardcoded gid.
    """
    gpu = probe()
    dri = gpu.get("dri") or {}
    nvidia = gpu.get("nvidia") or {}
    env = {
        "ETHOS_GPU_VAAPI": "1" if dri.get("available") else "0",
        "ETHOS_GPU_NVIDIA": "1" if nvidia.get("available") else "0",
    }
    if dri.get("render_gid"):
        env["ETHOS_RENDER_GID"] = str(dri["render_gid"])
    if dri.get("video_gid"):
        env["ETHOS_VIDEO_GID"] = str(dri["video_gid"])
    return env
