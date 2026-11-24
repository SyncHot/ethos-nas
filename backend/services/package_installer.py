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
import subprocess
import time
from pathlib import Path
from typing import Optional

import yaml

from backend.config import APP_VERSION, settings
from backend.services import (
    package_cleanup, package_env, package_jobs, package_ports, package_repo,
    package_runtime, package_state, package_wheels, packages_store, system_deps,
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
        if package_ports.port_free(port):
            continue
        owner = package_ports.port_owner(port)
        where = f" by {owner}" if owner else ""
        logger.warning("Package %s: port %d is already in use%s", pkg_id, port, where)
        raise PackageError(
            f"Port {port} is already used{where}. {manifest.get('name') or pkg_id} "
            f"needs it for its window — free the port or stop that container first.",
            409,
        )
    logger.debug("Package %s: ports %s are free", pkg_id, ports)


def _reserved_ports(pkg_id: str) -> set:
    """Host ports the *other* installed packages claim, so two installs cannot
    pick the same remapped port."""
    reserved = set()
    for record in packages_store.load_installed():
        if str(record.get("id")) == pkg_id:
            continue
        for section in ("ui", "healthcheck"):
            try:
                port = int((record.get(section) or {}).get("port") or 0)
            except (TypeError, ValueError):
                port = 0
            if port:
                reserved.add(port)
        for port in ((record.get("port_remap") or {}).get("reserved") or []):
            reserved.add(int(port))
    return reserved


def _port_plan(pkg_id: str, manifest: dict, package_dir: Path, *,
               previous: Optional[dict] = None) -> dict:
    """Effective host ports for a package, re-mapping a busy one instead of failing.

    Signed compose files publish a fixed port, and that port is regularly taken
    by another project on the host. When that happens the *published* side is
    remapped (a `docker-compose.override.yml` with `ports: !override`) and the
    new port goes into the record, which the window, the proxy and the health
    probe read. Returns `{"ui": port|None, "healthcheck": port|None, "remap": {}}`
    — `remap` is `{old: new}` and empty when nothing changed.
    """
    ui = (manifest.get("ui") or {})
    health = (manifest.get("healthcheck") or {})

    def _as_port(value) -> int:
        try:
            port = int(value or 0)
        except (TypeError, ValueError):
            return 0
        return port if 0 < port < 65536 else 0

    ui_port = _as_port(ui.get("port"))
    health_port = _as_port(health.get("port"))
    plan = {"ui": ui_port or None, "healthcheck": health_port or None, "remap": {}}
    if not ui_port:
        return plan

    reserved = _reserved_ports(pkg_id)
    keep = _as_port((previous or {}).get("to"))
    if keep and keep not in reserved and package_ports.port_free(keep):
        # Sticky: the URL the user already opened keeps working across updates.
        new_port = keep
    elif not package_ports.port_free(ui_port) or ui_port in reserved:
        try:
            new_port = package_ports.allocate_port(ui_port, reserved=reserved)
        except ValueError:
            owner = package_ports.port_owner(ui_port)
            where = f" by {owner}" if owner else ""
            raise PackageError(
                f"Port {ui_port} is already used{where} and no free port was found "
                f"nearby. Free the port or stop that container first.", 409,
            ) from None
    else:
        return plan

    compose_path = Path(package_dir) / "docker-compose.yml"
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("Package %s: could not read the compose file for the port remap: %s",
                     pkg_id, exc, exc_info=True)
        compose = {}
    mapping = {ui_port: new_port}
    if health_port and health_port != ui_port and not package_ports.port_free(health_port):
        mapping[health_port] = package_ports.allocate_port(health_port, reserved=reserved | {new_port})
    text = package_ports.build_override(compose, mapping)
    if not text:
        # Nothing in the compose file publishes that port (e.g. the UI is served
        # through the package proxy) — leave the record alone.
        logger.warning("Package %s: port %d is busy but no compose mapping uses it",
                       pkg_id, ui_port)
        return plan
    package_ports.write_override(package_dir, text)
    owner = package_ports.port_owner(ui_port)
    logger.info("Package %s: host port %d is in use%s — published on %d instead",
                pkg_id, ui_port, f" by {owner}" if owner else "", new_port)
    plan["ui"] = new_port
    if health_port == ui_port:
        plan["healthcheck"] = new_port
    plan["remap"] = {ui_port: new_port}
    plan["reserved"] = sorted(mapping.values())
    return plan


def _apply_port_plan(record: dict, plan: dict) -> dict:
    """Copy the record with the effective ports (never mutates the manifest)."""
    ui_port = plan.get("ui")
    if not ui_port:
        return record
    record["ui"] = {**(record.get("ui") or {}), "port": ui_port}
    if record.get("healthcheck") and plan.get("healthcheck"):
        record["healthcheck"] = {**(record.get("healthcheck") or {}),
                                 "port": plan["healthcheck"]}
    if plan.get("remap"):
        record["port_remap"] = {
            "from": next(iter(plan["remap"])),
            "to": ui_port,
            "reserved": plan.get("reserved") or [],
        }
    return record


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
    """Compose invocation, including the generated port override when present.

    `docker compose` only auto-loads `docker-compose.override.yml` for the
    default file name, and this one has to be explicit: it is what moves a
    package's published host port when the requested one is taken.
    """
    cmd = ["docker", "compose", "-f", str(path)]
    override = package_ports.override_path(path.parent)
    if override.exists():
        cmd += ["-f", str(override)]
    return [*cmd, "-p", pkg_id, *args]


def _settings_defs(record: Optional[dict]) -> list[dict]:
    """Settings a package declared (the manifest copy is kept in the record)."""
    defs = (record or {}).get("settings") or []
    return [d for d in defs if isinstance(d, dict) and d.get("key") and d.get("env")]


def validate_settings_values(definitions: list[dict], provided: Optional[dict], *,
                             base: Optional[dict] = None,
                             enforce_required: bool = True) -> dict:
    """Merge settings over their current values/defaults and validate them.

    `provided` may only contain declared keys. Values are always strings and the
    declared length limits are enforced here, so a broken client cannot push an
    oversized value into the package record. Secrets are never logged.
    """
    if provided is not None and not isinstance(provided, dict):
        raise PackageError("Settings must be an object", 400)
    known = {str(d["key"]) for d in definitions}
    unknown = sorted({str(key) for key in (provided or {})} - known)
    if unknown:
        raise PackageError(f"Unknown setting: {unknown[0]}", 400)
    merged = {str(key): str(value) for key, value in (base or {}).items() if str(key) in known}
    merged.update({str(key): str(value) for key, value in (provided or {}).items()})
    values: dict[str, str] = {}
    for definition in definitions:
        key = str(definition["key"])
        label = str(definition.get("label") or key)
        value = str(merged.get(key) or definition.get("default") or "")
        if definition.get("required") and not value:
            if enforce_required:
                raise PackageError(f"{label} is required", 400)
            logger.warning("Package setting %s is empty; the service may refuse to start", key)
        if value:
            minimum = int(definition.get("min_length") or 0)
            maximum = int(definition.get("max_length") or 256)
            if len(value) < minimum:
                raise PackageError(f"{label} must be at least {minimum} characters", 400)
            if len(value) > maximum:
                raise PackageError(f"{label} must be at most {maximum} characters", 400)
        values[key] = value
    return values


def _default_data_dir(pkg_id: str) -> Path:
    """Where a package's data lives when the admin did not pick a folder."""
    return packages_store.data_dir(pkg_id)


def validate_data_dir(pkg_id: str, value) -> Path:
    """Resolve the host folder a package may keep its data in.

    Admins may pick any folder under /home, /mnt, /media or the EthOS data root,
    but never a root itself, the package installation area or a path outside
    those trees. An empty value means the default (`packages/data/<id>`).
    """
    default = _default_data_dir(pkg_id)
    if value is None:
        return default
    if not isinstance(value, str):
        raise PackageError("Data folder must be a path", 400)
    raw = value.strip()
    if not raw:
        return default
    if "\x00" in raw:
        raise PackageError("Data folder contains an invalid character", 400)
    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        raise PackageError("Data folder must be an absolute path", 400)
    resolved = candidate.resolve()
    data_root = settings.DATA_DIR.resolve()
    roots = (Path("/home"), Path("/mnt"), Path("/media"), data_root)
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise PackageError(
            "Data folder must be inside /home, /mnt, /media or the EthOS data folder", 400,
        )
    if resolved in (Path("/home"), Path("/mnt"), Path("/media"), data_root):
        raise PackageError("Choose a folder below that path, not the root itself", 400)
    packages_root = packages_store.PACKAGES_ROOT.resolve()
    forbidden = (packages_root / "apps", packages_root / ".staging", packages_root / ".backup")
    if any(resolved == item or item in resolved.parents for item in forbidden):
        raise PackageError("Data folder cannot be inside the package installation area", 400)
    return resolved


def ensure_data_dir(path: Path) -> None:
    """Create the data folder; the container's entrypoint fixes ownership."""
    created = not path.exists()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PackageError(
            f"Cannot create the data folder {path}: {exc.strerror or exc}", 400,
        ) from exc
    if created:
        try:
            path.chmod(0o775)
        except OSError:
            logger.warning("Could not set permissions on the data folder %s", path,
                           exc_info=True)


def _compose_env(pkg_id: Optional[str] = None) -> dict:
    """Environment for `docker compose`, including the EthOS service identity.

    Compose interpolates `${VAR}` from this environment, so a manifest can run its
    container with the same uid/gid as EthOS (`PUID: "${ETHOS_SERVICE_UID}"`) and
    files it creates on /mnt or /media stay editable by the NAS and its accounts.
    A package's user settings are added too: `${GF_SECURITY_ADMIN_PASSWORD}` in the
    signed compose file resolves to the value stored with the package record.
    """
    env = dict(os.environ)
    env.setdefault("ETHOS_SERVICE_UID", str(os.getuid()))
    env.setdefault("ETHOS_SERVICE_GID", str(os.getgid()))
    env.setdefault("ETHOS_DATA_DIR", str(settings.DATA_DIR))
    if pkg_id:
        env.update(package_env.compose_vars(packages_store.get_package(pkg_id)))
    return env


def _run_compose(cmd: list[str], env: Optional[dict] = None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=COMPOSE_TIMEOUT,
                          env=env)


async def _compose(pkg_id: str, path: Path, *args: str) -> None:
    try:
        proc = await asyncio.to_thread(_run_compose, _compose_cmd(pkg_id, path, *args),
                                       _compose_env(pkg_id))
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
            capture_output=True, text=True, timeout=20, env=_compose_env(pkg_id),
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


async def repair_ports(pkg_id: str) -> dict:
    """Re-align a package's published host port with reality.

    The port is chosen at install time, but it can be lost later: a NAS boot may
    start another compose project first, a user starts a dev server, or someone
    adds a container by hand. The package container then dies on a port it can no
    longer bind. This re-plans the mapping (keeping the current remap while it is
    free, otherwise picking a new one), rewrites the override, updates the record
    and recreates the service when the published port changed.
    """
    check_pkg_id(pkg_id)
    record = packages_store.get_package(pkg_id)
    if not record:
        raise PackageError(f"Package {pkg_id!r} not found", 404)
    if not compose_file(pkg_id):
        raise PackageError("Only compose packages publish a host port", 400)
    target = packages_store.package_dir(pkg_id)
    previous_port = int((record.get("ui") or {}).get("port") or 0)
    plan = _port_plan(pkg_id, record, target, previous=record.get("port_remap"))
    updated = _apply_port_plan({**record}, plan)
    new_port = int((updated.get("ui") or {}).get("port") or 0)
    changed = bool(new_port and new_port != previous_port)
    if changed or plan.get("remap"):
        packages_store.replace_package(updated)
    await _compose(pkg_id, compose_file(pkg_id), "up", "-d",
                   *(["--force-recreate"] if changed else []))
    logger.info("Package %s: port repair finished (port %s -> %s, recreated=%s)",
                pkg_id, previous_port, new_port or previous_port, changed)
    return {
        "id": pkg_id,
        "name": updated.get("name") or pkg_id,
        "port": new_port or previous_port,
        "previous_port": previous_port,
        "remapped": bool(plan.get("remap")),
        "recreated": changed,
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


async def apply_settings(pkg_id: str, provided: Optional[dict],
                         data_dir: Optional[str] = None) -> dict:
    """Store new settings for an installed package and recreate its service.

    The compose file is signed and fixed; the values reach it through the
    environment as `${VAR}`. A compose package is force-recreated so a changed
    value takes effect immediately (`restart` alone keeps the old environment).
    `data_dir` is `None` when the caller did not touch the folder, an empty
    string to go back to the default, and a path to move to it. Files are not
    moved: the service starts on the new folder with an empty data set.
    """
    check_pkg_id(pkg_id)
    record = packages_store.get_package(pkg_id)
    if not record:
        raise PackageError(f"Package {pkg_id!r} not found", 404)
    definitions = _settings_defs(record)
    if not definitions and not record.get("data"):
        raise PackageError("This package has no configurable settings", 400)
    values = validate_settings_values(
        definitions, provided, base=record.get("settings_values") or {},
    )
    current_dir = str(record.get("data_dir") or "") if record.get("data") else ""
    new_dir = current_dir
    if record.get("data") and data_dir is not None:
        new_dir = str(validate_data_dir(pkg_id, data_dir))
    if values == (record.get("settings_values") or {}) and new_dir == current_dir:
        return {"ok": True, "package": pkg_id, "changed": False, "recreated": False,
                "data_dir": new_dir or None}
    updated = {
        **record, "settings_values": values, "settings_updated_at": _now_iso(),
    }
    if record.get("data"):
        updated["data_dir"] = new_dir
    packages_store.replace_package(updated)
    if record.get("data") and new_dir != current_dir:
        await asyncio.to_thread(ensure_data_dir, Path(new_dir))
    recreated = False
    path = compose_file(pkg_id)
    if _runtime(record) == "compose" and path is not None:
        await _compose(pkg_id, path, "up", "-d", "--force-recreate")
        recreated = True
    emit_event("info", f"Package {record.get('name') or pkg_id} settings updated",
               "Service recreated with the new settings" if recreated
               else "Settings stored", source="packages")
    logger.info("Package %s: settings updated (%d field(s), data folder changed=%s, recreated=%s)",
                pkg_id, len(values), new_dir != current_dir, recreated)
    return {"ok": True, "package": pkg_id, "changed": True, "recreated": recreated,
            "data_dir": new_dir or None}


def _write_record(manifest: dict, entry: dict, source: dict,
                  settings: Optional[dict] = None,
                  data_dir: Optional[Path] = None) -> dict:
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
        "settings_values": dict(settings or {}),
    }
    if manifest.get("data"):
        record["data_dir"] = str(data_dir or _default_data_dir(record["id"]))
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


async def install(entry: dict, job_id: str, provided_settings: Optional[dict] = None,
                  data_dir: Optional[str] = None) -> None:
    pkg_id = entry["id"]
    if packages_store.get_package(pkg_id):
        raise PackageError(f"Package {pkg_id!r} is already installed", 409)
    manifest = await package_repo.fetch_manifest(entry)
    check_min_ethos_version(manifest, APP_VERSION)
    _installable(manifest)
    # Reject bad values before a byte is downloaded; defaults apply for fields
    # the install form left untouched.
    provided_settings = validate_settings_values(_settings_defs(manifest), provided_settings)
    data_folder: Optional[Path] = None
    if manifest.get("data"):
        data_folder = validate_data_dir(pkg_id, data_dir)
    _preflight_disk(pkg_id)
    # A compose package can be re-mapped to a free port (`_port_plan`); a venv or
    # native one cannot — EthOS starts that process on the exact port itself.
    if not manifest.get("compose"):
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
    record = _write_record(manifest, entry, source, provided_settings, data_folder)
    if compose_file(pkg_id):
        plan = _port_plan(pkg_id, manifest, target)
        record = _apply_port_plan(record, plan)
        if plan.get("remap"):
            package_jobs.log(
                job_id,
                f"host port {next(iter(plan['remap']))} is busy — "
                f"publishing on {plan['ui']} instead",
            )
    if data_folder is not None:
        await asyncio.to_thread(ensure_data_dir, data_folder)
        package_jobs.log(job_id, f"data folder ready: {data_folder}")
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
    # Keep the user's settings across an update; a new manifest may add or drop
    # fields, so only declared keys survive and defaults fill the gaps.
    preserved = validate_settings_values(
        _settings_defs(manifest), None,
        base=(old or {}).get("settings_values") or {}, enforce_required=False,
    )
    data_folder: Optional[Path] = None
    if manifest.get("data"):
        # A folder chosen before the update stays in use; a stored path that is
        # no longer allowed falls back to the default instead of failing.
        try:
            data_folder = validate_data_dir(pkg_id, old.get("data_dir"))
        except PackageError:
            logger.warning("Stored data folder for %s is not allowed any more; using the default",
                           pkg_id, exc_info=True)
            data_folder = _default_data_dir(pkg_id)
    record = _write_record(manifest, entry, source, preserved, data_folder)
    if compose_file(pkg_id):
        plan = _port_plan(pkg_id, manifest, target, previous=old.get("port_remap"))
        record = _apply_port_plan(record, plan)
        if plan.get("remap"):
            package_jobs.log(
                job_id,
                f"host port {next(iter(plan['remap']))} is busy — "
                f"publishing on {plan['ui']} instead",
            )
    if data_folder is not None:
        await asyncio.to_thread(ensure_data_dir, data_folder)
        package_jobs.log(job_id, f"data folder ready: {data_folder}")
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
    reinstall, plus the core state a UI-only package owns (Cameras keeps its
    cameras in `surveillance.json`). All three default to off: package state
    (scan logs, certificates, settings) is normally worth keeping.
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
        if record.get("data") and record.get("data_dir"):
            # A package can keep its data outside the default tree; re-validate
            # before deleting anything so a corrupt record cannot point at /
            # or a shared root.
            try:
                state_dir = validate_data_dir(pkg_id, str(record["data_dir"]))
            except PackageError:
                logger.warning("Kept data folder %s for %s: path is no longer allowed",
                               record.get("data_dir"), pkg_id)
        freed = packages_store.dir_size(state_dir)
        shutil.rmtree(state_dir, ignore_errors=True)
        if state_dir.exists():
            package_jobs.log(job_id, f"warning: could not delete every file in {state_dir}")
            logger.warning("Package data for %s was only partly deleted: %s", pkg_id, state_dir)
        else:
            package_jobs.log(job_id, f"deleted {freed} bytes of package data")
        # A UI-only package (Cameras) keeps its configuration in the core, not
        # under packages/data/<id>; without this the engine kept restarting
        # pipelines for a package that no longer exists.
        core = package_state.purge(pkg_id)
        if core:
            if core["errors"]:
                package_jobs.log(job_id, f"warning: could not delete {len(core['errors'])} "
                                 f"core state path(s) for {pkg_id}")
                logger.warning("Core state for %s was only partly deleted: %s",
                               pkg_id, core["errors"])
            else:
                package_jobs.log(job_id,
                                 f"deleted {core['size_bytes']} bytes of {core['label']}")
    package_jobs.set_step(job_id, "done", "done")
    kept = [] if remove_data else ["app data"]
    if not remove_volumes:
        kept.append("volume data")
    summary = "all package data deleted" if not kept else f"kept {' and '.join(kept)}"
    emit_event("info", f"Package {record.get('name', pkg_id)} uninstalled",
               f"v{record.get('version', '')} · {summary}", source="packages")
