"""EthOS NAS — Docker Unified Router (Containers + Images + Volumes)

REST:
- GET     /api/containers                  — list all containers
- POST    /api/containers/{name}/action    — start | stop | restart
- DELETE  /api/containers/{name}           — remove a container (?force=true to force)
- GET     /api/images                      — list all images
- DELETE  /api/images/{image_id}           — remove an image
- POST    /api/images/pull                 — pull a new image from registry
- POST    /api/images/prune                — remove ALL unused images (not just dangling)
- GET     /api/volumes                     — list all volumes (+ which containers hold them)
- POST    /api/volumes/prune               — remove all unused volumes (named + anonymous)
- DELETE  /api/volumes/{name}              — remove a volume
- GET     /api/compose/projects            — list compose projects (live + remembered)
- POST    /api/compose/projects/{name}/action — up | down | destroy a compose project
- GET     /api/docker/engine               — host Docker Engine status (installed? reachable?)
- POST    /api/docker/engine/install       — install Docker Engine + Compose v2 (job)
- GET     /api/docker/engine/jobs/{id}     — progress of an engine installation

Functionality:
- Docker container lifecycle (start/stop/restart) via docker-py SDK
- Docker image management (list/pull/delete/prune)
- Docker volume listing with orphan detection and deletion
"""

import asyncio
import json
import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docker
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.config import settings
from backend.models import ContainerActionRequest
from backend.routers.auth import require_admin
from backend.services import docker_host, json_store, package_env, package_jobs, packages_store
from backend.services.notifications_service import emit_event
from backend.services.package_schema import PackageError

logger = logging.getLogger("ethos-nas.routers.docker")
router = APIRouter()

_docker_client: Optional[docker.DockerClient] = None
_VALID_ACTIONS = {"start", "stop", "restart"}
_VALID_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{0,62}$")
_VALID_ID = re.compile(r"^[a-zA-Z0-9:._\-]{3,130}$")

# Practical subset of the distribution/reference grammar:
# [domain[:port]/]component[/component...][:tag][@sha256:<digest>]
_IMAGE_REF_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-]*(?::\d{1,5})?"
    r"(?:/[A-Za-z0-9._\-]+)*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9._\-]{0,127})?"
    r"(?:@sha256:[a-f0-9]{64})?$"
)


class PullRequest(BaseModel):
    image: str
    model_config = {"extra": "forbid"}


class ComposeActionRequest(BaseModel):
    action: str
    model_config = {"extra": "forbid"}


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_docker() -> docker.DockerClient:
    """Return a lazy-initialised Docker SDK client."""
    global _docker_client
    if _docker_client is None:
        try:
            _docker_client = docker.from_env()
            _docker_client.ping()
            logger.info("Docker client initialised")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Docker daemon unreachable: {exc}")
    return _docker_client


def _engine_prune_all(client: docker.DockerClient, path: str) -> dict:
    """Call an Engine prune endpoint with ``all=1``.

    docker-py's high-level prune wrappers don't expose the ``all`` query flag,
    which is what makes the daemon remove *all* unused images/volumes instead
    of only dangling/anonymous ones. Uses the stable public ``post()`` method.
    """
    url = client.api._url(path)
    res = client.api.post(url, params={"all": 1})
    # api.post() returns a requests.Response — body is in .content (bytes).
    return json.loads(res.content or b"{}")


def _verify_running(container, settle_seconds: float = 3.0) -> Optional[str]:
    """Return a failure explanation if the container is not running after start.

    ``docker start`` succeeds whenever the process is *created*; a container that
    dies immediately (bad config, port already bound by another stack, missing
    network) still reports success and the stack silently ends up half up.
    """
    time.sleep(settle_seconds)
    try:
        container.reload()
    except docker.errors.NotFound:
        return f"'{container.name}' disappeared right after starting"
    if container.status == "running":
        return None

    state = (container.attrs or {}).get("State") or {}
    detail = f"'{container.name}' exited immediately (code {state.get('ExitCode')})"
    if state.get("Error"):
        detail += f": {state['Error']}"
    try:
        tail = "\n".join(container.logs(tail=20).decode("utf-8", "replace").strip().splitlines()[-6:])
    except Exception:  # logging must never mask the real failure
        tail = ""
    if tail:
        detail += f"\n\n{tail}"
    return detail


_PORT_CONFLICT_RE = re.compile(
    r"Bind for [0-9a-fA-F.:\[\]]+:(\d+) failed: port is already allocated"
)


def _explain_port_conflict(err: str) -> str:
    """Turn Docker's opaque bind error into an actionable message.

    The daemon reports only the port number, which is why a compose run that
    collides with another stack looks like a random failure. Resolve the port to
    the container that holds it so the operator knows what to move.
    """
    match = _PORT_CONFLICT_RE.search(err or "")
    if not match:
        return ""
    host_port = match.group(1)
    holders: set[str] = set()
    try:
        for other in _get_docker().containers.list():
            for bindings in (other.ports or {}).values():
                for binding in bindings or []:
                    if str(binding.get("HostPort", "")) == host_port:
                        holders.add(other.name)
    except Exception:
        pass  # intentional: a failure here must not break the caller
    who = f" (currently held by {', '.join(sorted(holders))})" if holders else ""
    return (
        f"Host port {host_port} is already published by another container{who}. "
        f"Change the published port for this project or stop the other stack, then retry."
    )



def _parse_ts(value: str) -> Optional[datetime]:
    """Parse a Docker RFC3339 timestamp (e.g. 2026-09-01T10:00:56.4Z)."""
    if not value or value.startswith("0001-"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _human_delta(seconds: float) -> str:
    """Format a duration in seconds as e.g. '3d 4h', '2h 5m', '45s'."""
    if seconds < 0:
        seconds = 0
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _container_state(c: docker.models.containers.Container) -> str:
    """Human-readable state, Docker-CLI style ('Up 3d 4h', 'Exited (1) 2h ago')."""
    state = (c.attrs or {}).get("State", {}) or {}
    status = c.status
    now = datetime.now(timezone.utc)
    if status == "running":
        started = _parse_ts(state.get("StartedAt", ""))
        return f"Up {_human_delta((now - started).total_seconds())}" if started else "Up"
    if status == "exited":
        code = state.get("ExitCode", 0)
        finished = _parse_ts(state.get("FinishedAt", ""))
        suffix = f" {_human_delta((now - finished).total_seconds())} ago" if finished else ""
        return f"Exited ({code}){suffix}"
    return status.capitalize()


def _container_dict(c: docker.models.containers.Container) -> dict:
    """Serialise a container object to a JSON-safe dict.

    The image name is read from the container's own attrs (Config.Image) instead of
    resolving ``c.image`` — that lookup 404s when the backing image has been pruned
    and would otherwise break the entire listing.
    """
    ports: list[str] = []
    for container_port, host_bindings in (c.ports or {}).items():
        if host_bindings:
            for b in host_bindings:
                ports.append(f"{b['HostPort']}→{container_port}")
        else:
            ports.append(container_port)

    config = c.attrs.get("Config") or {}
    image = config.get("Image") or ""
    if not image:
        image_id = getattr(c, "image_id", "") or ""
        image = image_id.split(":")[-1][:12] or "?"

    # docker-compose stamps these labels on every container it creates;
    # empty strings mean the container is standalone (not part of a compose project).
    labels = config.get("Labels") or {}

    state = c.attrs.get("State") or {}
    health = (state.get("Health") or {}).get("Status", "")
    networks = list(((c.attrs.get("NetworkSettings") or {}).get("Networks") or {}).keys())
    restart_policy = ((c.attrs.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name", "")

    return {
        "id": c.short_id,
        "name": c.name,
        "image": image,
        "status": c.status,
        "state": _container_state(c),
        "ports": ", ".join(ports) or "—",
        "compose_project": labels.get("com.docker.compose.project", ""),
        "compose_service": labels.get("com.docker.compose.service", ""),
        # Diagnostics: these are the fields that explain *why* a stack is only
        # half up. `restart: no` means a crash is permanent; a running container
        # with no network endpoint cannot resolve its peers (EAI_AGAIN).
        "restart_policy": restart_policy or "no",
        "health": health,
        "networks": networks,
        "network_detached": c.status == "running" and not networks,
        "exit_code": state.get("ExitCode") if c.status == "exited" else None,
        "restart_count": c.attrs.get("RestartCount", 0) or 0,
        "error": state.get("Error", "") or "",
    }


def _image_dict(img, used_image_ids: set) -> dict:
    """Serialise a Docker Image object."""
    tags = img.tags or []
    repo = tags[0].split(":")[0] if tags else "<none>"
    tag = tags[0].split(":")[1] if tags and ":" in tags[0] else "latest"
    return {
        "id": img.short_id,
        "full_id": img.id,
        "repository": repo,
        "tag": tag,
        "size": {"raw": img.attrs.get("Size", 0)},
        "created_at": img.attrs.get("Created", ""),
        "is_dangling": (not tags),
        "in_use": img.id in used_image_ids,
    }


def _volume_dict(v, holders: dict) -> dict:
    """Serialise a Docker Volume object.

    ``holders`` maps volume name -> list of {name, running} for every container
    (running OR exited) that references the volume. A volume referenced by an
    exited container cannot be removed until that container is deleted — this
    matches the daemon's own semantics, so we surface it instead of hiding it.
    """
    name = v.name
    attrs = v.attrs or {}
    usage = attrs.get("UsageData") or {}
    size = usage.get("Size")  # None on daemons that don't report it (e.g. 29.x)
    vols_holders = holders.get(name, [])
    exited = [h["name"] for h in vols_holders if not h["running"]]
    return {
        "name": name,
        "driver": attrs.get("Driver", "local"),
        "mountpoint": attrs.get("Mountpoint", ""),
        "is_orphaned": not vols_holders,
        "held_by_exited": exited,
        "size": size if isinstance(size, int) else None,
        "size_human": _fmt(size) if isinstance(size, int) and size >= 0 else None,
    }


def _fmt(b: int) -> str:
    """Format bytes to human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


# ════════════════════════════════════════════════════════════════════════════════
# ENGINE / HOST PROVISIONING
# ════════════════════════════════════════════════════════════════════════════════
#
# Docker Engine installation needs root, so it goes through the privileged
# helper installed by setup_scripts/setup_service.sh (see services/docker_host).

_ENGINE_JOB_OWNER = "system:docker"


@router.get("/docker/engine")
async def docker_engine_status() -> dict:
    """Host Docker Engine status: installed, reachable, Compose plugin, group access."""
    return await asyncio.to_thread(docker_host.engine_status)


@router.post("/docker/engine/install", dependencies=[Depends(require_admin)])
async def docker_engine_install() -> dict:
    """Install Docker Engine + Compose v2 from Docker's official apt repository."""
    status = await asyncio.to_thread(docker_host.engine_status)
    if status["daemon"]:
        return {"ok": True, "status": "already_running", "engine": status}
    if not status["helper"]:
        raise HTTPException(
            status_code=503,
            detail=("Docker installer helper is not installed on this host — run "
                    "`sudo bash setup_scripts/setup_service.sh` on the NAS, then retry"),
        )
    if not status["install_supported"]:
        raise HTTPException(
            status_code=400,
            detail=("Automatic Docker installation is supported on Ubuntu and Debian only "
                    f"(this host reports {(status.get('os') or {}).get('id') or 'an unknown distribution'})"),
        )
    if status["conflict"]:
        raise HTTPException(status_code=409, detail=status["conflict"])

    try:
        job = package_jobs.create("docker-install", _ENGINE_JOB_OWNER)
    except PackageError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    async def _work(job_id: str) -> None:
        await asyncio.to_thread(docker_host.install_docker, job_id)

    if not package_jobs.start(job, _work):
        raise HTTPException(status_code=503, detail="Server is shutting down — try again in a moment")
    emit_event("info", "Docker Engine installation started",
               "Installing from the official Docker apt repository", source="docker")
    return {"job_id": job["id"]}


@router.get("/docker/engine/jobs/{job_id}")
async def docker_engine_job(job_id: str) -> dict:
    """Poll an engine installation job (same shape as a package job)."""
    job = package_jobs.get(job_id)
    if not job or job.get("package_id") != _ENGINE_JOB_OWNER:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ════════════════════════════════════════════════════════════════════════════════
# CONTAINERS
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/containers")
async def list_containers(limit: int = Query(0, ge=0, le=5000),
                          offset: int = Query(0, ge=0)) -> dict:
    """List Docker containers (running and stopped), paginated."""
    try:
        client = _get_docker()
        containers = await asyncio.to_thread(client.containers.list, all=True)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to list containers: %s", exc)
        raise HTTPException(status_code=503, detail=f"Failed to list containers: {exc}")

    items = [_container_dict(c) for c in containers]
    # Running first, then alphabetical — matches how people scan a container list.
    items.sort(key=lambda x: (x["status"] != "running", x["name"].lower()))
    running = sum(1 for c in items if c["status"] == "running")
    total = len(items)
    window = items[offset:offset + limit] if limit else items[offset:]
    return {"containers": window, "total_count": total,
            "running_count": running, "limit": limit, "offset": offset}


@router.post("/containers/{name}/action", dependencies=[Depends(require_admin)])
async def container_action(name: str, body: ContainerActionRequest) -> dict:
    """Perform start / stop / restart on a named container."""
    if not _VALID_NAME.match(name):
        raise HTTPException(status_code=400, detail="Invalid container name")
    if body.action not in _VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action. Allowed: {_VALID_ACTIONS}")

    client = _get_docker()
    try:
        container = await asyncio.to_thread(client.containers.get, name)
        action_fn = getattr(container, body.action)
        await asyncio.to_thread(action_fn)
        # `start` reports success even when the process dies a second later
        # (bad config, port already bound, detached network). Verify, so the UI
        # can show the real reason instead of a false "started".
        if body.action in {"start", "restart"}:
            failure = await asyncio.to_thread(_verify_running, container)
            if failure:
                raise HTTPException(status_code=409, detail=failure)
        emit_event("info", f"Container {body.action}", name, source="docker")
        return {"ok": True, "container": name, "action": body.action}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"Container '{name}' not found")
    except docker.errors.APIError as exc:
        if getattr(exc, "response", None) is not None and exc.response.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail="Container is in a transitional state — try again in a moment",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/containers/{name}", dependencies=[Depends(require_admin)])
async def remove_container(name: str, force: bool = False) -> dict:
    """Remove a container. Pass ?force=true to remove a running one."""
    if not _VALID_NAME.match(name):
        raise HTTPException(status_code=400, detail="Invalid container name")

    client = _get_docker()
    try:
        container = await asyncio.to_thread(client.containers.get, name)
        await asyncio.to_thread(container.remove, force=force)
        emit_event("warning", "Container removed", name, source="docker")
        return {"ok": True, "container": name}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"Container '{name}' not found")
    except docker.errors.APIError as exc:
        if getattr(exc, "response", None) is not None and exc.response.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail="Container is running — stop it first or use force remove",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc))


# ════════════════════════════════════════════════════════════════════════════════
# IMAGES
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/images")
async def list_images() -> dict:
    """List all local Docker images."""
    try:
        client = _get_docker()
        images = await asyncio.to_thread(client.images.list, all=True)
        containers = await asyncio.to_thread(client.containers.list, all=True)
        # Full image IDs currently referenced by any container (no extra API calls).
        used_ids = {c.attrs.get("Image", "") for c in containers}
        items = [_image_dict(img, used_ids) for img in images]
        total_size = sum(i["size"]["raw"] or 0 for i in items)
        return {"images": items, "total_count": len(items), "total_size_bytes": total_size}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to list images: %s", exc)
        raise HTTPException(status_code=503, detail=f"Failed to list images: {exc}")


@router.delete("/images/{image_id}", dependencies=[Depends(require_admin)])
async def delete_image(image_id: str) -> dict:
    """Remove a Docker image by ID or tag."""
    if not _VALID_ID.match(image_id):
        raise HTTPException(status_code=400, detail="Invalid image ID")
    try:
        client = _get_docker()
        await asyncio.to_thread(client.images.remove, image_id)
        return {"ok": True, "image_id": image_id}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"Image '{image_id}' not found")
    except docker.errors.APIError as exc:
        if getattr(exc, "response", None) is not None and exc.response.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail="Image is in use by one or more containers — remove them first",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/images/pull", dependencies=[Depends(require_admin)])
async def pull_image(req: PullRequest) -> dict:
    """Pull a Docker image from registry."""
    if not req.image or not req.image.strip():
        raise HTTPException(status_code=400, detail="Image name is required")

    name = req.image.strip()

    # Accept full references (nginx, bitnami/redis:7.2, lscr.io/linuxserver/radarr:v5,
    # localhost:5000/team/app@sha256:<digest>); reject traversal/garbage early.
    # docker-py performs the final validation against the registry.
    if ".." in name or not (3 <= len(name) <= 255) or not _IMAGE_REF_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image reference: {name!r} (expected e.g. 'nginx' or 'bitnami/redis:latest')",
        )

    try:
        client = _get_docker()
        logger.info("Pulling image: %s", name)
        await asyncio.to_thread(client.images.pull, name)
        emit_event("success", "Image pulled", name, source="docker")
        return {"ok": True, "image": name}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Pull failed: {exc}")


@router.post("/images/prune", dependencies=[Depends(require_admin)])
async def prune_images() -> dict:
    """Remove all unused Docker images (dangling and unreferenced) using native prune API."""
    try:
        client = _get_docker()

        # Engine API with all=1: removes every image not referenced by any
        # container (running or exited), not just dangling ones. docker-py's
        # images.prune() doesn't expose the `all` flag, so call it directly.
        result = await asyncio.to_thread(_engine_prune_all, client, "/images/prune")

        deleted_count = 0
        freed_bytes = int(result.get("SpaceReclaimed") or 0)

        images_deleted = result.get("ImagesDeleted") if isinstance(result, dict) else None
        if isinstance(images_deleted, list):
            for item in images_deleted:
                if isinstance(item, (list, tuple)) and len(item) >= 1:
                    img_id = item[0]
                    size = item[1] if len(item) > 1 else 0
                    if img_id is not None:
                        deleted_count += 1
                        freed_bytes += size or 0
                elif isinstance(item, str):
                    deleted_count += 1
        if images_deleted and deleted_count == 0:
            deleted_count = len(images_deleted)

        return {
            "ok": True,
            "deleted_count": deleted_count,
            "freed_bytes": freed_bytes,
            "message": f"Removed {deleted_count} unused image{'s' if deleted_count != 1 else ''} ({_fmt(freed_bytes)} freed)",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Prune failed: {exc}")


# ════════════════════════════════════════════════════════════════════════════════
# VOLUMES
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/volumes")
async def list_volumes() -> dict:
    """List all Docker volumes with orphan detection."""
    try:
        client = _get_docker()
        volumes = await asyncio.to_thread(client.volumes.list)
        containers = await asyncio.to_thread(client.containers.list, all=True)

        # Map volume name -> holders (any container, running or exited — the
        # daemon keeps a reference until the container itself is removed).
        holders: dict[str, list[dict]] = {}
        for c in containers:
            for mount in c.attrs.get("Mounts", []):
                if mount.get("Type") == "volume" and mount.get("Name"):
                    holders.setdefault(mount["Name"], []).append(
                        {"name": c.name, "running": c.status == "running"}
                    )

        items = [_volume_dict(v, holders) for v in volumes]
        orphaned = sum(1 for v in items if v["is_orphaned"])
        return {"volumes": items, "total_count": len(items), "orphaned_count": orphaned}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/volumes/prune", dependencies=[Depends(require_admin)])
async def prune_volumes() -> dict:
    """Remove all unused volumes (named + anonymous) via the native prune API.

    Volumes still referenced by any container — including exited ones — are
    left untouched; remove those containers first.
    """
    try:
        client = _get_docker()
        result = await asyncio.to_thread(_engine_prune_all, client, "/volumes/prune")

        deleted: dict | list = result.get("VolumesDeleted") or []
        names = list(deleted.keys()) if isinstance(deleted, dict) else [str(x) for x in deleted]
        freed_bytes = int(result.get("SpaceReclaimed") or 0)
        if not freed_bytes and isinstance(deleted, dict):
            freed_bytes = sum(int(s or 0) for s in deleted.values())

        return {
            "ok": True,
            "deleted_count": len(names),
            "freed_bytes": freed_bytes,
            "message": f"Removed {len(names)} unused volume{'s' if len(names) != 1 else ''} ({_fmt(freed_bytes)} freed)",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Prune failed: {exc}")


@router.delete("/volumes/{name}", dependencies=[Depends(require_admin)])
async def delete_volume(name: str) -> dict:
    """Remove a Docker volume by name."""
    if not _VALID_NAME.match(name):
        raise HTTPException(status_code=400, detail="Invalid volume name")
    try:
        client = _get_docker()
        volume = await asyncio.to_thread(client.volumes.get, name)
        await asyncio.to_thread(volume.remove)
        return {"ok": True, "name": name}
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"Volume '{name}' not found")
    except docker.errors.APIError as exc:
        if getattr(exc, "response", None) is not None and exc.response.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail="Volume is in use by a container — remove the container first",
            ) from exc
        raise HTTPException(status_code=500, detail=str(exc))


# ════════════════════════════════════════════════════════════════════════════
# COMPOSE PROJECTS
# ════════════════════════════════════════════════════════════════════════════
#
# docker-compose stamps every container with project labels (working dir, config
# files). We mirror those into a small registry file so that `up` keeps working
# after `down` has removed all of a project's containers.

COMPOSE_REGISTRY_FILE = settings.DATA_DIR / "compose_projects.json"
_VALID_PROJECT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-]{0,62}$")
_COMPOSE_ACTIONS = {"up", "down", "destroy"}


def _load_compose_registry() -> dict:
    return json_store.get(COMPOSE_REGISTRY_FILE, dict).load()  # {project: spec}


def _installed_package_ids() -> set[str]:
    """Packages ids — their compose projects are the system stacks.

    The installer runs ``docker compose -p <pkg_id>``, so a project whose name
    matches an installed package was created and is updated by Packages.
    """
    try:
        return {str(p.get("id") or "") for p in packages_store.load_installed() if p.get("id")}
    except Exception:
        logger.error("Failed to read installed packages for compose grouping", exc_info=True)
        return set()


def _save_compose_registry(registry: dict) -> None:
    json_store.get(COMPOSE_REGISTRY_FILE, dict).save(registry)


def _compose_locations_from_containers(containers) -> dict:
    """Map project name -> {working_dir, config_files} from container labels."""
    locations: dict[str, dict] = {}
    for c in containers:
        labels = ((c.attrs.get("Config") or {}).get("Labels")) or {}
        project = labels.get("com.docker.compose.project", "")
        if not project:
            continue
        entry = locations.setdefault(project, {"working_dir": "", "config_files": []})
        wd = labels.get("com.docker.compose.project.working_dir", "")
        if wd and not entry["working_dir"]:
            entry["working_dir"] = wd
        for f in filter(None, (p.strip() for p in labels.get("com.docker.compose.project.config_files", "").split(","))):
            if f not in entry["config_files"]:
                entry["config_files"].append(f)
    return locations


def _remember_compose_locations(live: dict) -> None:
    """Persist live project locations (only when something new appeared/changed)."""
    registry = _load_compose_registry()
    changed = False
    for name, loc in live.items():
        if not loc["working_dir"] and not loc["config_files"]:
            continue
        prev = registry.get(name)
        if (not prev or prev.get("working_dir") != loc["working_dir"]
                or prev.get("config_files") != loc["config_files"]):
            registry[name] = {
                "working_dir": loc["working_dir"],
                "config_files": loc["config_files"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            changed = True
    if changed:
        _save_compose_registry(registry)


def _resolve_project_location(name: str, containers) -> tuple[str, list[str]]:
    """Resolve (working_dir, config_files) for a project.

    Live container labels win; the remembered registry is the fallback so that
    `up` works after `down` removed every container of the project.
    """
    live = _compose_locations_from_containers(containers).get(name, {})
    working_dir = live.get("working_dir", "")
    config_files = list(live.get("config_files", []))
    if not working_dir and not config_files:
        reg = _load_compose_registry().get(name, {})
        working_dir = reg.get("working_dir", "")
        config_files = list(reg.get("config_files", []))
    # Compose auto-discovers docker-compose.{yml,yaml} in the cwd, so a missing
    # working dir can be derived from the config file location.
    if not working_dir and config_files:
        working_dir = str(Path(config_files[0]).parent)
    return working_dir, config_files


_DEFAULT_COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")


def _location_problem(working_dir: str, config_files: list[str]) -> str:
    """Explain why `docker compose` cannot run for a project, or '' when it can.

    A project's containers remember the *directory* they were created from, but
    that directory can outlive the compose file (e.g. a project started from a
    temp dir that was cleaned up). Checking the directory alone made the UI
    offer Down/Destroy buttons that could only ever fail.
    """
    if not working_dir:
        return "no compose location is known for this project"
    if not Path(working_dir).is_dir():
        return f"the compose directory {working_dir} no longer exists"
    if config_files:
        missing = [f for f in config_files if not Path(f).is_file()]
        if missing:
            return f"compose file missing: {', '.join(missing)}"
        return ""
    if not any((Path(working_dir) / name).is_file() for name in _DEFAULT_COMPOSE_FILES):
        return f"no compose file found in {working_dir}"
    return ""


def _teardown_project_by_label(name: str, remove_volumes: bool) -> str:
    """Tear a compose project down using its labels instead of its compose file.

    Container/network/volume labels carry the project name, so this reproduces
    `docker compose down [--volumes]` even when the compose file is gone and the
    stack would otherwise be impossible to remove from the UI.
    """
    client = _get_docker()
    selector = {"label": f"com.docker.compose.project={name}"}
    removed: list[str] = []
    skipped: list[str] = []

    for container in client.containers.list(all=True, filters=selector):
        try:
            container.remove(force=True, v=remove_volumes)
            removed.append(container.name)
        except Exception as exc:
            skipped.append(f"{container.name} ({exc})")

    for network in client.networks.list(filters=selector):
        try:
            network.remove()
            removed.append(f"network {network.name}")
        except Exception as exc:
            skipped.append(f"network {network.name} ({exc})")

    if remove_volumes:
        for volume in client.volumes.list(filters=selector):
            try:
                volume.remove(force=True)
                removed.append(f"volume {volume.name}")
            except Exception as exc:
                skipped.append(f"volume {volume.name} ({exc})")

    if not removed and not skipped:
        return "nothing to remove"
    summary = f"removed {', '.join(removed)}" if removed else "nothing removed"
    if skipped:
        summary += f"; could not remove {', '.join(skipped[:3])}"
    return summary


@router.get("/compose/projects")
async def list_compose_projects() -> dict:
    """List docker-compose projects: live ones plus remembered (stopped) locations."""
    try:
        client = _get_docker()
        containers = await asyncio.to_thread(client.containers.list, all=True)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to list containers: %s", exc)
        raise HTTPException(status_code=503, detail=f"Failed to list containers: {exc}")

    live = _compose_locations_from_containers(containers)
    _remember_compose_locations(live)  # keep registry fresh for later `up`
    registry = _load_compose_registry()

    by_project: dict[str, list] = {}
    for c in containers:
        labels = ((c.attrs.get("Config") or {}).get("Labels")) or {}
        project = labels.get("com.docker.compose.project", "")
        if project:
            by_project.setdefault(project, []).append(c)

    projects = []
    installed = _installed_package_ids()
    for name in sorted(set(live) | set(registry)):
        loc_live = live.get(name, {})
        loc_reg = registry.get(name, {})
        working_dir = loc_live.get("working_dir") or loc_reg.get("working_dir", "")
        config_files = loc_live.get("config_files") or loc_reg.get("config_files", [])
        members = by_project.get(name, [])
        problem = _location_problem(working_dir, config_files)
        projects.append({
            "name": name,
            "working_dir": working_dir,
            "config_files": config_files,
            "container_count": len(members),
            "running_count": sum(1 for c in members if c.status == "running"),
            "restarting_count": sum(1 for c in members if c.status == "restarting"),
            # Packages owns the stack (project name == package id); the UI
            # groups these as system apps and shows them with the `ethos-` prefix.
            "managed": name in installed,
            "package_id": name if name in installed else "",
            # Whether we can run compose commands for this project — and, when we
            # cannot, a human-readable reason the UI can show instead of failing.
            "known_location": not problem,
            "location_problem": problem,
        })
    return {"projects": projects}


@router.post("/compose/projects/{name}/action", dependencies=[Depends(require_admin)])
async def compose_project_action(name: str, body: ComposeActionRequest) -> dict:
    """Run `docker compose` for a project: up (-d), down, or destroy (down --volumes)."""
    if not _VALID_PROJECT.match(name):
        raise HTTPException(status_code=400, detail="Invalid project name")
    if body.action not in _COMPOSE_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Invalid action. Allowed: {sorted(_COMPOSE_ACTIONS)}")

    try:
        client = _get_docker()
        containers = await asyncio.to_thread(client.containers.list, all=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to list containers: {exc}")

    working_dir, config_files = _resolve_project_location(name, containers)
    problem = _location_problem(working_dir, config_files)
    if problem:
        # The compose file can disappear while its containers stay behind (a
        # project started from a temp dir is the usual case). Removing by label
        # keeps `down`/`destroy` working instead of stranding the stack forever.
        if body.action in {"down", "destroy"}:
            summary = await asyncio.to_thread(
                _teardown_project_by_label, name, body.action == "destroy"
            )
            return {
                "ok": True,
                "project": name,
                "action": body.action,
                "output": f"{summary} (compose file unavailable: {problem})",
            }
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot {body.action} '{name}': {problem}. "
                "Point the project at its compose file again, or use Down/Destroy to remove it."
            ),
        )

    cmd = ["docker", "compose"]
    for f in config_files:
        cmd += ["-f", f]
    cmd += ["-p", name]
    if body.action == "up":
        # --remove-orphans: an `up` that follows a renamed/removed service used to
        # leave the stale container behind, so the project never looked fully up.
        cmd += ["up", "-d", "--remove-orphans"]
    elif body.action == "down":
        cmd += ["down"]
    else:  # destroy
        cmd += ["down", "--volumes", "--remove-orphans"]

    # Package stacks interpolate `${ETHOS_PACKAGE_DATA_DIR}` and their stored
    # settings, so the same environment the installer uses has to reach compose
    # here too — otherwise `docker compose down` dies on
    # `invalid spec: :/var/lib/grafana: empty section between colons`.
    env = package_env.compose_env(name)
    try:
        proc = await asyncio.to_thread(
            subprocess.run, cmd, cwd=working_dir, env=env,
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Compose command timed out after 10 minutes")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to run compose: {exc}")

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[-800:]
        logger.error("compose %s for project %s failed: %s", body.action, name, err)
        if body.action in {"down", "destroy"}:
            # Removal only needs the labels: never leave a stack running because
            # compose refused to render its file (missing variable, bad YAML…).
            summary = await asyncio.to_thread(
                _teardown_project_by_label, name, body.action == "destroy"
            )
            emit_event("warning", f"Compose project {body.action} by labels", name,
                       source="docker")
            return {
                "ok": True,
                "project": name,
                "action": body.action,
                "output": f"{summary} (compose failed: {err[-200:]})",
            }
        conflict = _explain_port_conflict(err)
        if conflict:
            raise HTTPException(status_code=409, detail=conflict)
        raise HTTPException(status_code=500, detail=f"compose {body.action} failed: {err}")

    # Remember the location so `up` works after this action removes all containers.
    registry = _load_compose_registry()
    registry[name] = {
        "working_dir": working_dir,
        "config_files": config_files,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_compose_registry(registry)

    emit_event("info", f"Compose project {body.action}", name, source="docker")
    return {"ok": True, "project": name, "action": body.action, "output": (proc.stdout or "").strip()[-400:]}
