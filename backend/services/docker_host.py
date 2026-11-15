"""Docker Engine provisioning and host-level health.

Container management lives in ``routers/docker.py`` (docker-py). This module owns
the *host* side: whether the Engine is installed, whether this process can reach
its socket, and how to install it.

Installing Docker needs root, so the backend never runs apt itself. It invokes
``/usr/local/bin/ethos-docker-install`` (installed by
``setup_scripts/setup_service.sh``), which is allow-listed in the service user's
sudoers file and validates its own preconditions. The helper takes no arguments.
"""

import grp
import logging
import os
import pwd
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from backend.services import package_jobs
from backend.services.notifications_service import emit_event
from backend.services.package_schema import PackageError

logger = logging.getLogger("ethos-nas.services.docker_host")

HELPER_PATH = Path("/usr/local/bin/ethos-docker-install")
SOCKET_PATH = Path("/var/run/docker.sock")
SUPPORTED_OS = frozenset({"ubuntu", "debian"})
PROBE_TIMEOUT = 8
INSTALL_TIMEOUT = 900


def _run(cmd: list[str], timeout: int = PROBE_TIMEOUT) -> Optional[subprocess.CompletedProcess]:
    """Run a probe command; missing binaries and timeouts mean "not available"."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("docker probe failed (%s): %s", cmd[0], exc)
        return None


def _os_info() -> dict:
    info = {"id": "", "version": ""}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key == "ID":
                info["id"] = value.strip().strip('"')
            elif key == "VERSION_ID":
                info["version"] = value.strip().strip('"')
    except OSError:
        logger.debug("Could not read /etc/os-release", exc_info=True)
    return info


def _docker_group():
    try:
        return grp.getgrnam("docker")
    except (KeyError, OSError):
        return None


def _process_has_docker_group() -> bool:
    """True when *this* process can access the socket via group membership."""
    group = _docker_group()
    if group is None:
        return False
    return group.gr_gid in os.getgroups() or group.gr_gid == os.getegid()


def _user_granted_docker_group() -> bool:
    """True when the service account is listed in the docker group file.

    A mismatch with :func:`_process_has_docker_group` means the group was added
    after the service started — the process needs a restart to pick it up.
    """
    group = _docker_group()
    if group is None:
        return False
    try:
        user = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        logger.debug("Could not resolve the current user for the docker group check")
        return False
    return user in group.gr_mem


def _conflict() -> str:
    """Explain why the `docker` command may exist but belong to another engine."""
    if shutil.which("podman"):
        return ("Podman is installed; it provides a `docker` CLI shim that conflicts with "
                "Docker Engine. Remove Podman's docker shim first.")
    if shutil.which("snap") and Path("/snap/docker/current").exists():
        return ("Docker was installed with snap. Remove it (sudo snap remove docker) before "
                "installing Docker Engine from the Docker apt repository.")
    return ""


def engine_status() -> dict:
    """Snapshot of the host Docker installation (safe to call on every page load)."""
    cli = shutil.which("docker") or ""
    version = ""
    compose = ""
    if cli:
        proc = _run([cli, "version", "--format", "{{.Server.Version}}"])
        if proc is not None and proc.returncode == 0:
            version = (proc.stdout or "").strip()
        proc = _run([cli, "compose", "version", "--short"], timeout=10)
        if proc is not None and proc.returncode == 0:
            compose = (proc.stdout or "").strip()

    socket_exists = SOCKET_PATH.exists()
    group_member = _process_has_docker_group()
    granted = _user_granted_docker_group()
    os_info = _os_info()
    helper = HELPER_PATH.is_file()

    return {
        "ok": bool(version),
        "cli": bool(cli),
        "daemon": bool(version),
        "version": version,
        "compose": compose,
        "socket": {"path": str(SOCKET_PATH), "exists": socket_exists},
        "group_member": group_member,
        "group_granted": granted,
        "restart_required": granted and not group_member,
        "helper": helper,
        "install_supported": helper and os_info["id"] in SUPPORTED_OS,
        "conflict": "" if version else _conflict(),
        "os": os_info,
    }


def install_docker(job_id: Optional[str] = None) -> dict:
    """Install Docker Engine + Compose v2 through the privileged helper.

    Synchronous (apt takes minutes) — call through ``asyncio.to_thread`` or from a
    detached job. ``job_id`` may be ``None`` when there is nothing to report to.
    """
    package_jobs.set_step(job_id, "check")
    status = engine_status()
    package_jobs.log(
        job_id,
        f"engine probe: cli={'yes' if status['cli'] else 'no'} "
        f"daemon={'yes' if status['daemon'] else 'no'} helper={'yes' if status['helper'] else 'no'}",
    )
    if status["daemon"]:
        package_jobs.log(job_id, "Docker Engine is already running — nothing to install")
        package_jobs.set_step(job_id, "verify")
        return status
    if not status["helper"]:
        raise PackageError(
            "Docker installer helper is not installed on this host — run "
            "`sudo bash setup_scripts/setup_service.sh` on the NAS, then retry",
            503,
        )
    if not status["install_supported"]:
        raise PackageError(
            "Automatic Docker installation is supported on Ubuntu and Debian only "
            f"(this host reports {status['os']['id'] or 'an unknown distribution'})",
            400,
        )
    if status["conflict"]:
        raise PackageError(status["conflict"], 409)

    package_jobs.set_step(job_id, "install")
    package_jobs.log(job_id, "installing Docker Engine + Compose v2 from the official Docker repository")
    cmd = ["sudo", "-n", str(HELPER_PATH)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    except FileNotFoundError:
        logger.error("sudo not found — cannot run %s", HELPER_PATH)
        raise PackageError("sudo is not available on this system", 500) from None
    except subprocess.TimeoutExpired as exc:
        logger.error("Docker installation timed out after %ss", INSTALL_TIMEOUT)
        raise PackageError("Docker installation timed out — check journalctl -u docker and retry", 504) from exc

    for line in (proc.stdout or "").splitlines()[-40:]:
        package_jobs.log(job_id, line)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-500:] or f"exit code {proc.returncode}"
        logger.error("Docker installer helper failed (rc=%s): %s", proc.returncode, detail)
        raise PackageError(f"Docker installation failed: {detail}", 502)
    for line in (proc.stderr or "").splitlines()[-20:]:
        package_jobs.log(job_id, line)

    package_jobs.set_step(job_id, "verify")
    status = engine_status()
    if status["daemon"]:
        version = f"v{status['version']}" + (f" · compose {status['compose']}" if status["compose"] else "")
        package_jobs.log(job_id, f"Docker Engine running ({version})")
        emit_event("success", "Docker Engine installed", version, source="docker")
        return status
    if status["restart_required"] or status["socket"]["exists"]:
        # The service account was added to the docker group, but this process
        # started before that — group membership only applies to new processes.
        package_jobs.log(job_id, "installed — restart EthOS to activate docker group access")
        emit_event("info", "Docker Engine installed",
                   "Restart EthOS to activate Docker access", source="docker")
        return status
    logger.error("Docker helper reported success but the daemon is still unreachable")
    raise PackageError(
        "Docker was installed but its daemon is not reachable — check journalctl -u docker", 500,
    )


def ensure_system_deps(requires, job_id: Optional[str] = None, *, allow_install: bool = True) -> None:
    """Satisfy a package's ``system_requires`` list before its compose service starts.

    Only ``docker`` is supported today. Synchronous — call through
    ``asyncio.to_thread`` from async code.
    """
    names = {str(item) for item in (requires or []) if isinstance(item, str)}
    if "docker" not in names:
        return
    status = engine_status()
    if status["daemon"]:
        package_jobs.log(job_id, f"Docker Engine ready (v{status['version']})")
        return
    package_jobs.set_step(job_id, "deps")
    package_jobs.log(job_id, "this package requires Docker Engine")
    if not allow_install or not status["helper"]:
        raise PackageError(
            "This package requires Docker Engine, which is not installed. Open the Docker Manager app "
            "and install it (or run `sudo bash setup_scripts/setup_service.sh` on the NAS), then retry.",
            503,
        )
    install_docker(job_id)
    if not engine_status()["daemon"]:
        raise PackageError(
            "Docker Engine is installed but EthOS cannot reach it yet — restart EthOS, then retry the install",
            503,
        )
