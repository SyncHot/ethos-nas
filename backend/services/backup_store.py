"""Backup Manager v2 — schedules, history and settings stores.

The v1 router kept everything in one 1.1k-line file. This module owns the data
model, normalization and validation so the router stays thin and the engine can
reuse the same shapes.

Stores (under `settings.DATA_DIR`):
- `backup_schedules.json` v2 — schedule definitions + last-run summary
- `backup_history.json` v2 — completed runs, newest first, capped at 200
- `backup_settings.json` v1 — retention/verify/digest defaults

All writes go through `services/json_store.py` (atomic + locked).
"""

import copy
import json
import logging
import os
import re
import shutil
import time
import uuid
from typing import Optional

from backend.config import settings
from backend.services import cron_utils, json_store

logger = logging.getLogger("ethos-nas.services.backup_store")

SCHEDULES_FILE = settings.DATA_DIR / "backup_schedules.json"
HISTORY_FILE = settings.DATA_DIR / "backup_history.json"
SETTINGS_FILE = settings.DATA_DIR / "backup_settings.json"

SCHEMA_VERSION = 2
HISTORY_LIMIT = 200
RETENTION_MAX = 365
DEFAULT_RETENTION = {"keep_last": 0, "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 6}
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
BW_RE = re.compile(r"^\d+(?:\.\d+)?[kKmMgG]?$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ── small helpers ────────────────────────────────────────────────────────────

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id() -> str:
    return str(uuid.uuid4())[:8]


def canon(path: str) -> str:
    """Absolute, symlink-resolved path used for containment checks."""
    return os.path.realpath(os.path.abspath(os.path.expanduser(path or "")))


def stored_path(path: str) -> str:
    """Absolute path as stored (symlinks kept)."""
    return os.path.abspath(os.path.expanduser(path or ""))


def is_within(child: str, parent: str) -> bool:
    """True when `child` is inside `parent` (including equality)."""
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def safe_name(name: str) -> str:
    """Filesystem-safe label used as a backup sub-directory name."""
    value = _SAFE_NAME_RE.sub("_", (name or "").strip()).strip("._")
    return value[:80] or "backup"


def norm_type(value) -> str:
    value = (value or "folder").strip().lower()
    if value in ("rsync", "sync", "folder"):
        return "folder"
    if value in ("docker-compose", "docker_compose", "compose", "docker"):
        return "docker-compose"
    return ""


def _as_int(value, default: int, lo: int = 0, hi: int = RETENTION_MAX) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def default_remote() -> dict:
    return {"enabled": False, "remote": "", "path": "", "keep_days": 30, "bwlimit": ""}


def norm_retention(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, default in DEFAULT_RETENTION.items():
        out[key] = _as_int(raw.get(key), default)
    return out


def norm_options(raw, btype: str) -> dict:
    """Coerce per-type options into a canonical shape (unknown keys dropped)."""
    raw = raw if isinstance(raw, dict) else {}
    exclude = raw.get("exclude")
    if isinstance(exclude, str):
        exclude = [p.strip() for p in exclude.split(",") if p.strip()]
    elif not isinstance(exclude, list):
        exclude = []
    base = {
        "exclude": [str(p)[:200] for p in exclude][:50],
        "exclude_defaults": bool(raw.get("exclude_defaults", True)),
        "preserve_acls": bool(raw.get("preserve_acls", True)),
        "min_free_gb": _as_int(raw.get("min_free_gb"), 5, 0, 100_000),
    }
    if btype != "docker-compose":
        return base

    projects = raw.get("projects")
    if projects in (None, "", "auto"):
        norm_projects = None
    elif isinstance(projects, str):
        norm_projects = [p.strip() for p in projects.split(",") if p.strip()]
    elif isinstance(projects, list):
        norm_projects = [str(p).strip() for p in projects if str(p).strip()]
    else:
        norm_projects = None
    if norm_projects is not None:
        norm_projects = [p[:63] for p in norm_projects][:100]

    base.update({
        "projects": norm_projects,
        "include_volumes": bool(raw.get("include_volumes", True)),
        "include_images": bool(raw.get("include_images", True)),
        "quiesce": bool(raw.get("quiesce", False)),
    })
    return base


def norm_remote(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = default_remote()
    out["enabled"] = bool(raw.get("enabled", False))
    out["remote"] = str(raw.get("remote") or "").strip()[:120]
    out["path"] = str(raw.get("path") or "").strip().strip("/")[:300]
    out["keep_days"] = _as_int(raw.get("keep_days"), 30, 1, 3650)
    out["bwlimit"] = str(raw.get("bwlimit") or "").strip()[:20]
    return out


def norm_verify(value) -> str:
    value = str(value or "quick").strip().lower()
    return value if value in ("quick", "deep", "off") else "quick"


def normalize_schedule(raw: dict) -> dict:
    """Return the canonical v2 shape without dropping unknown keys."""
    out = copy.deepcopy(raw or {})
    btype = norm_type(out.get("type"))
    out["type"] = btype or "folder"
    out["enabled"] = bool(out.get("enabled", True))
    out["cron"] = str(out.get("cron") or "").strip() or "0 2 * * *"
    out["name"] = str(out.get("name") or "").strip()[:120]
    out["source"] = stored_path(str(out.get("source") or ""))
    out["destination"] = stored_path(str(out.get("destination") or ""))
    out["options"] = norm_options(out.get("options"), out["type"])
    out["retention"] = norm_retention(out.get("retention"))
    out["verify_after_run"] = norm_verify(out.get("verify_after_run"))
    out["skip_if_missing"] = bool(out.get("skip_if_missing", True))
    out["remote"] = norm_remote(out.get("remote"))
    out.setdefault("created_at", now_iso())
    return out


def backup_target(schedule: dict) -> str:
    """Parent folder that holds a schedule's snapshots."""
    destination = canon(schedule.get("destination") or "")
    if norm_type(schedule.get("type")) == "docker-compose":
        base = os.path.basename((schedule.get("source") or "").rstrip("/")) or "projects"
        return os.path.join(destination, "docker", safe_name(base))
    source = (schedule.get("source") or "").rstrip("/")
    base = os.path.basename(source)
    return os.path.join(destination, base) if base else destination


# ── validation ───────────────────────────────────────────────────────────────

def validate_schedule(schedule: dict) -> Optional[str]:
    """Return a human-readable error when the schedule is unusable."""
    btype = norm_type(schedule.get("type"))
    if not btype:
        return "Backup type must be folder or docker-compose"

    destination_raw = str(schedule.get("destination") or "").strip()
    if not destination_raw:
        return "Destination path must be a folder"
    destination = canon(destination_raw)
    if destination == "/":
        return "Destination path must be a folder"

    source_raw = str(schedule.get("source") or "").strip()
    source = canon(source_raw)
    label = "Source path" if btype == "folder" else "Compose folder"
    if not source_raw or source == "/":
        return f"{label} must be set"
    if not os.path.exists(source):
        return f"{label} not found: {source}"
    if not os.path.isdir(source):
        return f"{label} is not a directory: {source}"

    if os.path.exists(destination) and not os.path.isdir(destination):
        return f"Destination exists and is not a folder: {destination}"
    parent = os.path.dirname(destination)
    if not os.path.isdir(parent):
        return f"Destination parent does not exist: {parent}"

    if destination == source:
        return "Source and destination must be different folders"
    if is_within(destination, source):
        return "Destination cannot be inside the source folder"
    if is_within(source, destination):
        return "Source cannot be inside the destination folder"

    if not cron_utils.is_valid(str(schedule.get("cron") or "")):
        return "Schedule must be a valid 5-field cron expression or @macro"

    retention = norm_retention(schedule.get("retention"))
    if sum(retention.values()) <= 0:
        return "Retention must keep at least one generation"

    options = norm_options(schedule.get("options"), btype)
    if btype == "docker-compose" and options.get("projects"):
        bad = [p for p in options["projects"] if not PROJECT_RE.match(p)]
        if bad:
            return f"Invalid compose project name: {bad[0]}"

    remote = norm_remote(schedule.get("remote"))
    if remote["enabled"]:
        if not remote["remote"] or not REMOTE_RE.match(remote["remote"]):
            return "Remote target name is invalid"
        if not remote["path"] or ".." in remote["path"].split("/"):
            return "Remote path is invalid"
        if remote["bwlimit"] and not BW_RE.match(remote["bwlimit"]):
            return "Remote bandwidth limit is invalid (e.g. 10M)"
    return None


def schedule_conflict(draft: dict, schedules: list[dict],
                      exclude_id: Optional[str] = None) -> Optional[str]:
    """Reject two schedules that write to the same target folder."""
    target = os.path.realpath(backup_target(draft))
    for other in schedules:
        if exclude_id and other.get("id") == exclude_id:
            continue
        if os.path.realpath(backup_target(other)) == target:
            return f'Backup target already used by "{other.get("name", "?")}" ({target})'
    return None


# ── stores ───────────────────────────────────────────────────────────────────

def _migrate_schedules(from_version: int, to_version: int, data: dict) -> dict:
    schedules = []
    for raw in (data.get("schedules") or []):
        if not isinstance(raw, dict):
            continue
        schedules.append(normalize_schedule(raw))
    logger.info("Backup schedules migrated v%s→v%s (%d entries)",
                from_version, to_version, len(schedules))
    return {"schedules": schedules}


def _migrate_history(from_version: int, to_version: int, data: dict) -> dict:
    history = []
    for raw in (data.get("history") or [])[:HISTORY_LIMIT]:
        if not isinstance(raw, dict):
            continue
        entry = dict(raw)
        entry.setdefault("trigger", "manual")
        entry.setdefault("generation", None)
        entry.setdefault("bytes_copied", 0)
        entry.setdefault("files_total", 0)
        entry.setdefault("remote_status", "")
        history.append(entry)
    logger.info("Backup history migrated v%s→v%s (%d entries)",
                from_version, to_version, len(history))
    return {"history": history}


_SCHEDULES_STORE = json_store.get(
    SCHEDULES_FILE, lambda: {"schedules": []}, version=SCHEMA_VERSION, migrate=_migrate_schedules)
_HISTORY_STORE = json_store.get(
    HISTORY_FILE, lambda: {"history": []}, version=SCHEMA_VERSION, migrate=_migrate_history)
_SETTINGS_STORE = json_store.get(SETTINGS_FILE, lambda: {
    "default_retention": copy.deepcopy(DEFAULT_RETENTION),
    "verify_after_run": "quick",
    "digest_enabled": True,
    "digest_weekday": 0,
    "digest_hour": 8,
    "last_digest_date": "",
}, version=1)


_v2_persisted = False


def _persist_legacy_store() -> None:
    """Write the v2 shape when the v1 file predates the `version` field.

    `json_store` only runs a migrator when the file carries a numeric version;
    the v1 backup store was written without one, so its schedules stayed v1 on
    disk (and lost the new defaults after a restart). Rewrite once, keeping a
    timestamped copy of the original.
    """
    global _v2_persisted
    if _v2_persisted:
        return
    _v2_persisted = True
    path = _SCHEDULES_STORE.path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(raw, dict) or "version" in raw:
        return
    schedules = [normalize_schedule(s) for s in (raw.get("schedules") or [])
                 if isinstance(s, dict)]
    backup = path.with_name(f"{path.stem}.v1-{int(time.time())}{path.suffix}")
    try:
        shutil.copy2(path, backup)
    except OSError:
        logger.warning("Could not keep a copy of %s before migration", path, exc_info=True)
    _SCHEDULES_STORE.save({"schedules": schedules})
    logger.info("Backup schedules store upgraded to v2 (%d entries, backup: %s)",
                len(schedules), backup.name)


def load_schedules() -> list[dict]:
    _persist_legacy_store()
    data = _SCHEDULES_STORE.load()
    return [normalize_schedule(s) for s in (data.get("schedules") or []) if isinstance(s, dict)]


def find_schedule(schedule_id: str) -> Optional[dict]:
    for schedule in load_schedules():
        if schedule.get("id") == schedule_id:
            return schedule
    return None


def add_schedule(schedule: dict) -> dict:
    entry = normalize_schedule(schedule)
    entry["id"] = entry.get("id") or new_id()

    def _add(data: dict) -> dict:
        data.setdefault("schedules", []).append(entry)
        return entry

    return _SCHEDULES_STORE.update(_add)


def replace_schedule(schedule_id: str, updates: dict) -> Optional[dict]:
    def _apply(data: dict) -> Optional[dict]:
        for index, current in enumerate(data.get("schedules") or []):
            if current.get("id") != schedule_id:
                continue
            merged = normalize_schedule({**current, **updates})
            merged["id"] = schedule_id
            data["schedules"][index] = merged
            return merged
        return None

    return _SCHEDULES_STORE.update(_apply)


def patch_schedule(schedule_id: str, fields: dict) -> None:
    """Update last-run bookkeeping without a full re-validation."""
    def _apply(data: dict) -> None:
        for current in data.get("schedules") or []:
            if current.get("id") == schedule_id:
                current.update(fields)
                return

    _SCHEDULES_STORE.update(_apply)


def delete_schedule(schedule_id: str) -> bool:
    def _remove(data: dict) -> bool:
        before = len(data.get("schedules") or [])
        data["schedules"] = [s for s in (data.get("schedules") or [])
                             if s.get("id") != schedule_id]
        return len(data["schedules"]) != before

    return bool(_SCHEDULES_STORE.update(_remove))


def record_history(entry: dict) -> None:
    def _add(data: dict) -> None:
        data.setdefault("history", []).insert(0, entry)
        data["history"] = data["history"][:HISTORY_LIMIT]

    _HISTORY_STORE.update(_add)


def load_history() -> list[dict]:
    return list(_HISTORY_STORE.load().get("history") or [])


def history_page(limit: int = 50, offset: int = 0) -> dict:
    items = load_history()
    total = len(items)
    offset = max(0, offset)
    page = items[offset:] if limit <= 0 else items[offset:offset + limit]
    return {"items": page, "total": total, "limit": limit, "offset": offset}


def get_settings() -> dict:
    return _SETTINGS_STORE.load()


def save_settings(updates: dict) -> dict:
    def _apply(data: dict) -> dict:
        if "default_retention" in updates:
            data["default_retention"] = norm_retention(updates["default_retention"])
        if "verify_after_run" in updates:
            data["verify_after_run"] = norm_verify(updates["verify_after_run"])
        if "digest_enabled" in updates:
            data["digest_enabled"] = bool(updates["digest_enabled"])
        if "digest_weekday" in updates:
            data["digest_weekday"] = _as_int(updates["digest_weekday"], 0, 0, 6)
        if "digest_hour" in updates:
            data["digest_hour"] = _as_int(updates["digest_hour"], 8, 0, 23)
        if "last_digest_date" in updates:
            data["last_digest_date"] = str(updates["last_digest_date"] or "")[:10]
        return data

    return _SETTINGS_STORE.update(_apply)


def public_schedule(schedule: dict, with_next: bool = True) -> dict:
    """Schedule shape returned to the UI (adds next_run and target path)."""
    out = normalize_schedule(schedule)
    out["id"] = schedule.get("id")
    if with_next and out.get("enabled") is not False:
        try:
            out["next_run"] = cron_utils.next_run(out.get("cron", ""))
        except Exception:
            logger.debug("Could not compute next_run for %s", out.get("id"), exc_info=True)
            out["next_run"] = None
    else:
        out["next_run"] = None
    out["target_path"] = backup_target(out)
    return out
