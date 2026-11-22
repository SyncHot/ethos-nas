"""Install / update / uninstall a package as a Compose service.

The install is staged first: manifests and files are downloaded and verified
into a staging directory, the compose file is linted, and only then is the
package directory swapped in. Updates keep a backup of the previous directory
and restore it if the new service fails to start.
"""

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

from backend.config import APP_VERSION, settings
from backend.services import (
    package_cleanup, package_jobs, package_repo, package_runtime, package_wheels,
    packages_store, system_deps,
)
from backend.services.compose_policy import enforce_compose
from backend.services.notifications_service import emit_event
from backend.services.package_schema import (
    PackageError, check_min_ethos_version, check_pkg_id, version_gt,
)

logger = logging.getLogger("ethos-nas.services.package_installer")

COMPOSE_TIMEOUT = 300

# `docker info` is slow and the root does not move while the daemon runs.
_DOCKER_ROOT_CACHE: Optional[str] = None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _discard_dir(path: Path, reason: str) -> bool:
    """Remove a package directory, falling back to moving it aside.

    A failed compose start can leave files the service user cannot delete (a
    container that ran as root may have created entries next to the read-only
    mount). Deleting then silently fails and the next install refuses with
    "Package directory already exists". Renaming only needs write access to the
    parent — which the service owns — so a retry can always proceed.
    """
    if not path.exists():
        return True
    shutil.rmtree(path, ignore_errors=True)
    if not path.exists():
        return True
    stale = packages_store.BACKUP_DIR / f"{path.name}-{reason}-{int(time.time())}"
    try:
        stale.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(stale))
    except OSError as exc:
        logger.error("Could not remove or move package directory %s: %s", path, exc)
        return False
    logger.warning("Leftover package directory %s moved to %s", path, stale)
    return True


def _docker_root() -> Optional[Path]:
    """Filesystem the Docker daemon writes images and volumes to.

    Images land in `Docker Root Dir`, not in `DATA_DIR`, so the free-space
    reserve has to be checked there too or a 2 GB image fills `/` while the
    preflight reports plenty of room. The path is cached: `docker info` takes
    seconds and the daemon's root does not change while it runs.
    """
    global _DOCKER_ROOT_CACHE
    configured = str(getattr(settings, "PACKAGES_DOCKER_ROOT", "") or "").strip()
    if configured:
        return Path(configured)
    if _DOCKER_ROOT_CACHE is None:
        try:
            proc = subprocess.run(
                ["docker", "info", "--format", "{{.DockerRootDir}}"],
                capture_output=True, text=True, timeout=10,
            )
            _DOCKER_ROOT_CACHE = (proc.stdout or "").strip() if proc.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("docker info probe failed", exc_info=True)
            _DOCKER_ROOT_CACHE = ""
    return Path(_DOCKER_ROOT_CACHE) if _DOCKER_ROOT_CACHE else None


def _port_owner(port: int) -> str:
    """Container publishing `port`, for a readable conflict message."""
    try:
        proc = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("docker ps probe failed for the port preflight", exc_info=True)
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


def _preflight_ports(manifest: dict, pkg_id: str) -> None:
    """Fail a fresh install before it downloads anything when a port is taken.

    EthOS embeds the package UI from the port in `ui.port`, so a collision is not
    a cosmetic problem: `docker compose up` dies with
    ``Bind for 127.0.0.1:3000 failed: port is already allocated`` after the files
    are already deployed, which reads like a broken package. Name the port and
    the container holding it instead.
    """
    ports = []
    for candidate in ((manifest.get("ui") or {}).get("port"),
                      (manifest.get("healthcheck") or {}).get("port")):
        try:
            port = int(candidate or 0)
        except (TypeError, ValueError):
            port = 0
        if 0 < port < 65536 and port not in ports:
            ports.append(port)
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                owner = _port_owner(port)
                where = f" by {owner}" if owner else ""
                logger.warning("Package %s: port %d is already in use%s", pkg_id, port, where)
                raise PackageError(
                    f"Port {port} is already used{where}. {manifest.get('name') or pkg_id} "
                    f"needs it for its window — free the port or stop that container first.",
                    409,
                ) from None
    logger.debug("Package %s: ports %s are free", pkg_id, ports)


def _preflight_disk(pkg_id: str) -> None:
    """Refuse an install/update that cannot fit the configured free-space reserve.

    Pulling images and writing a wheelhouse is the heaviest write the core does;
    a package that dies at 90 % leaves containers and partial state behind, so
    the reserve is checked before anything is downloaded — on the package store
    **and** on the Docker root, which is usually a different filesystem.
    """
    required_mb = max(0, int(settings.PACKAGES_MIN_FREE_MB))
    if required_mb <= 0:
        return
    targets = [Path(settings.DATA_DIR)]
    root = _docker_root()
    if root is not None and str(root) not in {str(t) for t in targets}:
        targets.append(root)
    for target in targets:
        try:
            free = shutil.disk_usage(str(target)).free
        except OSError:
            logger.warning("Cannot read free space for %s", target, exc_info=True)
            continue
        if free < required_mb * 1024 * 1024:
            raise PackageError(
                f"Not enough free disk space for {pkg_id} on {target}: "
                f"{free // (1024 * 1024)} MB available, {required_mb} MB required. "
                f"Free some space and retry",
                507,
            )


def _runtime(record: dict) -> str:
    return str(record.get("runtime") or "compose")


def compose_file(pkg_id: str) -> Optional[Path]:
    check_pkg_id(pkg_id)
    path = packages_store.package_dir(pkg_id) / "docker-compose.yml"
    return path if path.exists() else None


def _compose_cmd(pkg_id: str, path: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(path), "-p", pkg_id, *args]


def _compose_env() -> dict:
    """Environment for `docker compose`, including the EthOS service identity.

    Compose interpolates `${VAR}` from this environment, so a manifest can run its
    container with the same uid/gid as EthOS (`PUID: "${ETHOS_SERVICE_UID}"`) and
    files it creates on /mnt or /media stay editable by the NAS and its accounts.
    """
    env = dict(os.environ)
    env.setdefault("ETHOS_SERVICE_UID", str(os.getuid()))
    env.setdefault("ETHOS_SERVICE_GID", str(os.getgid()))
    env.setdefault("ETHOS_DATA_DIR", str(settings.DATA_DIR))
    return env


def _run_compose(cmd: list[str], env: Optional[dict] = None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=COMPOSE_TIMEOUT,
                          env=env)


async def _compose(pkg_id: str, path: Path, *args: str) -> None:
    try:
        proc = await asyncio.to_thread(_run_compose, _compose_cmd(pkg_id, path, *args),
                                       _compose_env())
    except FileNotFoundError:
        raise PackageError("docker is not available on this system", 503) from None
    except subprocess.TimeoutExpired as exc:
        raise PackageError(f"docker compose {args[0]} timed out", 504) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise PackageError(f"docker compose {args[0]} failed: {detail}", 500)


async def _status(pkg_id: str, record: Optional[dict] = None) -> str:
    record = record if record is not None else packages_store.get_package(pkg_id)
    if record and _runtime(record) == "venv":
        return package_runtime.status(pkg_id)
    if record and _runtime(record) == "files":
        return "ready"
    if compose_file(pkg_id) is None:
        return "not_configured"
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "ls", "--format", "json"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        logger.debug("compose status probe failed for %s", pkg_id, exc_info=True)
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    try:
        for project in json.loads(proc.stdout or "[]"):
            if project.get("Name") == pkg_id:
                status = str(project.get("Status") or "").lower()
                if "running" in status:
                    return "running"
                if "exited" in status or "created" in status:
                    return "stopped"
                return status or "unknown"
    except ValueError:
        return "unknown"
    return "stopped"


def _parse_compose_ps(raw: str) -> list[dict]:
    """Parse `docker compose ps --format json` (a JSON array or one object per line)."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _container_view(row: dict) -> dict:
    exit_code = row.get("ExitCode")
    return {
        "name": str(row.get("Name") or row.get("Service") or ""),
        "service": str(row.get("Service") or ""),
        "state": str(row.get("State") or ""),
        "health": str(row.get("Health") or ""),
        "exit_code": exit_code if isinstance(exit_code, int) else None,
    }


def aggregate_health(containers: list[dict]) -> str:
    """`unhealthy` wins, then `starting`, then `healthy`, otherwise `none`."""
    health = [str(item.get("health") or "") for item in containers if item.get("health")]
    if "unhealthy" in health:
        return "unhealthy"
    if "starting" in health:
        return "starting"
    if health and all(item == "healthy" for item in health):
        return "healthy"
    return "none"


async def compose_ps(pkg_id: str, *, is_compose: Optional[bool] = None) -> list[dict]:
    """Per-container rows for a compose package (empty list when the probe fails)."""
    check_pkg_id(pkg_id)
    if is_compose is False or compose_file(pkg_id) is None:
        return []
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            ["docker", "compose", "-p", pkg_id, "ps", "--format", "json", "--all"],
            capture_output=True, text=True, timeout=20, env=_compose_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.debug("compose ps probe failed for %s", pkg_id, exc_info=True)
        return []
    if proc.returncode != 0:
        logger.debug("compose ps for %s exited %s: %s", pkg_id, proc.returncode,
                     (proc.stderr or "").strip()[:200])
        return []
    return [_container_view(row) for row in _parse_compose_ps(proc.stdout)]


async def package_status(pkg_id: str) -> dict:
    check_pkg_id(pkg_id)
    record = packages_store.get_package(pkg_id)
    runtime = _runtime(record or {})
    containers = await compose_ps(pkg_id, is_compose=runtime == "compose")
    return {
        "package": pkg_id,
        "status": await _status(pkg_id, record),
        "runtime": runtime,
        "health": aggregate_health(containers),
        "containers": containers,
    }


async def compose_action(pkg_id: str, action: str) -> dict:
    check_pkg_id(pkg_id)
    if action not in {"start", "stop", "restart"}:
        raise PackageError("Action must be start, stop or restart", 400)
    record = packages_store.get_package(pkg_id)
    if record and _runtime(record) == "files":
        raise PackageError("This package has no service to start or stop", 400)
    if record and _runtime(record) == "venv":
        if action == "stop":
            await package_runtime.stop(pkg_id)
        elif action == "start":
            await package_runtime.start(pkg_id, record)
        else:
            await package_runtime.restart(pkg_id)
        state = package_runtime.status(pkg_id)
        emit_event("info", f"Package {action}", f"{pkg_id}: {state}", source="packages")
        return {"ok": True, "package": pkg_id, "action": action, "status": state}
    path = compose_file(pkg_id)
    if path is None:
        return {"ok": True, "package": pkg_id, "action": action, "status": "not_configured"}
    args = {"start": ("up", "-d"), "stop": ("down",), "restart": ("up", "-d", "--force-recreate")}[action]
    await _compose(pkg_id, path, *args)
    status = await _status(pkg_id)
    emit_event("info", f"Package {action}", f"{pkg_id}: {status}", source="packages")
    return {"ok": True, "package": pkg_id, "action": action, "status": status}


def _write_record(manifest: dict, entry: dict, source: dict) -> dict:
    record = {
        **manifest,
        "runtime": manifest.get("runtime") or "compose",
        "source": source.get("url", ""),
        "source_name": source.get("name", ""),
        "trust": source.get("trust", "unverified"),
        "key_id": source.get("key_id", ""),
        "manifest_url": entry.get("manifest_url", ""),
        "manifest_sha256": entry.get("manifest_sha256"),
        "installed_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    return record


def _installable(manifest: dict) -> str:
    """Reject a manifest that conflicts with what is already installed."""
    installed = {str(p.get("id")) for p in packages_store.load_installed()}
    for conflict_id in manifest.get("conflicts") or []:
        if conflict_id in installed:
            raise PackageError(
                f"Conflicts with installed package {conflict_id!r}; uninstall it first", 409,
            )
    return ""


async def _ensure_requires(manifest: dict, job_id: str, seen: set[str] | None = None,
                           depth: int = 0) -> None:
    """Install missing dependencies depth-first, before the package itself.

    Dependencies are installed inside the caller's job so the UI shows one
    operation; each one writes its own log lines and is rolled back by the
    normal install path if it fails.
    """
    seen = set(seen or ())
    requires = list(manifest.get("requires") or [])
    if not requires:
        return
    if depth > 5:
        raise PackageError("Dependency chain is too deep (max 5 levels)", 409)
    installed = {str(p.get("id")) for p in packages_store.load_installed()}
    for dep_id in requires:
        if dep_id == manifest.get("id"):
            raise PackageError(f"Package {dep_id!r} requires itself", 409)
        if dep_id in installed:
            continue
        if dep_id in seen:
            raise PackageError(f"Dependency cycle detected at {dep_id!r}", 409)
        entry = await package_repo.get_catalog_entry(dep_id)
        dep_manifest = await package_repo.fetch_manifest(entry)
        check_min_ethos_version(dep_manifest, APP_VERSION)
        _installable(dep_manifest)
        await _ensure_requires(dep_manifest, job_id, seen | {dep_id}, depth + 1)
        package_jobs.log(job_id, f"installing dependency {dep_id} v{dep_manifest['version']}")
        await install(entry, job_id)


async def _stage_package(entry: dict, job_id: str, manifest: Optional[dict] = None) -> tuple[dict, Path]:
    source = next((s for s in packages_store.load_sources() if s.get("url") == entry.get("source")), None)
    if not source:
        raise PackageError("Package source is no longer configured", 409)
    package_jobs.set_step(job_id, "verify")
    manifest = manifest or await package_repo.fetch_manifest(entry)
    check_min_ethos_version(manifest, APP_VERSION)
    pkg_id = manifest["id"]
    package_jobs.log(job_id, f"manifest verified for {pkg_id} v{manifest['version']}")

    staging = packages_store.STAGING_DIR / f"{pkg_id}-{int(time.time())}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    package_jobs.set_step(job_id, "download")
    compose = manifest.get("compose")
    files = manifest.get("files") or []
    total_units = len(files) + (1 if compose or manifest.get("compose_url") else 0)
    done_units = 0
    if compose:
        await asyncio.to_thread(
            package_repo.download_verified, compose["url"], staging / "docker-compose.yml", compose["sha256"],
        )
        done_units += 1
        package_jobs.set_step_progress(job_id, "download", done_units, total_units)
        package_jobs.log(job_id, "compose file verified")
    elif manifest.get("compose_url"):
        # Legacy v1 package from an unverified source: no checksum available.
        data = await asyncio.to_thread(
            package_repo.get_bytes, manifest["compose_url"], settings.PACKAGES_MAX_ARTIFACT_BYTES,
        )
        (staging / "docker-compose.yml").write_bytes(data)
        done_units += 1
        package_jobs.set_step_progress(job_id, "download", done_units, total_units)
        package_jobs.log(job_id, "legacy compose file downloaded (unverified)")

    for item in files:
        target = staging / item["path"]
        await asyncio.to_thread(package_repo.download_verified, item["url"], target, item["sha256"])
        try:
            target.chmod(int(item.get("mode") or 0o644))
        except OSError:
            logger.debug("Could not chmod %s", target, exc_info=True)
        done_units += 1
        package_jobs.set_step_progress(job_id, "download", done_units, total_units)
        package_jobs.log(job_id, f"file verified: {item['path']}")

    package_jobs.set_step(job_id, "policy")
    compose_path = staging / "docker-compose.yml"
    if compose_path.exists():
        warnings = enforce_compose(compose_path.read_text(encoding="utf-8"),
                                  manifest.get("capabilities"),
                                  package_id=pkg_id, package_dir=staging)
        for warning in warnings:
            package_jobs.log(job_id, f"warning: {warning}")
        package_jobs.log(job_id, "compose policy passed")
    elif manifest.get("runtime") == "venv":
        package_jobs.log(job_id, "no compose service; venv package service")
    else:
        package_jobs.log(job_id, "no compose service; package is files-only")

    python_cfg = manifest.get("python")
    if python_cfg:
        package_jobs.set_step(job_id, "wheels")
        await asyncio.to_thread(package_wheels.stage, staging, python_cfg, job_id)

    (staging / "ethos-package.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest, staging


async def install(entry: dict, job_id: str) -> None:
    pkg_id = entry["id"]
    if packages_store.get_package(pkg_id):
        raise PackageError(f"Package {pkg_id!r} is already installed", 409)
    manifest = await package_repo.fetch_manifest(entry)
    check_min_ethos_version(manifest, APP_VERSION)
    _installable(manifest)
    _preflight_disk(pkg_id)
    _preflight_ports(manifest, pkg_id)
    package_jobs.set_step(job_id, "requires")
    await _ensure_requires(manifest, job_id)
    manifest, staging = await _stage_package(entry, job_id, manifest)
    # `system_requires` (e.g. docker, samba) is resolved here, before anything is
    # deployed: a missing host dependency would otherwise surface as an opaque
    # compose failure after the files were already moved into place.
    try:
        await asyncio.to_thread(system_deps.ensure, manifest.get("system_requires"), job_id, pkg_id)
    except PackageError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    target = packages_store.package_dir(pkg_id)
    package_jobs.set_step(job_id, "deploy")
    if target.exists():
        # A previous install may have rolled back without removing its files
        # (root-owned entries survive `rmtree` as the service user). Reclaim the
        # directory instead of refusing to install forever.
        if not _discard_dir(target, "stale"):
            shutil.rmtree(staging, ignore_errors=True)
            raise PackageError(
                f"Stale package directory for {pkg_id} cannot be moved aside; "
                f"remove {target} on the NAS and retry", 409,
            )
        package_jobs.log(job_id, "warning: reclaimed a stale package directory")
    packages_store.APPS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(target))

    source = next((s for s in packages_store.load_sources() if s.get("url") == entry.get("source")), {})
    record = _write_record(manifest, entry, source)
    packages_store.add_package(record)
    package_jobs.log(job_id, "files deployed")

    if compose_file(pkg_id):
        package_jobs.set_step(job_id, "start")
        try:
            await _compose(pkg_id, compose_file(pkg_id), "up", "-d")
        except PackageError as exc:
            packages_store.remove_package(pkg_id)
            _discard_dir(target, "failed")
            raise PackageError(f"Service failed to start: {exc.detail}", exc.status) from exc
        package_jobs.log(job_id, "service started")
    elif _runtime(record) == "venv":
        # The venv must be created in its final location: pip writes absolute
        # paths and shebangs, so a venv built in staging would break on move.
        try:
            package_jobs.set_step(job_id, "venv")
            await package_runtime.build(target, record, job_id)
            package_jobs.set_step(job_id, "start")
            await package_runtime.start(pkg_id, record, job_id)
        except PackageError as exc:
            await package_runtime.stop(pkg_id)
            packages_store.remove_package(pkg_id)
            shutil.rmtree(target, ignore_errors=True)
            raise PackageError(f"Service failed to start: {exc.detail}", exc.status) from exc
        package_jobs.log(job_id, "package service started")

    package_jobs.set_step(job_id, "done", "done")
    emit_event("success", f"Package {manifest['name']} installed",
               f"v{manifest['version']} from {source.get('name') or source.get('url', '')}",
               source="packages")


async def update(pkg_id: str, job_id: str) -> None:
    check_pkg_id(pkg_id)
    old = packages_store.get_package(pkg_id)
    if not old:
        raise PackageError(f"Package {pkg_id!r} not found", 404)
    entry = await package_repo.get_catalog_entry(pkg_id)
    if not version_gt(str(entry.get("version", "")), str(old.get("version", ""))):
        raise PackageError(f"Already up to date (v{old.get('version')})", 400)
    manifest = await package_repo.fetch_manifest(entry)
    check_min_ethos_version(manifest, APP_VERSION)
    _installable(manifest)
    _preflight_disk(pkg_id)
    package_jobs.set_step(job_id, "requires")
    await _ensure_requires(manifest, job_id)
    manifest, staging = await _stage_package(entry, job_id, manifest)
    try:
        await asyncio.to_thread(system_deps.ensure, manifest.get("system_requires"), job_id, pkg_id)
    except PackageError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    target = packages_store.package_dir(pkg_id)
    backup = packages_store.BACKUP_DIR / f"{pkg_id}-{int(time.time())}"
    backup.parent.mkdir(parents=True, exist_ok=True)

    package_jobs.set_step(job_id, "backup")
    if _runtime(old) == "venv":
        await package_runtime.stop(pkg_id)
    if compose_file(pkg_id):
        await _compose(pkg_id, compose_file(pkg_id), "down")
    if target.exists():
        shutil.move(str(target), str(backup))
    shutil.move(str(staging), str(target))
    source = next((s for s in packages_store.load_sources() if s.get("url") == entry.get("source")), {})
    record = _write_record(manifest, entry, source)
    packages_store.replace_package(record)
    package_jobs.log(job_id, f"deployed v{manifest['version']}")

    try:
        if compose_file(pkg_id):
            package_jobs.set_step(job_id, "start")
            await _compose(pkg_id, compose_file(pkg_id), "up", "-d")
        elif _runtime(record) == "venv":
            package_jobs.set_step(job_id, "venv")
            await package_runtime.build(target, record, job_id)
            package_jobs.set_step(job_id, "start")
            await package_runtime.start(pkg_id, record, job_id)
    except PackageError as exc:
        logger.error("Update %s failed to start; rolling back: %s", pkg_id, exc.detail)
        await package_runtime.stop(pkg_id)
        _discard_dir(target, "failed")
        if backup.exists():
            shutil.move(str(backup), str(target))
        packages_store.replace_package(old)
        if _runtime(old) == "venv":
            try:
                await package_runtime.start(pkg_id, old)
            except PackageError as rollback_exc:
                logger.error("Rollback start failed for %s: %s", pkg_id, rollback_exc.detail)
        elif compose_file(pkg_id):
            try:
                await _compose(pkg_id, compose_file(pkg_id), "up", "-d")
            except PackageError as rollback_exc:
                logger.error("Rollback start failed for %s: %s", pkg_id, rollback_exc.detail)
        raise PackageError(f"Update failed to start; previous version restored: {exc.detail}", exc.status) from exc

    shutil.rmtree(backup, ignore_errors=True)
    package_jobs.set_step(job_id, "done", "done")
    emit_event("success", f"Package {manifest['name']} updated",
               f"v{old.get('version')} → v{manifest['version']}", source="packages")


async def uninstall(pkg_id: str, job_id: str, remove_volumes: bool = False,
                    remove_images: bool = False, remove_data: bool = False) -> None:
    """Tear the package down.

    ``remove_volumes`` deletes the named volumes declared by the package's
    compose file; ``remove_images`` deletes the images it referenced, but only
    those no container uses any more; ``remove_data`` deletes
    ``<DATA_DIR>/packages/data/<id>``, the state every runtime keeps across a
    reinstall. All three default to off: package state (scan logs, certificates,
    settings) is normally worth keeping.
    """
    check_pkg_id(pkg_id)
    record = packages_store.get_package(pkg_id)
    if not record:
        raise PackageError(f"Package {pkg_id!r} not found", 404)
    dependents = [
        str(p.get("id")) for p in packages_store.load_installed()
        if pkg_id in (p.get("requires") or []) and p.get("id") != pkg_id
    ]
    if dependents:
        raise PackageError(
            f"Package {pkg_id!r} is required by {', '.join(dependents)}; uninstall those first", 409,
        )
    path = compose_file(pkg_id)
    package_jobs.set_step(job_id, "stop")
    if _runtime(record) == "venv":
        await package_runtime.stop(pkg_id)
    if path:
        # The compose file must outlive `down` for the image discovery below.
        await _compose(pkg_id, path, "down", *(["--volumes"] if remove_volumes else []))
    if remove_images and path:
        package_jobs.set_step(job_id, "images")
        removable, shared = await package_cleanup.plan(path, pkg_id)
        for entry in shared:
            owners = ", ".join(str(o.get("container") or "?") for o in entry.get("in_use_by") or [])
            package_jobs.log(job_id, f"Keeping image {entry['ref']} — in use by {owners}")
        for result in await package_cleanup.remove_images(removable):
            suffix = f": {result['detail']}" if result.get("detail") else ""
            package_jobs.log(job_id, f"{'Removed' if result['ok'] else 'Kept'} image {result['ref']}{suffix}")
    # Host software goes with the last package that needed it (refcounted). This
    # runs while the record still exists — release() excludes this id itself.
    package_jobs.set_step(job_id, "sysdeps")
    await asyncio.to_thread(system_deps.release, pkg_id, job_id)
    package_jobs.set_step(job_id, "cleanup")
    packages_store.remove_package(pkg_id)
    target = packages_store.package_dir(pkg_id)
    if target.exists():
        _discard_dir(target, "uninstall")
    if remove_data:
        package_jobs.set_step(job_id, "data")
        state_dir = packages_store.data_dir(pkg_id)
        freed = packages_store.dir_size(state_dir)
        shutil.rmtree(state_dir, ignore_errors=True)
        if state_dir.exists():
            package_jobs.log(job_id, f"warning: could not delete every file in {state_dir}")
            logger.warning("Package data for %s was only partly deleted: %s", pkg_id, state_dir)
        else:
            package_jobs.log(job_id, f"deleted {freed} bytes of package data")
    package_jobs.set_step(job_id, "done", "done")
    kept = [] if remove_data else ["app data"]
    if not remove_volumes:
        kept.append("volume data")
    summary = "all package data deleted" if not kept else f"kept {' and '.join(kept)}"
    emit_event("info", f"Package {record.get('name', pkg_id)} uninstalled",
               f"v{record.get('version', '')} · {summary}", source="packages")
