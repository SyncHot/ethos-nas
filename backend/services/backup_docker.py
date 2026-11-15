"""Docker Compose discovery and volume/image snapshots for the backup engine.

The core process runs as the `ethos-nas` service user with docker-group access,
so docker-py (synchronous, always wrapped in `asyncio.to_thread`) can inspect
containers. Snapshots use short-lived `alpine` containers so named volumes never
have to be mounted by the host.

Project discovery is label-based (`com.docker.compose.project` +
`...working_dir`). The v1 engine matched the source folder's basename, which is
why a schedule covering a parent directory silently backed up only `config/`.
"""

import asyncio
import hashlib
import logging
import os
from typing import Optional

from backend.services.backup_proc import OperationHandle, run

logger = logging.getLogger("ethos-nas.services.backup_docker")


class DockerUnavailable(RuntimeError):
    """Docker engine or socket is not reachable."""


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_projects_sync(source: Optional[str] = None) -> list[dict]:
    """Group containers by compose project; optionally filter by working dir."""
    try:
        import docker as _docker
        client = _docker.from_env()
    except Exception as exc:  # docker missing, socket denied, engine down
        raise DockerUnavailable(str(exc)) from exc

    projects: dict[str, dict] = {}
    try:
        containers = client.containers.list(all=True)
    except Exception as exc:
        raise DockerUnavailable(str(exc)) from exc

    for container in containers:
        attrs = container.attrs or {}
        labels = (attrs.get("Config") or {}).get("Labels") or {}
        name = labels.get("com.docker.compose.project")
        if not name:
            continue
        working_dir = labels.get("com.docker.compose.project.working_dir") or ""
        entry = projects.setdefault(name, {
            "name": name,
            "working_dir": working_dir,
            "containers": [],
            "volumes": set(),
            "images": {},
            "bind_configs": set(),
        })
        if not entry["working_dir"] and working_dir:
            entry["working_dir"] = working_dir
        entry["containers"].append(container.name)
        for mount in attrs.get("Mounts", []):
            mount_name = mount.get("Name") or ""
            source_path = mount.get("Source") or ""
            if mount.get("Type") == "volume" and mount_name:
                entry["volumes"].add(mount_name)
            elif (mount.get("Type") == "bind" and working_dir
                  and source_path.startswith(working_dir.rstrip("/") + "/")):
                entry["bind_configs"].add(source_path[len(working_dir.rstrip("/")) + 1:])
        try:
            image = container.image
            tag = image.tags[0] if image.tags else f"<{image.short_id}>"
            entry["images"][tag] = image.id
        except Exception:  # image may have been removed while listing
            logger.debug("Could not resolve image for %s", container.name, exc_info=True)

    wanted_root = os.path.realpath(source) if source else None
    out = []
    for name in sorted(projects):
        entry = projects[name]
        working_dir = (entry["working_dir"] or "").rstrip("/")
        if wanted_root:
            if not working_dir:
                logger.warning("Compose project %s has no working_dir label — skipped", name)
                continue
            real = os.path.realpath(working_dir)
            if not (real == wanted_root or real.startswith(wanted_root + os.sep)):
                continue
        out.append({
            "name": name,
            "working_dir": working_dir,
            "containers": sorted(entry["containers"]),
            "volumes": sorted(entry["volumes"]),
            "images": [{"tag": tag, "id": image_id}
                       for tag, image_id in sorted(entry["images"].items())],
            "bind_configs": sorted(entry["bind_configs"]),
        })
    return out


async def discover_projects(source: Optional[str] = None) -> list[dict]:
    return await asyncio.to_thread(discover_projects_sync, source)


def pick_projects(schedule: dict, discovered: list[dict]) -> tuple[list[dict], list[str]]:
    """Apply `options.projects` (None = every discovered project)."""
    selected = (schedule.get("options") or {}).get("projects")
    if not selected:
        return discovered, []
    wanted = {str(name) for name in selected}
    found = [project for project in discovered if project["name"] in wanted]
    missing = sorted(wanted - {project["name"] for project in found})
    return found, missing


async def _docker(args: list[str], op: OperationHandle) -> tuple[int, str]:
    rc, out, err = await run(args, op=op, capture=True)
    text = (out + b"\n" + err).decode(errors="replace").strip()
    return rc, text


async def stop_containers(containers: list[str], op: OperationHandle) -> list[str]:
    """Stop containers; returns the ones actually stopped (for restart)."""
    stopped = []
    for name in containers:
        op.check()
        rc, text = await _docker(["docker", "stop", name], op)
        if rc == 0:
            stopped.append(name)
        else:
            logger.warning("docker stop %s failed: %s", name, text[-200:])
    return stopped


async def start_containers(containers: list[str], op: OperationHandle) -> list[str]:
    started = []
    for name in containers:
        rc, text = await _docker(["docker", "start", name], op)
        if rc == 0:
            started.append(name)
        else:
            logger.warning("docker start %s failed: %s", name, text[-200:])
    return started


async def archive_volume(volume: str, out_path: str, op: OperationHandle) -> dict:
    """Snapshot one named volume through a throwaway alpine container."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    temp_path = f"{out_path}.part"
    if os.path.exists(temp_path):
        os.unlink(temp_path)
    rc, _out, err = await run(
        ["docker", "run", "--rm",
         "-v", f"{volume}:/src:ro",
         "-v", f"{os.path.dirname(out_path)}:/out:rw",
         "alpine", "tar", "--numeric-owner", "-czf",
         f"/out/{os.path.basename(temp_path)}", "-C", "/src", "."],
        op=op, capture=True, stdout_null=True,
    )
    if rc != 0:
        raise RuntimeError(f"Volume snapshot failed for {volume}: "
                           f"{err.decode(errors='replace')[-300:]}")
    os.replace(temp_path, out_path)
    return {"volume": volume, "file": os.path.basename(out_path),
            "size": os.path.getsize(out_path), "sha256": await asyncio.to_thread(_sha256, out_path)}


async def restore_volume(volume: str, archive_path: str, op: OperationHandle) -> None:
    filename = os.path.basename(archive_path)
    if not filename.endswith((".tar.gz", ".tgz", ".tar")):
        raise RuntimeError(f"Unexpected volume archive: {filename}")
    flags = "-xzf" if filename.endswith((".tar.gz", ".tgz")) else "-xf"
    rc, _out, err = await run(
        ["docker", "run", "--rm",
         "-v", f"{os.path.dirname(archive_path)}:/bkp:ro",
         "-v", f"{volume}:/data:rw",
         "alpine", "tar", flags, f"/bkp/{filename}", "-C", "/data"],
        op=op, capture=True, stdout_null=True,
    )
    if rc != 0:
        raise RuntimeError(f"Volume restore failed for {volume}: "
                           f"{err.decode(errors='replace')[-300:]}")


async def save_image(tag: str, out_path: str, op: OperationHandle) -> dict:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    temp_path = f"{out_path}.part"
    if os.path.exists(temp_path):
        os.unlink(temp_path)
    rc, _out, err = await run(["docker", "save", "-o", temp_path, tag],
                              op=op, capture=True, stdout_null=True)
    if rc != 0:
        raise RuntimeError(f"Image export failed for {tag}: {err.decode(errors='replace')[-300:]}")
    os.replace(temp_path, out_path)
    return {"tag": tag, "file": os.path.basename(out_path),
            "size": os.path.getsize(out_path), "sha256": await asyncio.to_thread(_sha256, out_path)}


async def link_or_save_image(tag: str, image_id: str, out_path: str,
                             previous: Optional[dict], op: OperationHandle) -> dict:
    """Reuse the previous tar (hardlink) when the image id did not change."""
    if previous and previous.get("id") == image_id:
        previous_path = os.path.join(os.path.dirname(out_path), previous.get("file") or "")
        if previous.get("file") and os.path.isfile(previous_path):
            try:
                os.link(previous_path, out_path)
                return {"tag": tag, "file": os.path.basename(out_path),
                        "size": previous.get("size", 0), "sha256": previous.get("sha256", ""),
                        "reused": True}
            except OSError:
                logger.debug("Could not hardlink previous image tar %s", previous_path, exc_info=True)
    saved = await save_image(tag, out_path, op)
    saved["id"] = image_id
    return saved


async def load_image(archive_path: str, op: OperationHandle) -> None:
    rc, _out, err = await run(["docker", "load", "-i", archive_path],
                              op=op, capture=True, stdout_null=True)
    if rc != 0:
        raise RuntimeError(f"Image restore failed for {os.path.basename(archive_path)}: "
                           f"{err.decode(errors='replace')[-300:]}")


async def verify_archive(path: str) -> bool:
    """Check a tar archive can be fully read (integrity)."""
    import tarfile

    def _check() -> bool:
        try:
            with tarfile.open(path, "r:*") as archive:
                for member in archive:
                    if member.isfile():
                        handle = archive.extractfile(member)
                        if handle is not None:
                            while handle.read(1024 * 1024):
                                pass
            return True
        except (OSError, tarfile.TarError):
            logger.warning("Backup archive is damaged: %s", path, exc_info=True)
            return False

    return await asyncio.to_thread(_check)
