"""EthOS NAS — Downloads router (queued HTTP/magnet/debrid downloads).

A self-contained, dependency-free download engine (stdlib `urllib`, run in
threads so the event loop stays free). Supports:
- concurrent downloads (configurable, default 3)
- pause / resume (HTTP `Range` resume — partial file is kept)
- cancel
- live progress + speed (bytes/s, EMA-smoothed)
- persistent task list (survives restarts; in-flight tasks re-queue)
- debrid providers: Real-Debrid, AllDebrid, TorBox (magnet/torrent → cached
  on the provider, then streamed to the NAS download folder via API links)
- .torrent file upload (parsed locally with a stdlib bencode decoder → magnet)
- watch folders: one or more named directories can be watched; new *.torrent
  files dropped in each are parsed and queued, downloading to that folder's own
  destination path (into the watched folder itself when none is set)

Data files live in backend/data/ alongside the other routers' state:
- downloads_config.json   {download_dir, max_concurrent, auto_download_debrid,
                           watch_folders:[{name,folder,destination}],
                           watch_enabled, debrid:{...}}
- downloads_tasks.json    [ {task...}, ... ]
"""

# ── Module map — grep a section name, then read only that slice ─────────────
# State / Persistence   downloads_config.json, downloads_tasks.json, throttles
# Helpers               filename/path safety, speed EMA, formatting
# Debrid providers      Real-Debrid, AllDebrid, TorBox + provider dispatch
# .torrent parsing      stdlib bencode decoder → magnet
# Debrid engine         magnet → provider cache → stream to NAS (3 phases)
# Engine                built-in HTTP(S) downloader, pause/resume/cancel
# Watch folders         directory watchers, *.torrent ingest
# Endpoints             /api/downloads/*, qBittorrent-compatible API

import asyncio
import concurrent.futures
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from backend.config import settings
from backend.routers.auth import require_admin
from backend.services import path_scope, pressure_guard, user_data
from backend.services import download_rss
from backend.services.bencode import parse_magnet as _parse_magnet
from backend.services.bencode import parse_torrent as _parse_torrent
from backend.services.download_transfer import check_disk_space as _check_disk_space
from backend.services.download_transfer import finalize_part as _finalize_part
from backend.services.download_transfer import part_path as _part_path
from backend.services.download_transfer import resume_source as _resume_source
from backend.services.download_transfer import stream_to_file as _stream_to_file
from backend.services.download_transfer import forget_shared_limiter as _forget_shared_limiter

logger = logging.getLogger("ethos-nas.routers.downloads")

# `scope_request` publishes the caller's role for path_scope.sanitize(). Every
# destination this router accepts (download_dir, per-task folder, watch folders)
# runs through sanitize(), so a non-admin cannot write outside /home, /mnt and
# /media. Admins keep full access, matching the files/file_ops routers.
router = APIRouter(dependencies=[Depends(path_scope.scope_request)])

# Task and RSS-seen stores still live here; config I/O is in
# backend/services/download_config.py (TODO.md P2-01).
from backend.services import download_tasks
from backend.services import json_store
from backend.services.download_config import (
    category_target as _category_target,
    default_debrid as _default_debrid,
    load_config as _load_config,
    norm_categories as _norm_categories,
    norm_rss_feeds as _norm_rss_feeds,
    norm_watch_entry as _norm_watch_entry,
    parse_hhmm as _parse_hhmm,
    qb_get_api_key as _qb_get_api_key,
    remove_category as _remove_category,
    save_config as _save_config,
    upsert_category as _upsert_category,
)
from backend.services import download_providers as _providers
from backend.services import download_engine
from backend.services import download_retry as _retry_policy
from backend.services import download_schedule as _schedule_policy
from backend.services import download_urls
from backend.services import watch_folders

DATA_DIR = settings.DATA_DIR
TASKS_FILE = DATA_DIR / "downloads_tasks.json"
_TASKS_STORE = json_store.get(TASKS_FILE)  # list payload — versions apply to dicts


class AddTaskRequest(BaseModel):
    """Body of `POST /api/downloads/tasks` (TODO.md P1-09).

    A single URL of any supported kind (http/https, magnet, premium hoster) plus
    an optional display name and destination folder. `extra="forbid"` turns a
    typo'd field into a 422 instead of a silently ignored key.
    """

    model_config = {"extra": "forbid"}

    url: str = Field(min_length=1, max_length=8192, description="http(s), magnet: or premium hoster link")
    name: str | None = Field(default=None, max_length=255)
    folder: str | None = Field(default=None, max_length=4096,
                               description="Absolute path or a subpath of the download dir")
    category: str | None = Field(default=None, max_length=64,
                                 description="Category name; its save_path becomes the destination")
    mirrors: list[str] | None = Field(default=None, max_length=10,
                                      description="Alternative URLs tried in order when the source fails")


class AddMirrorsRequest(BaseModel):
    """Body of `POST /api/downloads/tasks/{id}/mirrors`."""

    model_config = {"extra": "forbid"}

    urls: list[str] = Field(min_length=1, max_length=10)


class PackageLimitRequest(BaseModel):
    """Body of `POST /api/downloads/packages/{key}/limit` (0 clears the limit)."""

    model_config = {"extra": "forbid"}

    bps: int = Field(ge=0, le=10_000_000_000, description="Shared bytes/s for the whole package")


class SelectFilesRequest(BaseModel):
    """Body of `POST /api/downloads/tasks/{id}/files/select`."""

    model_config = {"extra": "forbid"}

    file_ids: list[int | str] = Field(min_length=1, max_length=2000)


class VerifyDebridRequest(BaseModel):
    """Body of `POST /api/downloads/verify` — provider credential check.

    `key` is the legacy single-secret form; `credentials` carries any provider's
    declared fields (tb7.pl needs `tb7_username` + `tb7_password`). A masked
    value ("****…") verifies the stored credential instead of the sent one.
    """

    model_config = {"extra": "forbid"}

    provider: str = Field(min_length=1, max_length=32)
    key: str = Field(default="", max_length=512)
    credentials: dict = Field(default_factory=dict)


class PriorityRequest(BaseModel):
    """Body of `POST /api/downloads/tasks/{id}/priority` (higher runs first)."""

    model_config = {"extra": "forbid"}

    priority: int = Field(ge=-1000, le=1000)


class ReorderRequest(BaseModel):
    """Body of `POST /api/downloads/tasks/reorder` — waiting tasks, first first."""

    model_config = {"extra": "forbid"}

    order: list[str] = Field(min_length=1, max_length=500)


class SaveConfigRequest(BaseModel):
    """Body of `POST /api/downloads/config` — deliberately partial.

    Every key is independent and only the keys present are applied (the handler
    merges over the stored config); `extra="allow"` keeps forward compatibility
    with newer clients, while the known keys are typed so `/docs` is accurate.
    Range clamping stays in the handler (a value outside the allowed band is
    clamped, not rejected).
    """

    model_config = {"extra": "allow"}

    # Typed loosely on purpose: the handler clamps numbers and answers 400 for a
    # non-numeric value, so a string must reach it instead of failing at 422.
    max_concurrent: int | str | None = None
    download_dir: str | None = Field(default=None, max_length=4096)
    auto_download_debrid: bool | None = None
    auto_retry: bool | None = None
    retry_max: int | None = None
    retry_delay: int | None = None
    duplicate_policy: str | None = None
    auto_extract: bool | None = None
    auto_extract_delete: bool | None = None
    watch_folders: list | None = None
    watch_enabled: bool | None = None
    schedule: dict | None = None
    speed_limit: dict | None = None
    categories: list | None = None
    rss_feeds: list | None = None
    debrid: dict | None = None


ALLOWED_SCHEMES = download_urls.ALLOWED_SCHEMES


def _is_file_host(url: str) -> bool:
    """True when the URL's host is a known premium file hoster."""
    return download_urls.is_file_host(url)


# ── State ─────────────────────────────────────────────────────────────────────
_tasks: List[dict] = []            # ordered by created_at ascending
_active: dict = {}                 # task id -> asyncio.Task (running worker)
_events: dict = {}                 # task id -> threading.Event (pause/cancel signal)
_pump_lock = asyncio.Lock()
_save_lock = threading.Lock()      # guard JSON persistence (threads + loop)
# Dedicated pool for long-lived download/debrid workers. The loop's default
# executor is small (cpu+4) and shared with asyncio.to_thread — debrid tasks
# hold a thread for hours while polling, which would starve short calls.
_worker_pool = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="dl-worker")


# ── Persistence ───────────────────────────────────────────────────────────────
# Config I/O lives in backend/services/download_config.py and task serialization
# in backend/services/download_tasks.py; the wrappers below keep the in-memory
# task list the single source of truth for workers and endpoints.


def _write_tasks_file() -> None:
    download_tasks.write(_tasks, _TASKS_STORE)


def _save_tasks() -> None:
    download_tasks.save(_tasks, _save_lock, _TASKS_STORE)


def _load_tasks() -> None:
    global _tasks
    _tasks = download_tasks.load(_TASKS_STORE)
    # Tasks created before the per-user split belong to the admin.
    legacy_owner = user_data.admin_username()
    for t in _tasks:
        if not t.get("owner"):
            t["owner"] = legacy_owner


_load_tasks()


def _owner() -> str:
    """Owner for a newly created task."""
    return user_data.current_username()


def _owned(task: dict) -> bool:
    """True when the caller may see or mutate this task."""
    username, role = path_scope.current_identity()
    if role == "admin":
        return True
    return str(task.get("owner") or "") == username


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _uid() -> str:
    return "dl_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "_" + os.urandom(3).hex()


def _name_from_url(url: str) -> str:
    return download_urls.name_from_url(url)


# Part-file helpers and the transfer engine live in
# backend/services/download_transfer.py (TODO.md P2-01), imported above.


def _unique_dest(download_dir: str, name: str) -> str:
    return download_urls.unique_dest(download_dir, name)


def _validate_url(url: str) -> str:
    try:
        return download_urls.validate_url(url, ALLOWED_SCHEMES)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _is_admin(request: Request | None = None) -> bool:
    """True when the caller is an admin (or when no request context exists)."""
    if request is not None:
        user = getattr(request.state, "current_user", None) or {}
        if user:
            return str(user.get("role") or "user") == "admin"
    return path_scope.CURRENT_ROLE.get() == "admin"


def _reject_private_url(url: str) -> None:
    """Non-admins must not turn the NAS into an internal-network proxy.

    Loopback/RFC1918/link-local targets are blocked on resolution (no redirect
    or DNS-rebinding protection beyond this check, which matches the threat
    model of the other user-facing features). Admins keep full access so a LAN
    media server can still be used as a source.
    """
    if _is_admin():
        return
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot resolve download host: {exc}") from exc
    for info in infos:
        if download_urls.is_blocked_ip(info[4][0]):
            raise HTTPException(
                status_code=403,
                detail="Downloads from private, loopback or link-local addresses require an admin account",
            )


def _safe_join(base: str, *parts: str) -> str:
    """Join provider-supplied relative paths under `base`, rejecting escapes.

    Torrent metadata (`info.files[].path`) and provider file lists are
    attacker-controlled. `os.path.join(base, "../../etc/cron.d/x")` must never
    leave the task's content directory.
    """
    return download_urls.safe_join(base, *parts)


# ── Debrid provider API layer (Real-Debrid / AllDebrid / TorBox) ─────────────

# ── Debrid provider clients (extracted — TODO.md P2-01) ─────────────────────
# Real-Debrid / AllDebrid / TorBox clients, the shared retrying HTTP helper and
# the provider dispatch live in backend/services/debrid.py. The underscore
# aliases keep every existing call site in this module unchanged.
from backend.services.debrid import (
    add_magnet as _debrid_add_magnet,
    torrent_status as _debrid_torrent_status,
    select_files as _debrid_select_files,
    download_link as _debrid_download_link,
    delete_torrent as _debrid_delete_torrent,
    ProviderHTTPError as _ProviderHTTPError,
    ad_unlock as _ad_unlock,
    ad_magnet_files as _ad_magnet_files,
)


# ── .torrent parsing (stdlib bencode decoder — no external deps) ─────────────
# Implemented in backend/services/bencode.py (TODO.md P2-01) and imported above
# as `_parse_magnet` / `_parse_torrent`; the qBittorrent shim uses it too.


# Provider clients are imported above from backend/services/debrid.py.


# ── Debrid engine (magnet → provider cache → stream to NAS) ─────────────────

DEBRID_POLL_INTERVAL = 5.0      # seconds between provider status polls
DEBRID_MAX_WAIT = 6 * 3600      # give up after this long in waiting_files_selection


def _debrid_worker(task: dict, stop_event: threading.Event) -> None:
    """Blocking debrid lifecycle for one task (runs in a worker thread).

    Phases: add magnet → poll status → (waiting_files_selection until the user
    selects files or auto_download_debrid kicks in) → completed → stream every
    selected file to the NAS download folder. Updates `task` in place; the
    caller owns final status on stop_event.
    """
    with path_scope.system_scope():
        _debrid_worker_scoped(task, stop_event)


def _debrid_worker_scoped(task: dict, stop_event: threading.Event) -> None:
    cfg = _load_config()
    provider = str(task.get("provider") or "")
    spec = _providers.get(provider)
    if spec is None:
        raise RuntimeError(f"Unknown download provider: {provider!r}")
    # Credentials live in the config (engine fields); a legacy task may still
    # carry only its single `_debrid_key`, which is used as a fallback.
    creds = _providers.provider_credentials(cfg, provider)
    if not _providers.credentials_complete(provider, creds):
        creds = _providers.credentials_from_secret(provider, str(task.get("_debrid_key") or "")) or creds
    key = _providers.primary_secret(provider, creds)
    tid = int(task.get("_debrid_id") or 0)
    is_link = task.get("kind") == "debrid_link"
    # A link task may still resolve through another configured link provider,
    # so its own credentials are only required for a magnet/torrent task.
    if not is_link and not _providers.credentials_complete(provider, creds):
        raise RuntimeError(f"{spec.label} credentials are no longer configured (set them in Settings)")

    # ── Phase 1: resolve premium link / add the magnet ─────────────────────
    if is_link:
        # File-hosting link (RapidGator, Mega, tb7.pl-supported hosts, …):
        # no torrent — a link provider resolves it to a direct download URL.
        # The task's own provider is tried first (when still configured), then
        # every other configured provider that supports the host, so a second
        # account (e.g. AllDebrid next to tb7.pl) takes over when the first one
        # cannot unlock the link.
        source = _source_url(task)
        pool = []
        if provider and _providers.credentials_complete(provider, creds):
            pool.append((provider, creds))
        for pid, pcreds in _providers.link_candidates(cfg, source):
            if pid not in [p for p, _c in pool]:
                pool.append((pid, pcreds))
        link = None
        errors = []
        for pid, pcreds in pool:
            pspec = _providers.get(pid)
            label = pspec.label if pspec else pid
            try:
                if pid != provider:
                    logger.info("Download %s: unlocking the link with %s", task["id"], label)
                candidate = _providers.resolve_link(pid, pcreds, source, stop_event)
            except Exception as exc:  # noqa: BLE001 — surface it and try the next provider
                errors.append(f"{label}: {exc}")
                logger.warning("Download %s: %s could not unlock the link: %s",
                               task["id"], label, exc)
                if stop_event.is_set():
                    return  # paused/canceled mid-unlock
                continue
            if str(candidate.get("url") or ""):
                link = candidate
                provider, creds = pid, pcreds
                task["provider"] = pid
                key = _providers.primary_secret(pid, pcreds)
                task["_debrid_key"] = key
                break
            errors.append(f"{label}: no direct link returned")
            if stop_event.is_set():
                return
        if link is None:
            if not pool:
                raise RuntimeError(
                    "No link provider with credentials can unlock this host — "
                    "configure one in Settings → Providers")
            raise RuntimeError("; ".join(errors) or "No link provider could unlock this URL")
        if link.get("name"):
            task["name"] = os.path.basename(str(link["name"]).replace("\\", "/")) or task["name"]
        if link.get("size"):
            task["size"] = int(link["size"])
        fname = os.path.basename(task["name"].replace("\\", "/")) or "download.bin"
        # Per-task folder (set at creation) wins over the global default dir.
        base_dir = os.path.abspath(task.get("path") or cfg["download_dir"])
        dest = os.path.abspath(os.path.join(base_dir, fname))
        task["_files"] = [{
            "id": 0,
            "name": fname,
            "size": int(link.get("size") or 0),
            "path": "",
            "is_selected": True,
            "dest": dest,
            "_link_url": str(link.get("url") or ""),   # resolved premium URL — phase 3 uses it directly
            "_link_url_headers": dict(link.get("headers") or {}),  # transient: stripped before persisting
        }]
        task["_debrid_status"] = "completed"
    else:
        # Magnet task: add it to the provider (only if not already added).
        if not tid:
            tid = _debrid_add_magnet(provider, key, task["url"])
            task["_debrid_id"] = tid
            _save_tasks()

        # ── Phase 2: poll until the torrent is cached (or files need selection) ─
        wait_since = None
        while not stop_event.is_set():
            st = _debrid_torrent_status(provider, key, tid)
            task["_debrid_status"] = st["status"]
            task["_debrid_progress"] = int(st.get("progress") or 0)
            # Surface provider-side transfer bytes/speed (AllDebrid reports them
            # while the magnet is being cached) so the UI shows live progress.
            if st.get("downloaded") is not None:
                task["downloaded"] = int(st["downloaded"])
            if st.get("speed") is not None:
                task["speed"] = int(st["speed"])
            if st.get("name"):
                task["name"] = st["name"]
            if int(st.get("size") or 0) > 0:
                task["size"] = max(int(task.get("size") or 0), int(st["size"]))
            files = st.get("files") or []
            if files:
                # Merge provider file list into the task (keep per-file NAS state).
                old = {f.get("id"): f for f in (task.get("_files") or [])}
                merged = []
                for f in files:
                    prev = old.get(f["id"]) or {}
                    merged.append({**prev, **f})
                task["_files"] = merged
            _save_tasks()

            status = st["status"]
            if status == "waiting_files_selection":
                wait_since = wait_since or _now()
                auto = bool(cfg.get("auto_download_debrid")) and provider != "torbox"
                if auto:
                    # Auto-select everything (largest file first is not needed — all).
                    ids = [f["id"] for f in files]
                    if ids:
                        _debrid_select_files(provider, key, tid, ids)
                        wait_since = None
                    # Fall through to the poll sleep below. Re-querying
                    # immediately (the old `continue`) hammered the provider
                    # when its status had not flipped yet and could trip 429s.
                if _now() - wait_since > DEBRID_MAX_WAIT:
                    raise RuntimeError("Timed out waiting for file selection on the debrid provider")
            elif status in ("completed", "cached", "downloaded"):
                # "downloaded" is Real-Debrid's final state; without it an RD
                # task polled forever instead of starting phase 3.
                if provider == "alldebrid" and not task.get("_files"):
                    # AD: per-file list + links come from the dedicated endpoint.
                    tree = _ad_magnet_files(key, tid)
                    auto = bool(cfg.get("auto_download_debrid"))
                    for f in tree:
                        f["is_selected"] = auto  # AD caches everything; NAS pulls selected only
                    task["_files"] = tree
                    if not auto:
                        # Wait until the user picks files in the UI (or times out).
                        wait_since = _now()
                        while not stop_event.is_set():
                            if any(f.get("is_selected") for f in task.get("_files") or []):
                                break
                            if _now() - wait_since > DEBRID_MAX_WAIT:
                                raise RuntimeError("Timed out waiting for file selection")
                            time.sleep(DEBRID_POLL_INTERVAL)
                        if stop_event.is_set():
                            return
                    _save_tasks()
                break
            elif status in ("error", "dead", "virus"):
                detail = st.get("detail") or ""
                if status == "virus":
                    detail = "flagged as virus by the provider"
                raise RuntimeError(f"Debrid torrent failed: {status}" + (f" — {detail}" if detail else ""))
            else:
                wait_since = None  # magnet_upload / queued / downloading / metaDL…

            time.sleep(DEBRID_POLL_INTERVAL)

    if stop_event.is_set():
        return  # paused/canceled — caller owns final status

    # ── Phase 3: stream every selected file to the NAS ─────────────────────
    # Progress now tracks the local transfer (starting from the bytes already on
    # disk) instead of the provider-side cache state, which is already at 100%.
    cfg = _load_config()
    # Per-task destination wins over the global default dir. Magnet tasks store
    # <target_dir>/<torrent-name> in task["path"] (set from the watch-folder
    # entry's destination or the task folder at creation); debrid_link tasks
    # already carry per-file dests from phase 1.
    raw_path = str(task.get("path") or "").strip()
    base_dir = os.path.dirname(os.path.abspath(raw_path)) if raw_path else os.path.abspath(cfg["download_dir"])
    total_size = sum(int(f.get("size") or 0) for f in task["_files"] if f.get("is_selected"))
    if total_size > 0:
        task["size"] = total_size
    task["_debrid_status"] = "downloading_to_nas"

    # Live byte-level progress across all files (skipped/already-done count as done).
    done_bytes = sum(int(f.get("size") or 0) for f in task["_files"]
                     if f.get("is_selected") and f.get("downloaded"))
    task["downloaded"] = done_bytes
    # Restart the bar at the local transfer position (was 100% on the provider side).
    task["_debrid_progress"] = int(done_bytes * 100 / total_size) if total_size > 0 else 0

    def _tick_progress(extra: int = 0) -> None:
        nonlocal done_bytes
        done_bytes += extra
        task["downloaded"] = done_bytes
        if total_size > 0:
            task["_debrid_progress"] = min(99, int(done_bytes * 100 / total_size))

    for f in list(task.get("_files") or []):
        if stop_event.is_set():
            return
        if not f.get("is_selected"):
            continue
        fid = f["id"]
        fname = os.path.basename(str(f.get("name") or "")) or "download.bin"
        rel = str(f.get("path") or "").strip("/") or fname
        # AllDebrid's tree paths include the torrent's root folder, and we are
        # already creating <base>/<task.name>/ — strip a leading duplicate of
        # it so files don't end up double-nested.
        root = os.path.basename(str(task["name"]).replace("\\", "/")).strip("/")
        if root and (rel == root or rel.startswith(root + "/")):
            rel = rel[len(root):].lstrip("/") or fname
        # Preserve the torrent's folder layout under <dest>/<torrent>/….
        # Provider-supplied paths are untrusted: `..`, absolute paths and
        # symlink escapes are rejected instead of writing outside the task dir.
        if f.get("dest"):
            dest = f["dest"]
        else:
            try:
                dest = _safe_join(os.path.join(base_dir, task["name"]), rel)
            except ValueError as exc:
                raise RuntimeError(f"Refusing unsafe file path from provider: {rel[:200]}") from exc
        f["dest"] = dest

        # Resume after pause/restart: skip files already fully on disk.
        if f.get("downloaded") and os.path.exists(dest) and os.path.getsize(dest) > 0:
            continue

        # Premium file-hosting links carry their resolved URL; magnets fetch it from the provider.
        url = str(f.get("_link_url") or "")
        if not url and provider == "torbox":
            link = _debrid_download_link(provider, key, fid, tid)
            url = link.get("url") or ""
            if link.get("name"):
                fname = os.path.basename(link["name"]) or fname
        elif provider == "realdebrid" and not url:
            raise RuntimeError(
                f"Real-Debrid returned no download link for {fname} — select the file again at the provider"
            )
        elif provider == "alldebrid" and url and "alldebrid.com/f/" in url:
            # /magnet/files links are alldebrid.com/f/… pages — unlock to a direct
            # URL. debrid_link tasks already carry a resolved direct URL (phase 1)
            # and must NOT be re-unlocked.
            unlocked = _ad_unlock(key, url, stop_event)
            if not unlocked.get("url"):
                if stop_event.is_set():
                    return
                raise RuntimeError(f"AllDebrid returned no download link for file {fid}")
            url = unlocked["url"]
        if not url:
            raise RuntimeError(f"Provider returned no download link for file {fid}")

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # Resume a partial file left behind by an earlier stalled/failed run.
        expected = int(f.get("size") or 0)
        resume = _resume_source(dest)
        if expected and resume >= expected:
            _finalize_part(dest)
            f["downloaded"] = True  # already complete on disk
            continue
        counted = resume  # bytes credited to progress for this file so far
        if resume > 0:
            _tick_progress(resume)  # count the bytes already on disk

        last_pos = resume
        last_bytes = resume
        last_t = _now()
        ema = 0.0

        def _on_open(status, content_length, headers, start_pos):
            nonlocal last_pos, last_bytes, last_t, ema
            total = (start_pos + content_length) if (status == 206 and start_pos > 0) else content_length
            if total > 0 and not f.get("size"):
                f["size"] = total
            last_pos = start_pos
            last_bytes = start_pos
            last_t = _now()
            ema = 0.0

        def _on_chunk(pos):
            nonlocal last_pos, last_bytes, last_t, ema, counted
            if pos < last_pos:
                # Server ignored Range and restarted from zero — uncount the
                # bytes already credited for this file.
                _tick_progress(-counted)
                counted = 0
                last_pos = 0
            delta = pos - last_pos
            last_pos = pos
            counted += delta
            _tick_progress(delta)
            now = _now()
            dt = now - last_t
            if dt >= 0.25:
                rate = (pos - last_bytes) / dt
                ema = rate if ema == 0.0 else (rate * 0.35 + ema * 0.65)
                task["speed"] = int(ema)
                last_bytes = pos
                last_t = now

        holder = {"dest": dest}
        cfg_now = _load_config()
        limits = cfg_now.get("speed_limit") or {}
        pkg_key, pkg_limit = _package_limit(task, cfg_now)
        final_pos = _stream_to_file(
            url, holder, resume, stop_event, _on_chunk, _on_open,
            limit_bps=pkg_limit or int(limits.get("per_task_bps") or 0),
            global_bps=int(limits.get("global_bps") or 0),
            limit_key=f"pkg:{pkg_key}" if pkg_limit else "",
            headers=f.get("_link_url_headers") or None,
        )
        # Only mark complete when the transfer actually finished — a pause
        # mid-file must leave it resumable instead of corrupting on re-scan.
        f["downloaded"] = bool(expected and final_pos >= expected) or (not expected and final_pos > 0)

    task["_debrid_progress"] = 100
    task["speed"] = 0


def _finalize_debrid(t: dict, worker: "asyncio.Task") -> None:
    """Set the final status of a debrid task once its worker coroutine exits."""
    try:
        exc = worker.exception()
    except BaseException:
        return  # task was canceled — nothing to finalize
    # "waiting" = re-queued by a reset while this worker was still draining —
    # do not overwrite the fresh state with an error.
    if t["status"] in ("paused", "canceled", "waiting"):
        _save_tasks()
        return
    if exc is not None:
        logger.error("Debrid task %s failed: %s", t["id"], exc, exc_info=True)
        _fail_task(t, str(exc))
        return
    selected = [f for f in (t.get("_files") or []) if f.get("is_selected")]
    if selected and all(f.get("downloaded") for f in selected):
        _set_status(t, "completed")
    else:
        _fail_task(t, "Debrid transfer ended before all files finished")


async def _run_debrid(task: dict, stop_event: threading.Event) -> None:
    """Wrap the blocking debrid worker in an executor (mirrors _run_download)."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_worker_pool, _debrid_worker, task, stop_event)


# ── Download packages (multi-part archives) ───────────────────────────────────
# A course release is usually split into `.part01.rar` … `.partNN.rar` (or
# `.r00` / `.zip.001` volumes). JDownloader-style grouping keeps those parts in
# one row: the fields are derived from the display name and destination on
# every read, so old task records group exactly like new ones and nothing is
# migrated or duplicated in `downloads_tasks.json`.
_PART_PATTERNS = (
    re.compile(r"(?i)^(?P<base>.+?)\.part0*(?P<part>\d{1,4})\.(?:rar|zip|7z)$"),
    re.compile(r"(?i)^(?P<base>.+?)\.r(?P<part>\d{2})$"),
    re.compile(r"(?i)^(?P<base>.+?)\.z(?P<part>\d{2})$"),
    re.compile(r"(?i)^(?P<base>.+?)\.(?P<part>\d{3})$"),
)


def _package_info(task: dict) -> Optional[dict]:
    """Package membership for a multi-part archive, or None for a plain file."""
    name = os.path.basename(str(task.get("name") or "").replace("\\", "/")).strip()
    # Aggregator links are file-host landing pages (`…part02.rar.html`).
    name = re.sub(r"(?i)\.html?$", "", name).strip()
    if not name:
        return None
    for pattern in _PART_PATTERNS:
        match = pattern.match(name)
        if not match:
            continue
        base = match.group("base").strip().rstrip(".")
        if not base:
            return None
        path = str(task.get("path") or "")
        # HTTP tasks store the destination file; debrid tasks the folder.
        folder = path if task.get("kind") in ("debrid", "debrid_link") else os.path.dirname(path)
        # The owner is part of the key: two accounts with the same relative
        # names in a shared mount must not be grouped into one package.
        owner = str(task.get("owner") or "")
        digest = hashlib.sha1(f"{owner}\0{folder}\0{base.lower()}".encode("utf-8")).hexdigest()
        return {
            "package": base,
            "package_key": digest[:12],
            "part": int(match.group("part") or 0),
        }
    return None


def _package_tasks(package_key: str) -> list[dict]:
    """Tasks that belong to one derived package key (visible to the caller)."""
    key = str(package_key or "").strip().lower()
    if not key:
        return []
    return [t for t in _tasks
            if _owned(t) and (_package_info(t) or {}).get("package_key") == key]


def _package_limit(task: dict, cfg: dict) -> tuple[str, int]:
    """(package_key, bytes/s) for a package-wide cap; ("", 0) when none is set."""
    info = _package_info(task)
    if not info:
        return "", 0
    limits = cfg.get("package_limits") or {}
    try:
        bps = int(limits.get(info["package_key"]) or 0)
    except (TypeError, ValueError):
        bps = 0
    key = info["package_key"]
    return (key, bps) if bps > 0 else ("", 0)


# ── Post-download archive extraction ────────────────────────────────────────
_ARCHIVE_SUFFIXES = (".zip", ".rar", ".7z", ".tar", ".tgz", ".tbz", ".txz",
                     ".gz", ".bz2", ".xz", ".zst", ".lz4")
_FIRST_VOLUME_RE = re.compile(r"(?i)\.part0*1\.(?:rar|zip|7z)$")
_EXTRACT_JOBS: set = set()


def _is_archive_path(path: str) -> bool:
    return str(path or "").lower().endswith(_ARCHIVE_SUFFIXES)


def _local_archive_paths(task: dict) -> list[str]:
    """Local archive files a finished task left on disk."""
    if task.get("kind") == "http":
        candidates = [str(task.get("path") or "")]
    else:
        candidates = [str(f.get("dest") or "") for f in (task.get("_files") or [])
                      if f.get("downloaded")]
    return [p for p in candidates if p and _is_archive_path(p) and os.path.isfile(p)]


def _first_volume(paths: list[str]) -> Optional[str]:
    """Pick the archive to feed the extractor for a single or multi-part set."""
    archives = sorted({p for p in paths if _is_archive_path(p) and os.path.isfile(p)})
    if not archives:
        return None
    for path in archives:
        if _FIRST_VOLUME_RE.search(os.path.basename(path)):
            return path
    for path in archives:
        if path.lower().endswith((".rar", ".zip", ".7z")):
            return path
    return archives[0]


def _config_for_tasks(tasks: list[dict]) -> dict:
    """Config of the account that owns a download.

    The pump and the extractor run as `system`, but extraction is a per-user
    preference: reading the system slice would always see the defaults.
    """
    owner = next((str(t.get("owner") or "") for t in (tasks or []) if t.get("owner")), "")
    if owner and owner != user_data.current_username():
        with path_scope.identity_scope(owner, user_data.user_role(owner)):
            return _load_config()
    return _load_config()


def _schedule_auto_extract(task: dict) -> None:
    """Queue extraction once every part of the package is complete."""
    if not _config_for_tasks([task]).get("auto_extract"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_auto_extract_task(task))


async def _auto_extract_task(task: dict) -> None:
    with path_scope.system_scope():
        await _auto_extract_task_scoped(task)


async def _auto_extract_task_scoped(task: dict) -> None:
    info = _package_info(task)
    group = _package_tasks(info["package_key"]) if info else [task]
    if not group or any(t.get("status") != "completed" for t in group):
        return
    if any(t.get("_extract_state") for t in group):
        return
    archive = _first_volume([p for t in group for p in _local_archive_paths(t)])
    if archive:
        _start_extract(group, archive)


def _start_extract(tasks: list[dict], archive: str) -> None:
    """Run an extraction in the background and record its state on the tasks."""
    for t in tasks:
        t["_extract_state"] = "running"
        t["_extract_dest"] = ""
        t["_extract_error"] = ""
    _save_tasks()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        for t in tasks:
            t["_extract_state"] = ""
        _save_tasks()
        return
    job = loop.create_task(_run_extract(tasks, archive))
    _EXTRACT_JOBS.add(job)
    job.add_done_callback(_EXTRACT_JOBS.discard)


async def _run_extract(tasks: list[dict], archive: str) -> None:
    with path_scope.system_scope():
        await _run_extract_scoped(tasks, archive)


async def _run_extract_scoped(tasks: list[dict], archive: str) -> None:
    from backend.services import files_service
    from backend.services.notifications_service import emit_event
    try:
        result = await asyncio.to_thread(files_service.extract_archive, archive)
        dest = str(result.get("dest_dir") or "")
        # Only a run that actually produced files counts as "successful"; an
        # archive whose members were all skipped (already extracted) keeps its
        # source, so a 0-file run can never destroy the only copy.
        if (int(result.get("extracted_count") or 0) > 0
                and _config_for_tasks(tasks).get("auto_extract_delete")):
            for t in tasks:
                for path in _local_archive_paths(t):
                    try:
                        os.remove(path)
                    except OSError as exc:
                        logger.warning("Extract: could not remove %s: %s", path, exc)
        for t in tasks:
            t["_extract_state"] = "done"
            t["_extract_dest"] = dest
        _save_tasks()
        logger.info("Extracted %s → %s (%s files)", archive, dest, result.get("extracted_count"))
        emit_event("success", "Archive extracted",
                   f"{os.path.basename(archive)} → {dest}", source="downloads")
    except Exception as exc:  # noqa: BLE001 — extraction must not kill the loop
        for t in tasks:
            t["_extract_state"] = "error"
            t["_extract_error"] = str(exc)
        _save_tasks()
        logger.error("Extraction failed for %s: %s", archive, exc, exc_info=True)
        emit_event("error", "Extraction failed",
                   f"{os.path.basename(archive)} — {str(exc)[:200]}", source="downloads")


def _manual_extract(tasks: list[dict]) -> dict:
    if not tasks:
        raise HTTPException(status_code=404, detail="Download not found")
    if any(t.get("status") != "completed" for t in tasks):
        raise HTTPException(status_code=409, detail="Only completed downloads can be extracted")
    if any(t.get("_extract_state") == "running" for t in tasks):
        raise HTTPException(status_code=409, detail="Extraction is already running")
    archive = _first_volume([p for t in tasks for p in _local_archive_paths(t)])
    if not archive:
        raise HTTPException(status_code=400, detail="No archive found for this download")
    _start_extract(tasks, archive)
    return {"ok": True, "status": "extracting", "archive": archive}


def _public(task: dict) -> dict:
    """Task dict shaped for the API (drops internal bookkeeping)."""
    out = {
        "id": task["id"],
        "url": task["url"],
        "name": task["name"],
        "path": task.get("path", ""),
        "owner": task.get("owner", ""),
        "status": task["status"],
        "size": int(task.get("size") or 0),
        "downloaded": int(task.get("downloaded") or 0),
        "speed": int(task.get("speed") or 0),
        "progress": _progress(task),
        "error": task.get("error", ""),
        "created_at": task.get("created_at", 0),
        "started_at": task.get("started_at", 0),
        "finished_at": task.get("finished_at", 0),
        "kind": task.get("kind", "http"),
        "priority": int(task.get("priority") or 0),
        "category": task.get("category") or task.get("_qb_category") or "",
        "retry_count": int(task.get("_retry_count") or 0),
        "retry_at": int(task.get("_next_retry_at") or 0),
        "mirror_count": len(_mirror_urls(task)),
        "mirror_index": int(task.get("_mirror_index") or 0),
        "extract_state": task.get("_extract_state", ""),
        "extract_dest": task.get("_extract_dest", ""),
        "extract_error": task.get("_extract_error", ""),
    }
    package = _package_info(task)
    if package:
        out.update(package)
    if task.get("kind") in ("debrid", "debrid_link"):
        out["provider"] = task.get("provider", "")
        out["debrid_status"] = task.get("_debrid_status", "")
        out["files"] = [
            {
                "id": f.get("id"),
                "name": f.get("name", ""),
                "size": int(f.get("size") or 0),
                "path": f.get("path", ""),
                "selected": bool(f.get("is_selected")),
                "downloaded_to_nas": bool(f.get("downloaded")),
            }
            for f in (task.get("_files") or [])
        ]
    return out


def _progress(task: dict) -> float:
    if task.get("kind") == "debrid":
        try:
            return max(0.0, min(1.0, int(task.get("_debrid_progress") or 0) / 100))
        except (TypeError, ValueError):
            return 0.0
    total = int(task.get("size") or 0)
    done = int(task.get("downloaded") or 0)
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, done / total))


def _find(task_id: str) -> Optional[dict]:
    for t in _tasks:
        if t["id"] == task_id:
            return t if _owned(t) else None
    return None


def _find_duplicate(url: str, target_dir: str) -> Optional[dict]:
    """Active/waiting/paused task for the same URL and destination folder."""
    url = str(url or "")
    if not url:
        return None
    folder = os.path.abspath(str(target_dir or ""))
    for t in _tasks:
        if not _owned(t):
            continue
        if str(t.get("url") or "") != url:
            continue
        if t.get("status") not in ("waiting", "downloading", "debrid_downloading", "paused"):
            continue
        kind = t.get("kind")
        own = t.get("path") if kind in ("debrid", "debrid_link") else os.path.dirname(str(t.get("path") or ""))
        if os.path.abspath(str(own or "")) == folder:
            return t
    return None


# ── Mirror / hoster failover ────────────────────────────────────────────────
# One file usually exists on several hosters with the same basename. A task
# keeps the primary `url` (identity, duplicate detection, what the user sees)
# and an internal `_mirrors` list; `_active_url` is whatever the worker fetches
# right now. When a source is exhausted the next mirror restarts from byte 0
# with a fresh retry budget.

_MIRROR_LIMIT = 10
_MIRROR_KINDS = ("http", "debrid_link")


def _source_url(task: dict) -> str:
    """URL the worker should fetch — a mirror after a failover, else the primary."""
    return str(task.get("_active_url") or task.get("url") or "")


def _mirror_urls(task: dict) -> list[str]:
    """Primary URL followed by every extra source, de-duplicated."""
    urls = [str(task.get("url") or "")] + [str(u) for u in (task.get("_mirrors") or [])]
    return [u for u in dict.fromkeys(urls) if u]


def _attach_mirrors(task: dict, extras) -> int:
    """Validate and store extra source URLs; returns how many are now configured.

    Invalid, non-http(s), private-network (for non-admins) and duplicate URLs are
    skipped with a log line instead of failing the whole add — a bad mirror must
    not block the working source.
    """
    if task.get("kind") not in _MIRROR_KINDS:
        return 0
    primary = str(task.get("url") or "")
    kept = [str(u) for u in (task.get("_mirrors") or []) if u]
    for raw in extras or []:
        url = str(raw or "").strip()
        if not url or url == primary or url in kept:
            continue
        try:
            url = _validate_url(url)
            _reject_private_url(url)
        except HTTPException as exc:
            logger.info("Downloads: mirror rejected (%s): %s", exc.detail, str(raw)[:160])
            continue
        kept.append(url)
    kept = kept[:_MIRROR_LIMIT]
    if kept:
        task["_mirrors"] = kept
    return len(_mirror_urls(task))


def _try_next_mirror(task: dict) -> bool:
    """Switch the task to its next mirror URL. False when every source was tried."""
    if task.get("kind") not in _MIRROR_KINDS:
        return False
    urls = _mirror_urls(task)
    index = int(task.get("_mirror_index") or 0)
    if index + 1 >= len(urls):
        return False
    index += 1
    task["_mirror_index"] = index
    task["_active_url"] = urls[index]
    # A different host serves a different byte layout: never resume across mirrors.
    partial = f"{task.get('path') or ''}.part"
    try:
        if partial != ".part" and os.path.isfile(partial):
            os.remove(partial)
    except OSError as exc:
        logger.warning("Download %s: could not drop the partial file for the mirror switch: %s",
                       task["id"], exc)
    task["_resume_pos"] = 0
    task["_saved_resume_pos"] = 0
    task["downloaded"] = 0
    task["size"] = 0
    task["speed"] = 0
    # Every source gets its own retry budget.
    task["_retry_count"] = 0
    task["_next_retry_at"] = 0
    task["error"] = ""
    task["status"] = "waiting"
    _events.pop(task["id"], None)
    _save_tasks()
    logger.info(
        "Download %s: switching to mirror %d/%d (%s)",
        task["id"], index + 1, len(urls), urls[index][:160],
    )
    return True


# Window/backoff policy lives in services/download_schedule.py and
# services/download_retry.py (P2-01).


def _queue_order(tasks: Optional[list] = None) -> list:
    """Waiting tasks in start order: higher `priority` first, then FIFO."""
    return download_engine.queue_order(_tasks if tasks is None else tasks)


_window_timer: Optional[asyncio.TimerHandle] = None


def _cancel_window_timer() -> None:
    global _window_timer
    if _window_timer is not None:
        _window_timer.cancel()
        _window_timer = None


def _schedule_window_pump(cfg: dict) -> None:
    """Re-run the pump when the download window opens (one timer at a time)."""
    global _window_timer
    if _window_timer is not None and not _window_timer.cancelled():
        return
    loop = asyncio.get_running_loop()
    delay = _schedule_policy.seconds_until_window(cfg)

    def _go() -> None:
        global _window_timer
        _window_timer = None
        loop.create_task(_pump())

    _window_timer = loop.call_later(delay, _go)
    logger.info("Download schedule is closed — next attempt in %.0f min", delay / 60)


# ── Engine ────────────────────────────────────────────────────────────────────

def _download_worker(task: dict, stop_event: threading.Event) -> None:
    """Blocking download in a worker thread. Updates `task` in place."""
    with path_scope.system_scope():
        _download_worker_scoped(task, stop_event)


def _download_worker_scoped(task: dict, stop_event: threading.Event) -> None:
    cfg = _load_config()
    url = _source_url(task)
    dest = task.get("path") or _unique_dest(cfg["download_dir"], task["name"])
    task["path"] = dest

    resume = max(int(task.get("_resume_pos") or 0), _resume_source(dest))
    state = {"dest": dest, "opened": False}
    last_bytes = resume
    last_t = _now()
    ema = 0.0

    def _on_open(status, content_length, headers, start_pos):
        nonlocal last_bytes, last_t, ema
        # Content-Disposition may name the file better than the URL did (first attempt only).
        if not state["opened"]:
            cd = headers.get("Content-Disposition") or ""
            if "filename=" in cd and not _tasks_changed_name(task):
                fname = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
                if fname and "/" not in fname and "\\" not in fname and not os.path.exists(state["dest"]):
                    base = os.path.dirname(state["dest"]) or "."
                    state["dest"] = _unique_dest(base, fname)
                    task["name"] = fname
                    task["path"] = state["dest"]
            state["opened"] = True
        # Server answered 200 (ignoring Range) → start fresh from zero.
        if resume > 0 and status == 200:
            task["_resume_pos"] = 0
            task["downloaded"] = 0
        total = (start_pos + content_length) if (status == 206 and start_pos > 0) else content_length
        if total > 0:
            task["size"] = total
        last_bytes = start_pos
        last_t = _now()
        ema = 0.0

    def _on_chunk(pos):
        nonlocal last_bytes, last_t, ema
        task["_resume_pos"] = pos
        task["downloaded"] = pos
        now = _now()
        # Persist the resume offset at most every 2 s / 8 MiB. Without this a
        # restart reopened the file at byte 0 even when the partial data was
        # still on disk.
        if (pos - int(task.get("_saved_resume_pos") or 0) >= 8 * 1024 * 1024
                or now - float(task.get("_last_resume_save") or 0) >= 2.0):
            task["_saved_resume_pos"] = pos
            task["_last_resume_save"] = now
            _save_tasks()
        dt = now - last_t
        if dt >= 0.25:
            rate = (pos - last_bytes) / dt
            ema = rate if ema == 0.0 else (rate * 0.35 + ema * 0.65)
            task["speed"] = int(ema)
            last_bytes = pos
            last_t = now

    os.makedirs(os.path.dirname(state["dest"]), exist_ok=True)
    limits = cfg.get("speed_limit") or {}
    pkg_key, pkg_limit = _package_limit(task, cfg)
    _stream_to_file(url, state, resume, stop_event, _on_chunk, _on_open,
                    limit_bps=pkg_limit or int(limits.get("per_task_bps") or 0),
                    global_bps=int(limits.get("global_bps") or 0),
                    limit_key=f"pkg:{pkg_key}" if pkg_limit else "")
    task["speed"] = 0
    _save_tasks()


def _tasks_changed_name(task: dict) -> bool:
    # The name came from a URL that already had a meaningful basename.
    return bool(task.get("_name_fixed"))


def _notify_status_change(task: dict, prev: str | None, status: str) -> None:
    """Emit a system notification on meaningful download state transitions.

    Called from every site that changes task["status"] to completed/error so the
    Notification Center sees both HTTP-engine and debrid outcomes. Never raises —
    a notification failure must not break the download pipeline.
    """
    if not prev or prev == status:
        return
    try:
        from backend.services.notifications_service import emit_event
    except Exception:  # noqa: BLE001
        return
    name = task.get("name") or "download"
    if status == "completed":
        emit_event("success", "Download completed", f'"{name}" finished', source="downloads")
    elif status == "error":
        detail = (task.get("error") or "").strip()
        msg = f'"{name}" failed' + (f" — {detail[:200]}" if detail else "")
        emit_event("error", "Download failed", msg, source="downloads")


def _set_status(task: dict, status: str) -> None:
    prev = task.get("status")
    task["status"] = status
    if status in ("completed", "error", "canceled", "skipped"):
        task["speed"] = 0
        task["finished_at"] = task.get("finished_at") or _now()
    if status == "completed":
        task["_resume_pos"] = 0
        task["_retry_count"] = 0
        task["_next_retry_at"] = 0
    _save_tasks()
    _notify_status_change(task, prev, status)
    if status == "completed":
        _schedule_auto_extract(task)


# ── Task retry (JDownloader-style backoff) ──────────────────────────────────
# Retry policy lives in services/download_retry.py (P2-01).


def _fail_task(task: dict, message: str, *, permanent: bool = False) -> None:
    """Mark a task failed, re-queueing transient errors with backoff."""
    cfg = _load_config()
    task["error"] = str(message or "Download failed")
    task["speed"] = 0
    attempts = int(task.get("_retry_count") or 0)
    limit = int(cfg.get("retry_max") or 0) if cfg.get("auto_retry", True) else 0
    if not permanent and limit and attempts < limit and _retry_policy.is_retryable(task["error"]):
        attempts += 1
        task["_retry_count"] = attempts
        task["_next_retry_at"] = _now() + _retry_policy.retry_delay(cfg, attempts)
        task["status"] = "waiting"
        _events.pop(task["id"], None)
        _save_tasks()
        logger.warning(
            "Download %s failed (%s) — retry %d/%d in %ds",
            task["id"], task["error"][:160], attempts, limit,
            int(task["_next_retry_at"] - _now()),
        )
        _schedule_retry_wakeup()
        return
    # This source is out of options (dead link, or the retry budget is spent) —
    # a configured mirror gets a fresh chance before the task is given up.
    if _try_next_mirror(task):
        _schedule_retry_wakeup()
        return
    task["_next_retry_at"] = 0
    _set_status(task, "error")


_retry_timer: Optional[asyncio.TimerHandle] = None


def _schedule_retry_wakeup() -> None:
    """Re-run the pump when the earliest waiting retry is due."""
    global _retry_timer
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if _retry_timer is not None and not _retry_timer.cancelled():
        _retry_timer.cancel()
    _retry_timer = None
    now = _now()
    delay = download_engine.next_retry_delay(_tasks, now)
    if delay is None:
        return

    def _go() -> None:
        global _retry_timer
        _retry_timer = None
        loop.create_task(_pump())

    _retry_timer = loop.call_later(delay, _go)
    logger.info("Next download retry in %.0f s", delay)


async def _run_download(task: dict, stop_event: threading.Event) -> None:
    """Wrap the blocking worker; keep the task dict consistent on exit."""
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_worker_pool, _download_worker, task, stop_event)
    except Exception as exc:  # noqa: BLE001 — surface as task error, never crash the pool
        logger.error("Download %s failed: %s", task["id"], exc, exc_info=True)
        _fail_task(task, str(exc))
        return
    # Worker returned normally:
    if stop_event.is_set():
        # paused/canceled was requested — status already set by the caller
        _save_tasks()
        return
    # Reached EOF → completed (only if the file is non-empty or size was 0)
    _set_status(task, "completed")


def _on_worker_done(task_id: str, worker: "asyncio.Task", own_event=None) -> None:
    _active.pop(task_id, None)
    t = _find(task_id)
    if t is not None and t.get("kind") in ("debrid", "debrid_link"):
        # Debrid workers finalize their own status (provider errors etc.).
        _finalize_debrid(t, worker)
    # Only remove the event this worker owned — a newer worker may already
    # have replaced it in `_events` (e.g. right after a reset).
    if own_event is None or _events.get(task_id) is own_event:
        _events.pop(task_id, None)
    # Surface any exception already captured into the task; then schedule pump.
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_pump())
    except RuntimeError:
        pass  # intentional: a failure here must not break the caller


async def _pump() -> None:
    """Start waiting tasks (HTTP + debrid) up to the concurrency limit (priority, then FIFO)."""
    with path_scope.system_scope():
        await _pump_scoped()


async def _pump_scoped() -> None:
    async with _pump_lock:
        cfg = _load_config()
        limit = cfg["max_concurrent"]
        if not _schedule_policy.in_window(cfg):
            # Tasks stay `waiting`; one timer re-pumps when the window opens.
            _schedule_window_pump(cfg)
            return
        loop = asyncio.get_running_loop()
        started = []
        for t in _queue_order():
            if t["status"] != "waiting":
                continue
            if t["id"] in _active:
                continue
            if float(t.get("_next_retry_at") or 0) > _now():
                continue  # backoff — the retry timer will call the pump again
            if len(_active) + len(started) >= limit:
                break
            disk_error = _check_disk_space(t, cfg)
            if disk_error:
                t["status"] = "error"
                t["error"] = disk_error
                _save_tasks()
                _notify_status_change(t, "waiting", "error")
                continue
            ev = threading.Event()
            _events[t["id"]] = ev
            if t.get("kind") in ("debrid", "debrid_link"):
                if t.get("kind") == "debrid_link":
                    provider, creds = _providers.link_provider(cfg)
                    missing = ("Hoster link downloads need a link provider — "
                               "set AllDebrid or tb7.pl in Settings")
                else:
                    provider, creds = _providers.torrent_provider(cfg)
                    missing = "Torrent provider is no longer configured (set it in Settings)"
                if not provider or not _providers.credentials_complete(provider, creds):
                    # Config changed since the task was added — don't silently start.
                    t["status"] = "error"
                    t["error"] = missing
                    _save_tasks()
                    _notify_status_change(t, "waiting", "error")
                    continue
                if t.get("provider") and t["provider"] != provider and int(t.get("_debrid_id") or 0):
                    # Provider changed while the task was paused — the old torrent ID is meaningless on the new provider.
                    t["_debrid_id"] = 0
                t["provider"] = provider
                t["_debrid_key"] = _providers.primary_secret(provider, creds)
                t["status"] = "debrid_downloading"
                wt = loop.create_task(_run_debrid(t, ev))
            else:
                t["status"] = "downloading"
                wt = loop.create_task(_run_download(t, ev))
            t["_next_retry_at"] = 0
            t["started_at"] = t.get("started_at") or _now()
            t["speed"] = 0
            wt.add_done_callback(lambda _w, _id=t["id"], _ev=ev: _on_worker_done(_id, _w, _ev))
            _active[t["id"]] = wt
            started.append(t)
        if started:
            _save_tasks()
        _schedule_retry_wakeup()


# ── Watch folders ───────────────────────────────────────────────────────────────
# A background thread polls each configured watch folder for new *.torrent files
# and turns each one into a debrid task (same path as an uploaded .torrent),
# downloading to that entry's destination (the watched folder itself when unset).
# After a torrent is queued it is moved to <watch_folder>/.processed/ so it is
# not re-queued on restart.

_watch_stop = threading.Event()
_watch_thread: Optional[threading.Thread] = None
_seen_torrents: set = set()  # infohashes already queued this session (race guard)
_failed_torrents: set = set()  # filenames that failed to parse (log dedupe)
_pending_pumps: set = set()  # fire-and-forget _pump task refs (GC guard)


def _schedule_pump(loop: asyncio.AbstractEventLoop) -> None:
    """Queue `_pump` on the event loop from a worker thread, holding a task ref
    so the fire-and-forget coroutine cannot be garbage-collected mid-run."""
    def _go():
        t = asyncio.ensure_future(_pump())
        _pending_pumps.add(t)
        t.add_done_callback(_pending_pumps.discard)
    try:
        loop.call_soon_threadsafe(_go)
    except RuntimeError:
        pass  # intentional: a failure here must not break the caller


def _watch_scan_once(cfg: dict, loop: asyncio.AbstractEventLoop, entry: dict) -> None:
    """One pass over one watch folder; queue a task for each new .torrent."""
    wf = (entry.get("folder") or "").strip()
    provider, creds = _providers.torrent_provider(cfg)
    if not provider or not _providers.credentials_complete(provider, creds):
        return  # nothing to do — leave files for a later pass
    # A blank per-entry destination means "save into the watched folder itself"
    # (qBittorrent-style), not the global default download dir.
    dest_dir = (entry.get("destination") or "").strip() or wf

    def _build_task(info: dict) -> dict:
        name = os.path.basename(str(info.get("name") or "").replace("\\", "/")).strip()
        name = name or f"torrent-{str(info.get('hash') or '')[:12]}"
        return _make_debrid_task(dest_dir, info["magnet"], name, size=info.get("size") or 0)

    def _enqueue(task: dict, _info_hash: str) -> None:
        # Hold the lock across append+persist so a concurrent save cannot drop the
        # new task. Use the UNLOCKED writer here: threading.Lock is not reentrant,
        # and _save_tasks() would deadlock this thread while still holding the lock.
        with _save_lock:
            _tasks.append(task)
            _write_tasks_file()

    watch_folders.scan_folder(
        wf,
        parse_torrent=_parse_torrent,
        build_task=_build_task,
        enqueue=_enqueue,
        seen=_seen_torrents,
        failed=_failed_torrents,
        logger=logger,
        on_queued=lambda: _schedule_pump(loop),
    )


def _watch_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Background thread: poll every account's watch folders every few seconds."""
    while not _watch_stop.is_set():
        try:
            for username, _slice in list(user_data.iter_user_slices("downloads.json")):
                with path_scope.identity_scope(username, user_data.user_role(username)):
                    cfg = _load_config()
                    if cfg.get("watch_enabled"):
                        for entry in cfg.get("watch_folders") or []:
                            _watch_scan_once(cfg, loop, entry)
        except Exception as exc:  # noqa: BLE001 — never kill the scanner thread
            logger.error("Watch folder scan error: %s", exc, exc_info=True)
        _watch_stop.wait(3.0)


def start_watch_folder() -> None:
    """Start the watch-folder scanner (idempotent). Called from app lifespan."""
    global _watch_thread
    if _watch_thread is not None and _watch_thread.is_alive():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Watch folder: no running event loop — scanner not started")
        return
    _watch_stop.clear()
    _watch_thread = threading.Thread(target=_watch_loop, args=(loop,), name="downloads-watch", daemon=True)
    _watch_thread.start()


_engine_ready = False


def engine_ready() -> bool:
    """True once the startup pump has finished re-queueing interrupted tasks."""
    return _engine_ready


async def _pump_safe() -> None:
    global _engine_ready
    try:
        await _pump()
    except Exception as exc:  # noqa: BLE001 — never let the pump kill the app
        logger.error("Download engine startup pump failed: %s", exc, exc_info=True)
    finally:
        _engine_ready = True


async def start_download_engine() -> None:
    """Re-queue interrupted downloads without blocking app readiness.

    The pump replays the task list (waiting → active). It used to be awaited in
    `lifespan`, so the server accepted no traffic until every queued debrid task
    had been re-armed. It now runs as a tracked background task; `/api/system/
    status` exposes `download_engine_ready` for UIs that want to wait.
    """
    from backend.services import task_registry

    task_registry.spawn(_pump_safe(), name="download-engine-startup")
    task_registry.spawn(_rss_loop(), name="download-rss-loop")


async def stop_download_engine() -> None:
    """Stop all running download workers so the process can exit cleanly.

    Worker threads are non-daemon — if they keep running (downloading, polling
    a debrid provider), interpreter exit hangs in threading._python_exit. Set
    every stop event and cancel the asyncio wrappers; each worker returns at
    its next chunk check / poll tick / backoff wait.
    Called from app lifespan shutdown.
    """
    for ev in list(_events.values()):
        ev.set()
    for wt in list(_active.values()):
        wt.cancel()


def stop_watch_folder() -> None:
    """Stop the watch-folder scanner. Called from app lifespan shutdown."""
    global _watch_thread
    _watch_stop.set()
    if _watch_thread is not None and _watch_thread.is_alive():
        _watch_thread.join(timeout=5)
    _watch_thread = None


# ── RSS feeds + rules ────────────────────────────────────────────────────────
# The scanner — bounded fetch, rule matching, per-feed intervals, persisted
# seen set — lives in services/download_rss.py (TODO.md P2-01). The router
# supplies the per-identity config loader, the enqueue path and the bencode
# parser, and owns the background loop.

async def _rss_enqueue(url: str, name: str, folder, category) -> None:
    await add_task(AddTaskRequest(url=url, name=name, folder=folder, category=category))


async def _rss_scan(feed_ids: Optional[set] = None, force: bool = False) -> dict:
    """Poll due feeds and queue matching items. Returns {feeds, queued}."""
    with path_scope.system_scope():
        return await download_rss.scan(
            load_config=_load_config,
            enqueue=_rss_enqueue,
            parse_torrent=_parse_torrent,
            feed_ids=feed_ids,
            force=force,
        )


async def _rss_loop() -> None:
    """Background loop (tracked by task_registry) — one scan per minute."""
    while True:
        try:
            await _rss_scan()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("RSS scan failed", exc_info=True)
        await asyncio.sleep(60)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/tasks")
async def list_tasks(
    status: str = "",
    limit: int = Query(0, ge=0, le=5000),
    offset: int = Query(0, ge=0),
):
    """Tasks, most recent first (paginated — `total` is the unpaginated count)."""
    visible = [t for t in _tasks if _owned(t)]
    ordered = sorted(visible, key=lambda x: x.get("created_at", 0), reverse=True)
    if status:
        ordered = [t for t in ordered if t["status"] == status]
    page = ordered[offset:offset + limit] if limit else ordered[offset:]
    # Queue position reflects the real pump order (priority, then FIFO), which is
    # not the list's created_at sort.
    queue_pos = {t["id"]: i + 1 for i, t in enumerate(_queue_order())
                 if _owned(t)}
    tasks = []
    for t in page:
        item = _public(t)
        if t["id"] in queue_pos:
            item["queue_position"] = queue_pos[t["id"]]
        tasks.append(item)
    return {"tasks": tasks, "total": len(ordered),
            "limit": limit, "offset": offset,
            "active": len(_active),
            "max_concurrent": _load_config()["max_concurrent"]}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    return _public(t)


def _make_debrid_task(target_dir: str, url: str, name: str, size: int = 0) -> dict:
    """Build (not yet persisted) a debrid task for a magnet/torrent URL."""
    provider, creds = _providers.torrent_provider(_load_config())
    key = _providers.primary_secret(provider, creds)
    return {
        "id": _uid(),
        "url": url,
        "name": name,
        "path": os.path.abspath(os.path.join(target_dir, name)),
        "kind": "debrid",
        "owner": _owner(),
        "provider": provider,
        "_debrid_key": key,
        "_debrid_id": 0,
        "_debrid_status": "",
        "_debrid_progress": 0,
        "_files": [],
        "status": "waiting",
        "size": int(size or 0),
        "downloaded": 0,
        "speed": 0,
        "error": "",
        "category": "",
        "created_at": _now(),
        "started_at": 0,
        "finished_at": 0,
    }


@router.post("/tasks")
async def add_task(payload: AddTaskRequest):
    """Create (and start) a download.

    magnet:? links are sent to the configured torrent provider and streamed
    back once cached there; premium file-hosting links (RapidGator, Mega, …) are
    resolved through the configured hoster-link provider (AllDebrid or tb7.pl);
    any other http(s) URL runs the built-in HTTP engine.
    """
    raw_url = payload.url.strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="URL is required")
    if not pressure_guard.heavy_work_allowed():
        raise HTTPException(status_code=503, detail="The NAS is low on memory — try again in a moment",
                            headers={"Retry-After": "30"})

    cfg = _load_config()
    folder = (payload.folder or "").strip()
    category = (payload.category or "").strip()[:64]
    category_dir = _category_target(cfg, category) if category else ""
    if folder:
        # Absolute path or a subpath of download_dir; both are resolved and
        # role-scoped (non-admins: /home, /mnt, /media) before use.
        if os.path.isabs(folder) or folder.startswith("~"):
            target_dir = path_scope.sanitize(folder)
        else:
            target_dir = path_scope.sanitize(os.path.join(cfg["download_dir"], folder.rstrip("/")))
    elif category_dir:
        target_dir = category_dir
    else:
        target_dir = cfg["download_dir"]

    duplicate = _find_duplicate(raw_url, target_dir)
    if duplicate:
        logger.info("Downloads: duplicate URL ignored (%s)", raw_url[:160])
        out = _public(duplicate)
        out["duplicate"] = True
        return out

    # ── Magnet link → debrid task ───────────────────────────────────────────
    if raw_url.lower().startswith("magnet:?"):
        provider, creds = _providers.torrent_provider(cfg)
        key = _providers.primary_secret(provider, creds)
        if not provider or not key:
            raise HTTPException(
                status_code=400,
                detail="No torrent/debrid provider configured — set one in Settings "
                       "(tb7.pl only handles hoster links)",
            )
        info = _parse_magnet(raw_url)
        fallback = f"magnet-{info['hash'][:12]}" if info["hash"] else "magnet"
        name = (payload.name or "").strip() or info["name"] or fallback
        name = os.path.basename(name.replace("\\", "/")).strip() or fallback
        task = _make_debrid_task(target_dir, raw_url, name)
        # An explicit user-supplied name must not be replaced by the server's
        # Content-Disposition filename (dead flag: nothing set `_name_fixed`).
        task["_name_fixed"] = bool((payload.name or "").strip())
        task["category"] = category
        _tasks.append(task)
        _save_tasks()
        await _pump()
        return _public(task)

    # ── Premium file-hosting link (RapidGator, Mega, …) → link task ─────────
    url = _validate_url(raw_url)
    _reject_private_url(url)
    # Every configured link provider that supports this host is a candidate;
    # the one in `debrid.link_provider` goes first and the rest are fallbacks.
    candidates = await asyncio.to_thread(_providers.link_candidates, cfg, url)
    if not candidates and _providers.is_known_hoster(url):
        # A file-host landing page is never the file itself — downloading it
        # with the plain HTTP engine would just save the HTML.
        configured = await asyncio.to_thread(_providers.configured_link_providers, cfg)
        names = ", ".join((_providers.get(pid).label if _providers.get(pid) else pid)
                          for pid, _c in configured) or "none"
        host = urllib.parse.urlparse(url).hostname or "this hoster"
        raise HTTPException(
            status_code=400,
            detail=(f"{host} is a premium file hoster — this link needs a link provider. "
                    f"Configured: {names}. Add one that supports this host in "
                    "Settings → Providers (AllDebrid unlocks any host)."),
        )
    if candidates:
        provider, link_creds = candidates[0]
        key = _providers.primary_secret(provider, link_creds)
        # Route through the hoster-link provider's unlock API/flow.
        name = (payload.name or "").strip() or _name_from_url(url)
        name = os.path.basename(name.replace("\\", "/")).strip() or "download.bin"
        task = {
            "id": _uid(),
            "url": url,
            "name": name,
            "path": target_dir,
            "kind": "debrid_link",
            "_name_fixed": bool((payload.name or "").strip()),
            "category": category,
            "provider": provider,
            "_debrid_key": key,
            "_debrid_id": 0,
            "_debrid_status": "",
            "_debrid_progress": 0,
            "_files": [],
            "status": "waiting",
            "size": 0,
            "downloaded": 0,
            "speed": 0,
            "error": "",
            "created_at": _now(),
            "started_at": 0,
            "finished_at": 0,
        }
        _attach_mirrors(task, payload.mirrors)
        _tasks.append(task)
        _save_tasks()
        await _pump()
        return _public(task)

    # ── Plain http(s) URL → built-in engine ─────────────────────────────────
    name = (payload.name or "").strip() or _name_from_url(url)
    # Sanitize the filename (no path traversal, no NUL).
    name = os.path.basename(name.replace("\\", "/")).strip() or "download.bin"

    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Download folder is not writable: {exc}") from exc
    policy = str(cfg.get("duplicate_policy") or "rename")
    existing = os.path.join(target_dir, name)
    if policy == "skip" and os.path.exists(existing):
        raise HTTPException(status_code=409, detail=f"File already exists: {name}")
    dest = existing if policy == "overwrite" else _unique_dest(target_dir, name)

    task = {
        "id": _uid(),
        "url": url,
        "name": name,
        "path": dest,
        "kind": "http",
        "owner": _owner(),
        "status": "waiting",
        "size": 0,
        "downloaded": 0,
        "speed": 0,
        "error": "",
        "_resume_pos": 0,
        "_name_fixed": bool((payload.name or "").strip()),
        "category": category,
        "created_at": _now(),
        "started_at": 0,
        "finished_at": 0,
    }
    _attach_mirrors(task, payload.mirrors)
    _tasks.append(task)
    _save_tasks()
    await _pump()
    return _public(task)


@router.post("/tasks/torrent")
async def add_torrent(files: list[UploadFile] = File(...), folder: str = "", category: str = ""):
    """Upload one or more .torrent files → parse locally (bencode) → debrid tasks.

    Each torrent is parsed with the stdlib bencode decoder to build an
    equivalent magnet link, then routed through the configured debrid
    provider exactly like a pasted magnet URL would be. Files that fail to
    parse are reported in `errors` without aborting the rest of the batch.

    Destination precedence matches `POST /tasks`: explicit `folder`, then the
    category's save path, then the configured download directory.
    """
    cfg = _load_config()
    provider, torrent_creds = _providers.torrent_provider(cfg)
    if not pressure_guard.heavy_work_allowed():
        raise HTTPException(status_code=503, detail="The NAS is low on memory — try again in a moment",
                            headers={"Retry-After": "30"})
    if not provider or not _providers.credentials_complete(provider, torrent_creds):
        raise HTTPException(
            status_code=400,
            detail="No torrent/debrid provider configured — .torrent uploads need one in Settings",
        )

    folder = (folder or "").strip()
    category = (category or "").strip()[:64]
    category_dir = _category_target(cfg, category) if category else ""
    if folder:
        # Absolute path or a subpath of download_dir; role-scoped like add_task.
        if os.path.isabs(folder) or folder.startswith("~"):
            target_dir = path_scope.sanitize(folder)
        else:
            target_dir = path_scope.sanitize(os.path.join(cfg["download_dir"], folder.rstrip("/")))
    elif category_dir:
        target_dir = category_dir
    else:
        target_dir = cfg["download_dir"]

    created: list[dict] = []
    errors: list[dict] = []
    for f in files:
        data = await f.read()
        try:
            info = _parse_torrent(data)
        except (ValueError, IndexError) as exc:
            errors.append({"file": f.filename or "unknown", "error": str(exc)})
            continue
        name = os.path.basename(info["name"].replace("\\", "/")).strip() or f"torrent-{info['hash'][:12]}"
        task = _make_debrid_task(target_dir, info["magnet"], name, size=info["size"])
        task["category"] = category
        created.append(task)

    if not created:
        detail = "; ".join(f"{e['file']}: {e['error']}" for e in errors) or "no files uploaded"
        raise HTTPException(status_code=400, detail=f"No torrents queued — {detail}")

    _tasks.extend(created)
    _save_tasks()
    await _pump()
    return {"queued": [_public(t) for t in created], "errors": errors}


@router.post("/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if t["status"] not in ("downloading", "debrid_downloading", "waiting"):
        return _public(t)
    t["status"] = "paused"
    ev = _events.get(t["id"])
    if ev:
        ev.set()  # worker exits on next chunk check / poll tick
    _save_tasks()
    return _public(t)


def _usable_parent(path: str) -> bool:
    """True when the service user can create ``path`` (its parents included)."""
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return os.path.isdir(probe) and os.access(probe, os.W_OK)


def _rebase_task_to_download_dir(t: dict, cfg: dict) -> bool:
    """Re-root a not-yet-started task under the current download directory.

    ``download_dir`` is a user setting, but a task stores the absolute path it
    was created with. After the folder is changed (or an old qBittorrent path
    like ``/home/qb-admin/Downloads`` is imported), Resume used to fail with a
    bare ``[Errno 13] Permission denied`` because the background worker runs as
    the EthOS service user. When nothing has been downloaded yet, move the task
    to the configured folder (keeping its category subfolder and name).
    """
    if t.get("status") not in ("waiting", "paused", "error"):
        return False
    if int(t.get("downloaded") or 0) > 0:
        return False
    path = str(t.get("path") or "").strip()
    dl_dir = os.path.abspath(str(cfg.get("download_dir") or ""))
    if not path or not dl_dir:
        return False
    path = os.path.abspath(path)
    try:
        if os.path.commonpath([path, dl_dir]) == dl_dir:
            return False  # already inside the configured folder
    except ValueError:
        return False
    # debrid tasks store the task folder; http/file tasks store the file itself.
    root = path if t.get("kind") in ("debrid", "debrid_link") else os.path.dirname(path)
    if _usable_parent(root):
        return False  # deliberately outside download_dir and writable
    category = str(t.get("category") or t.get("_qb_category") or "").strip()
    base = dl_dir
    if category:
        safe = os.path.basename(category.replace("\\", "/"))
        if safe and safe not in (".", ".."):
            base = os.path.join(dl_dir, safe)
    if t.get("kind") == "debrid_link":
        new_path = base
    else:
        new_path = os.path.join(base, os.path.basename(path) or str(t.get("name") or "download"))
    old_root = root
    t["path"] = new_path
    for f in t.get("_files") or []:
        dest = str(f.get("dest") or "").strip()
        if not dest:
            continue
        if t.get("kind") == "debrid":
            try:
                rel = os.path.relpath(dest, old_root)
            except ValueError:
                rel = os.path.basename(dest)
            f["dest"] = os.path.join(new_path, rel)
        else:
            f["dest"] = os.path.join(new_path, os.path.basename(dest))
    logger.info(
        "Downloads: task %s destination moved to %s (old path not writable by the service user)",
        t.get("id"), new_path,
    )
    return True


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if t["status"] not in ("paused", "error"):
        return _public(t)
    _rebase_task_to_download_dir(t, _load_config())
    t["status"] = "waiting"
    t["error"] = ""
    t["_retry_count"] = 0
    t["_next_retry_at"] = 0
    # Fresh event for the new worker.
    _events.pop(t["id"], None)
    _save_tasks()
    await _pump()
    return _public(t)


@router.post("/tasks/{task_id}/skip")
async def skip_task(task_id: str):
    """Exclude one download (usually a broken package part) without deleting it.

    A skipped record stops running, keeps its row but never restarts, and the
    package it belongs to no longer counts it as a missing part. `unskip`
    returns it to the queue.
    """
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if t.get("status") == "completed":
        raise HTTPException(status_code=409, detail="Completed downloads cannot be skipped")
    if t.get("status") == "skipped":
        return _public(t)
    ev = _events.get(t["id"])
    if ev:
        ev.set()  # worker exits on the next chunk check / poll tick
    t["status"] = "skipped"
    t["speed"] = 0
    t["error"] = ""
    t["_next_retry_at"] = 0
    t["finished_at"] = t.get("finished_at") or _now()
    part = f"{t.get('path') or ''}.part"
    if t.get("kind") == "http" and part != ".part":
        try:
            if os.path.isfile(part):
                os.remove(part)
        except OSError as exc:
            logger.warning("Download %s: could not remove the part file: %s", t["id"], exc)
    _save_tasks()
    logger.info("Download %s skipped by the user", t["id"])
    return _public(t)


@router.post("/tasks/{task_id}/unskip")
async def unskip_task(task_id: str):
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if t.get("status") != "skipped":
        return _public(t)
    t["status"] = "waiting"
    t["error"] = ""
    t["finished_at"] = 0
    t["_retry_count"] = 0
    t["_next_retry_at"] = 0
    _events.pop(t["id"], None)
    _save_tasks()
    await _pump()
    return _public(t)


@router.post("/tasks/{task_id}/mirrors")
async def add_task_mirrors(task_id: str, payload: AddMirrorsRequest):
    """Add alternative sources to a direct or hoster link download.

    Mirrors keep the task identity (`url`) intact and are tried in order once
    the current source is exhausted.
    """
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if t.get("kind") not in _MIRROR_KINDS:
        raise HTTPException(status_code=400, detail="Mirrors are only supported for direct and hoster links")
    before = len(_mirror_urls(t))
    total = _attach_mirrors(t, payload.urls)
    if total <= before:
        raise HTTPException(status_code=400, detail="No usable mirror URL")
    _save_tasks()
    logger.info("Download %s: %d mirror(s) added", t["id"], total - before)
    return _public(t)


@router.post("/tasks/{task_id}/reset")
async def reset_task(task_id: str):
    """Reset a download: delete the partial file(s) and restart from byte 0.

    Works for every task kind:
    - http        → removes the partial file at task["path"]
    - debrid_link → removes each local copy, re-unlocks on the provider
    - debrid      → removes the downloaded folder (provider-side torrent kept)

    A reset also goes back to the primary source, so a manual retry re-tries the
    whole mirror chain instead of continuing behind the last failed mirror.
    """
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")

    # Stop any running worker before touching files on disk.
    ev = _events.get(t["id"])
    if ev:
        ev.set()
    wt = _active.get(t["id"])
    # Mark as waiting immediately so a late finalize from the dying debrid
    # worker cannot overwrite our state with "error".
    t["status"] = "waiting"
    t["error"] = ""
    t["_retry_count"] = 0
    t["_next_retry_at"] = 0
    t["_mirror_index"] = 0
    t.pop("_active_url", None)
    if wt is not None and not wt.done():
        try:
            await asyncio.wait_for(asyncio.shield(wt), timeout=15)
        except Exception:
            pass  # worker still draining — it will exit on the stop event

    kind = t.get("kind", "http")

    def _purge() -> None:
        """Remove the partially downloaded payload (can be many GB)."""
        if kind == "debrid":
            base = str(t.get("path") or "").strip()
            if base and os.path.isdir(base):
                shutil.rmtree(base, ignore_errors=True)
        elif kind == "debrid_link":
            for f in (t.get("_files") or []):
                d = str(f.get("dest") or "")
                if d and os.path.isfile(d):
                    try:
                        os.remove(d)
                    except OSError:
                        pass  # best-effort cleanup — nothing to report
        else:
            p = str(t.get("path") or "").strip()
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass  # best-effort cleanup — nothing to report
            if p and os.path.isfile(_part_path(p)):
                try:
                    os.remove(_part_path(p))
                except OSError:
                    pass  # best-effort cleanup — nothing to report

    await asyncio.to_thread(_purge)

    if kind == "debrid":
        for f in (t.get("_files") or []):
            f["downloaded"] = False
    elif kind == "debrid_link":
        for f in (t.get("_files") or []):
            f["downloaded"] = False

    t["_resume_pos"] = 0
    t["downloaded"] = 0
    t["speed"] = 0
    t["finished_at"] = 0
    if kind == "debrid":
        t["_debrid_progress"] = 0
        t["_debrid_status"] = ""
    _events.pop(t["id"], None)
    _save_tasks()
    await _pump()
    return _public(t)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, delete_file: bool = False):
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    ev = _events.get(t["id"])
    if ev:
        ev.set()  # stop the worker if it is mid-download / mid-poll
    t["status"] = "canceled"
    t["speed"] = 0

    # Debrid tasks: remove the torrent from the provider (best effort).
    if t.get("kind") == "debrid":
        tid = int(t.get("_debrid_id") or 0)
        key = str(t.get("_debrid_key") or "")
        if tid and key:
            try:
                await asyncio.to_thread(_debrid_delete_torrent, t.get("provider", ""), key, tid)
            except Exception as exc:
                # Provider outage must not turn “cancel” into HTTP 500 — the local
                # task is already canceled, the remote torrent is best-effort.
                logger.warning("Debrid cancel: provider delete failed for %s: %s", task_id, exc)

    if delete_file and t.get("path"):
        def _delete_payload() -> None:
            try:
                if t.get("kind") == "debrid":
                    shutil.rmtree(t["path"], ignore_errors=True)  # torrent folder
                elif t.get("kind") == "debrid_link":
                    for f in (t.get("_files") or []):
                        d = str(f.get("dest") or "")
                        if d and os.path.isfile(d):
                            os.remove(d)
                        if d and os.path.isfile(_part_path(d)):
                            os.remove(_part_path(d))
                elif os.path.exists(t["path"]):
                    os.remove(t["path"])
                if t.get("kind") == "http" and t.get("path") and os.path.isfile(_part_path(t["path"])):
                    os.remove(_part_path(t["path"]))
            except OSError:
                pass  # best-effort cleanup — nothing to report
        # Deleting a multi-GB torrent folder on the event loop stalled the UI.
        await asyncio.to_thread(_delete_payload)

    # Remove the record entirely.
    _tasks.remove(t)
    _active.pop(t["id"], None)
    _events.pop(t["id"], None)
    _save_tasks()
    await _pump()
    return {"ok": True, "deleted": t["id"]}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, delete_file: bool = False):
    return await cancel_task(task_id, delete_file)


@router.post("/tasks/{task_id}/extract")
async def extract_task(task_id: str):
    """Extract one completed download (or its whole package) next to the archive."""
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    info = _package_info(t)
    return _manual_extract(_package_tasks(info["package_key"]) if info else [t])


# ── Packages (multi-part archives) ───────────────────────────────────────────
# The UI renders one row per package and calls these to act on every part at
# once. Keys are derived from the filenames (`_package_info`), so they stay
# stable across restarts and need no migration of existing task records.

@router.post("/packages/{package_key}/pause")
async def pause_package(package_key: str):
    tasks = _package_tasks(package_key)
    if not tasks:
        raise HTTPException(status_code=404, detail="Package not found")
    paused = 0
    for t in tasks:
        if t["status"] in ("downloading", "debrid_downloading", "waiting"):
            t["status"] = "paused"
            t["speed"] = 0
            ev = _events.get(t["id"])
            if ev:
                ev.set()
            paused += 1
    if paused:
        _save_tasks()
        logger.info("Downloads: paused %d task(s) in package %s", paused, package_key)
    return {"ok": True, "package": package_key, "paused": paused, "total": len(tasks)}


@router.post("/packages/{package_key}/resume")
async def resume_package(package_key: str):
    tasks = _package_tasks(package_key)
    if not tasks:
        raise HTTPException(status_code=404, detail="Package not found")
    resumed = 0
    for t in tasks:
        if t["status"] in ("paused", "error"):
            t["status"] = "waiting"
            t["error"] = ""
            t["speed"] = 0
            t["_retry_count"] = 0
            t["_next_retry_at"] = 0
            _events.pop(t["id"], None)
            resumed += 1
    if resumed:
        _save_tasks()
        await _pump()
        logger.info("Downloads: resumed %d task(s) in package %s", resumed, package_key)
    return {"ok": True, "package": package_key, "resumed": resumed, "total": len(tasks)}


@router.post("/packages/{package_key}/cancel")
async def cancel_package(package_key: str, delete_file: bool = False):
    tasks = list(_package_tasks(package_key))
    if not tasks:
        raise HTTPException(status_code=404, detail="Package not found")
    for t in tasks:
        await cancel_task(t["id"], delete_file)
    logger.info("Downloads: removed %d task(s) from package %s (delete_file=%s)",
                len(tasks), package_key, delete_file)
    return {"ok": True, "package": package_key, "deleted": len(tasks)}


@router.post("/packages/{package_key}/extract")
async def extract_package(package_key: str):
    """Extract a completed package into a folder next to the first archive."""
    return _manual_extract(_package_tasks(package_key))


@router.post("/packages/{package_key}/limit", dependencies=[Depends(require_admin)])
async def set_package_limit(package_key: str, payload: PackageLimitRequest):
    """Cap the bandwidth of one multi-part package (0 removes the cap).

    The limit is shared by every part of the package through one token bucket in
    `services/download_transfer.py`, so two parts downloading at once split the
    budget instead of each using it.
    """
    key = str(package_key or "").strip().lower()
    tasks = _package_tasks(key)
    if not tasks:
        raise HTTPException(status_code=404, detail="Package not found")
    cfg = _load_config()
    limits = dict(cfg.get("package_limits") or {})
    if payload.bps > 0:
        limits[key] = int(payload.bps)
    else:
        limits.pop(key, None)
        _forget_shared_limiter(f"pkg:{key}")
    cfg["package_limits"] = limits
    _save_config(cfg)
    logger.info("Downloads: package %s limit set to %d B/s", key, payload.bps)
    return {"ok": True, "package": key, "bps": int(payload.bps), "tasks": len(tasks)}


@router.post("/tasks/pause-all")
async def pause_all():
    n = 0
    for t in _tasks:
        if not _owned(t):
            continue
        # Waiting (queued) tasks are paused too — otherwise they would start
        # as soon as an active slot frees up, defeating "pause all".
        if t["status"] in ("downloading", "debrid_downloading", "waiting"):
            t["status"] = "paused"
            ev = _events.get(t["id"])
            if ev:
                ev.set()
            n += 1
    if n:
        _save_tasks()
    return {"ok": True, "paused": n}


@router.post("/tasks/resume-all")
async def resume_all():
    """Resume every paused task (bulk toolbar action)."""
    n = 0
    cfg = _load_config()
    for t in _tasks:
        if not _owned(t):
            continue
        if t["status"] == "paused":
            _rebase_task_to_download_dir(t, cfg)
            t["status"] = "waiting"
            t["error"] = ""
            t["_retry_count"] = 0
            t["_next_retry_at"] = 0
            _events.pop(t["id"], None)
            n += 1
    if n:
        _save_tasks()
        await _pump()
    return {"ok": True, "resumed": n}


def _remove_failed_partial(task: dict) -> None:
    """Delete the unfinished payload of a failed task (best effort).

    A failed HTTP download leaves `<dest>.part`; `debrid_link` tasks track one
    destination per file. Finished files and debrid destination folders are left
    alone — the per-task Delete action (`cancel?delete_file=true`) stays the
    explicit "remove the data too" path.
    """
    candidates: list[str] = []
    if task.get("kind") == "http":
        dest = str(task.get("path") or "")
        if dest:
            candidates.append(_part_path(dest))
    elif task.get("kind") == "debrid_link":
        for entry in task.get("_files") or []:
            dest = str(entry.get("dest") or "")
            if dest:
                candidates.append(_part_path(dest))
    for path in candidates:
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("Failed-download cleanup: could not remove %s: %s", path, exc)


@router.post("/tasks/clear-failed")
async def clear_failed():
    """Remove every failed task record (status `error`) from the list.

    Partial `.part` payloads are deleted; anything already completed on disk and
    debrid destination folders stay. Mirrors `clear-completed`, which only
    removes `completed` records.
    """
    keep = []
    removed = 0
    for t in _tasks:
        if not _owned(t) or t["status"] != "error":
            keep.append(t)
            continue
        _remove_failed_partial(t)
        _active.pop(t["id"], None)
        _events.pop(t["id"], None)
        removed += 1
    if removed:
        _tasks[:] = keep
        _save_tasks()
        logger.info("Downloads: cleared %d failed task(s)", removed)
    return {"ok": True, "removed": removed}


@router.post("/tasks/retry-failed")
async def retry_failed():
    """Re-queue every failed task (manual "Retry all failed")."""
    retried = 0
    cfg = _load_config()
    for t in _tasks:
        if not _owned(t):
            continue
        if t["status"] != "error":
            continue
        _rebase_task_to_download_dir(t, cfg)
        t["status"] = "waiting"
        t["error"] = ""
        t["speed"] = 0
        t["_retry_count"] = 0
        t["_next_retry_at"] = 0
        _events.pop(t["id"], None)
        retried += 1
    if retried:
        _save_tasks()
        await _pump()
        logger.info("Downloads: retry-all re-queued %d task(s)", retried)
    return {"ok": True, "retried": retried}


@router.post("/tasks/clear-completed")
async def clear_completed():
    """Remove all completed task records from the list (bulk cleanup).

    Only the history entries are removed — the downloaded files stay on disk.
    """
    keep = []
    removed = 0
    for t in _tasks:
        if _owned(t) and t["status"] == "completed":
            _active.pop(t["id"], None)
            _events.pop(t["id"], None)
            removed += 1
        else:
            keep.append(t)
    if removed:
        _tasks[:] = keep
        _save_tasks()
    return {"ok": True, "removed": removed}


@router.post("/tasks/reorder")
async def reorder_tasks(payload: ReorderRequest):
    """Reorder waiting tasks. The first id gets the highest priority."""
    known = {t["id"] for t in _tasks if _owned(t)}
    missing = [tid for tid in payload.order if tid not in known]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown task id: {missing[0]}")
    top = len(payload.order)
    for index, tid in enumerate(payload.order):
        t = _find(tid)
        if t is not None:
            t["priority"] = top - index
    _save_tasks()
    await _pump()
    return {"ok": True, "order": payload.order}


@router.post("/tasks/{task_id}/priority")
async def set_task_priority(task_id: str, payload: PriorityRequest):
    """Set one task's queue priority (higher starts earlier)."""
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    t["priority"] = payload.priority
    _save_tasks()
    await _pump()
    return _public(t)


@router.post("/tasks/{task_id}/files/select")
async def select_debrid_files(task_id: str, payload: SelectFilesRequest):
    """Choose which files of a debrid torrent to download (per-file picker).

    Body: `{file_ids: [..]}` — the provider file IDs that should be selected.
    Works while the task is waiting for selection or already downloading;
    the provider-side selectFiles call is issued so the worker proceeds.
    """
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if t.get("kind") != "debrid":
        raise HTTPException(status_code=400, detail="Not a debrid task")
    ids = payload.file_ids
    files = t.get("_files") or []
    if not files:
        raise HTTPException(status_code=409, detail="File list is not available yet (torrent still being fetched)")
    wanted = {str(i) for i in ids}
    selected_any = False
    for f in files:
        sel = str(f.get("id")) in wanted
        f["is_selected"] = sel
        selected_any |= sel
    if not selected_any:
        raise HTTPException(status_code=400, detail="None of the given file IDs match this torrent")
    # Recompute the visible total for the newly selected set.
    t["size"] = sum(int(f.get("size") or 0) for f in files if f.get("is_selected"))
    # The byte counter now tracks the local transfer of the selected set — drop
    # any provider-side cache bytes so it doesn't show e.g. "19 GB / 6 MB".
    if t.get("_debrid_status") != "downloading_to_nas":
        t["downloaded"] = 0

    # Tell the provider which files to keep (no-op on TorBox, which downloads all).
    tid = int(t.get("_debrid_id") or 0)
    if tid:
        try:
            await asyncio.to_thread(
                _debrid_select_files, t["provider"], t["_debrid_key"], tid, [int(i) for i in ids]
            )
        except Exception as exc:
            logger.warning("Provider selectFiles failed for task %s: %s", task_id, exc)
    _save_tasks()
    return _public(t)


def _public_config(admin: bool = False) -> dict:
    """Config with provider credentials masked (save endpoint ignores masked values).

    The qBittorrent API key is a real credential — it authenticates the whole
    *arr integration. Only admins see it in full; everyone else gets a masked
    hint plus `qb_api_key_set` so the UI can explain why copying is disabled.
    """
    cfg = _load_config()
    cfg["debrid"] = _providers.mask_credentials(cfg.get("debrid"))

    key = str(cfg.get("qb_api_key") or "").strip()
    if admin:
        # Generate on first admin access — non-admins must not create state.
        full = _qb_get_api_key()
        cfg["qb_api_key"] = full
        key_set = bool(full)
    else:
        cfg["qb_api_key"] = ("****" + key[-4:]) if len(key) > 6 else ("****" if key else "")
        key_set = bool(key)
    cfg["qb_api_key_set"] = key_set
    return cfg


@router.get("/config")
async def get_config(request: Request):
    return _public_config(admin=_is_admin(request))


@router.post("/config")
async def save_config(request: Request, req: SaveConfigRequest):
    """Save the caller's downloads settings.

    Per-user fields (download dir, categories, watch folders, RSS, extraction
    policy) are always applied to the caller's slice. Engine fields (concurrency,
    schedule, speed limits, debrid credentials) are admin-only and ignored for
    other accounts.
    """
    is_admin = _is_admin(request)
    cfg = _load_config()
    # `exclude_unset` keeps the "omitted vs null" semantics the merge relies on.
    payload = req.model_dump(exclude_unset=True)
    if is_admin and "max_concurrent" in payload:
        try:
            cfg["max_concurrent"] = max(1, min(16, int(payload["max_concurrent"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_concurrent must be a number between 1 and 16")
    if "download_dir" in payload and payload["download_dir"]:
        # sanitize() applies the caller's scope (own home, /mnt, /media) and
        # rejects relative-path tricks; admins keep full access.
        p = path_scope.sanitize(str(payload["download_dir"]))
        if not os.path.isdir(p):
            raise HTTPException(status_code=400, detail=f"Download directory does not exist: {p}")
        if not os.access(p, os.W_OK):
            raise HTTPException(status_code=400, detail=f"Download directory is not writable: {p}")
        cfg["download_dir"] = p
    if is_admin and isinstance(payload.get("auto_download_debrid"), bool):
        cfg["auto_download_debrid"] = payload["auto_download_debrid"]
    if is_admin and isinstance(payload.get("auto_retry"), bool):
        cfg["auto_retry"] = payload["auto_retry"]
    if is_admin and "retry_max" in payload:
        try:
            cfg["retry_max"] = max(0, min(10, int(payload["retry_max"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="retry_max must be a number between 0 and 10")
    if is_admin and "retry_delay" in payload:
        try:
            cfg["retry_delay"] = max(10, min(3600, int(payload["retry_delay"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="retry_delay must be 10..3600 seconds")
    if payload.get("duplicate_policy") in ("rename", "skip", "overwrite"):
        cfg["duplicate_policy"] = payload["duplicate_policy"]
    for flag in ("auto_extract", "auto_extract_delete"):
        if isinstance(payload.get(flag), bool):
            cfg[flag] = payload[flag]
    if "watch_folders" in payload:
        raw = payload["watch_folders"]
        if isinstance(raw, list):
            # Accept paths even when they don't exist yet (created on first scan),
            # but every path still has to pass the role scope.
            entries = []
            for x in raw[:8]:
                e = _norm_watch_entry(x)
                if not e:
                    continue
                if e["folder"]:
                    e["folder"] = path_scope.sanitize(e["folder"])
                if e["destination"]:
                    e["destination"] = path_scope.sanitize(e["destination"])
                entries.append(e)
            cfg["watch_folders"] = entries or [{"name": "Default", "folder": "", "destination": ""}]
            # The list is now authoritative — drop the legacy single-folder value
            # so it cannot resurrect a removed entry on the next load.
            cfg["watch_folder"] = ""
    if isinstance(payload.get("watch_enabled"), bool):
        cfg["watch_enabled"] = payload["watch_enabled"]
    if is_admin and isinstance(payload.get("schedule"), dict):
        sched = {**cfg.get("schedule", {}), **payload["schedule"]}
        if _parse_hhmm(sched.get("start")) is None or _parse_hhmm(sched.get("end")) is None:
            raise HTTPException(status_code=400, detail="Schedule start/end must be HH:MM")
        sched["enabled"] = bool(sched.get("enabled"))
        cfg["schedule"] = sched
        _cancel_window_timer()  # a new window may open sooner than the old timer
    if is_admin and isinstance(payload.get("speed_limit"), dict):
        limits = {**cfg.get("speed_limit", {}), **payload["speed_limit"]}
        for key in ("global_bps", "per_task_bps"):
            try:
                limits[key] = max(0, int(limits.get(key) or 0))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"{key} must be a non-negative number")
        cfg["speed_limit"] = limits
    if "categories" in payload:
        cats = _norm_categories(payload.get("categories"))
        for cat in cats:
            path = cat["save_path"]
            if not path:
                continue
            if os.path.isabs(path) or path.startswith("~"):
                cat["save_path"] = path_scope.sanitize(path)
            else:
                cat["save_path"] = path_scope.sanitize(os.path.join(cfg["download_dir"], path))
        cfg["categories"] = cats
    if "rss_feeds" in payload:
        feeds = _norm_rss_feeds(payload.get("rss_feeds"))
        for feed in feeds:
            dest = feed["destination"]
            if not dest:
                continue
            if os.path.isabs(dest) or dest.startswith("~"):
                feed["destination"] = path_scope.sanitize(dest)
            else:
                feed["destination"] = path_scope.sanitize(os.path.join(cfg["download_dir"], dest))
        cfg["rss_feeds"] = feeds
    db_in = payload.get("debrid")
    if is_admin and isinstance(db_in, dict):
        # Merge over the CURRENT config so fields not sent by the UI (e.g. an
        # unchanged API key) are preserved; masked values ("****…") keep the old one.
        db = {**_default_debrid(), **(cfg.get("debrid") or {})}
        db = _providers.merge_credential_values(db, db_in)
        # Slot names are validated against provider capabilities, not a list.
        for slot, allowed in (("provider", _providers.is_torrent_provider),
                              ("link_provider", _providers.is_link_provider)):
            if slot in db_in:
                name = str(db_in.get(slot) or "").strip()
                if name and not allowed(name):
                    logger.warning("Ignoring %s=%r — the provider does not support that slot", slot, name)
                    name = ""
                db[slot] = name
        cfg["debrid"] = db
    _save_config(cfg)
    logger.info("Download config updated by %s",
                (getattr(request.state, "current_user", None) or {}).get("username", "?"))
    return _public_config(admin=is_admin)


@router.post("/config/qb-key/rotate", dependencies=[Depends(require_admin)])
async def rotate_qb_api_key():
    """Issue a new qBittorrent API key; Radarr/Sonarr must be updated after this."""
    cfg = _load_config()
    cfg["qb_api_key"] = "ethos_" + secrets.token_hex(24)
    _save_config(cfg)
    logger.info("qBittorrent API key rotated")
    return {"qb_api_key": cfg["qb_api_key"]}


@router.post("/verify", dependencies=[Depends(require_admin)])
async def verify_debrid(payload: VerifyDebridRequest):
    """Check provider credentials (API key, or tb7.pl username/password)."""
    provider = payload.provider.strip()
    spec = _providers.get(provider)
    if spec is None:
        raise HTTPException(status_code=400, detail="Unknown download provider")
    fields = _providers.credential_fields(provider)
    stored = _providers.provider_credentials(_load_config(), provider)
    incoming = {str(k): str(v) for k, v in (payload.credentials or {}).items() if isinstance(v, (str, int, float))}
    if payload.key.strip() and len(fields) == 1:
        incoming[fields[0].name] = payload.key.strip()
    creds = dict(stored)
    for field in fields:
        value = incoming.get(field.name)
        if value is None:
            continue
        value = value.strip()
        if field.secret and ("****" in value or not value):
            continue  # masked/blank → verify the stored secret
        if value:
            creds[field.name] = value
    missing = [field.label for field in fields if not str(creds.get(field.name) or "").strip()]
    if missing:
        raise HTTPException(status_code=400, detail=f"{spec.label} {', '.join(missing).lower()} is required")
    try:
        result = await asyncio.to_thread(_providers.verify, provider, creds)
    except _ProviderHTTPError as exc:
        # A provider saying "bad token" is a key problem (401), not an outage (502).
        logger.warning("%s verify rejected the credentials (HTTP %s): %s", provider, exc.status, exc.detail)
        if exc.status in (401, 403):
            raise HTTPException(
                status_code=401,
                detail=f"{spec.label} rejected the credentials: {exc.detail or 'not authorised'}",
            ) from exc
        if exc.status == 429:
            raise HTTPException(status_code=429,
                                detail=f"{spec.label} rate-limited the check — try again in a minute") from exc
        raise HTTPException(status_code=502,
                            detail=f"{spec.label} API error (HTTP {exc.status}): {exc.detail or 'no detail'}") from exc
    except Exception as exc:
        # Distinguish "provider is down" from "your key is wrong" — the old
        # blanket exception handler reported outages as an expired key.
        logger.warning("%s verify failed: %s", provider, exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not reach {spec.label}: {exc}") from exc
    if not result.get("ok"):
        raise HTTPException(status_code=401, detail=f"{spec.label} did not accept the credentials")
    out = {"ok": True, "provider": provider}
    if result.get("account"):
        out["account"] = result["account"]
    return out


@router.get("/providers")
async def list_providers(request: Request):
    """Provider catalogue for the package UI — never returns a secret."""
    cfg = _load_config()
    admin = _is_admin(request)
    torrent_name, _tc = _providers.torrent_provider(cfg)
    link_name, _lc = _providers.link_provider(cfg)
    items = []
    for spec in _providers.all_specs():
        creds = _providers.provider_credentials(cfg, spec.id)
        fields = []
        for field in spec.credentials:
            value = str(creds.get(field.name) or "")
            masked = ("****" + value[-4:]) if len(value) > 6 else "****"
            fields.append({
                "name": field.name,
                "label": field.label,
                "kind": field.kind,
                "secret": field.secret,
                "placeholder": field.placeholder,
                "set": bool(value),
                "value": masked if (field.secret and value) else ("" if field.secret else value),
            })
        hosts = []
        if spec.hoster_match == _providers.HOSTER_MATCH_LIST:
            # First call after boot fetches jdhostingi.txt — never block the loop.
            hosts = await asyncio.to_thread(_providers.hosters, spec.id)
        item = {
            "id": spec.id,
            "label": spec.label,
            "kind": spec.kind,
            "homepage": spec.homepage,
            "description": spec.description,
            "capabilities": sorted(spec.capabilities),
            "slots": _providers.eligible_slots(spec.id),
            "hoster_match": spec.hoster_match,
            "hosters": hosts,
            "credentials": fields,
        }
        account = _providers.cached_account(spec.id, creds) if admin else {}
        if account:
            item["account"] = account
        items.append(item)
    return {
        "providers": items,
        "slots": {
            "torrent": list(_providers.ids_by_capability(_providers.CAP_TORRENTS)),
            "link": list(_providers.ids_by_capability(_providers.CAP_LINK_UNLOCK)),
        },
        "active": {"torrent": torrent_name, "link": link_name},
    }


@router.get("/rss")
async def list_rss_feeds():
    """Configured RSS rules (no secrets)."""
    return {"feeds": _load_config().get("rss_feeds") or []}


@router.post("/rss/{feed_id}/run", dependencies=[Depends(require_admin)])
async def run_rss_feed(feed_id: str):
    """Run one feed now (ignores its interval) and report what was queued."""
    cfg = _load_config()
    if not any(f.get("id") == feed_id for f in cfg.get("rss_feeds") or []):
        raise HTTPException(status_code=404, detail="RSS feed not found")
    return await _rss_scan(feed_ids={feed_id}, force=True)


@router.get("/summary")
async def summary():
    """Lightweight rollup for the taskbar badge / dashboard widget."""
    visible = [t for t in _tasks if _owned(t)]
    counts = {"waiting": 0, "downloading": 0, "paused": 0, "completed": 0, "error": 0, "canceled": 0}
    for t in visible:
        s = "downloading" if t["status"] == "debrid_downloading" else t["status"]
        counts[s] = counts.get(s, 0) + 1
    active_speed = sum(int(t.get("speed") or 0) for t in visible
                       if t["status"] in ("downloading", "debrid_downloading"))
    return {
        "total": len(visible),
        "active": sum(1 for t in visible if t["id"] in _active),
        "counts": counts,
        "active_speed": active_speed,
        "max_concurrent": _load_config()["max_concurrent"],
    }
