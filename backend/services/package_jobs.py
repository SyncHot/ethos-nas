"""In-memory package install/update/uninstall jobs.

The HTTP request returns a job id immediately; the UI polls
``GET /api/packages/jobs/{id}`` for steps and log lines. Jobs are intentionally
process-local (a restart cancels them and their package state is rolled back).
"""

import asyncio
import logging
import threading
import time
import uuid
from typing import Awaitable, Callable

from backend.services import pressure_guard
from backend.services.package_schema import PackageError
from backend.services.task_registry import spawn

logger = logging.getLogger("ethos-nas.services.package_jobs")

_MAX_LOG = 300
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
_ACTIVE_PACKAGES: set[str] = set()
_SEMAPHORE = asyncio.Semaphore(2)

# Ordered step plans per job kind. ``set_step`` derives ``progress`` from these
# so the UI can draw a determinate bar without hard-coding installer internals.
STEP_PLANS: dict[str, tuple[str, ...]] = {
    # `wheels` downloads the offline wheelhouse, `venv` builds the private
    # environment and installs it (runtime 'venv' packages only; the steps are
    # skipped for compose packages, the plan is shared to keep progress stable).
    "install": ("requires", "verify", "download", "policy", "wheels", "deps", "deploy", "venv",
                "start", "done"),
    "update": ("requires", "verify", "download", "policy", "wheels", "deps", "backup", "venv",
               "start", "done"),
    "uninstall": ("stop", "images", "sysdeps", "cleanup", "data", "done"),
    # Host provisioning (Docker Engine). Not tied to a package id: the owner id
    # is `system:docker`, which cannot collide with a package id.
    "docker-install": ("check", "install", "verify", "done"),
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def create(kind: str, package_id: str) -> dict:
    with _LOCK:
        if package_id in _ACTIVE_PACKAGES:
            raise PackageError(f"Another operation is already running for {package_id}", 409)
        if not pressure_guard.heavy_work_allowed():
            # Building a venv / pulling images is the heaviest thing the core
            # does; refuse while the box is under hard memory pressure.
            raise PackageError(
                "The NAS is low on memory — try again in a moment", 503)
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        plan = STEP_PLANS.get(kind, ())
        job = {
            "id": job_id,
            "kind": kind,
            "package_id": package_id,
            "status": "queued",
            "step": "queued",
            "steps": [],
            "log": [],
            "progress": 0,
            "step_index": 0,
            "step_total": len(plan),
            "error": "",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
        }
        _JOBS[job_id] = job
        _ACTIVE_PACKAGES.add(package_id)
        return dict(job)


def get(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def list_jobs() -> list[dict]:
    with _LOCK:
        return [dict(job) for job in sorted(_JOBS.values(), key=lambda j: j["created_at"], reverse=True)]


def update(job_id: str, **fields) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.update(fields)


def log(job_id: str | None, message: str) -> None:
    """Append one log line to a job. A missing job id (system-scope call) is a no-op."""
    if job_id is None:
        return
    line = str(message).strip()[:500]
    if not line:
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["log"].append(line)
        if len(job["log"]) > _MAX_LOG:
            del job["log"][0: len(job["log"]) - _MAX_LOG]


def set_step(job_id: str, name: str, status: str = "running") -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        job["step"] = name
        plan = STEP_PLANS.get(str(job.get("kind") or ""), ())
        if name in plan:
            job["step_index"] = plan.index(name) + 1
            job["step_total"] = len(plan)
            # A step owns its slice of the bar; set_step opens the slice and
            # set_step_progress fills it so the bar never moves backwards.
            job["progress"] = 100 if name == "done" else round(plan.index(name) * 100 / len(plan))
        for step in job["steps"]:
            if step.get("name") == name:
                step["status"] = status
                break
        else:
            job["steps"].append({"name": name, "status": status})


def set_step_progress(job_id: str, name: str, done: int, total: int) -> None:
    """Interpolate the bar inside a long step (e.g. files downloaded so far)."""
    if total <= 0:
        return
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        plan = STEP_PLANS.get(str(job.get("kind") or ""), ())
        if name not in plan:
            return
        index = plan.index(name)
        start = index * 100 / len(plan)
        end = 100 if index == len(plan) - 1 else (index + 1) * 100 / len(plan)
        frac = max(0.0, min(1.0, done / total))
        job["progress"] = max(0, min(99, int(round(start + (end - start) * frac))))


def start(job: dict, work: Callable[[str], Awaitable[None]]) -> bool:
    """Run `work(job_id)` detached. Returns False when no event loop exists."""
    job_id = job["id"]
    package_id = job["package_id"]
    plan_size = len(STEP_PLANS.get(str(job.get("kind") or ""), ()))

    async def _run() -> None:
        async with _SEMAPHORE:
            update(job_id, status="running", started_at=_now())
            kind = str(job.get("kind") or "job")
            try:
                await work(job_id)
                update(job_id, status="success", step="done", progress=100,
                       step_index=plan_size, step_total=plan_size, finished_at=_now())
                _record_outcome(package_id, kind, "info", f"{kind} succeeded")
            except PackageError as exc:
                logger.error("Package job %s failed: %s", job_id, exc.detail)
                update(job_id, status="failed", error=exc.detail, finished_at=_now())
                _record_outcome(package_id, kind, "error", f"{kind} failed: {exc.detail}", exc.detail)
            except asyncio.CancelledError:
                update(job_id, status="cancelled", error="Cancelled during shutdown", finished_at=_now())
                _record_outcome(package_id, kind, "warning", f"{kind} cancelled during shutdown")
                raise
            except Exception as exc:
                logger.error("Package job %s crashed: %s", job_id, exc, exc_info=True)
                update(job_id, status="failed", error=str(exc), finished_at=_now())
                _record_outcome(package_id, kind, "error", f"{kind} crashed: {exc}")
            finally:
                with _LOCK:
                    _ACTIVE_PACKAGES.discard(package_id)

    task = spawn(_run(), name=f"pkg-{job['kind']}-{package_id}")
    if task is None:
        with _LOCK:
            _ACTIVE_PACKAGES.discard(package_id)
            _JOBS.pop(job_id, None)
        return False
    return True


def _record_outcome(package_id: str, kind: str, level: str, message: str, detail: str = "") -> None:
    """Mirror a job outcome into the unified log store (docs/logs.md).

    The per-job log stays in `package_jobs` (the Packages panel shows it);
    the unified store keeps one durable row per job so the Logs app can explain
    a failed install months later.
    """
    try:
        from backend.services import log_service

        if package_id.startswith("system:"):
            app, source = "core", f"packages.{package_id.split(':', 1)[1]}"
        else:
            app, source = f"pkg:{package_id}", "package-center"
        log_service.record(level, source, message, detail, app=app)
    except Exception:  # noqa: BLE001 — logging must not break the job
        logger.debug("Could not record package job outcome", exc_info=True)
