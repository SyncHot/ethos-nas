"""Compose policy for packages.

Packages run as containers, but a compose file can still ask for host
namespaces, devices, privileged mode or arbitrary host binds. These checks
reject the dangerous shapes before anything is deployed or started.

The device exceptions are the hardware ones a media/virtualisation package
cannot emulate: `/dev/kvm` (`capabilities: ["kvm"]`), the `/dev/dri` render
nodes (`["dri"]`, Intel/AMD VAAPI/QuickSync) and `/dev/nvidia*`
(`["nvidia"]`, NVENC). Every other device stays rejected.
"""

import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from backend.services.package_schema import PackageError
from backend.services.packages_store import APPS_DIR, PACKAGES_ROOT

logger = logging.getLogger("ethos-nas.services.compose_policy")

_ALLOWED_BIND_ROOTS = tuple(os.path.realpath(p) for p in ("/home", "/mnt", "/media"))
_FORBIDDEN_DEVICES = ("/dev/", "/var/run/docker.sock", "/run/docker.sock")
# Container runtimes a package may ask for. `nvidia` is the only non-default one
# and it needs the `nvidia` capability; anything else (kata, sysbox, …) is an
# unvetted escape hatch and stays rejected.
_ALLOWED_RUNTIMES = {"runc", "nvidia"}
# `deploy.resources.reservations.devices[].capabilities` the NVIDIA runtime takes.
_NVIDIA_DEVICE_CAPABILITIES = {"gpu", "video", "compute", "utility"}


def _device_capability(source: str) -> str | None:
    """Manifest capability that unlocks a `devices:` source path, or None."""
    if source == "/dev/kvm":
        return "kvm"
    if source == "/dev/dri" or source.startswith("/dev/dri/"):
        return "dri"
    if source.startswith("/dev/nvidia"):
        return "nvidia"
    return None


def _allowed_bind_roots(package_id: str | None = None, package_dir: str | Path | None = None) -> tuple[str, ...]:
    """Bind roots for one package.

    Shared data mounts are always allowed. The package store itself is not:
    a package may bind its own directory and its own data dir only, never
    another package, `installed.json` or `sources.json`.
    """
    roots = list(_ALLOWED_BIND_ROOTS)
    if package_dir:
        roots.append(os.path.realpath(str(package_dir)))
    if package_id:
        roots.append(os.path.realpath(str(APPS_DIR / package_id)))
        roots.append(os.path.realpath(str(PACKAGES_ROOT / "data" / package_id)))
    return tuple(dict.fromkeys(roots))


def _path_allowed(path: str, roots: tuple[str, ...]) -> bool:
    real = os.path.realpath(os.path.expanduser(path))
    packages_root = os.path.realpath(str(PACKAGES_ROOT))
    if real == packages_root or real.startswith(packages_root + os.sep):
        # Inside the package store only the package's own roots are allowed —
        # even when DATA_DIR itself sits under /home (the repo default), the
        # broad /home root must not expose installed.json or another package.
        return any(
            real == root or real.startswith(root + os.sep)
            for root in roots
            if root == packages_root or root.startswith(packages_root + os.sep)
        )
    return any(real == root or real.startswith(root + os.sep) for root in roots)


def _check_bind(source: str, service: str, errors: list[str], roots: tuple[str, ...]) -> None:
    source = str(source or "").strip()
    if not source or source.startswith("$"):
        return
    if any(source.startswith(device) for device in _FORBIDDEN_DEVICES):
        errors.append(f"{service}: bind mount of a host device/socket is not allowed: {source}")
        return
    if source.startswith("/") or source.startswith("~"):
        if not _path_allowed(source, roots):
            errors.append(f"{service}: bind mount outside allowed roots: {source}")
        return
    pure = PurePosixPath(source)
    if pure.is_absolute() or ".." in pure.parts:
        errors.append(f"{service}: relative bind mount must stay inside the package directory: {source}")


def _check_devices(devices: Any, service: str, capabilities: set[str], errors: list[str]) -> None:
    """Reject every `devices:` entry except the capability-gated allowlist."""
    if not devices:
        return
    if not isinstance(devices, list):
        errors.append(f"{service}: devices must be a list")
        return
    for entry in devices:
        if isinstance(entry, dict):
            source = str(entry.get("source") or "").strip()
        else:
            # Compose short syntax: "<host>:<container>[:perms]"
            source = str(entry or "").split(":", 1)[0].strip()
        required = _device_capability(source)
        if required is not None and required in capabilities:
            continue
        if required is not None:
            errors.append(f'{service}: device {source} requires capabilities: ["{required}"] in the manifest')
            continue
        errors.append(f"{service}: devices are not allowed: {entry}")


def _binds_share_rw(volumes: list) -> bool:
    """True when the service mounts one of the shared trees read-write."""
    for volume in volumes:
        if isinstance(volume, str):
            parts = volume.split(":")
            source, mode = parts[0], (parts[2] if len(parts) > 2 else "rw")
        elif isinstance(volume, dict):
            if str(volume.get("type") or "bind") != "bind":
                continue
            source = str(volume.get("source") or "")
            mode = "ro" if volume.get("read_only") else "rw"
        else:
            continue
        # Named volumes and package-relative paths are not share binds; only
        # absolute host paths can be one (a relative path must not be resolved
        # against the core's cwd, which usually lives under /home).
        raw = str(source or "").strip()
        if not (raw.startswith("/") or raw.startswith("~")):
            continue
        if "ro" in mode.split(","):
            continue
        real = os.path.realpath(os.path.expanduser(raw))
        if any(real == root or real.startswith(root + os.sep) for root in _ALLOWED_BIND_ROOTS):
            return True
    return False


def _declares_container_uid(service: dict) -> bool:
    """True when the service states which uid/gid writes to the shares."""
    if service.get("user"):
        return True
    env = service.get("environment") or {}
    if isinstance(env, dict):
        if any(key in {"PUID", "PGID", "USER_UID", "USER_GID"} for key in env):
            return True
        text = " ".join(str(value) for value in env.values())
    elif isinstance(env, list):
        if any(str(item).split("=", 1)[0] in {"PUID", "PGID", "USER_UID", "USER_GID"} for item in env):
            return True
        text = " ".join(str(item) for item in env)
    else:
        text = ""
    return "ETHOS_SERVICE_UID" in text or "ETHOS_SERVICE_GID" in text


def _check_gpu(service: dict, name: str, capabilities: set[str], errors: list[str]) -> None:
    """Gate `runtime:`, `gpus:` and GPU `deploy:` reservations on `nvidia`.

    The NVIDIA path needs a runtime the manifest cannot create, so it is a
    capability like the device exceptions: without it the manifest is rejected
    instead of silently starting a container that cannot see the GPU.
    """
    runtime = str(service.get("runtime") or "").strip().lower()
    if runtime:
        if runtime not in _ALLOWED_RUNTIMES:
            errors.append(f"{name}: runtime {runtime!r} is not allowed")
        elif runtime == "nvidia" and "nvidia" not in capabilities:
            errors.append(f'{name}: runtime nvidia requires capabilities: ["nvidia"] in the manifest')

    gpus = service.get("gpus")
    if gpus is not None:
        if "nvidia" not in capabilities:
            errors.append(f'{name}: gpus requires capabilities: ["nvidia"] in the manifest')
        else:
            for entry in gpus if isinstance(gpus, list) else [gpus]:
                text = str(entry or "").strip()
                if text != "all" and not text.startswith("device="):
                    errors.append(f"{name}: gpus entries must be 'all' or 'device=<ids>': {entry}")

    deploy = service.get("deploy")
    if deploy is None:
        return
    if "nvidia" not in capabilities:
        errors.append(f'{name}: deploy device reservations require capabilities: ["nvidia"] in the manifest')
        return
    _check_deploy_devices(deploy, name, errors)


def _check_deploy_devices(deploy: Any, name: str, errors: list[str]) -> None:
    """Only GPU device reservations may appear under `deploy`."""
    if not isinstance(deploy, dict):
        errors.append(f"{name}: deploy must be a mapping")
        return
    for key in sorted(deploy):
        if key != "resources":
            errors.append(f"{name}: deploy.{key} is not allowed; only GPU device reservations are")
            return
    resources = deploy.get("resources")
    if not isinstance(resources, dict):
        errors.append(f"{name}: deploy.resources must be a mapping")
        return
    for key in sorted(resources):
        if key != "reservations":
            errors.append(f"{name}: deploy.resources.{key} is not allowed; only device reservations are")
            return
    reservations = resources.get("reservations")
    if not isinstance(reservations, dict):
        errors.append(f"{name}: deploy.resources.reservations must be a mapping")
        return
    for key in sorted(reservations):
        if key != "devices":
            errors.append(f"{name}: deploy.resources.reservations.{key} is not allowed; only devices are")
            return
    devices = reservations.get("devices")
    if not isinstance(devices, list) or not devices:
        errors.append(f"{name}: deploy.resources.reservations.devices must be a non-empty list")
        return
    for entry in devices:
        _check_deploy_device_entry(entry, name, errors)


def _check_deploy_device_entry(entry: Any, name: str, errors: list[str]) -> None:
    """One `deploy…devices[]` reservation: driver nvidia, sane count/capabilities."""
    if not isinstance(entry, dict):
        errors.append(f"{name}: deploy device reservation must be a mapping")
        return
    driver = str(entry.get("driver") or "").strip().lower()
    if driver != "nvidia":
        errors.append(f"{name}: deploy device driver must be 'nvidia', got {driver or 'nothing'}")
    count = entry.get("count", "all")
    if str(count).strip().lower() != "all":
        try:
            valid_count = int(count) >= 0
        except (TypeError, ValueError):
            valid_count = False
        if not valid_count:
            errors.append(f"{name}: deploy device count must be 'all' or a number: {count}")
    declared = entry.get("capabilities")
    if declared is None:
        return
    if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
        errors.append(f"{name}: deploy device capabilities must be a list of strings")
        return
    unknown = sorted({item for item in declared if item not in _NVIDIA_DEVICE_CAPABILITIES})
    if unknown:
        errors.append(
            f"{name}: deploy device capability {unknown[0]!r} is not allowed "
            f"(known: {', '.join(sorted(_NVIDIA_DEVICE_CAPABILITIES))})"
        )


def lint_compose(
    text_or_dict: Any, capabilities: Any = None, package_id: str | None = None, package_dir: str | Path | None = None
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a docker-compose document.

    `capabilities` are the manifest capabilities; they unlock per-device
    exceptions (`/dev/kvm` for `kvm`, `/dev/dri` for `dri`, `/dev/nvidia*` and
    the `nvidia` runtime for `nvidia`). `package_id`/`package_dir` add
    the package's own directory and data dir to the bind allowlist; without
    them only the shared data mounts are allowed.
    """
    try:
        data = text_or_dict if isinstance(text_or_dict, dict) else yaml.safe_load(text_or_dict)
    except yaml.YAMLError as exc:
        return [f"Compose file is not valid YAML: {exc}"], []
    if not isinstance(data, dict):
        return ["Compose file must be a mapping"], []
    errors: list[str] = []
    warnings: list[str] = []
    caps = {str(item) for item in (capabilities or ())}
    bind_roots = _allowed_bind_roots(package_id, package_dir)

    if "include" in data:
        errors.append("compose include is not allowed")
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        return ["Compose file must define at least one service"], []

    for name, service in services.items():
        if not isinstance(service, dict):
            errors.append(f"{name}: service must be a mapping")
            continue
        if service.get("build") is not None:
            errors.append(f"{name}: build is not allowed; packages must reference an image")
        if service.get("privileged") is True:
            errors.append(f"{name}: privileged containers are not allowed")
        for key in ("pid", "ipc"):
            if str(service.get(key) or "").lower() == "host":
                errors.append(f"{name}: {key}: host is not allowed")
        if str(service.get("network_mode") or "").lower() == "host":
            errors.append(f"{name}: network_mode: host is not allowed")
        for key in ("cap_add", "security_opt", "volumes_from"):
            if service.get(key):
                errors.append(f"{name}: {key} is not allowed")
        _check_devices(service.get("devices"), name, caps, errors)
        _check_gpu(service, name, caps, errors)
        if service.get("extends"):
            errors.append(f"{name}: extends is not allowed")

        image = service.get("image")
        if not image:
            errors.append(f"{name}: image is required")
        else:
            image = str(image)
            if "@sha256:" not in image and (":" not in image.rsplit("/", 1)[-1] or image.endswith(":latest")):
                warnings.append(f"{name}: image is not pinned to a version/digest: {image}")

        volumes = service.get("volumes") or []
        if isinstance(volumes, dict):
            volumes = [volumes]
        if not isinstance(volumes, list):
            errors.append(f"{name}: volumes must be a list")
        else:
            for volume in volumes:
                if isinstance(volume, str):
                    source = volume.split(":", 1)[0]
                    _check_bind(source, name, errors, bind_roots)
                elif isinstance(volume, dict):
                    if str(volume.get("type") or "bind") == "bind":
                        _check_bind(volume.get("source") or "", name, errors, bind_roots)
                else:
                    errors.append(f"{name}: invalid volume entry")
            if _binds_share_rw(volumes) and not _declares_container_uid(service):
                # A container writing as its own uid creates files the EthOS
                # account cannot edit; manifests should use PUID/PGID (EthOS
                # exports ETHOS_SERVICE_UID/ETHOS_SERVICE_GID for interpolation).
                warnings.append(
                    f"{name}: writes to a share without PUID/PGID or user: — "
                    f"files may not be editable by the NAS account"
                )

        env_files = service.get("env_file") or []
        if isinstance(env_files, str):
            env_files = [env_files]
        for env_file in env_files if isinstance(env_files, list) else []:
            raw = str(env_file)
            if raw.startswith("/") or ".." in PurePosixPath(raw).parts:
                errors.append(f"{name}: env_file must stay inside the package directory: {raw}")

    return errors, warnings


def enforce_compose(
    text_or_dict: Any, capabilities: Any = None, package_id: str | None = None, package_dir: str | Path | None = None
) -> list[str]:
    """Raise PackageError on policy violations; return warnings."""
    errors, warnings = lint_compose(text_or_dict, capabilities, package_id, package_dir)
    if errors:
        raise PackageError("Compose policy violation: " + "; ".join(errors[:4]), 400)
    return warnings
