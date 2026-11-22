"""EthOS NAS — Ubuntu OS package updates (apt).

The backend never runs apt. It asks the root-owned helper
`/usr/local/bin/ethos-os-update` for exactly two actions:

    check  — refresh the apt lists and write <DATA_DIR>/os-update/check.json
    apply  — install every available upgrade (no removals, no autoremove)

The helper accepts no package names, versions or URLs from the web UI, runs
apt with `--with-new-pkgs` (kernel updates arrive that way, nothing is
removed), and writes progress to job.json/result.json so a service restart
does not lose the outcome. Reboot handling is explicit: the UI only shows a
button, nothing restarts on its own.

Scope is the *current* Ubuntu release: `do-release-upgrade` and snap refreshes
are deliberately out of scope. `unattended-upgrades` (plain Ubuntu installs)
keeps running; this service reports what is left. Spec: docs/os-updates.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.services import json_store
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.os_update")

STATE_VERSION = 1
_CHECK_LOCK = asyncio.Lock()
_loop_task: asyncio.Task | None = None
_loop_stop: asyncio.Event | None = None

# Everything the helper writes is treated as untrusted input: bound the list,
# the strings and the numeric fields before they reach the API/UI.
_MAX_PACKAGES = 500
_FIELD_MAX = 200
_MAX_STATE_BYTES = 2_000_000  # check.json is tiny in practice
_HUNG_JOB_SECONDS = 2 * 3600


class OsUpdateError(RuntimeError):
    """A check or apply could not be completed (missing helper, busy, apt)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# Patchable in tests (creating /run/reboot-required needs root).
REBOOT_FLAG_PATHS = (Path("/run/reboot-required"), Path("/var/run/reboot-required"))
REBOOT_PKGS_PATH = Path("/run/reboot-required.pkgs")


# ── State ───────────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    path = Path(settings.DATA_DIR) / "os-update"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Could not create %s", path, exc_info=True)
    return path


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            logger.warning("Ignoring oversized state file %s", path)
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _store() -> json_store.Store:
    return json_store.get(_state_dir() / "state.json", dict, version=STATE_VERSION)


def _load_state() -> dict:
    data = _store().load()
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    _store().save(state)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _parse_epoch(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value or "")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _bounded(value: Any, limit: int = _FIELD_MAX) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    return text[:limit]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Host identity ───────────────────────────────────────────────────────────

def _os_release() -> dict:
    values: dict[str, str] = {}
    try:
        for raw in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        logger.debug("/etc/os-release is unreadable", exc_info=True)
    return values


def os_identity() -> dict:
    info = _os_release()
    appliance = _read_json(Path("/etc/ethos-os/build-info.json"))
    return {
        "id": _bounded(info.get("ID"), 32),
        "version": _bounded(info.get("VERSION_ID"), 32),
        "pretty": _bounded(info.get("PRETTY_NAME") or "Linux", 160),
        "codename": _bounded(info.get("VERSION_CODENAME"), 48),
        "kernel": _bounded(platform.release(), 100),
        "ethos_os": _bounded(appliance.get("os_version"), 64),
    }


def auto_upgrades() -> str:
    """Whether Ubuntu installs security updates by itself (plain Ubuntu does)."""
    try:
        text = Path("/etc/apt/apt.conf.d/20auto-upgrades").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    enabled = re.search(r'APT::Periodic::Unattended-Upgrade\s+"?1"?\s*;', text) is not None
    return "enabled" if enabled else "disabled"


def reboot_required() -> dict:
    """Kernel/system-library updates leave /run/reboot-required behind."""
    flag = any(path.exists() for path in REBOOT_FLAG_PATHS)
    names: list[str] = []
    try:
        for raw in REBOOT_PKGS_PATH.read_text(encoding="utf-8").splitlines():
            name = _bounded(raw, 120)
            if name and name not in names:
                names.append(name)
            if len(names) >= 40:
                break
    except OSError:
        pass  # optional file — absent unless apt left a reboot notice
    return {"required": flag, "packages": names}


def helper_available() -> bool:
    path = Path(settings.OS_UPDATE_HELPER)
    return path.is_file() and os.access(path, os.X_OK)


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()[:64]
    except OSError:
        return ""


# ── Cached check result ─────────────────────────────────────────────────────

def _coerce_packages(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    packages: list[dict] = []
    for item in raw[:_MAX_PACKAGES]:
        if not isinstance(item, dict):
            continue
        name = _bounded(item.get("name"), 120)
        if not name:
            continue
        packages.append({
            "name": name,
            "from": _bounded(item.get("from"), 80),
            "to": _bounded(item.get("to"), 80),
            "origin": _bounded(item.get("origin"), 120),
            "security": item.get("security") is True,
        })
    return packages


def _fingerprint(packages: Any) -> str:
    """Stable id for the exact set of pending upgrades (dedupe key)."""
    if not isinstance(packages, list):
        return ""
    lines = sorted(
        f"{_bounded(item.get('name'), 120)}={_bounded(item.get('to'), 80)}"
        for item in packages if isinstance(item, dict) and item.get("name")
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12] if lines else ""


def status() -> dict:
    """Cached helper output + live reboot/auto-upgrade state (no privileged call)."""
    check = _read_json(_state_dir() / "check.json")
    state = _load_state()
    packages = _coerce_packages(check.get("packages"))
    count = max(0, min(1_000_000, _int(check.get("count"), len(packages))))
    security = max(0, min(count, _int(check.get("security_count"), 0)))
    reboot = reboot_required()
    checked_at = _int(check.get("checked_at_epoch"), 0)
    return {
        "enabled": settings.OS_UPDATE_ENABLED,
        "helper": helper_available(),
        "helper_path": settings.OS_UPDATE_HELPER,
        "os": os_identity(),
        "auto_upgrades": auto_upgrades(),
        "checked_at": checked_at or None,
        "checked_at_iso": _bounded(check.get("checked_at"), 64),
        "attempted_at_iso": _bounded(state.get("attempted_at_iso"), 64),
        "update_count": count,
        "security_count": security,
        "kept_back": max(0, _int(check.get("kept_back"), 0)),
        "packages": packages,
        "reboot_required": reboot["required"],
        "reboot_packages": reboot["packages"],
        "error": _bounded(state.get("error"), 500),
        "check_hours": settings.OS_UPDATE_CHECK_HOURS,
    }


def _record_error(detail: str, attempted: float) -> None:
    state = _load_state()
    _save_state({
        **state,
        "error": _bounded(detail, 500),
        "attempted_at": attempted,
        "attempted_at_iso": _iso(attempted),
    })


# ── Helper invocation ───────────────────────────────────────────────────────

def _helper_argv(action: str) -> list[str]:
    base = [settings.OS_UPDATE_HELPER, action]
    return ["sudo", "-n", *base] if settings.OS_UPDATE_VIA_SUDO else base


def _run_check() -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            _helper_argv("check"),
            capture_output=True, text=True,
            timeout=settings.OS_UPDATE_CHECK_TIMEOUT,
            cwd="/",
        )
    except subprocess.TimeoutExpired:
        raise OsUpdateError(
            f"Update check timed out after {settings.OS_UPDATE_CHECK_TIMEOUT}s", 504) from None
    except OSError as exc:
        raise OsUpdateError(f"Cannot run {settings.OS_UPDATE_HELPER}: {exc}", 502) from None


def _helper_error(proc: subprocess.CompletedProcess) -> tuple[str, int]:
    text = (proc.stderr or proc.stdout or "").strip()
    last = text.splitlines()[-1][:300] if text else "the helper failed"
    if proc.returncode == 3:
        return "Another OS update operation is already running", 409
    return f"{last} (exit {proc.returncode})", 502


async def check(force: bool = False) -> dict:
    """Refresh the apt lists through the helper, caching the result."""
    if not settings.OS_UPDATE_ENABLED:
        return {**status(), "error": "OS update checks are disabled (ETHOS_OS_UPDATE_ENABLED=0)"}

    state = _load_state()
    checked_at = _parse_epoch(state.get("checked_at_iso")) or float(state.get("checked_at") or 0)
    if not force and checked_at and (time.time() - checked_at) < settings.OS_UPDATE_TTL_SECONDS:
        return status()

    async with _CHECK_LOCK:
        # Another caller may have refreshed while we waited for the lock.
        state = _load_state()
        checked_at = _parse_epoch(state.get("checked_at_iso")) or float(state.get("checked_at") or 0)
        if not force and checked_at and (time.time() - checked_at) < settings.OS_UPDATE_TTL_SECONDS:
            return status()

        if not helper_available():
            raise OsUpdateError(
                f"OS update helper not installed ({settings.OS_UPDATE_HELPER}). "
                "Run: sudo bash setup_scripts/setup_service.sh", 409)

        attempted = time.time()
        try:
            proc = await asyncio.to_thread(_run_check)
        except OsUpdateError as exc:
            _record_error(str(exc), attempted)
            raise

        if proc.returncode != 0:
            detail, code = _helper_error(proc)
            _record_error(detail, attempted)
            logger.warning("OS update check failed: %s", detail)
            if code == 409:
                raise OsUpdateError(detail, 409)
            return status()

        check_data = _read_json(_state_dir() / "check.json")
        packages = check_data.get("packages")
        fingerprint = _fingerprint(packages)
        previous = state.get("packages_fingerprint") or ""
        _save_state({
            **state,
            "error": "",
            "checked_at": attempted,
            "checked_at_iso": _iso(attempted),
            "attempted_at": attempted,
            "attempted_at_iso": _iso(attempted),
            "packages_fingerprint": fingerprint,
        })
        count = max(0, _int(check_data.get("count"), 0))
        security = max(0, _int(check_data.get("security_count"), 0))
        logger.info("OS update check: %d update(s), %d security, %d kept back",
                    count, security, max(0, _int(check_data.get("kept_back"), 0)))
        if count > 0 and fingerprint and fingerprint != previous:
            suffix = f" ({security} security)" if security else ""
            emit_event(
                "info",
                "Ubuntu updates available",
                f"{count} package update(s){suffix} are waiting on the NAS. "
                "Open Settings → System to install them.",
                source="os-updates",
                dedupe_key=f"os-update:{fingerprint}",
                action={"label": "Open Settings", "appId": "settings"},
            )
        return status()


# ── Apply (root helper) ─────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _job_is_stale(job: dict) -> bool:
    """A running job whose helper is gone (crash, reboot) or hung for hours."""
    if not isinstance(job, dict) or job.get("status") != "running":
        return False
    pid = _int(job.get("pid"), 0)
    if pid > 0 and not _pid_alive(pid):
        return True
    started = _parse_epoch(job.get("at"))
    return not started or (time.time() - started) > _HUNG_JOB_SECONDS


def _report_result(result: dict) -> None:
    """Emit at most once per result stamp; the reboot hint once per boot."""
    stamp = _bounded(result.get("at"), 64)
    if not stamp:
        return
    state = _load_state()
    if stamp == state.get("last_result_at"):
        return
    outcome = _bounded(result.get("status"), 32)
    upgraded = max(0, _int(result.get("upgraded"), 0))
    if outcome == "ok":
        emit_event("success", "Ubuntu updated", f"{upgraded} package(s) upgraded",
                   source="os-updates", dedupe_key=f"os-update-result:{stamp}")
    elif outcome == "failed":
        emit_event("error", "Ubuntu update failed", _bounded(result.get("detail"), 300),
                   source="os-updates", dedupe_key=f"os-update-result:{stamp}")
    if result.get("reboot_required"):
        emit_event(
            "warning",
            "Reboot required",
            "Ubuntu installed updates that need a restart (kernel or system libraries). "
            "Open Settings → System to reboot the NAS.",
            source="os-updates",
            dedupe_key=f"os-update-reboot:{_boot_id()}",
            action={"label": "Open Settings", "appId": "settings"},
        )
    _save_state({**state, "last_result_at": stamp, "last_result": outcome})


def apply_status() -> dict:
    """Progress/result written by `ethos-os-update apply` (no privileged call)."""
    job = _read_json(_state_dir() / "job.json")
    result = _read_json(_state_dir() / "result.json")
    stale = _job_is_stale(job)
    if stale:
        logger.warning("Stale OS update job (step %s, pid %s)", job.get("step"), job.get("pid"))
        if not result:
            result = {
                "status": "failed",
                "step": "done",
                "detail": "The update helper is no longer running (service or host restart).",
                "upgraded": 0,
                "reboot_required": reboot_required()["required"],
                # Reuse the job timestamp: a fresh `at` on every poll would
                # defeat the dedupe and re-notify forever.
                "at": _bounded(job.get("at"), 64) or "stale",
            }
    try:
        if result:
            _report_result(result)
    except Exception:
        logger.error("Could not report the OS update result", exc_info=True)
    return {
        "helper": helper_available(),
        "running": job.get("status") == "running" and not stale,
        "stale": stale,
        "job": job or None,
        "result": result or None,
    }


def reconcile_result() -> None:
    """After a restart: report an outcome the UI may not have seen."""
    try:
        apply_status()
    except Exception:
        logger.error("OS update result reconcile failed", exc_info=True)


def _spawn_helper() -> None:
    subprocess.Popen(
        _helper_argv("apply"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd="/",
    )


async def request_apply() -> dict:
    """Ask the root helper to install every available upgrade."""
    if not settings.OS_UPDATE_ENABLED:
        raise OsUpdateError("OS updates are disabled (ETHOS_OS_UPDATE_ENABLED=0)", 409)
    if not helper_available():
        raise OsUpdateError(
            f"OS update helper not installed ({settings.OS_UPDATE_HELPER}). "
            "Run: sudo bash setup_scripts/setup_service.sh", 409)

    current = status()
    if not current.get("checked_at"):
        raise OsUpdateError("No update list is cached — run a check first.", 409)
    if _int(current.get("update_count"), 0) <= 0:
        raise OsUpdateError("Ubuntu is already up to date.", 409)
    if apply_status().get("running"):
        raise OsUpdateError("An OS update is already running", 409)

    try:
        await asyncio.to_thread(_spawn_helper)
    except OSError as exc:
        raise OsUpdateError(f"Cannot start the helper: {exc}", 500) from None

    logger.info("OS update requested (%d package(s), %d security)",
                _int(current.get("update_count")), _int(current.get("security_count")))
    return {
        "status": "updating",
        "count": _int(current.get("update_count")),
        "security_count": _int(current.get("security_count")),
    }


# ── Background checker ──────────────────────────────────────────────────────

async def _loop(stop: asyncio.Event) -> None:
    interval = max(1.0, float(settings.OS_UPDATE_CHECK_HOURS)) * 3600
    logger.info("OS update checker started (every %.0f h)", interval / 3600)
    while not stop.is_set():
        # Sleep first: a short-lived dev/test instance must never run apt.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass  # intentional: the timeout is the scheduled wake-up
        try:
            await check(force=True)
        except OsUpdateError as exc:
            logger.warning("OS update checker: %s", exc)
        except Exception:
            logger.error("OS update checker iteration failed", exc_info=True)
    logger.info("OS update checker stopped")


def start_os_update_checker() -> None:
    global _loop_task, _loop_stop
    if _loop_task is not None and not _loop_task.done():
        return
    from backend.services.task_registry import spawn

    _loop_stop = asyncio.Event()
    _loop_task = spawn(_loop(_loop_stop), name="os-update-checker")


async def stop_os_update_checker() -> None:
    global _loop_task, _loop_stop
    if _loop_stop is not None:
        _loop_stop.set()
    if _loop_task is not None and not _loop_task.done():
        try:
            await asyncio.wait_for(_loop_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug("OS update checker did not stop in time", exc_info=True)
    _loop_task = None
    _loop_stop = None
