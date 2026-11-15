"""Compose policy for packages.

Packages run as containers, but a compose file can still ask for host
namespaces, devices, privileged mode or arbitrary host binds. These checks
reject the dangerous shapes before anything is deployed or started.

The only device exception is `/dev/kvm`, and only for a manifest that declares
`"capabilities": ["kvm"]` — hardware-assisted virtual machines cannot work
without it. Every other device stays rejected.
"""

import logging
import os
from pathlib import PurePosixPath
from typing import Any

import yaml

from backend.services.package_schema import PackageError
from backend.services.packages_store import APPS_DIR, PACKAGES_ROOT, STAGING_DIR

logger = logging.getLogger("ethos-nas.services.compose_policy")

_ALLOWED_BIND_ROOTS = tuple(
    os.path.realpath(p) for p in (
        "/home", "/mnt", "/media",
        str(APPS_DIR), str(STAGING_DIR), str(PACKAGES_ROOT),
    )
)
_FORBIDDEN_DEVICES = ("/dev/", "/var/run/docker.sock", "/run/docker.sock")
# Devices a package may request, each gated by a manifest capability.
_DEVICE_CAPABILITIES = {"/dev/kvm": "kvm"}


def _path_allowed(path: str) -> bool:
    real = os.path.realpath(os.path.expanduser(path))
    return any(real == root or real.startswith(root + os.sep) for root in _ALLOWED_BIND_ROOTS)


def _check_bind(source: str, service: str, errors: list[str]) -> None:
    source = str(source or "").strip()
    if not source or source.startswith("$"):
        return
    if any(source.startswith(device) for device in _FORBIDDEN_DEVICES):
        errors.append(f"{service}: bind mount of a host device/socket is not allowed: {source}")
        return
    if source.startswith("/") or source.startswith("~"):
        if not _path_allowed(source):
            errors.append(f"{service}: bind mount outside allowed roots: {source}")
        return
    pure = PurePosixPath(source)
    if pure.is_absolute() or ".." in pure.parts:
        errors.append(f"{service}: relative bind mount must stay inside the package directory: {source}")


def _check_devices(devices: Any, service: str, capabilities: set[str],
                   errors: list[str]) -> None:
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
        required = _DEVICE_CAPABILITIES.get(source)
        if required is not None and required in capabilities:
            continue
        if required is not None:
            errors.append(
                f"{service}: device {source} requires capabilities: [\"{required}\"] in the manifest"
            )
            continue
        errors.append(f"{service}: devices are not allowed: {entry}")


def lint_compose(text_or_dict: Any,
                 capabilities: Any = None) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a docker-compose document.

    `capabilities` are the manifest capabilities; they unlock per-device
    exceptions such as `/dev/kvm` for `kvm`.
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
                    _check_bind(source, name, errors)
                elif isinstance(volume, dict):
                    if str(volume.get("type") or "bind") == "bind":
                        _check_bind(volume.get("source") or "", name, errors)
                else:
                    errors.append(f"{name}: invalid volume entry")

        env_files = service.get("env_file") or []
        if isinstance(env_files, str):
            env_files = [env_files]
        for env_file in env_files if isinstance(env_files, list) else []:
            raw = str(env_file)
            if raw.startswith("/") or ".." in PurePosixPath(raw).parts:
                errors.append(f"{name}: env_file must stay inside the package directory: {raw}")

    return errors, warnings


def enforce_compose(text_or_dict: Any, capabilities: Any = None) -> list[str]:
    """Raise PackageError on policy violations; return warnings."""
    errors, warnings = lint_compose(text_or_dict, capabilities)
    if errors:
        raise PackageError("Compose policy violation: " + "; ".join(errors[:4]), 400)
    return warnings
