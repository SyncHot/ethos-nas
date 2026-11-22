"""Install / update / uninstall a package as a Compose service.

The install is staged first: manifests and files are downloaded and verified
into a staging directory, the compose file is linted, and only then is the
package directory swapped in. Updates keep a backup of the previous directory
and restore it if the new service fails to start.
"""

import asyncio
import json
import logging
import shutil
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


def _runtime(record: dict) -> str:
    return str(record.get("runtime") or "compose")


def compose_file(pkg_id: str) -> Optional[Path]:
    check_pkg_id(pkg_id)
    path = packages_store.package_dir(pkg_id) / "docker-compose.yml"
    return path if path.exists() else None


def _compose_cmd(pkg_id: str, path: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(path), "-p", pkg_id, *args]


def _run_compose(cmd: list[str]):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=COMPOSE_TIMEOUT)


async def _compose(pkg_id: str, path: Path, *args: str) -> None:
    try:
        proc = await asyncio.to_thread(_run_compose, _compose_cmd(pkg_id, path, *args))
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


async def package_status(pkg_id: str) -> dict:
    check_pkg_id(pkg_id)
    record = packages_store.get_package(pkg_id)
    return {
        "package": pkg_id,
        "status": await _status(pkg_id, record),
        "runtime": _runtime(record or {}),
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
                    remove_images: bool = False) -> None:
    """Tear the package down.

    ``remove_volumes`` deletes the named volumes declared by the package's
    compose file; ``remove_images`` deletes the images it referenced, but only
    those no container uses any more. Both default to off: package state (scan
    logs, certificates, settings) is normally worth keeping across a reinstall.
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
    package_jobs.set_step(job_id, "done", "done")
    summary = "volume data deleted" if remove_volumes else "volumes kept"
    emit_event("info", f"Package {record.get('name', pkg_id)} uninstalled",
               f"v{record.get('version', '')} · {summary}", source="packages")
