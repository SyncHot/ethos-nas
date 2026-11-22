"""Host-port allocation for package UIs.

A package ships a **signed static compose file**, so its published host port
cannot be edited at install time — and that port is often already taken on a
real NAS (another compose project, a dev server, a leftover container). EthOS
embeds the package UI from that port, so a collision used to end the install
with a raw ``Bind for 127.0.0.1:3000 failed: port is already allocated`` after
the files were already deployed.

Instead of failing, the installer **remaps the host side only**:

1. pick a free port (preferring the requested one),
2. write ``docker-compose.override.yml`` next to the package compose file with
   ``ports: !override`` (Compose ≥ 2.24 replaces the list instead of appending),
3. store the effective port in the package record so the window iframe, the
   package proxy and health probes all use it.

The container keeps listening on its own port; only the published one changes.
"""

from __future__ import annotations

import logging
import socket
import subprocess
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger("ethos-nas.services.package_ports")

OVERRIDE_NAME = "docker-compose.override.yml"
#: How far to scan above the requested port before giving up.
SCAN_LIMIT = 60


def port_free(port: int) -> bool:
    """True when nothing on the host holds `port` on the wildcard address.

    Docker publishes on 0.0.0.0 by default, so a listener bound to 127.0.0.1
    (a dev server) still makes the bind fail — hence the wildcard probe.
    """
    if not 1 <= int(port) <= 65535:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("0.0.0.0", int(port)))
        except OSError:
            return False
    return True


def port_owner(port: int) -> str:
    """Container publishing `port`, for a readable conflict message."""
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("docker ps probe failed for the port lookup", exc_info=True)
        return ""
    if proc.returncode != 0:
        return ""
    for line in (proc.stdout or "").splitlines():
        name, _, ports = line.partition("\t")
        for chunk in ports.split(","):
            published = chunk.strip().split("->")[0]
            if published.rsplit(":", 1)[-1] == str(port):
                return name.strip()
    return ""


def allocate_port(preferred: int, *, reserved: Iterable[int] = (), limit: int = SCAN_LIMIT) -> int:
    """First free port at or above `preferred`, skipping already-reserved ones.

    `reserved` holds the ports other installed packages claim, so two installs
    started back-to-back cannot both see the same port as free.
    """
    taken = {int(p) for p in reserved if p}
    start = max(1, int(preferred or 1))
    for candidate in range(start, min(65535, start + limit) + 1):
        if candidate in taken:
            continue
        if port_free(candidate):
            return candidate
    raise ValueError(f"No free port within {limit} of {start}")


def _split_spec(spec) -> Optional[tuple[str, str]]:
    """(host, container) for a short-syntax port mapping, else None."""
    if not isinstance(spec, str):
        return None
    parts = spec.split(":")
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def remap_spec(spec, old: int, new: int):
    """Return `spec` with its published port `old` replaced by `new` (or `spec`).

    Handles the short syntax (`"3000:3000"`, `"127.0.0.1:3000:3000"`), the long
    syntax (`{published: 3000, target: 3000}`) and leaves anything else alone.
    """
    if isinstance(spec, dict):
        published = str(spec.get("published") or "")
        if published == str(old):
            return {**spec, "published": str(new)}
        return spec
    split = _split_spec(spec)
    if not split or split[0] != str(old):
        return spec
    parts = spec.rsplit(":", 2)
    parts[-2] = str(new)
    return ":".join(parts)


def _emit_spec(spec) -> str:
    if isinstance(spec, dict):
        keys = ("host_ip", "published", "target", "protocol")
        body = ", ".join(f'{k}: "{spec[k]}"' for k in keys if spec.get(k) not in (None, ""))
        return "{" + body + "}"
    return f'"{spec}"'


def build_override(compose: dict, mapping: dict[int, int]) -> str:
    """Compose override text remapping the published ports in `mapping`.

    `ports: !override` (Compose >= 2.24) replaces the base list, so the original
    mapping is dropped instead of being published next to the new one. Returns
    "" when no service publishes an affected port.
    """
    services = (compose or {}).get("services") or {}
    blocks: list[str] = []
    for name, service in services.items():
        ports = (service or {}).get("ports") or []
        changed = False
        rendered: list[str] = []
        for spec in ports:
            new_spec = spec
            for old, new in mapping.items():
                new_spec = remap_spec(new_spec, old, new)
            if new_spec != spec:
                changed = True
            rendered.append(_emit_spec(new_spec))
        if not changed:
            continue
        blocks.append(f"  {name}:\n    ports: !override\n"
                      + "".join(f"      - {line}\n" for line in rendered))
    if not blocks:
        return ""
    header = ("# Generated by EthOS: the host port of this package was re-mapped\n"
              "# because the requested one was already in use. Do not edit — the\n"
              "# installer rewrites it on the next update.\n")
    return header + "services:\n" + "".join(blocks)


def override_path(package_dir: Path) -> Path:
    return Path(package_dir) / OVERRIDE_NAME


def write_override(package_dir: Path, text: str) -> Path:
    """Persist (or drop) the override file for a package directory."""
    path = override_path(package_dir)
    if not text:
        if path.exists():
            path.unlink()
            logger.info("Removed stale port override %s", path)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    logger.info("Wrote port override %s", path)
    return path
