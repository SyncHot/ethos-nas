"""EthOS NAS — EthOS OS image (A/B) updates.

The OS vendor publishes a signed `ethos-os-release` index with a slot-agnostic
rootfs artifact. This service downloads that artifact into `<DATA_DIR>/os-image/`
and asks the root helper `ethos-os-image apply` to write it into the standby
slot. The web tier can never supply a URL, device or slot: the helper verifies
the signed index and the image hash itself and derives the target from the
partition labels.

Reboot stays manual. The image's boot counter and health gate roll a bad slot
back automatically (docs/ab-updates.md in ethos-os-release).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.config import APP_VERSION, settings
from backend.services import json_store, package_repo
from backend.services.notifications_service import emit_event
from backend.services.package_schema import version_gt

logger = logging.getLogger("ethos-nas.services.os_image")

STATE_VERSION = 1
_CHECK_LOCK = asyncio.Lock()
_loop_task: asyncio.Task | None = None
_loop_stop: asyncio.Event | None = None
_download_task: asyncio.Task | None = None

MAX_STATE_BYTES = 4_000_000
FILE_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
HUNG_JOB_SECONDS = 4 * 3600


class OsImageError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ── State ───────────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    path = Path(settings.DATA_DIR) / "os-image"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Could not create %s", path, exc_info=True)
    return path


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _store() -> json_store.Store:
    return json_store.get(_state_dir() / "state.json", dict, version=STATE_VERSION)


def _load_state() -> dict:
    data = _store().load()
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    _store().save(state)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _bounded(value: Any, limit: int = 200) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable()).strip()
    return text[:limit]


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── Host identity ───────────────────────────────────────────────────────────

def os_identity() -> dict:
    info = _read_json(Path("/etc/ethos-os/build-info.json"))
    machine = platform.machine()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine)
    layout = ""
    try:
        layout = Path("/etc/ethos-os/layout").read_text(encoding="utf-8").strip()
    except OSError:
        logger.debug("/etc/ethos-os/layout is unreadable", exc_info=True)
    return {
        "version": _bounded(info.get("os_version"), 64),
        "variant": _bounded(info.get("variant") or "nas", 32),
        "arch": _bounded(arch, 16),
        "layout": _bounded(layout, 16),
    }


def slot_state() -> dict:
    data = _read_json(Path(settings.DATA_DIR) / "system" / "slots.json")
    return {
        "active": _bounded(data.get("active") or "", 1),
        "slots": data.get("slots") if isinstance(data.get("slots"), dict) else {},
        "last_result": data.get("last_result") if isinstance(data.get("last_result"), dict) else {},
        "updated_at": _bounded(data.get("updated_at"), 64),
    }


def helper_available() -> bool:
    path = Path(settings.OS_IMAGE_HELPER)
    return path.is_file() and os.access(path, os.X_OK)


# ── Index ───────────────────────────────────────────────────────────────────

def _coerce_rootfs(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    sha = str(raw.get("sha256") or "").strip().lower()
    url = str(raw.get("url") or "").strip()
    image = str(raw.get("image") or "").strip()
    try:
        size = int(raw.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size = 0
    if not SHA_RE.match(sha) or not url.startswith("https://") or not FILE_RE.match(image):
        return {}
    return {"image": image, "url": url, "sha256": sha,
            "size_bytes": max(0, min(size, settings.OS_IMAGE_MAX_BYTES))}


def _coerce_release(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise OsImageError("OS image entry is not an object")
    rootfs = _coerce_rootfs(raw.get("rootfs"))
    version = _bounded(raw.get("version"), 32)
    if not version:
        raise OsImageError("OS image entry has no version")
    return {
        "version": version,
        "build": _int(raw.get("build")),
        "git_sha": _bounded(raw.get("git_sha"), 64),
        "released_at": _bounded(raw.get("released_at"), 64),
        "notes": _bounded(raw.get("notes"), 4000),
        "layout": _bounded(raw.get("layout") or "single", 16),
        "min_ethos_version": _bounded(raw.get("min_ethos_version"), 32),
        "rootfs": rootfs,
    }


def parse_index(payload: bytes) -> dict | None:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise OsImageError("OS image index is not valid JSON") from None
    if not isinstance(data, dict) or data.get("schema") != 2 or data.get("kind") != "ethos-os":
        raise OsImageError("unsupported OS image index")
    identity = os_identity()
    latest = (data.get("latest") or {}).get(f"{identity['arch']}/{identity['variant']}")
    if latest is None:
        # Images are published per arch/variant; being absent is not an error.
        for key, value in (data.get("latest") or {}).items():
            if str(key).split("/")[0] == identity["arch"]:
                latest = value
                break
    return _coerce_release(latest) if latest is not None else None


def fetch_latest() -> dict | None:
    url = settings.OS_IMAGE_INDEX_URL
    try:
        payload = package_repo.get_bytes(url, 256 * 1024)
        signature = package_repo.get_bytes(url + ".sig", 4096)
        package_repo.verify_signature(
            payload, signature.decode("ascii", "replace").strip(), settings.OS_IMAGE_PUBKEY)
    except package_repo.PackageError as exc:
        raise OsImageError(str(exc)) from None
    except UnicodeDecodeError:
        raise OsImageError("OS image signature is not base64 text") from None
    return parse_index(payload)


def update_available(latest: dict | None, identity: dict) -> bool | None:
    if not latest:
        return None
    if not identity.get("version"):
        return None
    return version_gt(latest["version"], identity["version"])


def status() -> dict:
    state = _load_state()
    identity = os_identity()
    latest = state.get("latest") or None
    downloaded = _downloaded_file(latest)
    return {
        "enabled": settings.OS_IMAGE_ENABLED,
        "helper": helper_available(),
        "helper_path": settings.OS_IMAGE_HELPER,
        "index_url": settings.OS_IMAGE_INDEX_URL,
        "identity": identity,
        "layout": identity.get("layout") or "",
        "ab_supported": identity.get("layout") == "ab1",
        "slots": slot_state(),
        "latest": latest,
        "update_available": update_available(latest, identity),
        "checked_at": state.get("checked_at"),
        "checked_at_iso": state.get("checked_at_iso"),
        "error": _bounded(state.get("error"), 500),
        "downloaded": downloaded,
        "download": _download_status(),
        "install": install_status(),
        "check_hours": settings.OS_IMAGE_CHECK_HOURS,
    }


async def check(force: bool = False) -> dict:
    if not settings.OS_IMAGE_ENABLED:
        return {**status(), "error": "OS image checks are disabled (ETHOS_OS_IMAGE_ENABLED=0)"}
    state = _load_state()
    checked = float(state.get("checked_at") or 0)
    if not force and checked and (time.time() - checked) < settings.OS_IMAGE_TTL_SECONDS:
        return status()
    async with _CHECK_LOCK:
        state = _load_state()
        checked = float(state.get("checked_at") or 0)
        if not force and checked and (time.time() - checked) < settings.OS_IMAGE_TTL_SECONDS:
            return status()
        now = time.time()
        try:
            latest = await asyncio.to_thread(fetch_latest)
        except OsImageError as exc:
            logger.warning("OS image check failed: %s", exc)
            _save_state({**state, "checked_at": now, "checked_at_iso": _iso(now),
                         "error": str(exc)})
            return status()
        previous = (state.get("latest") or {}).get("rootfs", {}).get("sha256", "")
        available = update_available(latest, os_identity())
        _save_state({**state, "checked_at": now, "checked_at_iso": _iso(now),
                     "latest": latest, "error": ""})
        logger.info("OS image check: %s available=%s", (latest or {}).get("version"), available)
        rootfs_sha = (latest or {}).get("rootfs", {}).get("sha256", "")
        if available and latest and rootfs_sha and rootfs_sha != previous:
            emit_event("info", "EthOS OS update available",
                       f"EthOS OS {latest['version']} is published. Open Settings → System.",
                       source="os-image", dedupe_key=f"os-image:{rootfs_sha}",
                       action={"label": "Open Settings", "appId": "settings"})
        return status()


# ── Download ────────────────────────────────────────────────────────────────

def _job_path() -> Path:
    return _state_dir() / "job.json"


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


def _download_status() -> dict:
    job = _read_json(_job_path())
    if job.get("status") == "downloading" and not _pid_alive(_int(job.get("pid"))):
        job = {**job, "status": "failed", "detail": "download was interrupted"}
    return job or {}


def _downloaded_file(latest: dict | None) -> dict:
    if not latest:
        return {}
    rootfs = latest.get("rootfs") or {}
    name = rootfs.get("image") or ""
    if not FILE_RE.match(name):
        return {}
    path = _state_dir() / name
    try:
        size = path.stat().st_size
    except OSError:
        return {}
    return {"file": name, "size_bytes": size, "sha256": rootfs.get("sha256", ""),
            "ready": size == rootfs.get("size_bytes")}


def _progress_writer(file_name: str):
    last = {"at": 0.0}

    def _progress(done: int, total: int) -> None:
        now = time.monotonic()
        if now - last["at"] < 1.0 and done != total:
            return
        last["at"] = now
        percent = int(done * 100 / total) if total > 0 else 0
        _write_json(_job_path(), {
            "status": "downloading", "step": "download", "percent": max(0, min(99, percent)),
            "bytes_done": done, "bytes_total": total, "file": file_name,
            "pid": os.getpid(), "at": _iso(time.time()),
        })
    return _progress


def _download_worker(latest: dict) -> None:
    rootfs = latest.get("rootfs") or {}
    name = rootfs["image"]
    destination = _state_dir() / name
    _write_json(_job_path(), {
        "status": "downloading", "step": "download", "percent": 0,
        "bytes_done": 0, "bytes_total": rootfs.get("size_bytes", 0),
        "file": name, "pid": os.getpid(), "at": _iso(time.time()),
    })
    try:
        meta = package_repo.download_to_file(
            rootfs["url"], destination,
            max_bytes=settings.OS_IMAGE_MAX_BYTES,
            expected_sha256=rootfs["sha256"],
            progress=_progress_writer(name),
        )
    except package_repo.PackageError as exc:
        logger.warning("OS image download failed: %s", exc)
        _write_json(_job_path(), {"status": "failed", "step": "download", "percent": 0,
                                  "detail": str(exc)[:300], "at": _iso(time.time())})
        return
    _write_json(_job_path(), {"status": "downloaded", "step": "download", "percent": 100,
                              "file": name, "sha256": meta["sha256"],
                              "size_bytes": meta["size_bytes"], "at": _iso(time.time())})
    emit_event("success", "EthOS OS image downloaded",
               f"{latest['version']} is ready to install into the standby slot.",
               source="os-image", dedupe_key=f"os-image-download:{rootfs['sha256']}")


def download_status() -> dict:
    """Progress/result of the backend download job (public for the router)."""
    return _download_status()


async def start_download() -> dict:
    global _download_task
    if not settings.OS_IMAGE_ENABLED:
        raise OsImageError("OS image updates are disabled (ETHOS_OS_IMAGE_ENABLED=0)", 409)
    identity = os_identity()
    if identity.get("layout") != "ab1":
        raise OsImageError("This system is not using the A/B layout — reflash first.", 409)
    state = _load_state()
    latest = state.get("latest") or None
    if not latest or not (latest.get("rootfs") or {}).get("url"):
        raise OsImageError("Check for an OS image first.", 409)
    if not FILE_RE.match(str((latest.get("rootfs") or {}).get("image") or "")):
        raise OsImageError("The published image has no usable artifact.", 409)
    if not update_available(latest, identity):
        raise OsImageError(f"EthOS OS {identity.get('version') or '?'} is up to date.", 409)
    if _download_status().get("status") == "downloading":
        raise OsImageError("A download is already running", 409)
    if _downloaded_file(latest).get("ready"):
        return {"status": "downloaded", **_downloaded_file(latest)}
    from backend.services.task_registry import spawn

    _download_task = spawn(asyncio.to_thread(_download_worker, latest), name="os-image-download")
    logger.info("OS image download started (%s)", latest["version"])
    return {"status": "downloading", "version": latest["version"]}


async def delete_download() -> dict:
    if _download_status().get("status") == "downloading":
        raise OsImageError("Wait for the running download to finish", 409)
    if install_status().get("running"):
        raise OsImageError("An install is running", 409)
    state = _load_state()
    latest = state.get("latest") or {}
    name = (latest.get("rootfs") or {}).get("image") or ""
    removed = False
    if FILE_RE.match(name):
        path = _state_dir() / name
        try:
            path.unlink()
            removed = True
        except OSError:
            logger.debug("No downloaded image to remove", exc_info=True)
    for path in (_job_path(), _state_dir() / "request.json"):
        path.unlink(missing_ok=True)
    return {"removed": removed}


# ── Install (root helper) ───────────────────────────────────────────────────

def _helper_argv(action: str) -> list[str]:
    base = [settings.OS_IMAGE_HELPER, action]
    return ["sudo", "-n", *base] if settings.OS_IMAGE_VIA_SUDO else base


def _helper_job_stale(job: dict) -> bool:
    if not isinstance(job, dict) or job.get("status") != "running":
        return False
    pid = _int(job.get("pid"))
    if pid > 0 and not _pid_alive(pid):
        return True
    try:
        started = datetime.fromisoformat(str(job.get("at") or "")).timestamp()
    except (TypeError, ValueError):
        return True
    return (time.time() - started) > HUNG_JOB_SECONDS


def _report_result(result: dict) -> None:
    stamp = _bounded(result.get("at"), 64)
    if not stamp:
        return
    state = _load_state()
    if stamp == state.get("last_result_at"):
        return
    outcome = _bounded(result.get("status"), 32)
    if outcome == "ok":
        emit_event("warning", "EthOS OS image staged",
                   f"{result.get('detail') or 'Reboot to apply'}",
                   source="os-image", dedupe_key=f"os-image-result:{stamp}",
                   action={"label": "Open Settings", "appId": "settings"})
    elif outcome == "failed":
        emit_event("error", "EthOS OS image install failed",
                   _bounded(result.get("detail"), 300),
                   source="os-image", dedupe_key=f"os-image-result:{stamp}")
    _save_state({**state, "last_result_at": stamp, "last_result": outcome})


def install_status() -> dict:
    job = _read_json(_job_path())
    result = _read_json(_state_dir() / "result.json")
    stale = _helper_job_stale(job)
    if stale and not result:
        result = {"status": "failed", "detail": "the image helper is no longer running",
                  "at": _bounded(job.get("at"), 64) or "stale"}
    if result:
        try:
            _report_result(result)
        except Exception:
            logger.error("Could not report the OS image result", exc_info=True)
    return {"running": job.get("status") == "running" and not stale,
            "stale": stale, "job": job or None, "result": result or None}


def _spawn_helper() -> None:
    subprocess.Popen(_helper_argv("apply"), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, cwd="/")


async def install() -> dict:
    if not settings.OS_IMAGE_ENABLED:
        raise OsImageError("OS image updates are disabled (ETHOS_OS_IMAGE_ENABLED=0)", 409)
    if not helper_available():
        raise OsImageError(
            f"OS image helper not installed ({settings.OS_IMAGE_HELPER}). "
            "Run: sudo bash setup_scripts/setup_service.sh", 409)
    identity = os_identity()
    if identity.get("layout") != "ab1":
        raise OsImageError("This system is not using the A/B layout — reflash first.", 409)
    latest = _load_state().get("latest") or None
    if not latest:
        raise OsImageError("Check for an OS image first.", 409)
    min_version = str(latest.get("min_ethos_version") or "")
    if min_version and version_gt(min_version, APP_VERSION):
        raise OsImageError(
            f"This image needs EthOS NAS {min_version} or newer (running {APP_VERSION}).", 409)
    active = slot_state().get("active") or ""
    if str((slot_state().get("slots") or {}).get(active, {}).get("status") or "") == "candidate":
        raise OsImageError("A candidate slot is already pending — reboot or roll back first.", 409)
    downloaded = _downloaded_file(latest)
    if not downloaded.get("ready"):
        raise OsImageError("The image is not downloaded yet.", 409)
    if install_status().get("running"):
        raise OsImageError("An install is already running", 409)
    request = {"action": "apply", "file": downloaded["file"], "requested_at": _iso(time.time())}
    _write_json(_state_dir() / "request.json", request)
    os.chmod(_state_dir() / "request.json", 0o600)
    try:
        await asyncio.to_thread(_spawn_helper)
    except OSError as exc:
        raise OsImageError(f"Cannot start the helper: {exc}", 500) from None
    logger.info("OS image install requested (%s)", latest.get("version"))
    return {"status": "installing", "version": latest.get("version")}


def reconcile_result() -> None:
    try:
        install_status()
    except Exception:
        logger.error("OS image result reconcile failed", exc_info=True)


# ── Background checker ──────────────────────────────────────────────────────

async def _loop(stop: asyncio.Event) -> None:
    interval = max(1.0, float(settings.OS_IMAGE_CHECK_HOURS)) * 3600
    logger.info("OS image checker started (every %.0f h)", interval / 3600)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass  # intentional: the timeout is the scheduled wake-up
        try:
            await check(force=True)
        except OsImageError as exc:
            logger.warning("OS image checker: %s", exc)
        except Exception:
            logger.error("OS image checker iteration failed", exc_info=True)
    logger.info("OS image checker stopped")


def start_os_image_checker() -> None:
    global _loop_task, _loop_stop
    if _loop_task is not None and not _loop_task.done():
        return
    from backend.services.task_registry import spawn

    _loop_stop = asyncio.Event()
    _loop_task = spawn(_loop(_loop_stop), name="os-image-checker")


async def stop_os_image_checker() -> None:
    global _loop_task, _loop_stop
    if _loop_stop is not None:
        _loop_stop.set()
    if _loop_task is not None and not _loop_task.done():
        try:
            await asyncio.wait_for(_loop_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug("OS image checker did not stop in time", exc_info=True)
    _loop_task = None
    _loop_stop = None
