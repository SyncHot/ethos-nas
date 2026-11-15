"""Discover and remove what an uninstalled package leaves behind in Docker.

Uninstall always removes the containers, the compose network and the package
directory. Named volumes and images are kept by default; the caller opts in to
removing them. This module answers "what would be removed" for the confirmation
dialog and performs *scoped* removals only:

* volumes — the ones labelled with this package's compose project;
* images — the ones the package's compose file references, and only while no
  container uses them any more.

There is deliberately no ``docker image prune`` / ``docker volume prune`` here:
those act on the whole host and would delete unrelated stacks on a NAS.
"""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Iterable, Optional

from backend.services.package_schema import PackageError

logger = logging.getLogger("ethos-nas.services.package_cleanup")

TIMEOUT = 60
_PROJECT_LABEL = "com.docker.compose.project"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)


async def _output(cmd: list[str], *, strict: bool = False) -> str:
    """Run a read-only docker command and return stdout ('' on a tolerated failure)."""
    label = " ".join(cmd[1:3])
    try:
        proc = await asyncio.to_thread(_run, cmd)
    except FileNotFoundError:
        raise PackageError("docker is not available on this system", 503) from None
    except subprocess.TimeoutExpired:
        logger.warning("docker %s timed out after %ss", label, TIMEOUT)
        return ""
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-300:]
        if strict:
            raise PackageError(f"docker {label} failed: {detail}", 500)
        logger.warning("docker %s failed: %s", label, detail)
        return ""
    return proc.stdout or ""


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


# ── Discovery ───────────────────────────────────────────────────────────────

async def project_containers(pkg_id: str) -> list[str]:
    """Container names belonging to the package's compose project (running or not)."""
    out = await _output([
        "docker", "ps", "-a", "--format", "{{.Names}}",
        "--filter", f"label={_PROJECT_LABEL}={pkg_id}",
    ])
    return _lines(out)


async def project_volumes(pkg_id: str) -> list[str]:
    """Named volumes created by the package's compose project."""
    out = await _output([
        "docker", "volume", "ls", "-q",
        "--filter", f"label={_PROJECT_LABEL}={pkg_id}",
    ])
    return sorted(set(_lines(out)))


async def package_image_refs(compose_path: Path, pkg_id: str) -> list[str]:
    """Images referenced by the package's compose file (works while it exists)."""
    out = await _output([
        "docker", "compose", "-f", str(compose_path), "-p", pkg_id, "config", "--images",
    ])
    return sorted(set(_lines(out)))


async def _image_id(ref: str) -> str:
    out = (await _output(["docker", "image", "inspect", "--format", "{{.Id}}", ref])).strip()
    return _lines(out)[0] if out else ""


async def _usage_map() -> dict[str, list[dict]]:
    """image id -> [{"container": name, "project": compose project}] for every container."""
    rows = _lines(await _output([
        "docker", "ps", "-a", "--format",
        "{{.Image}}\t{{.Names}}\t{{.Label \"" + _PROJECT_LABEL + "\"}}",
    ]))
    if not rows:
        return {}
    ids: dict[str, str] = {}
    usage: dict[str, list[dict]] = {}
    for row in rows:
        parts = row.split("\t")
        if len(parts) < 2:
            continue
        ref, name = parts[0].strip(), parts[1].strip().lstrip("/")
        project = parts[2].strip() if len(parts) > 2 else ""
        if not ref or not name:
            continue
        if ref not in ids:
            ids[ref] = await _image_id(ref)
        image_id = ids[ref]
        if image_id:
            usage.setdefault(image_id, []).append({"container": name, "project": project})
    return usage


# ── Planning (pure) ─────────────────────────────────────────────────────────

def split_images(images: dict[str, str], usage: dict[str, list[dict]],
                 own_containers: Iterable[str] = ()) -> tuple[list[dict], list[dict]]:
    """Split ``{ref: image_id}`` into (safe to remove, still in use elsewhere).

    ``own_containers`` are the package's own container names; a reference held
    only by those is removable once the project is down.
    """
    own = {str(name).lstrip("/") for name in own_containers}
    removable: list[dict] = []
    shared: list[dict] = []
    for ref, image_id in sorted(images.items()):
        owners = [
            owner for owner in usage.get(image_id, [])
            if str(owner.get("container", "")).lstrip("/") not in own
        ]
        entry = {"ref": ref, "id": image_id, "in_use_by": owners}
        (shared if owners else removable).append(entry)
    return removable, shared


async def plan(compose_path: Path, pkg_id: str,
               own_containers: Iterable[str] = ()) -> tuple[list[dict], list[dict]]:
    images: dict[str, str] = {}
    for ref in await package_image_refs(compose_path, pkg_id):
        image_id = await _image_id(ref)
        if image_id:
            images[ref] = image_id
    return split_images(images, await _usage_map(), own_containers)


async def preview(pkg_id: str, compose_path: Optional[Path]) -> dict:
    """Inventory for the uninstall confirmation dialog.

    Read-only, so a host without Docker yields an empty inventory (``docker:
    False``) instead of an error: the dialog must still be usable there.
    """
    inventory = {
        "containers": [],
        "volumes": [],
        "images": [],
        "shared_images": [],
        "has_compose": compose_path is not None,
        "docker": True,
    }
    try:
        inventory["containers"] = await project_containers(pkg_id)
        inventory["volumes"] = await project_volumes(pkg_id)
        if compose_path is not None:
            images, shared = await plan(compose_path, pkg_id, own_containers=inventory["containers"])
            inventory["images"] = images
            inventory["shared_images"] = shared
    except PackageError:
        inventory.update({"containers": [], "volumes": [], "images": [], "shared_images": [],
                          "docker": False})
    return inventory


# ── Removal ─────────────────────────────────────────────────────────────────

async def remove_images(entries: Iterable[dict]) -> list[dict]:
    """`docker rmi` each entry; failures are reported, never raised.

    Usage is re-checked right before removing: an image can be pulled into use
    between the planning step and the removal (another package being installed,
    or the caller planning while the project is still up). That must not fail
    the uninstall job — the image is simply kept.
    """
    results: list[dict] = []
    usage = await _usage_map()
    for entry in entries:
        image_id = str(entry.get("id") or "")
        ref = str(entry.get("ref") or image_id)
        if not image_id:
            continue
        holders = usage.get(image_id) or []
        if holders:
            names = ", ".join(str(h.get("container") or "?") for h in holders)
            logger.warning("Keeping image %s: still used by %s", ref, names)
            results.append({"ref": ref, "ok": False, "detail": f"still used by {names}"})
            continue
        try:
            proc = await asyncio.to_thread(_run, ["docker", "rmi", image_id])
        except FileNotFoundError:
            raise PackageError("docker is not available on this system", 503) from None
        except subprocess.TimeoutExpired:
            logger.warning("docker rmi %s timed out after %ss", ref, TIMEOUT)
            results.append({"ref": ref, "ok": False, "detail": "timed out"})
            continue
        if proc.returncode == 0:
            results.append({"ref": ref, "ok": True, "detail": ""})
            continue
        detail = (proc.stderr or proc.stdout or "").strip()[-300:]
        logger.warning("Keeping image %s: %s", ref, detail)
        results.append({"ref": ref, "ok": False, "detail": detail})
    return results
