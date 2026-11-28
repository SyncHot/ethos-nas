"""Hardware-transcoding additions for a compose package.

A package declares a *need* in its manifest (``capabilities: ["dri"]`` for an
Intel/AMD GPU, ``["nvidia"]`` for a graphics card) but never names a device: the
signed compose file cannot know which nodes exist or which group ids a container
must join. EthOS therefore materialises the GPU part at install/start time, next
to the port remap, in the generated ``docker-compose.override.yml``:

    devices: [/dev/dri/renderD128]
    group_add: ["990", "44"]
    # or, for NVIDIA:
    runtime: nvidia
    deploy: {resources: {reservations: {devices: [{driver: nvidia, ...}]}}}

Two rules keep this safe and portable:

* only devices the host really has (and the service account can open) are
  passed, so a GPU-less NAS still installs the package and transcodes in
  software; ``runtime: nvidia`` is injected only when the Docker daemon
  advertises that runtime, because a missing runtime fails the container start;
* the manifest contributes capabilities only — the override is written from the
  probe result, so a package cannot ask for an arbitrary host device.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from backend.services import gpu_host

logger = logging.getLogger("ethos-nas.services.package_gpu")

CAPABILITY_DRI = "dri"
CAPABILITY_NVIDIA = "nvidia"
GPU_CAPABILITIES = (CAPABILITY_DRI, CAPABILITY_NVIDIA)

NVIDIA_RUNTIME = "nvidia"


def requested(manifest: Optional[dict]) -> list[str]:
    """GPU capabilities a manifest declares, in a stable order."""
    caps = (manifest or {}).get("capabilities") or []
    if not isinstance(caps, list):
        return []
    wanted = {str(item) for item in caps}
    return [name for name in GPU_CAPABILITIES if name in wanted]


def fragment(manifest: Optional[dict], gpu: Optional[dict] = None) -> dict:
    """Per-service compose additions for the GPU capabilities of `manifest`.

    Returns ``{}`` when the package asks for no GPU, or when the host cannot
    provide it — in both cases the package runs in software mode. Shape:
    ``{"devices": [...], "group_add": [...], "runtime": "nvidia"|None,
    "deploy": {...}|None}``.
    """
    wanted = requested(manifest)
    if not wanted:
        return {}
    facts = gpu if gpu is not None else gpu_host.probe()
    additions: dict[str, Any] = {}

    if CAPABILITY_DRI in wanted:
        dri = facts.get("dri") or {}
        devices = list(dri.get("openable") or [])
        if devices:
            additions["devices"] = devices
            groups = []
            for gid in (dri.get("render_gid"), dri.get("video_gid")):
                if gid and int(gid) not in groups:
                    groups.append(int(gid))
            if groups:
                additions["group_add"] = [str(gid) for gid in groups]

    if CAPABILITY_NVIDIA in wanted:
        nvidia = facts.get("nvidia") or {}
        if nvidia.get("available"):
            additions["runtime"] = NVIDIA_RUNTIME
            additions["deploy"] = {
                "resources": {
                    "reservations": {
                        "devices": [{
                            "driver": NVIDIA_RUNTIME,
                            "count": "all",
                            "capabilities": ["gpu"],
                        }],
                    },
                },
            }
            extra = [node for node in (nvidia.get("nodes") or [])]
            if extra:
                additions["devices"] = list(dict.fromkeys([*(additions.get("devices") or []), *extra]))
    return additions


def summary(manifest: Optional[dict], gpu: Optional[dict] = None,
            additions: Optional[dict] = None) -> dict:
    """GPU state stored with the package record and shown by Package Center."""
    wanted = requested(manifest)
    if not wanted:
        return {}
    facts = gpu if gpu is not None else gpu_host.probe()
    layer = additions if additions is not None else fragment(manifest, facts)
    dri = facts.get("dri") or {}
    nvidia = facts.get("nvidia") or {}
    reasons = []
    if CAPABILITY_DRI in wanted and not layer.get("devices"):
        reasons.append(dri.get("reason") or "no usable /dev/dri render node")
    if CAPABILITY_NVIDIA in wanted and not layer.get("runtime"):
        reasons.append(nvidia.get("reason") or "no usable NVIDIA device")
    return {
        "requested": wanted,
        "applied": bool(layer.get("devices") or layer.get("runtime")),
        "devices": list(layer.get("devices") or []),
        "runtime": layer.get("runtime") or None,
        "vendor": dri.get("vendor") or "unknown",
        "reason": "; ".join(reason for reason in reasons if reason),
    }


def log_lines(manifest: Optional[dict], summary_data: dict) -> list[str]:
    """Job-log lines describing what was (not) passed through."""
    if not summary_data:
        return []
    if summary_data.get("applied"):
        parts = []
        if summary_data.get("devices"):
            parts.append("/dev/dri/" + ", /dev/dri/".join(
                node.rsplit("/", 1)[-1] for node in summary_data["devices"]
                if node.startswith(gpu_host.DRI_DIR)))
        if summary_data.get("runtime"):
            parts.append(f"runtime {summary_data['runtime']}")
        detail = "; ".join(part for part in parts if part) or "no device"
        return [f"GPU requested ({', '.join(summary_data['requested'])}) — passing {detail}"]
    return [
        f"GPU requested ({', '.join(summary_data['requested'])}) but unavailable — "
        f"software transcoding only ({summary_data.get('reason') or 'no GPU'})"
    ]
