"""Supervisor for ``runtime: venv`` packages.

A venv package owns a private virtualenv at ``<package>/venv`` and one loopback
process. This module builds the venv from the package's offline wheelhouse,
starts the entry point as the service user, pipes stdout/stderr into the unified
log store as ``pkg:<id>``, health-probes the declared port and restarts the
process a bounded number of times.

Nothing here imports the package's code into the core interpreter: the core
process only speaks HTTP to ``127.0.0.1:<ui.port>``. That is what keeps heavy
dependencies (opencv, yt-dlp, …) out of ``.venv`` — see docs/app-store.md.
"""

import asyncio
import logging
import os
import shutil
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.config import settings
from backend.services import log_service, packages_store
from backend.services.package_schema import PackageError
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.package_runtime")

VENV_DIRNAME = "venv"
BUILD_TIMEOUT = 900          # venv + offline pip install
STOP_TIMEOUT = 8             # SIGTERM grace before SIGKILL
HEALTH_TIMEOUT = 45          # cold start on a Raspberry Pi is slow
RESTART_LIMIT = 3
RESTART_WINDOW = 600         # seconds; the counter resets after this
LOG_MAX = 2000


@dataclass
class _Handle:
    package_id: str
    process: asyncio.subprocess.Process
    port: int
    readers: list[asyncio.Task] = field(default_factory=list)
    supervisor: Optional[asyncio.Task] = None
    stopping: bool = False


_HANDLES: dict[str, _Handle] = {}


# ── Paths ───────────────────────────────────────────────────────────────────

def venv_dir(package_dir: Path) -> Path:
    return Path(package_dir) / VENV_DIRNAME


def venv_python(package_dir: Path) -> Path:
    return venv_dir(package_dir) / "bin" / "python"


def data_dir(pkg_id: str) -> Path:
    return packages_store.PACKAGES_ROOT / "data" / pkg_id


def _runtime_record(pkg_id: str) -> dict:
    record = packages_store.get_package(pkg_id)
    if not record:
        raise PackageError(f"Package {pkg_id!r} not found", 404)
    if str(record.get("runtime") or "compose") != "venv":
        raise PackageError(f"Package {pkg_id!r} is not a runtime 'venv' package", 400)
    return record


def _service_port(record: dict) -> int:
    python_cfg = record.get("python") or {}
    port = int(python_cfg.get("port") or ((record.get("ui") or {}).get("port") or 0))
    if not 1 <= port <= 65535:
        raise PackageError("runtime 'venv' package has no usable service port", 500)
    return port


# ── Stale process tracking ──────────────────────────────────────────────────
#
# A restart through `/api/system/restart` finishes with `os.execv`, which keeps
# the PID and never runs the ASGI lifespan shutdown. Package services started by
# the previous image therefore stay alive as children of the same process, while
# the new image has no `_Handle` for them — autostart then fails with “port
# already in use” and Stop/Uninstall cannot reach them. A pidfile written at
# start (plus the `ETHOS_PACKAGE_ID` env marker) lets the next image stop them.

def _run_dir() -> Path:
    return packages_store.PACKAGES_ROOT / "run"


def _pid_file(pkg_id: str) -> Path:
    return _run_dir() / f"{pkg_id}.pid"


def _write_pidfile(pkg_id: str, pid: int) -> None:
    try:
        _run_dir().mkdir(parents=True, exist_ok=True)
        _pid_file(pkg_id).write_text(str(pid), encoding="utf-8")
    except OSError:
        logger.warning("Could not write the runtime pidfile for %s", pkg_id, exc_info=True)


def _clear_pidfile(pkg_id: str) -> None:
    try:
        _pid_file(pkg_id).unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not remove the runtime pidfile for %s", pkg_id, exc_info=True)


def _read_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            errors="replace")
    except OSError:
        return ""


def _pid_running(pid: int) -> bool:
    """True when the PID exists and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    parts = stat.rsplit(")", 1)
    if len(parts) != 2:
        return False
    return parts[1].split()[0] != "Z"


def _is_package_process(pid: int, pkg_id: str, package_dir: Path) -> bool:
    """Identify one of our package services without trusting the PID alone."""
    cmdline = _read_cmdline(pid)
    if not cmdline or str(package_dir) not in cmdline:
        return False
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        # The cmdline already points inside this package directory.
        return True
    return f"ETHOS_PACKAGE_ID={pkg_id}".encode() in environ


def _find_package_processes(pkg_id: str, package_dir: Path) -> list[int]:
    """PIDs running this package (including orphans reparented to init)."""
    found: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return found
    me = os.getpid()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        if _is_package_process(pid, pkg_id, package_dir):
            found.append(pid)
    return found


def cleanup_stale(pkg_id: str) -> bool:
    """Stop a package process left behind by an in-place restart.

    Prefers the pidfile, then falls back to a `/proc` scan marked with
    `ETHOS_PACKAGE_ID` — the latter also covers orphans reparented to init after
    a hard crash. Returns True when at least one process was signalled.
    """
    package_dir = packages_store.package_dir(pkg_id)
    pids: list[int] = []
    try:
        raw = _pid_file(pkg_id).read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if raw:
        try:
            pid = int(raw)
        except ValueError:
            pid = 0
        if pid > 1 and _pid_running(pid) and _is_package_process(pid, pkg_id, package_dir):
            pids = [pid]

    if not pids:
        pids = _find_package_processes(pkg_id, package_dir)

    if not pids:
        _clear_pidfile(pkg_id)
        return False

    for pid in pids:
        logger.warning("Stopping stale package process %s (pid %s) left by an in-place restart",
                       pkg_id, pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError as exc:
            logger.error("Could not signal stale pid %s for %s: %s", pid, pkg_id, exc)
            continue

        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline and _pid_running(pid):
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                # Harmless: the PID may not be our child (reparented after a
                # hard restart) or may already have been reaped.
                pass
            time.sleep(0.1)
        if _pid_running(pid):
            logger.warning("Package %s ignored SIGTERM; killing pid %s", pkg_id, pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                # Harmless: the process exited between the check and the kill.
                pass
            try:
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                # Harmless: nothing left to reap for this PID.
                pass

    _clear_pidfile(pkg_id)
    return True


# ── Process helpers ─────────────────────────────────────────────────────────

def _base_env(pkg_id: str, package_dir: Path, port: int) -> dict:
    state = data_dir(pkg_id)
    (state / "tmp").mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": str(state),
        "TMPDIR": str(state / "tmp"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ETHOS_PACKAGE_ID": pkg_id,
        "ETHOS_PACKAGE_PORT": str(port),
        "ETHOS_PACKAGE_DIR": str(package_dir),
        "ETHOS_PACKAGE_DATA_DIR": str(state),
        # Read-only hint for one-time migrations out of the core data dir (a
        # package runs as the same user, so this grants no new access).
        "ETHOS_CORE_DATA_DIR": str(settings.DATA_DIR),
    }


def _pip_env(pkg_id: str, package_dir: Path) -> dict:
    env = _base_env(pkg_id, package_dir, 0)
    # No user/system pip configuration, no index, no network fallback.
    env.update({
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
    })
    return env


async def _run(cmd: list[str], *, cwd: Path, env: dict, timeout: int, what: str) -> tuple[int, str]:
    """Run a command with an argument list (never shell) and a hard timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError as exc:
        raise PackageError(f"{what} failed: executable not found ({exc})", 500) from exc
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        logger.error("%s timed out after %ss for %s", what, timeout, cwd)
        raise PackageError(f"{what} timed out after {timeout}s", 504) from None
    text = stdout.decode(errors="replace") if stdout else ""
    return proc.returncode or 0, text


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _probe(port: int, path: str) -> bool:
    url = f"http://127.0.0.1:{port}{path if path.startswith('/') else '/' + path}"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:
        return False


# ── venv build ──────────────────────────────────────────────────────────────

async def build(package_dir: Path, record: dict, job_id: str) -> None:
    """Create the private venv and install the wheelhouse, fully offline."""
    from backend.services import package_jobs, package_wheels

    pkg_id = str(record.get("id") or "")
    python_cfg = record.get("python") or {}
    requirements = list(python_cfg.get("requirements") or [])

    required_python = str(python_cfg.get("requires_python") or "").strip()
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if required_python and required_python != running:
        raise PackageError(
            f"Package needs Python {required_python}; this core runs {running}", 409,
        )

    wheelhouse = Path(package_dir) / package_wheels.WHEELHOUSE_DIRNAME
    wheels = sorted(wheelhouse.glob("*.whl")) if wheelhouse.is_dir() else []
    if not requirements:
        # Stdlib-only service: the venv exists for isolation from the core
        # interpreter, but there is nothing to install.
        package_jobs.log(job_id, "stdlib-only package environment (no dependencies)")
    elif not wheels:
        raise PackageError("Wheelhouse is missing from the package payload; reinstall the package", 500)
    else:
        package_jobs.log(job_id, f"building private venv from {len(wheels)} wheel(s)")

    target = venv_dir(package_dir)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    rc, output = await _run(
        [sys.executable, "-m", "venv", str(target)],
        cwd=Path(package_dir), env=_base_env(pkg_id, Path(package_dir), 0),
        timeout=300, what="venv creation",
    )
    if rc != 0:
        logger.error("venv creation failed for %s: %s", pkg_id, output[-400:])
        raise PackageError(f"Could not create the package venv: {output.strip()[-300:]}", 500)

    if not requirements:
        logger.info("Built empty venv for %s (stdlib-only service)", pkg_id)
        return

    command = [
        str(venv_python(package_dir)), "-m", "pip", "install",
        "--no-index", f"--find-links={wheelhouse}",
        "--only-binary", ":all:", "--no-input", "--no-cache-dir",
        "--disable-pip-version-check", *requirements,
    ]
    rc, output = await _run(
        command, cwd=Path(package_dir), env=_pip_env(pkg_id, Path(package_dir)),
        timeout=BUILD_TIMEOUT, what="offline pip install",
    )
    tail = " | ".join(line for line in output.strip().splitlines()[-3:])
    if rc != 0:
        logger.error("pip install failed for %s (rc=%s): %s", pkg_id, rc, tail)
        raise PackageError(f"Offline dependency install failed: {tail[-300:]}", 500)
    package_jobs.log(job_id, f"dependencies installed: {', '.join(requirements)[:180]}")
    logger.info("Built venv for %s (%d requirement(s))", pkg_id, len(requirements))


# ── Lifecycle ───────────────────────────────────────────────────────────────

def is_running(pkg_id: str) -> bool:
    handle = _HANDLES.get(pkg_id)
    return bool(handle and handle.process.returncode is None)


def status(pkg_id: str) -> str:
    return "running" if is_running(pkg_id) else "stopped"


async def _pipe(pkg_id: str, stream: asyncio.StreamReader, level: str) -> None:
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode(errors="replace").strip()
        if text:
            log_service.record(level, "package-runtime", text[:LOG_MAX], app=f"pkg:{pkg_id}")


async def _wait_healthy(pkg_id: str, port: int, path: str, process: asyncio.subprocess.Process) -> None:
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        if process.returncode is not None:
            raise PackageError(f"Service exited during startup (code {process.returncode})", 502)
        if await asyncio.to_thread(_probe, port, path):
            return
        await asyncio.sleep(1.0)
    raise PackageError(f"Service did not become healthy on 127.0.0.1:{port} within {HEALTH_TIMEOUT}s", 504)


async def _supervise(pkg_id: str) -> None:
    """Restart the service when it exits unexpectedly (bounded, rate-limited)."""
    restarts = 0
    window_start = time.time()
    while True:
        handle = _HANDLES.get(pkg_id)
        if not handle:
            return
        returncode = await handle.process.wait()
        for reader in handle.readers:
            reader.cancel()
        if handle.stopping or _HANDLES.get(pkg_id) is not handle:
            return
        _HANDLES.pop(pkg_id, None)
        _clear_pidfile(pkg_id)

        now = time.time()
        if now - window_start > RESTART_WINDOW:
            restarts, window_start = 0, now
        restarts += 1
        record = packages_store.get_package(pkg_id)
        name = str((record or {}).get("name") or pkg_id)
        if restarts > RESTART_LIMIT:
            message = f"Service exited with code {returncode}; giving up after {RESTART_LIMIT} restarts"
            logger.error("%s: %s", pkg_id, message)
            log_service.record("error", "package-runtime", message, app=f"pkg:{pkg_id}")
            emit_event("error", f"Package {name} is not running", message, source="packages",
                       dedupe_key=f"pkg-crash:{pkg_id}")
            return

        logger.warning("%s: service exited (code %s), restart %d/%d",
                       pkg_id, returncode, restarts, RESTART_LIMIT)
        log_service.record("warning", "package-runtime",
                           f"Service exited (code {returncode}); restarting ({restarts}/{RESTART_LIMIT})",
                           app=f"pkg:{pkg_id}")
        await asyncio.sleep(2)
        if not record:
            return
        try:
            await start(pkg_id, record, supervise=False)
        except PackageError as exc:
            logger.error("Restart failed for %s: %s", pkg_id, exc.detail)
            emit_event("error", f"Package {name} is not running", exc.detail, source="packages",
                       dedupe_key=f"pkg-crash:{pkg_id}")
            return


async def start(pkg_id: str, record: Optional[dict] = None, job_id: Optional[str] = None,
                *, supervise: bool = True) -> dict:
    """Launch the package service and wait until its health endpoint answers."""
    record = record or _runtime_record(pkg_id)
    if is_running(pkg_id):
        return {"package": pkg_id, "status": "running", "port": _service_port(record)}

    # A process can outlive an `os.execv` restart; it would make the port check
    # below fail even though the package is ours. Stop it first.
    await asyncio.to_thread(cleanup_stale, pkg_id)

    package_dir = packages_store.package_dir(pkg_id)
    python_bin = venv_python(package_dir)
    if not python_bin.exists():
        raise PackageError("Package environment is not built; reinstall the package", 409)
    entry = (record.get("python") or {}).get("entry") or ""
    entry_path = package_dir / str(entry)
    if not entry or not entry_path.is_file():
        raise PackageError(f"python.entry is missing from the package: {entry or 'unset'}", 500)

    port = _service_port(record)
    if await asyncio.to_thread(_port_in_use, port):
        raise PackageError(f"Port {port} is already in use; stop the other service and retry", 409)

    process = await asyncio.create_subprocess_exec(
        str(python_bin), str(entry_path),
        cwd=str(package_dir), env=_base_env(pkg_id, package_dir, port),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _write_pidfile(pkg_id, process.pid)
    handle = _Handle(package_id=pkg_id, process=process, port=port)
    handle.readers = [
        asyncio.create_task(_pipe(pkg_id, process.stdout, "info"), name=f"pkg-out-{pkg_id}"),
        asyncio.create_task(_pipe(pkg_id, process.stderr, "warning"), name=f"pkg-err-{pkg_id}"),
    ]
    _HANDLES[pkg_id] = handle

    health = record.get("healthcheck") or {}
    path = str(health.get("path") or "/")
    try:
        await _wait_healthy(pkg_id, port, path, process)
    except PackageError:
        await stop(pkg_id)
        raise

    if supervise:
        handle.supervisor = asyncio.create_task(_supervise(pkg_id), name=f"pkg-super-{pkg_id}")
    logger.info("Started package service %s on 127.0.0.1:%s", pkg_id, port)
    if job_id:
        log_service.record("info", "package-runtime", f"Service listening on 127.0.0.1:{port}",
                           app=f"pkg:{pkg_id}")
    return {"package": pkg_id, "status": "running", "port": port}


async def stop(pkg_id: str) -> bool:
    """Terminate the service (SIGTERM, then SIGKILL) and drop the handle."""
    handle = _HANDLES.pop(pkg_id, None)
    if not handle:
        # No handle (e.g. autostart failed after an in-place restart): still try
        # to stop a process identified by the pidfile / env marker.
        return await asyncio.to_thread(cleanup_stale, pkg_id)
    handle.stopping = True
    process = handle.process
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            logger.debug("Package process for %s already gone", pkg_id)
        try:
            await asyncio.wait_for(process.wait(), timeout=STOP_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Package %s ignored SIGTERM; killing it", pkg_id)
            process.kill()
            await process.wait()
    for reader in handle.readers:
        reader.cancel()
    if handle.supervisor and not handle.supervisor.done():
        handle.supervisor.cancel()
    _clear_pidfile(pkg_id)
    logger.info("Stopped package service %s", pkg_id)
    return True


async def restart(pkg_id: str) -> dict:
    record = _runtime_record(pkg_id)
    await stop(pkg_id)
    return await start(pkg_id, record)


async def shutdown_all() -> None:
    """Stop every supervised service and any stale process left by a restart."""
    for pkg_id in list(_HANDLES):
        try:
            await stop(pkg_id)
        except Exception:
            logger.error("Could not stop package service %s", pkg_id, exc_info=True)
    # No handle but a live process: only possible after os.execv / a crash.
    for record in packages_store.load_installed():
        if str(record.get("runtime") or "compose") != "venv":
            continue
        pkg_id = str(record.get("id") or "")
        if not pkg_id:
            continue
        try:
            await asyncio.to_thread(cleanup_stale, pkg_id)
        except Exception:
            logger.error("Could not clean up stale package process %s", pkg_id, exc_info=True)


async def autostart_all() -> None:
    """Start installed venv packages after boot; failures become notifications."""
    for record in packages_store.load_installed():
        if str(record.get("runtime") or "compose") != "venv":
            continue
        pkg_id = str(record.get("id") or "")
        if not pkg_id or is_running(pkg_id):
            continue
        try:
            await start(pkg_id, record)
        except PackageError as exc:
            logger.error("Autostart failed for %s: %s", pkg_id, exc.detail)
            emit_event("warning", f"Package {record.get('name') or pkg_id} did not start",
                       exc.detail, source="packages", dedupe_key=f"pkg-autostart:{pkg_id}")
