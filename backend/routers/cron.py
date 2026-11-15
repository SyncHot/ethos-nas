"""EthOS NAS — Cron Job Manager Router

REST:
- GET    /api/cron          — list all cron jobs from crontab
- POST   /api/cron          — add a new cron job
- PUT    /api/cron/{job_id} — update an existing cron job
- DELETE /api/cron/{job_id} — remove a cron job

Uses ``crontab -l`` and ``crontab -`` to read/write the user crontab.
Each managed job is tagged with ``# ETHOS-CRON:{job_id}:{enabled}`` in the crontab.
"""

import asyncio, json, logging, re, uuid, subprocess, shlex, tempfile
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.routers.auth import require_admin

logger = logging.getLogger("ethos-nas.routers.cron")
# Cron entries execute arbitrary commands as the service user — admin-only.
router = APIRouter(dependencies=[Depends(require_admin)])

# Every handler is a read-modify-write of a single crontab. Without a lock two
# concurrent requests read the same text and the second write silently drops the
# first job — and `crontab -l/-` are subprocess calls that must not run on the
# event loop (each can block for up to 10 s).
_CRON_LOCK = asyncio.Lock()

# ── Models ────────────────────────────────────────────────────────────────────

class CronJobCreate(BaseModel):
    schedule: str   # e.g. "0 2 * * *"
    command: str    # e.g. "/usr/bin/backup.sh"
    comment: str = ""
    enabled: bool = True
    model_config = {"extra": "forbid"}

class CronJobUpdate(BaseModel):
    schedule: Optional[str] = None
    command: Optional[str] = None
    comment: Optional[str] = None
    enabled: Optional[bool] = None
    model_config = {"extra": "forbid"}

# ── Crontab parsing ───────────────────────────────────────────────────────────

_CRON_LINE_RE = re.compile(
    r'^(?P<schedule>(?:[0-9*,/\-]+|@(?:reboot|yearly|annually|monthly|weekly|daily|midnight|hourly))\s+'
    r'(?:[0-9*,/\-]+\s+){3,4}[0-9*,/\-]+)'
    r'\s+(?P<command>.+)$'
)

_ETHOS_MARKER_RE = re.compile(r'^#\s*ETHOS-CRON:(?P<job_id>[a-f0-9\-]+):(?P<enabled>true|false)$')
_ETHOS_MARKER_FMT = lambda job_id, enabled: f'# ETHOS-CRON:{job_id}:{"true" if enabled else "false"}'

def _read_crontab() -> str:
    """Return the crontab text, or empty string if no crontab exists."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            # rstrip so the trailing newline of `crontab -l` output doesn't
            # turn into an extra blank line on every read/write round-trip.
            return result.stdout.rstrip("\n")
        # crontab -l exits 1 when no crontab is installed
        return ""
    except FileNotFoundError:
        logger.warning("crontab command not found — is cron installed?")
        return ""
    except Exception as exc:
        logger.debug("crontab -l error: %s", exc)
        return ""


def _write_crontab(content: str) -> bool:
    """Write content to the user crontab. Returns True on success."""
    try:
        proc = subprocess.Popen(
            ["crontab", "-"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        stdout, stderr = proc.communicate(input=content, timeout=10)
        if proc.returncode != 0:
            logger.error("crontab write failed: %s", stderr)
            return False
        return True
    except FileNotFoundError:
        logger.warning("crontab command not found")
        return False
    except Exception as exc:
        logger.error("crontab write error: %s", exc)
        return False


def _parse_crontab(text: str) -> list[dict]:
    """Parse crontab text into a list of job dicts.

    EthOS-managed jobs have an # ETHOS-CRON:... marker comment before them.
    Non-managed lines (comments, env vars, blank lines) are preserved as-is.
    """
    jobs = []
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        marker_match = _ETHOS_MARKER_RE.match(line.strip())
        if marker_match:
            # Managed job — next line should be the cron expression
            job_id = marker_match.group("job_id")
            enabled = marker_match.group("enabled") == "true"
            i += 1
            if i < len(lines):
                cron_line = lines[i].strip()
                # Disabled jobs are stored with their line commented out so the
                # cron daemon skips them — strip that prefix before matching.
                if cron_line.startswith("#"):
                    cron_line = cron_line.lstrip("#").lstrip()
                cron_match = _CRON_LINE_RE.match(cron_line)
                if cron_match:
                    # Comment lives on its own line above the marker (if any).
                    comment = ""
                    j = i - 2
                    while j >= 0:
                        prev = lines[j].strip()
                        if prev.startswith("#") and not _ETHOS_MARKER_RE.match(prev):
                            comment = prev.lstrip("#").lstrip().strip()
                            break
                        elif prev == "":
                            j -= 1
                            continue
                        else:
                            # Non-comment, non-blank line — stop looking
                            break
                    # NOTE: no '#' splitting of the command — cron passes the
                    # whole line to a shell, so '#' inside commands is legal
                    # and must survive round-trips intact.
                    cmd = cron_match.group("command").strip()
                    sched = cron_match.group("schedule").strip()
                    jobs.append({
                        "id": job_id,
                        "schedule": sched,
                        "command": cmd,
                        "comment": comment,
                        "enabled": enabled,
                    })
        i += 1
    return jobs


def _serialize_crontab(text: str, jobs: list[dict]) -> str:
    """Rewrite crontab text, replacing all EthOS-managed job blocks.

    Lines not managed by EthOS are kept unchanged. A managed block is the
    marker line + its cron line + (when present) the comment line directly
    above it — removing all three keeps repeated updates idempotent instead
    of duplicating comments on every save.
    """
    lines = text.split("\n")
    skip: set[int] = set()
    i = 0
    while i < len(lines):
        if _ETHOS_MARKER_RE.match(lines[i].strip()):
            marker_idx = i
            cron_idx = i + 1 if i + 1 < len(lines) else None
            comment_idx: Optional[int] = None
            j = marker_idx - 1
            while j >= 0 and lines[j].strip() == "":
                j -= 1
            if j >= 0:
                prev = lines[j].strip()
                if prev.startswith("#") and not _ETHOS_MARKER_RE.match(prev):
                    comment_idx = j
            skip.add(marker_idx)
            if cron_idx is not None:
                skip.add(cron_idx)
            if comment_idx is not None:
                skip.add(comment_idx)
            i += 2
            continue
        i += 1
    new_lines = [ln for idx, ln in enumerate(lines) if idx not in skip]

    # Append all managed jobs at the end
    for job in jobs:
        sched = job["schedule"].strip()
        cmd = job["command"].strip()
        comment = (job.get("comment") or "").strip()
        enabled = job.get("enabled", True)

        if comment:
            new_lines.append(f"# {comment}")
        new_lines.append(_ETHOS_MARKER_FMT(job["id"], enabled))
        cron_line = f"{sched} {cmd}"
        # Disabled jobs are commented out so the cron daemon skips them.
        new_lines.append(cron_line if enabled else f"# {cron_line}")

    new_lines.append("")  # trailing newline
    return "\n".join(new_lines)


# ── API endpoints ─────────────────────────────────────────────────────────────

@router.get("")
async def list_cron_jobs() -> list[dict]:
    """List all EthOS-managed cron jobs from the user crontab."""
    text = await asyncio.to_thread(_read_crontab)
    jobs = _parse_crontab(text)
    return jobs


@router.post("")
async def add_cron_job(body: CronJobCreate) -> dict:
    """Add a new cron job to the user crontab."""
    # Validate schedule format (basic check)
    if not body.schedule or not body.schedule.strip():
        raise HTTPException(status_code=400, detail="Schedule is required")
    if not body.command or not body.command.strip():
        raise HTTPException(status_code=400, detail="Command is required")

    new_job = {
        "id": str(uuid.uuid4()),
        "schedule": body.schedule.strip(),
        "command": body.command.strip(),
        "comment": body.comment.strip(),
        "enabled": body.enabled,
    }

    async with _CRON_LOCK:
        text = await asyncio.to_thread(_read_crontab)
        jobs = _parse_crontab(text)
        jobs.append(new_job)
        new_text = _serialize_crontab(text, jobs)
        if not await asyncio.to_thread(_write_crontab, new_text):
            raise HTTPException(status_code=500, detail="Failed to write crontab")

    logger.info("Added cron job %s: %s", new_job["id"], new_job["command"])
    return new_job


@router.put("/{job_id}")
async def update_cron_job(job_id: str, body: CronJobUpdate) -> dict:
    """Update an existing cron job by ID."""
    async with _CRON_LOCK:
        text = await asyncio.to_thread(_read_crontab)
        jobs = _parse_crontab(text)

        found = None
        for job in jobs:
            if job["id"] == job_id:
                found = job
                break

        if not found:
            raise HTTPException(status_code=404, detail=f"Cron job {job_id} not found")

        if body.schedule is not None:
            found["schedule"] = body.schedule.strip()
        if body.command is not None:
            found["command"] = body.command.strip()
        if body.comment is not None:
            found["comment"] = body.comment.strip()
        if body.enabled is not None:
            found["enabled"] = body.enabled

        new_text = _serialize_crontab(text, jobs)
        if not await asyncio.to_thread(_write_crontab, new_text):
            raise HTTPException(status_code=500, detail="Failed to write crontab")

    logger.info("Updated cron job %s", job_id)
    return found


@router.delete("/{job_id}")
async def delete_cron_job(job_id: str) -> dict:
    """Remove a cron job by ID."""
    async with _CRON_LOCK:
        text = await asyncio.to_thread(_read_crontab)
        jobs = _parse_crontab(text)

        before = len(jobs)
        jobs = [j for j in jobs if j["id"] != job_id]

        if len(jobs) == before:
            raise HTTPException(status_code=404, detail=f"Cron job {job_id} not found")

        new_text = _serialize_crontab(text, jobs)
        if not await asyncio.to_thread(_write_crontab, new_text):
            raise HTTPException(status_code=500, detail="Failed to write crontab")

    logger.info("Deleted cron job %s", job_id)
    return {"ok": True, "deleted": job_id}
