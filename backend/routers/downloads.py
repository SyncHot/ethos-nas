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

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from backend.config import settings
from backend.routers.auth import require_admin
from backend.services import path_scope
from backend.services import rss_service
from backend.services.bencode import parse_magnet as _parse_magnet
from backend.services.bencode import parse_torrent as _parse_torrent
from backend.services.download_transfer import check_disk_space as _check_disk_space
from backend.services.download_transfer import finalize_part as _finalize_part
from backend.services.download_transfer import part_path as _part_path
from backend.services.download_transfer import resume_source as _resume_source
from backend.services.download_transfer import stream_to_file as _stream_to_file

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
    DEBRID_PROVIDERS,
    active_debrid as _active_debrid,
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


class SelectFilesRequest(BaseModel):
    """Body of `POST /api/downloads/tasks/{id}/files/select`."""

    model_config = {"extra": "forbid"}

    file_ids: list[int | str] = Field(min_length=1, max_length=2000)


class VerifyDebridRequest(BaseModel):
    """Body of `POST /api/downloads/verify` — provider key check."""

    model_config = {"extra": "forbid"}

    provider: str = Field(min_length=1, max_length=32)
    key: str = Field(default="", max_length=512)


class PriorityRequest(BaseModel):
    """Body of `POST /api/downloads/tasks/{id}/priority` (higher runs first)."""

    model_config = {"extra": "forbid"}

    priority: int = Field(ge=-1000, le=1000)


class ReorderRequest(BaseModel):
    """Body of `POST /api/downloads/tasks/reorder` — waiting tasks, first first."""

    model_config = {"extra": "forbid"}

    order: list[str] = Field(min_length=1, max_length=500)


ALLOWED_SCHEMES = {"http", "https"}
# File-hosting domains that need a premium account (or debrid) to download.
# Links from these hosts are routed through the configured debrid provider;
# any other http(s) URL is downloaded directly by the built-in engine.
FILE_HOSTERS = {
    "rapidgator.net", "mega.nz", "mediafire.com", "1fichier.com", "uptobox.com",
    "uploadever.com", "filefactory.com", "depositfiles.com", "filer.net",
    "gigapeta.com", "krakenfiles.com", "pixeldrain.com", "1024terabytes.com",
    "turbobit.net", "nowvideo.sx", "doodstream.com", "vidhide.com", "ok.ru",
    "dropbox.com", "we.tl", "sendgb.com", "filemoon.in", "files.to",
}


def _is_file_host(url: str) -> bool:
    """True when the URL's host is a known premium file hoster."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return False
    if not host:
        return False
    for h in FILE_HOSTERS:
        if host == h or host.endswith("." + h):
            return True
    return False


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


_load_tasks()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _uid() -> str:
    return "dl_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "_" + os.urandom(3).hex()


def _name_from_url(url: str) -> str:
    try:
        path = urllib.parse.urlparse(url).path
    except Exception:
        path = url
    base = os.path.basename(path.split("?")[0].split("#")[0])
    base = (base or "").strip()
    return base if base and base != "/" else "download.bin"


# Part-file helpers and the transfer engine live in
# backend/services/download_transfer.py (TODO.md P2-01), imported above.


def _unique_dest(download_dir: str, name: str) -> str:
    target = os.path.join(download_dir, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    i = 1
    while os.path.exists(os.path.join(download_dir, f"{stem} ({i}){ext}")):
        i += 1
    return os.path.join(download_dir, f"{stem} ({i}){ext}")


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail="Only http:// and https:// URLs are allowed")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="URL must include a host")
    return url


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
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
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
    base_abs = os.path.abspath(base)
    dest = os.path.abspath(os.path.join(base_abs, *parts))
    if dest != base_abs and not dest.startswith(base_abs + os.sep):
        raise ValueError("unsafe path in torrent metadata")
    real_base = os.path.realpath(base_abs)
    real_parent = os.path.realpath(os.path.dirname(dest))
    if real_parent != real_base and not real_parent.startswith(real_base + os.sep):
        raise ValueError("unsafe path in torrent metadata (symlink escape)")
    return dest


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
    verify as _debrid_verify,
    ProviderHTTPError as _ProviderHTTPError,
    ad_link_info as _ad_link_info,
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
    cfg = _load_config()
    provider, key = task["provider"], task["_debrid_key"]
    tid = int(task.get("_debrid_id") or 0)
    is_link = task.get("kind") == "debrid_link"

    # ── Phase 1: resolve premium link / add the magnet ─────────────────────
    if is_link:
        # File-hosting link (RapidGator, Mega, …): no torrent — resolve it to a
        # direct download URL through AllDebrid's premium API.
        info = _ad_link_info(key, task["url"])
        if info.get("name"):
            task["name"] = os.path.basename(info["name"].replace("\\", "/")) or task["name"]
        if info.get("size"):
            task["size"] = int(info["size"])
        link = _ad_unlock(key, task["url"], stop_event)
        fname = os.path.basename(link.get("name") or "") or os.path.basename(task["name"].replace("\\", "/")) or "download.bin"
        # Per-task folder (set at creation) wins over the global default dir.
        base_dir = os.path.abspath(task.get("path") or cfg["download_dir"])
        dest = os.path.abspath(os.path.join(base_dir, fname))
        task["_files"] = [{
            "id": 0,
            "name": fname,
            "size": int(link.get("size") or info.get("size") or 0),
            "path": "",
            "is_selected": True,
            "dest": dest,
            "_link_url": link["url"],   # resolved premium URL — phase 3 uses it directly
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
        limits = _load_config().get("speed_limit") or {}
        final_pos = _stream_to_file(
            url, holder, resume, stop_event, _on_chunk, _on_open,
            limit_bps=int(limits.get("per_task_bps") or 0),
            global_bps=int(limits.get("global_bps") or 0),
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
        digest = hashlib.sha1(f"{folder}\0{base.lower()}".encode("utf-8")).hexdigest()
        return {
            "package": base,
            "package_key": digest[:12],
            "part": int(match.group("part") or 0),
        }
    return None


def _package_tasks(package_key: str) -> list[dict]:
    """Tasks that belong to one derived package key."""
    key = str(package_key or "").strip().lower()
    if not key:
        return []
    return [t for t in _tasks if (_package_info(t) or {}).get("package_key") == key]


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


def _schedule_auto_extract(task: dict) -> None:
    """Queue extraction once every part of the package is complete."""
    if not _load_config().get("auto_extract"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(_auto_extract_task(task))


async def _auto_extract_task(task: dict) -> None:
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
    from backend.services import files_service
    from backend.services.notifications_service import emit_event
    try:
        result = await asyncio.to_thread(files_service.extract_archive, archive)
        dest = str(result.get("dest_dir") or "")
        if _load_config().get("auto_extract_delete"):
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
            return t
    return None


def _find_duplicate(url: str, target_dir: str) -> Optional[dict]:
    """Active/waiting/paused task for the same URL and destination folder."""
    url = str(url or "")
    if not url:
        return None
    folder = os.path.abspath(str(target_dir or ""))
    for t in _tasks:
        if str(t.get("url") or "") != url:
            continue
        if t.get("status") not in ("waiting", "downloading", "debrid_downloading", "paused"):
            continue
        kind = t.get("kind")
        own = t.get("path") if kind in ("debrid", "debrid_link") else os.path.dirname(str(t.get("path") or ""))
        if os.path.abspath(str(own or "")) == folder:
            return t
    return None


def _queue_order(tasks: Optional[list] = None) -> list:
    """Waiting tasks in start order: higher `priority` first, then FIFO."""
    candidates = [t for t in (_tasks if tasks is None else tasks)
                  if t.get("status") == "waiting"]
    return sorted(candidates,
                  key=lambda x: (-int(x.get("priority") or 0), x.get("created_at", 0)))


def _in_schedule_window(cfg: dict, now: Optional[datetime] = None) -> bool:
    """True when transfers may start now (no schedule = always true)."""
    sched = cfg.get("schedule") or {}
    if not sched.get("enabled"):
        return True
    start = _parse_hhmm(sched.get("start"))
    end = _parse_hhmm(sched.get("end"))
    if start is None or end is None or start == end:
        return True
    current = now or datetime.now()
    minute = current.hour * 60 + current.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end  # overnight window


def _seconds_until_window(cfg: dict) -> float:
    """Seconds until the next window start (minimum 5 s)."""
    sched = cfg.get("schedule") or {}
    start = _parse_hhmm(sched.get("start"))
    if start is None:
        return 60.0
    current = datetime.now()
    minute = current.hour * 60 + current.minute + current.second / 60.0
    delta = (start - minute) % 1440.0 or 1440.0
    return max(5.0, delta * 60.0)


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
    delay = _seconds_until_window(cfg)

    def _go() -> None:
        global _window_timer
        _window_timer = None
        loop.create_task(_pump())

    _window_timer = loop.call_later(delay, _go)
    logger.info("Download schedule is closed — next attempt in %.0f min", delay / 60)


# ── Engine ────────────────────────────────────────────────────────────────────

def _download_worker(task: dict, stop_event: threading.Event) -> None:
    """Blocking download in a worker thread. Updates `task` in place."""
    cfg = _load_config()
    url = task["url"]
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
    _stream_to_file(url, state, resume, stop_event, _on_chunk, _on_open,
                    limit_bps=int(limits.get("per_task_bps") or 0),
                    global_bps=int(limits.get("global_bps") or 0))
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
    if status in ("completed", "error", "canceled"):
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
# A network hiccup must not leave a download dead until the user notices. A
# permanent failure (dead hoster link, bad debrid key, full disk) is not
# retried — otherwise a package of expired links would hammer the provider.
_PERMANENT_ERROR_RE = re.compile(
    r"(?i)(not available|not found|404|410|invalid|expired|bad[_ ]?token|"
    r"no debrid provider|no longer configured|not configured|unsupported|forbidden|403|quota|"
    r"permission|no space|disk is full|already exists)"
)


def _is_retryable(message: str) -> bool:
    """False for failures another identical attempt cannot fix."""
    return not _PERMANENT_ERROR_RE.search(str(message or ""))


def _retry_delay(cfg: dict, attempt: int) -> int:
    """Exponential base * 2^(attempt-1), capped at six hours."""
    base = max(10, int(cfg.get("retry_delay") or 60))
    return min(6 * 3600, base * (2 ** max(0, attempt - 1)))


def _fail_task(task: dict, message: str, *, permanent: bool = False) -> None:
    """Mark a task failed, re-queueing transient errors with backoff."""
    cfg = _load_config()
    task["error"] = str(message or "Download failed")
    task["speed"] = 0
    attempts = int(task.get("_retry_count") or 0)
    limit = int(cfg.get("retry_max") or 0) if cfg.get("auto_retry", True) else 0
    if not permanent and limit and attempts < limit and _is_retryable(task["error"]):
        attempts += 1
        task["_retry_count"] = attempts
        task["_next_retry_at"] = _now() + _retry_delay(cfg, attempts)
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
    pending = [float(t.get("_next_retry_at") or 0) for t in _tasks
               if t.get("status") == "waiting"
               and float(t.get("_next_retry_at") or 0) > now]
    if not pending:
        return
    delay = max(1.0, min(pending) - now)

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
    async with _pump_lock:
        cfg = _load_config()
        limit = cfg["max_concurrent"]
        if not _in_schedule_window(cfg):
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
                provider, key = _active_debrid(cfg)
                if not provider or not key:
                    # Config changed since the task was added — don't silently start.
                    t["status"] = "error"
                    t["error"] = "Debrid provider is no longer configured (set it in Settings)"
                    _save_tasks()
                    _notify_status_change(t, "waiting", "error")
                    continue
                if t.get("kind") == "debrid_link" and provider != "alldebrid":
                    t["status"] = "error"
                    t["error"] = "Debrid link downloads require AllDebrid as the active provider"
                    _save_tasks()
                    _notify_status_change(t, "waiting", "error")
                    continue
                if t.get("provider") and t["provider"] != provider and int(t.get("_debrid_id") or 0):
                    # Provider changed while the task was paused — the old torrent ID is meaningless on the new provider.
                    t["_debrid_id"] = 0
                t["provider"] = provider
                t["_debrid_key"] = key
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


def _remember(bucket: set, value: str, cap: int = 5000) -> None:
    """Add to a dedupe set while keeping it bounded (the watch loop is long-lived)."""
    bucket.add(value)
    if len(bucket) > cap:
        for extra in list(bucket)[: len(bucket) - cap]:
            bucket.discard(extra)

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
    if not wf or not os.path.isdir(wf):
        return
    provider, _key = _active_debrid(cfg)
    if not provider:  # nothing to do — leave files for a later pass
        return
    # A blank per-entry destination means "save into the watched folder itself"
    # (qBittorrent-style), not the global default download dir.
    dest_dir = (entry.get("destination") or "").strip() or wf
    try:
        entries = sorted(os.listdir(wf))
    except OSError as exc:
        logger.warning("Watch folder unreadable (%s): %s", wf, exc)
        return
    for fname in entries:
        if not fname.lower().endswith(".torrent"):
            continue
        fpath = os.path.join(wf, fname)
        try:
            with open(fpath, "rb") as fh:
                data = fh.read()
            info = _parse_torrent(data)
        except (ValueError, OSError) as exc:
            if fname not in _failed_torrents:
                logger.warning("Watch folder: skipping %s (%s)", fname, exc)
                _remember(_failed_torrents, fname)
            continue
        if info["hash"] in _seen_torrents:
            continue
        name = os.path.basename(info["name"].replace("\\", "/")).strip() or f"torrent-{info['hash'][:12]}"
        task = _make_debrid_task(dest_dir, info["magnet"], name, size=info["size"])
        # Hold the lock across append+persist so a concurrent save cannot drop
        # the new task. Use the UNLOCKED writer here: threading.Lock is not
        # reentrant, and _save_tasks() would deadlock this thread while still
        # holding the lock (which then wedges every later save on the loop).
        with _save_lock:
            _tasks.append(task)
            _remember(_seen_torrents, info["hash"])
            _write_tasks_file()
        # Move out of the scan root so it isn't re-queued on restart.
        try:
            proc = os.path.join(wf, ".processed")
            os.makedirs(proc, exist_ok=True)
            dest = os.path.join(proc, fname)
            if os.path.exists(dest):
                base, ext = os.path.splitext(fname)
                n = 1
                while os.path.exists(os.path.join(proc, f"{base}.{n}{ext}")):
                    n += 1
                dest = os.path.join(proc, f"{base}.{n}{ext}")
            shutil.move(fpath, dest)
        except OSError as exc:
            logger.warning("Watch folder: could not move %s to .processed (%s)", fname, exc)
        logger.info("Watch folder: queued %s (%s)", fname, info["hash"][:12])
        _schedule_pump(loop)


def _watch_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Background thread: poll the watch folder every few seconds."""
    while not _watch_stop.is_set():
        try:
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
# A background loop polls each enabled feed at its own interval, matches items
# against an include/exclude regex and queues the resulting magnet/.torrent as a
# normal Downloads task. Seen item ids are persisted (bounded) so a
# restart does not re-queue the same release.

RSS_SEEN_FILE = DATA_DIR / "downloads_rss_seen.json"
_RSS_SEEN_STORE = json_store.get(RSS_SEEN_FILE, list)
_RSS_SEEN_CACHE: set = set(_RSS_SEEN_STORE.load() or [])
_RSS_SEEN_MAX = 5000
_rss_last_run: dict = {}
_rss_seen_lock = threading.Lock()
_rss_scan_lock = asyncio.Lock()


def _rss_mark_seen(keys: list) -> None:
    if not keys:
        return
    with _rss_seen_lock:
        _RSS_SEEN_CACHE.update(keys)
        if len(_RSS_SEEN_CACHE) > _RSS_SEEN_MAX:
            for extra in list(_RSS_SEEN_CACHE)[: len(_RSS_SEEN_CACHE) - _RSS_SEEN_MAX]:
                _RSS_SEEN_CACHE.discard(extra)
        _RSS_SEEN_STORE.save(sorted(_RSS_SEEN_CACHE))


def _fetch_bytes_bounded(url: str, cap: int) -> bytes:
    """GET an http(s) URL with a hard size cap (feeds, .torrent links)."""
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError(f"unsupported URL scheme: {str(url)[:32]}")
    req = urllib.request.Request(url, headers={"User-Agent": "EthOS-NAS-DownloadStation/2.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 — scheme checked above
        data = resp.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"response is larger than {cap // (1024 * 1024)} MiB")
    return data


async def _rss_scan(feed_ids: Optional[set] = None, force: bool = False) -> dict:
    """Poll due feeds and queue matching items. Returns {feeds, queued}."""
    async with _rss_scan_lock:
        cfg = _load_config()
        now = _now()
        checked = 0
        queued = 0
        for feed in cfg.get("rss_feeds") or []:
            if feed_ids is not None and feed.get("id") not in feed_ids:
                continue
            if not feed.get("enabled") or not feed.get("pattern"):
                continue
            last = _rss_last_run.get(feed["id"], 0)
            if not force and now - last < int(feed.get("interval_minutes") or 15) * 60:
                continue
            _rss_last_run[feed["id"]] = now
            checked += 1
            try:
                raw = await asyncio.to_thread(_fetch_bytes_bounded, feed["url"], 2 * 1024 * 1024)
                items = await asyncio.to_thread(rss_service.parse_feed, raw.decode("utf-8", "replace"))
            except Exception as exc:
                logger.warning("RSS feed %s failed: %s", feed["url"], exc)
                continue
            new_keys = []
            for item in items:
                key = str(item.get("id") or "")[:512]
                if not key or key in _RSS_SEEN_CACHE:
                    continue
                new_keys.append(key)
                if not rss_service.matches(item, feed.get("pattern") or "", feed.get("exclude") or ""):
                    continue
                url = item.get("magnet") or item.get("link") or ""
                if not url:
                    continue
                name = (item.get("title") or "rss-download")[:255]
                try:
                    if url.lower().startswith("magnet:"):
                        pass
                    elif url.lower().split("?")[0].endswith(".torrent"):
                        data = await asyncio.to_thread(_fetch_bytes_bounded, url, 10 * 1024 * 1024)
                        info = _parse_torrent(data)
                        url, name = info["magnet"], (item.get("title") or info["name"])[:255]
                    await add_task(AddTaskRequest(
                        url=url,
                        name=name,
                        folder=feed.get("destination") or None,
                        category=feed.get("category") or None,
                    ))
                    queued += 1
                    logger.info("RSS: queued %r from %s", name, feed["id"])
                except Exception as exc:
                    logger.warning("RSS item %r could not be queued: %s", key[:80], exc)
            _rss_mark_seen(new_keys)
        return {"feeds": checked, "queued": queued}


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
    ordered = sorted(_tasks, key=lambda x: x.get("created_at", 0), reverse=True)
    if status:
        ordered = [t for t in ordered if t["status"] == status]
    page = ordered[offset:offset + limit] if limit else ordered[offset:]
    # Queue position reflects the real pump order (priority, then FIFO), which is
    # not the list's created_at sort.
    queue_pos = {t["id"]: i + 1 for i, t in enumerate(_queue_order())}
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
    provider, key = _active_debrid(_load_config())
    return {
        "id": _uid(),
        "url": url,
        "name": name,
        "path": os.path.abspath(os.path.join(target_dir, name)),
        "kind": "debrid",
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

    magnet:? links are sent to the configured debrid provider and streamed
    back once cached there; premium file-hosting links (RapidGator, Mega, …)
    are resolved through AllDebrid's link API when it is the active provider;
    any other http(s) URL runs the built-in HTTP engine.
    """
    raw_url = payload.url.strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="URL is required")

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
        provider, key = _active_debrid(cfg)
        if not provider or not key:
            raise HTTPException(status_code=400, detail="No debrid provider configured — set one in Settings first")
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

    # ── Premium file-hosting link (RapidGator, Mega, …) → debrid task ───────
    url = _validate_url(raw_url)
    _reject_private_url(url)
    provider, key = _active_debrid(cfg)
    if provider == "alldebrid" and key and _is_file_host(url):
        # Route through AllDebrid's premium API (link/infos + link/unlock).
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
    _tasks.append(task)
    _save_tasks()
    await _pump()
    return _public(task)


@router.post("/tasks/torrent")
async def add_torrent(files: list[UploadFile] = File(...), folder: str = ""):
    """Upload one or more .torrent files → parse locally (bencode) → debrid tasks.

    Each torrent is parsed with the stdlib bencode decoder to build an
    equivalent magnet link, then routed through the configured debrid
    provider exactly like a pasted magnet URL would be. Files that fail to
    parse are reported in `errors` without aborting the rest of the batch.
    """
    cfg = _load_config()
    provider, key = _active_debrid(cfg)
    if not provider or not key:
        raise HTTPException(status_code=400, detail="No debrid provider configured — set one in Settings first")

    folder = (folder or "").strip()
    if folder:
        # Absolute path or a subpath of download_dir; role-scoped like add_task.
        if os.path.isabs(folder) or folder.startswith("~"):
            target_dir = path_scope.sanitize(folder)
        else:
            target_dir = path_scope.sanitize(os.path.join(cfg["download_dir"], folder.rstrip("/")))
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
        created.append(_make_debrid_task(target_dir, info["magnet"], name, size=info["size"]))

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


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    t = _find(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Task not found")
    if t["status"] not in ("paused", "error"):
        return _public(t)
    t["status"] = "waiting"
    t["error"] = ""
    t["_retry_count"] = 0
    t["_next_retry_at"] = 0
    # Fresh event for the new worker.
    _events.pop(t["id"], None)
    _save_tasks()
    await _pump()
    return _public(t)


@router.post("/tasks/{task_id}/reset")
async def reset_task(task_id: str):
    """Reset a download: delete the partial file(s) and restart from byte 0.

    Works for every task kind:
    - http        → removes the partial file at task["path"]
    - debrid_link → removes each local copy, re-unlocks on the provider
    - debrid      → removes the downloaded folder (provider-side torrent kept)
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


@router.post("/tasks/pause-all")
async def pause_all():
    n = 0
    for t in _tasks:
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
    for t in _tasks:
        if t["status"] == "paused":
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
        if t["status"] != "error":
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
    for t in _tasks:
        if t["status"] != "error":
            continue
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
        if t["status"] == "completed":
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
    known = {t["id"] for t in _tasks}
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
    """Config with debrid credentials masked (save endpoint ignores masked values).

    The qBittorrent API key is a real credential — it authenticates the whole
    *arr integration. Only admins see it in full; everyone else gets a masked
    hint plus `qb_api_key_set` so the UI can explain why copying is disabled.
    """
    cfg = _load_config()
    db = dict(cfg.get("debrid") or {})
    for k in ("realdebrid_api_token", "alldebrid_api_key", "torbox_api_key"):
        v = db.get(k)
        if isinstance(v, str) and v:
            db[k] = ("****" + v[-4:]) if len(v) > 6 else "****"
    cfg["debrid"] = db

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


@router.post("/config", dependencies=[Depends(require_admin)])
async def save_config(request: Request, payload: dict = Body(...)):
    cfg = _load_config()
    if "max_concurrent" in payload:
        try:
            cfg["max_concurrent"] = max(1, min(16, int(payload["max_concurrent"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_concurrent must be a number between 1 and 16")
    if "download_dir" in payload and payload["download_dir"]:
        # sanitize() applies the non-admin scope (/home, /mnt, /media) and rejects
        # relative-path tricks; admins keep full access.
        p = path_scope.sanitize(str(payload["download_dir"]))
        if not os.path.isdir(p):
            raise HTTPException(status_code=400, detail=f"Download directory does not exist: {p}")
        if not os.access(p, os.W_OK):
            raise HTTPException(status_code=400, detail=f"Download directory is not writable: {p}")
        cfg["download_dir"] = p
    if isinstance(payload.get("auto_download_debrid"), bool):
        cfg["auto_download_debrid"] = payload["auto_download_debrid"]
    if isinstance(payload.get("auto_retry"), bool):
        cfg["auto_retry"] = payload["auto_retry"]
    if "retry_max" in payload:
        try:
            cfg["retry_max"] = max(0, min(10, int(payload["retry_max"])))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="retry_max must be a number between 0 and 10")
    if "retry_delay" in payload:
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
    if isinstance(payload.get("schedule"), dict):
        sched = {**cfg.get("schedule", {}), **payload["schedule"]}
        if _parse_hhmm(sched.get("start")) is None or _parse_hhmm(sched.get("end")) is None:
            raise HTTPException(status_code=400, detail="Schedule start/end must be HH:MM")
        sched["enabled"] = bool(sched.get("enabled"))
        cfg["schedule"] = sched
        _cancel_window_timer()  # a new window may open sooner than the old timer
    if isinstance(payload.get("speed_limit"), dict):
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
    if isinstance(db_in, dict):
        # Merge over the CURRENT config so fields not sent by the UI (e.g. an
        # unchanged API key) are preserved; masked values ("****…") keep the old one.
        db = {**_default_debrid(), **(cfg.get("debrid") or {})}
        for k in _default_debrid():
            v = db_in.get(k)
            if isinstance(v, str):
                v = v.strip()
                if "****" not in v:
                    db[k] = v
        if db["provider"] not in DEBRID_PROVIDERS:
            db["provider"] = ""
        cfg["debrid"] = db
    _save_config(cfg)
    logger.info("Download config updated by %s",
                (getattr(request.state, "current_user", None) or {}).get("username", "?"))
    return _public_config(admin=True)


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
    """Check a debrid provider API key against the provider's user endpoint."""
    provider = payload.provider
    key = payload.key.strip()
    if provider not in DEBRID_PROVIDERS:
        raise HTTPException(status_code=400, detail="Unknown debrid provider")
    if "****" in key:
        # UI sends the masked value when the user hasn't re-typed the key —
        # verify against the stored one instead.
        field = {"realdebrid": "realdebrid_api_token", "alldebrid": "alldebrid_api_key", "torbox": "torbox_api_key"}[provider]
        key = str((_load_config().get("debrid") or {}).get(field) or "").strip()
        logger.info("%s verify: using the saved key (masked value sent by the UI)", provider)
    else:
        key = "".join(key.split())  # RD/AD tokens are pasted with stray newlines/spaces
    if not key:
        raise HTTPException(status_code=400, detail="API key is required")
    try:
        ok = await asyncio.to_thread(_debrid_verify, provider, key)
    except _ProviderHTTPError as exc:
        # A provider saying "bad token" is a key problem (401), not an outage (502).
        logger.warning("%s verify rejected the key (HTTP %s): %s", provider, exc.status, exc.detail)
        if exc.status in (401, 403):
            raise HTTPException(
                status_code=401,
                detail=f"{provider} rejected the API key: {exc.detail or 'not authorised'}",
            ) from exc
        if exc.status == 429:
            raise HTTPException(status_code=429,
                                detail=f"{provider} rate-limited the check — try again in a minute") from exc
        raise HTTPException(status_code=502,
                            detail=f"{provider} API error (HTTP {exc.status}): {exc.detail or 'no detail'}") from exc
    except Exception as exc:
        # Distinguish "provider is down" from "your key is wrong" — the old
        # blanket exception handler reported outages as an expired key.
        logger.warning("%s verify failed: %s", provider, exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not reach {provider}: {exc}") from exc
    if not ok:
        raise HTTPException(status_code=401, detail=f"{provider} API key is invalid or expired")
    return {"ok": True, "provider": provider}


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
    counts = {"waiting": 0, "downloading": 0, "paused": 0, "completed": 0, "error": 0, "canceled": 0}
    for t in _tasks:
        s = "downloading" if t["status"] == "debrid_downloading" else t["status"]
        counts[s] = counts.get(s, 0) + 1
    active_speed = sum(int(t.get("speed") or 0) for t in _tasks
                       if t["status"] in ("downloading", "debrid_downloading"))
    return {
        "total": len(_tasks),
        "active": len(_active),
        "counts": counts,
        "active_speed": active_speed,
        "max_concurrent": _load_config()["max_concurrent"],
    }
