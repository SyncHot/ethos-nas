"""EthOS NAS — core update check (signed release index).

The public repository publishes `releases/index.json` plus an Ed25519
signature (`index.json.sig`). This service fetches it, verifies the signature
against the pinned release key (`settings.UPDATE_PUBKEY`), and compares the
published `git_sha`/`build` with the running checkout.

It is deliberately **check-only**: nothing is downloaded, installed or executed.
The Settings → System card shows the result and the operator upgrades with the
documented commands (docs/open-source.md). Spec: docs/updates.md.

State lives in `<DATA_DIR>/update/state.json` (versioned store) so a restart
keeps the last result and the background checker does not re-notify.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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

logger = logging.getLogger("ethos-nas.services.update_service")

STATE_VERSION = 1
_CHECK_LOCK = asyncio.Lock()
_loop_task: asyncio.Task | None = None
_loop_stop: asyncio.Event | None = None

REPO_SLUG = "SyncHot/ethos-nas"


def release_download_url(version: str) -> str:
    """GitHub source archive for a release tag ("" without a usable version).

    Every published release is tagged `v<version>` by the release ritual, so the
    tag archive is the canonical downloadable copy of that release. The Settings
    card links to it; nothing here downloads or executes it.
    """
    v = str(version or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z._-]{1,32}", v):
        return ""
    return f"https://github.com/{REPO_SLUG}/archive/refs/tags/v{v}.zip"


# ── State store ─────────────────────────────────────────────────────────────

def _state_path() -> Path:
    """Resolved per call so ETHOS_DATA_DIR is honoured (and testable)."""
    return Path(settings.DATA_DIR) / "update" / "state.json"


def _store() -> json_store.Store:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("Could not create %s", path.parent, exc_info=True)
    return json_store.get(path, dict, version=STATE_VERSION)


def _load() -> dict:
    data = _store().load()
    return data if isinstance(data, dict) else {}


def _save(state: dict) -> None:
    _store().save(state)


# ── Identity of the running build ───────────────────────────────────────────

def _git(*args: str) -> str:
    root = Path(__file__).resolve().parent.parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        logger.debug("git %s unavailable", " ".join(args), exc_info=True)
        return ""


def current_release() -> dict:
    """Version identity of the running checkout (git is best-effort).

    `build` is the commit count, which is only meaningful in a full clone. An
    EthOS OS image ships a `--depth 1` clone, where `rev-list --count HEAD`
    reports 1 no matter how far along the release train the appliance is — the
    update card then advertised an update forever, even right after updating.
    A shallow checkout reports build 0, so `is_newer` falls back to comparing
    the commit sha (which the signed index pins exactly).
    """
    shallow = _git("rev-parse", "--is-shallow-repository") == "true"
    build = 0
    if not shallow:
        try:
            build = int(_git("rev-list", "--count", "HEAD") or 0)
        except ValueError:
            build = 0
    return {
        "version": APP_VERSION,
        "git_sha": _git("rev-parse", "--short", "HEAD"),
        "build": build,
        "shallow": shallow,
        "download_url": release_download_url(APP_VERSION),
    }


# ── Index fetch + validation ────────────────────────────────────────────────

class UpdateError(RuntimeError):
    """A check or apply could not be completed (bad signature, network, state)."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _coerce_release(raw: Any) -> dict:
    """Keep only the fields the UI renders, with bounded types."""
    if not isinstance(raw, dict):
        raise UpdateError("release entry is not an object")
    sha = str(raw.get("git_sha") or "").strip()[:64]
    version = str(raw.get("version") or "").strip()[:32]
    if not sha and not version:
        raise UpdateError("release entry has neither git_sha nor version")
    try:
        build = int(raw.get("build") or 0)
    except (TypeError, ValueError):
        build = 0
    return {
        "version": version,
        "git_sha": sha,
        "build": build,
        "released_at": str(raw.get("released_at") or "").strip()[:64],
        "notes": str(raw.get("notes") or "").strip()[:8000],
        "commit_url": f"https://github.com/{REPO_SLUG}/commit/{sha}" if sha else "",
        "download_url": release_download_url(version),
    }


def parse_index(payload: bytes) -> dict:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"release index is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise UpdateError("unsupported release index schema")
    latest = data.get("latest")
    if latest is None and isinstance(data.get("releases"), list) and data["releases"]:
        latest = data["releases"][0]
    return _coerce_release(latest)


def fetch_latest() -> dict:
    """Fetch, verify and parse the release index (blocking; call in a thread)."""
    url = settings.UPDATE_INDEX_URL
    try:
        payload = package_repo.get_bytes(url, settings.UPDATE_MAX_INDEX_BYTES)
        signature = package_repo.get_bytes(url + ".sig", 4096)
    except package_repo.PackageError as exc:
        raise UpdateError(str(exc)) from None
    try:
        package_repo.verify_signature(
            payload, signature.decode("ascii", "replace").strip(), settings.UPDATE_PUBKEY,
        )
    except package_repo.PackageError as exc:
        raise UpdateError(str(exc)) from None
    except UnicodeDecodeError:
        raise UpdateError("release signature is not base64 text") from None
    return parse_index(payload)


# ── Comparison ──────────────────────────────────────────────────────────────

def _shas_match(left: str, right: str) -> bool:
    left, right = (left or "").lower(), (right or "").lower()
    if not left or not right:
        return False
    return left.startswith(right) or right.startswith(left)


def is_newer(latest: dict, current: dict) -> bool:
    """True when the published release is ahead of the running checkout.

    The monotonic commit count is authoritative: the commit that ships the index
    itself is one ahead of the indexed release and must not report an update.
    The commit sha and the semantic version are fallbacks for installs without
    git metadata (tarball deployments).
    """
    latest_build = int(latest.get("build") or 0)
    current_build = int(current.get("build") or 0)
    if latest_build and current_build:
        return latest_build > current_build
    latest_sha, current_sha = latest.get("git_sha", ""), current.get("git_sha", "")
    if latest_sha and current_sha:
        return not _shas_match(latest_sha, current_sha)
    return version_gt(str(latest.get("version") or ""), str(current.get("version") or ""))


# ── Public API ──────────────────────────────────────────────────────────────

def status() -> dict:
    """Cached result only — never triggers a network request."""
    state = _load()
    return {
        "enabled": settings.UPDATE_ENABLED,
        "index_url": settings.UPDATE_INDEX_URL,
        "key_fingerprint": package_repo.key_fingerprint(settings.UPDATE_PUBKEY),
        "current": current_release(),
        "latest": state.get("latest") or None,
        "update_available": state.get("update_available"),
        "checked_at": state.get("checked_at"),
        "checked_at_iso": state.get("checked_at_iso"),
        "error": state.get("error", ""),
        "auto_check_hours": settings.UPDATE_CHECK_HOURS,
    }


async def check(force: bool = False) -> dict:
    """Check the signed index, caching the result for UPDATE_TTL_SECONDS."""
    if not settings.UPDATE_ENABLED:
        return {**status(), "error": "Update checks are disabled (ETHOS_UPDATE_ENABLED=0)"}

    state = _load()
    checked_at = state.get("checked_at") or 0
    if not force and checked_at and (time.time() - checked_at) < settings.UPDATE_TTL_SECONDS:
        return status()

    async with _CHECK_LOCK:
        # Another caller may have refreshed while we waited for the lock.
        state = _load()
        checked_at = state.get("checked_at") or 0
        if not force and checked_at and (time.time() - checked_at) < settings.UPDATE_TTL_SECONDS:
            return status()

        current = await asyncio.to_thread(current_release)
        now = time.time()
        try:
            latest = await asyncio.to_thread(fetch_latest)
        except UpdateError as exc:
            logger.warning("Core update check failed: %s", exc)
            _save({
                "checked_at": now,
                "checked_at_iso": _iso(now),
                "current": current,
                "latest": state.get("latest") or None,
                "update_available": state.get("update_available"),
                "error": str(exc),
            })
            return status()

        update_available = is_newer(latest, current)
        previous_sha = (state.get("latest") or {}).get("git_sha", "")
        _save({
            "checked_at": now,
            "checked_at_iso": _iso(now),
            "current": current,
            "latest": latest,
            "update_available": update_available,
            "error": "",
        })
        logger.info(
            "Core update check: running %s (%s), latest %s (%s), update_available=%s",
            current.get("version"), current.get("git_sha") or "?",
            latest.get("version"), latest.get("git_sha") or "?", update_available,
        )
        if update_available and latest.get("git_sha") and latest["git_sha"] != previous_sha:
            emit_event(
                "info",
                "EthOS NAS update available",
                f"v{latest.get('version') or '?'} ({latest.get('git_sha')}) is published. "
                "Open Settings → System for details.",
                source="updates",
                dedupe_key=f"core-update:{latest['git_sha']}",
                action={"label": "Open Settings", "appId": "settings"},
            )
        return status()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


# ── Apply (root helper) ────────────────────────────────────────────────────

def _state_dir() -> Path:
    return Path(settings.DATA_DIR) / "update"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _process_started_at() -> float:
    """Epoch seconds when this Python process started (0.0 when unknown)."""
    try:
        ticks = os.sysconf("SC_CLK_TCK")
        with open("/proc/self/stat", encoding="utf-8") as fh:
            fields = fh.read().split()
        start_ticks = int(fields[21])
        with open("/proc/uptime", encoding="utf-8") as fh:
            uptime = float(fh.read().split()[0])
        return time.time() - (uptime - start_ticks / ticks)
    except (OSError, ValueError, IndexError):
        return 0.0


def _job_started_at(job: dict) -> float:
    try:
        return datetime.fromisoformat(str(job.get("at") or "")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _job_is_stale(job: dict) -> bool:
    """A `running` job that cannot still be alive.

    The helper performs the service restart itself. An older helper lived in
    the unit's cgroup and was killed by that restart, leaving `job.json` at
    step `restart` forever — every later update then answered 409 "already
    running". A restart/verify step that predates this process means the helper
    is gone; a job whose last step is over two hours old is treated as hung.

    The helper now records its PID, so a helper that died for any other reason
    (a `die` in the fetch block, a missing repo owner) is detected immediately
    instead of two hours later — that was the "update spins with no progress"
    bug reports.
    """
    if not isinstance(job, dict) or job.get("status") != "running":
        return False
    started = _job_started_at(job)
    if started <= 0:
        return True
    process_start = _process_started_at()
    if job.get("step") in ("restart", "verify") and process_start and started < process_start:
        return True
    try:
        pid = int(job.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            logger.warning("Update helper pid %s is gone (step %s)", pid, job.get("step"))
            return True
        except PermissionError:
            pass  # exists, owned by root — still running
        except OSError:
            logger.debug("Could not probe update helper pid %s", pid, exc_info=True)
    return (time.time() - started) > 2 * 3600


def _finalize_dead_job(job: dict) -> dict:
    """Write a terminal result for a helper that is no longer running.

    Only the helper writes these files, so a `running` job with a dead PID can
    only mean it exited without reporting (set -e before the EXIT trap existed).
    The UI polled that state forever; turning it into a failure both stops the
    spinner and tells the operator what to do.
    """
    step = str(job.get("step") or "?")
    detail = (
        f"the update helper stopped during step {step} without reporting a result; "
        "see `journalctl -u ethos-nas | grep ethos-update` on the server"
    )
    result = {
        "status": "failed",
        "step": "done",
        "from": job.get("from", ""),
        "to": job.get("to", ""),
        "version": job.get("version", ""),
        "detail": detail,
        "at": _iso(time.time()),
    }
    job.update({"status": "failed", "step": "done", "detail": detail, "at": result["at"]})
    try:
        state_dir = _state_dir()
        (state_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
        os.chmod(state_dir / "result.json", 0o644)
        (state_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
        os.chmod(state_dir / "job.json", 0o644)
        request = state_dir / "request.json"
        if request.exists():
            request.unlink()
        logger.warning("Finalized dead update job: %s", detail)
    except OSError:
        logger.warning("Could not finalize the dead update job", exc_info=True)
    return result


def helper_available() -> bool:
    path = Path(settings.UPDATE_HELPER)
    return path.is_file() and os.access(path, os.X_OK)


def apply_status() -> dict:
    """Progress/result written by `ethos-update` (no network, no privileged call)."""
    job = _read_json(_state_dir() / "job.json")
    result = _read_json(_state_dir() / "result.json")
    stale = _job_is_stale(job)
    if stale:
        logger.warning("Stale update job (step %s, at %s) — not treated as running",
                       job.get("step"), job.get("at"))
        if not result:
            result = _finalize_dead_job(job)
    return {
        "helper": helper_available(),
        "running": job.get("status") == "running" and not stale,
        "stale": stale,
        "job": job or None,
        "result": result or None,
    }


def _spawn_helper() -> None:
    """Start the root helper detached (returns immediately)."""
    subprocess.Popen(
        ["sudo", "-n", settings.UPDATE_HELPER],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd="/",
    )


async def request_apply() -> dict:
    """Ask the root helper to install the published release.

    Only a request file is written — the helper fetches and verifies the signed
    index itself, so a compromised backend cannot redirect the update.
    """
    if not settings.UPDATE_ENABLED:
        raise UpdateError("Update checks are disabled (ETHOS_UPDATE_ENABLED=0)", 409)
    if not helper_available():
        raise UpdateError(
            f"Update helper not installed ({settings.UPDATE_HELPER}). "
            "Run: sudo bash setup_scripts/setup_service.sh", 409,
        )
    current = status()
    if current.get("update_available") is not True:
        raised = "No newer release is known — run a check first." if current.get("checked_at") is None \
            else "This server is already up to date."
        raise UpdateError(raised, 409)
    if apply_status().get("running"):
        raise UpdateError("An update is already running", 409)

    # Pre-update data snapshot: the helper rolls back code, not JSON stores.
    try:
        from backend.services import config_archive

        await asyncio.to_thread(config_archive.take_snapshot, "pre-update")
        logger.info("Pre-update snapshot written")
    except Exception:
        logger.error("Pre-update snapshot failed — continuing", exc_info=True)

    state_dir = _state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    request = state_dir / "request.json"
    request.write_text(json.dumps({"action": "apply", "requested_at": _iso(time.time())}),
                       encoding="utf-8")
    os.chmod(request, 0o600)

    _spawn_helper()
    logger.info("Core update requested — helper started (target %s)",
                (current.get("latest") or {}).get("git_sha", "?"))
    return {"status": "updating", "target": current.get("latest")}


def reconcile_result() -> None:
    """After a restart: report the helper outcome once and remember it."""
    state_dir = _state_dir()
    result = _read_json(state_dir / "result.json")
    if not result:
        job = _read_json(state_dir / "job.json")
        if _job_is_stale(job):
            # The old helper was killed by its own restart. The checkout tells
            # us whether the update actually landed.
            current = current_release()
            target_version = str(job.get("version") or "")
            running_version = str(current.get("version") or "")
            completed = bool(target_version) and not version_gt(target_version, running_version)
            result = {
                "status": "ok" if completed else "failed",
                "step": "done",
                "from": job.get("from", ""),
                "to": job.get("to", ""),
                "version": running_version or target_version,
                "at": _iso(time.time()),
                "detail": (
                    f"helper was stopped by its restart; running {running_version} "
                    f"is at least {target_version}"
                    if completed else
                    f"helper stopped at step {job.get('step') or '?'}; "
                    f"running {running_version or 'unknown'}"
                ),
            }
            try:
                (state_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
                os.chmod(state_dir / "result.json", 0o644)
                job.update({"status": result["status"], "step": "done",
                            "detail": result["detail"], "at": result["at"]})
                (state_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
                os.chmod(state_dir / "job.json", 0o644)
                stale_request = state_dir / "request.json"
                if stale_request.exists():
                    stale_request.unlink()
                logger.info("Finalized stale update job as %s", result["status"])
            except OSError:
                logger.warning("Could not finalize the stale update job", exc_info=True)
    if not result:
        return
    stamp = str(result.get("at") or "")
    if not stamp or stamp == _load().get("last_result_at"):
        return
    state = result.get("status")
    version = result.get("version") or result.get("to", "")
    detail = str(result.get("detail") or "")
    dedupe = f"core-update-result:{stamp}"
    if state == "ok":
        emit_event("success", "EthOS NAS updated", f"Now running {version}".strip(),
                   source="updates", dedupe_key=dedupe)
    elif state == "rolled_back":
        emit_event("error", "Update failed — previous version restored", detail,
                   source="updates", dedupe_key=dedupe)
    elif state == "failed":
        emit_event("error", "Update failed", detail, source="updates", dedupe_key=dedupe)
    _save({**_load(), "last_result_at": stamp})


# ── Background checker ──────────────────────────────────────────────────────

async def _loop(stop: asyncio.Event) -> None:
    interval = max(1.0, float(settings.UPDATE_CHECK_HOURS)) * 3600
    logger.info("Core update checker started (every %.0f h)", interval / 3600)
    while not stop.is_set():
        # Sleep first: a short-lived dev/test instance must never reach the network.
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass  # intentional: the timeout is the scheduled wake-up, not a failure
        try:
            await check(force=True)
        except Exception:
            logger.error("Core update checker iteration failed", exc_info=True)
    logger.info("Core update checker stopped")


def start_update_checker() -> None:
    global _loop_task, _loop_stop
    if _loop_task is not None and not _loop_task.done():
        return
    from backend.services.task_registry import spawn

    _loop_stop = asyncio.Event()
    _loop_task = spawn(_loop(_loop_stop), name="core-update-checker")


async def stop_update_checker() -> None:
    global _loop_task, _loop_stop
    if _loop_stop is not None:
        _loop_stop.set()
    if _loop_task is not None and not _loop_task.done():
        try:
            await asyncio.wait_for(_loop_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.debug("Update checker did not stop in time", exc_info=True)
    _loop_task = None
    _loop_stop = None
