"""Offline wheelhouse for ``runtime: venv`` packages.

A venv package never talks to PyPI: the manifest lists one wheel per
distribution and architecture, each with a sha256, and installation runs pip
with ``--no-index --find-links=<pkg>/.wheelhouse``. This keeps installs
deterministic and works without network access once the artifacts are cached.

Trust: wheels are downloaded through ``package_repo.download_verified``, so they
inherit the signed index → manifest → sha256 chain. Sdists and wheels built for
another architecture are rejected here, before pip ever runs.
"""

import logging
import platform
import re
import shutil
from pathlib import Path

from backend.services import package_jobs, package_repo
from backend.services.package_schema import PackageError

logger = logging.getLogger("ethos-nas.services.package_wheels")

# Directory inside the package payload (and inside staging before the move).
WHEELHOUSE_DIRNAME = ".wheelhouse"

_ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


def host_arch() -> str:
    """Map the running CPU to a manifest architecture key."""
    machine = platform.machine().strip().lower()
    arch = _ARCH_ALIASES.get(machine)
    if not arch:
        raise PackageError(f"Packages are not supported on this CPU: {machine or 'unknown'}", 400)
    return arch


def usable_wheels(python_cfg: dict) -> list[dict]:
    """Wheels declared for this machine's architecture plus pure-Python ones."""
    if not python_cfg:
        return []
    arch = host_arch()
    return [w for w in python_cfg.get("wheels") or [] if w.get("arch") in (arch, "any")]


def _check_platform_tag(filename: str, arch: str) -> None:
    pure = filename.endswith("-none-any.whl")
    if arch == "any":
        if not pure:
            raise PackageError(f"Wheel {filename} is architecture specific; set its arch explicitly", 400)
        return
    if pure:
        return  # pure Python, architecture independent
    if arch not in filename:
        raise PackageError(
            f"Wheel {filename} is not built for {arch}; the package must ship one wheel per architecture",
            400,
        )


def _dist_key(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(".", "_")


def _warn_missing_requirements(wheelhouse: Path, requirements: list[str], job_id: str) -> None:
    """Best-effort check that every direct requirement has a wheel present.

    Transitive dependencies are not listed in the manifest, so a missing wheel is
    only a warning here; pip reports the real failure with its own message.
    """
    present = [_dist_key(path.name.split("-", 1)[0]) for path in wheelhouse.glob("*.whl")]
    for requirement in requirements:
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", str(requirement))
        name = _dist_key(match.group(1)) if match else ""
        if name and not any(item == name for item in present):
            package_jobs.log(job_id, f"warning: no wheel found for '{requirement}' in the wheelhouse")


def stage(staging: Path, python_cfg: dict, job_id: str) -> Path:
    """Download and verify the wheelhouse into ``<staging>/.wheelhouse``."""
    wheels = usable_wheels(python_cfg)
    if not wheels:
        if not (python_cfg.get("requirements") or []):
            # Stdlib-only service: nothing to install, nothing to verify.
            package_jobs.log(job_id, "no python dependencies; skipping the wheelhouse")
            return staging
        raise PackageError(
            f"Package ships no wheels for this architecture ({host_arch()})", 400,
        )
    wheelhouse = staging / WHEELHOUSE_DIRNAME
    if wheelhouse.exists():
        shutil.rmtree(wheelhouse, ignore_errors=True)
    wheelhouse.mkdir(parents=True)

    for index, wheel in enumerate(wheels, 1):
        filename = wheel["filename"]
        _check_platform_tag(filename, wheel["arch"])
        package_repo.download_verified(wheel["url"], wheelhouse / filename, wheel["sha256"])
        package_jobs.set_step_progress(job_id, "wheels", index, len(wheels))
        package_jobs.log(job_id, f"wheel verified: {filename}")

    _warn_missing_requirements(wheelhouse, list(python_cfg.get("requirements") or []), job_id)
    logger.info("Wheelhouse ready with %d wheel(s) for %s", len(wheels), host_arch())
    return wheelhouse
