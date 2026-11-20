"""qBittorrent API compatibility layer for the Downloads engine.

Lets Radarr / Sonarr (and any qBittorrent client) drive the EthOS NAS
Download. The qBittorrent API is a simple REST + form-encoded
interface with a SID-cookie auth flow; Radarr/Sonarr speak it natively
as a "qBittorrent" download client.

Auth: POST /qb/auth/login with form fields `username` / `password`.
The password is the `qb_api_key` from downloads_config.json (auto-generated
on first use). A random SID is issued as a cookie; all subsequent /qb/*
requests must carry it.
"""

import asyncio
import base64
import logging
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("ethos-nas.services.qb_compat")

router = APIRouter()

# ── Session store ──────────────────────────────────────────────────────────────
_qb_sessions: dict = {}  # sid -> expiry timestamp


def _qb_check_auth(request: Request) -> bool:
    """Validate the SID (cookie, ?sid= query param, or ?token=ethos_…) on a qBittorrent request."""
    _prune_sessions()
    # 1. SID cookie
    sid = request.cookies.get("SID", "")
    if sid and sid in _qb_sessions:
        if _qb_sessions[sid] > time.time():
            return True
        del _qb_sessions[sid]
    # 2. SID as query param (some qBittorrent clients send ?sid=…)
    sid_q = request.query_params.get("sid", "")
    if sid_q and sid_q in _qb_sessions:
        if _qb_sessions[sid_q] > time.time():
            return True
        del _qb_sessions[sid_q]
    # 3. Fallback: ethos API key via query param
    tok = request.query_params.get("token", "")
    if tok:
        try:
            from backend.routers.auth_pam import find_user_by_api_key
            if find_user_by_api_key(tok):
                return True
        except Exception:
            logger.warning("qBittorrent auth: api-key lookup failed", exc_info=True)
    return False


def _require_auth(request: Request) -> None:
    if not _qb_check_auth(request):
        raise HTTPException(status_code=403, detail="Authentication required")


def _prune_sessions() -> None:
    """Drop expired SID sessions and cap the store so it cannot grow forever."""
    now = time.time()
    for sid, exp in list(_qb_sessions.items()):
        if exp <= now:
            _qb_sessions.pop(sid, None)
    if len(_qb_sessions) > 100:
        for sid, _exp in sorted(_qb_sessions.items(), key=lambda kv: kv[1])[:len(_qb_sessions) - 100]:
            _qb_sessions.pop(sid, None)


def _parse_form(request_body: bytes) -> dict:
    try:
        return parse_qs(request_body.decode("utf-8", errors="replace"))
    except Exception:
        return {}


def _info_hash(t: dict) -> str:
    """Real BitTorrent info hash for a task (stored value, else derived from its magnet).

    Radarr/Sonarr match grabbed releases against /torrents/info by this hash to show
    download progress — the Downloads task id is not usable for that.
    """
    stored = str(t.get("hash") or "").strip().lower()
    if stored:
        return stored
    m = re.search(r"urn:btih:([a-f0-9]{40,64})", str(t.get("url") or ""), re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return t["id"]


def _resolve_task(h: str):
    """Resolve a qBittorrent 'hash' (info hash or task id) to a Downloads task."""
    from backend.routers.downloads import _tasks
    h = h.strip().lower()
    for t in _tasks:
        if t["id"] == h or _info_hash(t).lower() == h:
            return t
    return None


def _qb_torrent_obj(t: dict) -> dict:
    """Map a Downloads task to a qBittorrent torrent object."""
    from backend.routers.downloads import _progress
    status = t["status"]
    progress = _progress(t)
    speed = int(t.get("speed") or 0)
    size = int(t.get("size") or 0)
    downloaded = int(t.get("downloaded") or 0)

    # States must be valid qBittorrent states — NzbDrone (Radarr/Sonarr) switches on
    # the exact state string and treats unknown values as "downloading" with a warning.
    _STATE_MAP = {
        "waiting": "queuedDL",
        "downloading": "downloading",
        "debrid_downloading": "downloading",
        "debrid_selecting": "metaDL",
        "paused": "pausedDL",
        # A skipped part is excluded from the package: Radarr/Sonarr must not import it.
        "skipped": "pausedDL",
        # NzbDrone maps uploading/stalledUP/… → Completed, which triggers import.
        "completed": "uploading",
        "error": "error",
        "canceled": "pausedUP",
    }
    qbt_state = _STATE_MAP.get(status, "unknown")

    eta = 0
    if 0 < progress < 1.0 and speed > 0:
        eta = int((size - downloaded) / speed)
    elif progress < 1.0:
        eta = 86400  # default 24h

    return {
        "hash": _info_hash(t),
        "name": t.get("name", ""),
        "state": qbt_state,
        "size": size,
        "completed": downloaded,
        "progress": progress,
        "dlspeed": speed,
        "upspeed": 0,
        "eta": eta,
        # NzbDrone's model expects the official qBittorrent v2 snake_case names.
        "save_path": t.get("path", ""),
        "content_path": t.get("path", ""),
        "category": t.get("_qb_category", ""),
        "tags": [t.get("_qb_category", "")] if t.get("_qb_category") else [],
        "uploadLimit": 0,
        "downloadLimit": 0,
        "upspeedLimit": 0,
        "dlspeedLimit": 0,
        "additionDate": int(t.get("created_at", 0)),
        "completionDate": int(t.get("finished_at", 0)),
        "ratio": min(10.0, progress),
        "ratio_limit": -1,
        "seeding_time": 0,
        "seeding_time_limit": -1,
        "inactive_seeding_time_limit": -1,
        "last_activity": int(t.get("finished_at") or t.get("created_at") or 0),
        "seeds": 0 if progress < 1.0 else 1,
        "leechs": 0,
        "availability": 1.0 if progress >= 1.0 else 0.0,
        "infoHash": _info_hash(t),
        "magnet": t.get("url", "") if t.get("url", "").startswith("magnet:") else "",
        "url": t.get("url", ""),
        "error": t.get("error", ""),
    }


# ── qBittorrent Endpoints ──────────────────────────────────────────────────────
# Radarr/Sonarr's QBittorrent client tries the v2 API first (GET /api/v2/…),
# then falls back to v1 (GET /api/…). We mount the router at /api/downloads/qb/api
# so the v1 paths land at /api/downloads/qb/api/… . For v2, we register the
# same handlers under a /v2/ prefix on a second router (mounted separately).

@router.get("/app/version")
async def qb_app_version():
    """qBittorrent: return the app version string (no auth required)."""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content="4.3.5")


@router.get("/app/webapiVersion")
async def qb_webapi_version():
    """qBittorrent v2: return the web API version as plain text.

    NzbDrone/Radarr checks this to determine if the v2 API is supported.
    Must be plain text (not JSON) — NzbDrone parses it as a number.
    """
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content="2.0")


@router.get("/app/preferences")
async def qb_app_preferences(request: Request):
    """qBittorrent: return app preferences.

    NzbDrone calls this after login to get qBittorrent settings.
    Returns a minimal set of fields that NzbDrone expects.
    """
    _require_auth(request)

    from backend.routers.downloads import _load_config
    cfg = _load_config()
    dl_dir = cfg.get("download_dir", str(Path.home() / "Downloads"))
    per_task_limit = int((cfg.get("speed_limit") or {}).get("per_task_bps") or 0)
    sched = cfg.get("schedule") or {}

    return {
        "download_path": dl_dir,
        "save_path": dl_dir,
        "temp_path": "",
        "temp_path_enabled": False,
        "category_paths_enabled": False,
        "max_active_downloads": -1,
        "max_active_global": 16,
        "max_active_per_tracker": 0,
        "max_ratios": -1,
        "max_ratio": -1,
        "max_seeding_time": -1,
        "max_upload_speed": 0,
        "max_download_speed": per_task_limit,
        "scheduler_enabled": bool(sched.get("enabled")),
        "start_paused_enabled": False,
        "max_active_seeds": -1,
        "max_upload_disk_usage": 0,
        "max_download_disk_usage": 0,
        "auto_tmm_enabled": False,
        "default_save_path": dl_dir,
        "create_subfolder_enabled": True,
        "torrent_content_layout": 0,
        # NzbDrone rejects magnet links without trackers when DHT is disabled; the
        # debrid engine doesn't use BitTorrent networking at all, so report it on.
        "dht": True,
    }


@router.post("/auth/login")
async def qb_auth_login(request: Request):
    """qBittorrent: authenticate. Form fields: username, password.

    Returns a 200 with a SID cookie on success, 403 on failure.
    """
    body = await request.body()
    # Never log the body: it is the login form and contains the password.
    logger.info("QB login: ct=%s body_len=%d ua=%s",
                request.headers.get("content-type", ""),
                len(body),
                request.headers.get("user-agent", ""))
    form = _parse_form(body)
    password = (form.get("password", [""])[0]).strip()

    from backend.routers.downloads import _qb_get_api_key
    expected_key = _qb_get_api_key()

    if not password or not secrets.compare_digest(password, expected_key):
        return JSONResponse(
            status_code=403,
            content={"error": "Fails to authenticate.", "message": "Invalid username or password"},
        )

    sid = secrets.token_hex(32)
    _qb_sessions[sid] = time.time() + 86400 * 7  # 7-day session
    logger.info("qBittorrent session issued (sid=%s…)", sid[:8])

    # qBittorrent v2 returns an empty body and sets the SID via Set-Cookie.
    # NzbDrone uses a .NET CookieContainer which should handle this automatically.
    from fastapi.responses import Response
    resp = Response(content="")
    # No SameSite attribute — .NET HttpClient's CookieContainer may reject
    # SameSite=lax cookies for cross-origin requests. qBittorrent itself
    # doesn't set SameSite on the SID cookie.
    resp.set_cookie("SID", sid, max_age=86400 * 7, httponly=True)
    return resp


@router.post("/auth/logout")
async def qb_auth_logout(request: Request):
    """qBittorrent: clear the SID session."""
    sid = request.cookies.get("SID", "")
    if sid in _qb_sessions:
        del _qb_sessions[sid]
    resp = JSONResponse(content={"message": "Ok"})
    resp.delete_cookie("SID")
    return resp


async def _fetch_torrent_bytes(url: str) -> bytes:
    """Fetch a .torrent file from an indexer URL (blocking urllib → thread)."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"unsupported URL scheme: {url[:32]}")
    import urllib.request as _ur

    def _get():
        # url scheme validated to http(s) at function entry (guard above)
        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) qBittorrent/5.2"})  # nosec B310
        with _ur.urlopen(req, timeout=30) as resp:  # nosec B310
            # Bounded read: an indexer or a redirect to a huge file must not
            # exhaust memory. 10 MiB is far above any real .torrent.
            return resp.read(10 * 1024 * 1024 + 1)

    data = await asyncio.to_thread(_get)
    if not data or len(data) < 20:
        raise ValueError("empty torrent response")
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("torrent response is larger than 10 MiB")
    return data


def _safe_category_dirname(category: str) -> str:
    """Category name → safe subdirectory name (qBittorrent-style category paths)."""
    name = os.path.basename(str(category or "").strip().replace("\\", "/"))
    return re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")


@router.post("/torrents/add")
async def qb_torrents_add(request: Request):
    """qBittorrent v2: add a torrent (urlencoded or multipart form).

    Fields: urls (.torrent URL or magnet — what Radarr/Sonarr send),
    magnet/magnetUri, torrents (base64 .torrent data or file upload),
    savePath, category.
    """
    _require_auth(request)

    from backend.routers.downloads import AddTaskRequest, _load_config, _parse_torrent, _save_tasks, _tasks, add_task

    await request.body()  # cache raw body so form parsing can re-read it
    try:
        form = await request.form()
    except Exception as exc:
        logger.warning("qBittorrent add: form parse failed (%s), falling back to urlencoded", exc)
        form = {k: v[0] for k, v in _parse_form(await request.body()).items()}

    def field(name):
        """First value of a form field as str ('' if absent)."""
        v = form.get(name)
        if v is None:
            return ""
        return str(v).strip()

    urls_val = field("urls") or field("magnet") or field("magnetUri")
    save_path = field("savePath")
    category = field("category")

    magnet, name_hint = "", ""
    if urls_val:
        if urls_val.startswith("magnet:"):
            magnet = urls_val
        elif urls_val.lower().startswith(("http://", "https://")):
            # Radarr sends the indexer's .torrent URL — fetch it and convert to a magnet.
            try:
                data = await _fetch_torrent_bytes(urls_val)
                info = _parse_torrent(data)
            except Exception as exc:
                logger.error("qBittorrent add: failed to fetch/parse torrent url %s: %s", urls_val, exc)
                raise HTTPException(status_code=400, detail=f"Could not fetch torrent from url: {exc}") from exc
            magnet, name_hint = info["magnet"], info.get("name", "")
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported urls value (expected magnet or http(s) .torrent URL): {urls_val[:80]}")

    if not magnet:
        torrents_val = field("torrents")  # base64 string in urlencoded forms
        if torrents_val:
            try:
                info = _parse_torrent(base64.b64decode(torrents_val))
                magnet, name_hint = info["magnet"], info.get("name", "")
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Could not parse torrent data: {exc}") from exc

    if not magnet:
        # Multipart file upload (real qBittorrent web UI sends a 'torrents' file part).
        from starlette.datastructures import UploadFile
        f = form.get("torrents")
        if isinstance(f, UploadFile):
            try:
                info = _parse_torrent(await f.read())
                magnet, name_hint = info["magnet"], info.get("name", "")
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Could not parse uploaded torrent file: {exc}") from exc

    if not magnet:
        raise HTTPException(status_code=400, detail="No magnet or torrent provided")

    # Category-based routing (qBittorrent-style category paths): *arr clients send a
    # category but no savePath, so route each category into its own subdirectory of
    # the configured download dir — Radarr "movies" → <download_dir>/movies,
    # Sonarr "tv" → <download_dir>/tv. An explicit savePath always wins, but it
    # is confined to the download dir: this endpoint is authenticated by an API
    # key, not by a user, so it must not become an arbitrary-write primitive.
    if save_path:
        dl_dir = os.path.abspath(str(_load_config().get("download_dir") or ""))
        if os.path.isabs(save_path) or save_path.startswith("~"):
            sp = os.path.abspath(os.path.expanduser(save_path))
            if not dl_dir or not (sp == dl_dir or sp.startswith(dl_dir + os.sep)):
                raise HTTPException(
                    status_code=403,
                    detail="savePath must be inside the configured download directory",
                )
            save_path = sp
    if not save_path and category:
        # A configured category save_path wins over the implicit <download_dir>/<name>.
        from backend.routers.downloads import _category_target
        save_path = _category_target(_load_config(), category)
    if not save_path:
        safe_cat = _safe_category_dirname(category)
        if safe_cat:
            save_path = safe_cat  # relative — add_task() joins it under download_dir

    try:
        payload = {"url": magnet, "folder": save_path or "", "category": category}
        if name_hint:
            payload["name"] = str(name_hint)[:255]
        # add_task() takes a validated model; calling it with a bare dict raised
        # AttributeError and turned every Radarr/Sonarr grab into a 500.
        task = await add_task(AddTaskRequest(**payload))

        # Store the real info hash on the task — Radarr matches grabbed releases
        # against /torrents/info by this hash to show download progress.
        for t in _tasks:
            if t["id"] == task["id"]:
                m = re.search(r"urn:btih:([a-f0-9]{40,64})", magnet, re.IGNORECASE)
                if m and not t.get("hash"):
                    t["hash"] = m.group(1).lower()
                if category:
                    t["_qb_category"] = category
                break
        _save_tasks()

        logger.info("qBittorrent: added %s (%s) to %s [%s]",
                    task.get("name", "?"), task["id"], save_path or "default", category or "no category")
        return {"added": True, "id": task["id"], "name": task.get("name", "")}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("qBittorrent add failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/torrents/info")
async def qb_torrents_info(request: Request, hash: str = "", hashes: str = "", category: str = ""):
    """qBittorrent: list torrents. Filters: hash/hashes (comma-separated), category.

    Radarr/NzbDrone polls with the plural `hashes` param — both are supported.
    """
    _require_auth(request)

    from backend.routers.downloads import _tasks
    tasks = list(_tasks)
    wanted = {h.strip().lower() for h in (hash + "," + hashes).split(",") if h.strip()}
    if wanted:
        tasks = [t for t in tasks if t["id"].lower() in wanted or _info_hash(t) in wanted]
    if category:
        tasks = [t for t in tasks if t.get("_qb_category") == category]
    return [_qb_torrent_obj(t) for t in tasks]


def _task_content(t: dict):
    """(root_dir, [file entries]) for a task's files.

    Mirrors where the debrid engine writes files (phase 3 of downloads.py):
    magnet tasks land in <base>/<torrent-name>/…, link tasks directly under their
    target dir. A file's `dest` wins once set; otherwise the layout is derived.
    """
    from backend.routers.downloads import _load_config

    cfg = _load_config()
    selected = [f for f in (t.get("_files") or []) if f.get("is_selected")] or list(t.get("_files") or [])
    raw_path = str(t.get("path") or "").strip()
    base_dir = os.path.dirname(os.path.abspath(raw_path)) if raw_path else os.path.abspath(cfg.get("download_dir", ""))

    entries, dirs = [], []
    for f in selected:
        d = str(f.get("dest") or "").strip()
        if not d:
            fname = os.path.basename(str(f.get("name") or "")) or "download.bin"
            rel = str(f.get("path") or "").strip("/")
            d = os.path.join(base_dir, t.get("name", ""), rel or fname)
        entries.append((d, int(f.get("size") or 0)))
        dirs.append(os.path.dirname(d))

    root = os.path.commonpath(dirs) if dirs else (raw_path or cfg.get("download_dir", ""))
    files = [{"name": os.path.relpath(d, root).replace(os.sep, "/"), "size": size, "progress": 1.0}
             for d, size in entries]
    return root, files

@router.get("/torrents/properties")
async def qb_torrents_properties(request: Request, hash: str = ""):
    """qBittorrent: single-torrent properties.

    NzbDrone uses this to check a torrent is loaded (IsTorrentLoaded) and to
    resolve the import path after completion — without it, Radarr imports fail.
    """
    _require_auth(request)

    t = _resolve_task(hash)
    if not t:
        raise HTTPException(status_code=404, detail="Torrent not found")
    root, _files = _task_content(t)
    return {"hash": _info_hash(t), "save_path": root, "seeding_time": 0}

@router.get("/torrents/files")
async def qb_torrents_files(request: Request, hash: str = ""):
    """qBittorrent: file list of one torrent (names relative to the content dir)."""
    _require_auth(request)

    t = _resolve_task(hash)
    if not t:
        raise HTTPException(status_code=404, detail="Torrent not found")
    _root, files = _task_content(t)
    return files

@router.post("/torrents/setCategory")
async def qb_torrents_set_category(request: Request):
    """qBittorrent: set a torrent's category (NzbDrone uses this for post-import relabeling)."""
    _require_auth(request)

    from fastapi.responses import PlainTextResponse
    from backend.routers.downloads import _save_tasks

    form = await request.form()
    hashes = str(form.get("hashes", ""))
    category = str(form.get("category", ""))
    changed = False
    for h in (x.strip() for x in hashes.split(",")):
        t = _resolve_task(h)
        if t:
            t["_qb_category"] = category
            changed = True
    if changed:
        _save_tasks()
        logger.info("qBittorrent: set category %r on %d torrent(s)", category, len(hashes.split(",")))
    return PlainTextResponse(content="")

@router.post("/torrents/setShareLimits")
async def qb_torrents_set_share_limits(request: Request):
    """qBittorrent: per-torrent share limits (no-op — the debrid engine doesn't seed)."""
    _require_auth(request)

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content="")

@router.post("/torrents/topPrio")
async def qb_torrents_top_prio(request: Request):
    """qBittorrent: move torrent(s) to the top of the queue (priority bump)."""
    _require_auth(request)

    from fastapi.responses import PlainTextResponse
    form = await request.form()
    hashes = str(form.get("hashes", "") or "")
    ids = [h.strip() for h in hashes.replace("\n", ",").split(",") if h.strip()]
    if ids:
        from backend.routers.downloads import _pump, _save_tasks, _tasks
        top = max([int(t.get("priority") or 0) for t in _tasks] or [0]) + 1
        changed = 0
        for h in ids:
            t = _resolve_task(h)
            if t is not None:
                t["priority"] = top
                changed += 1
        if changed:
            _save_tasks()
            await _pump()
            logger.info("qBittorrent: moved %d torrent(s) to the top", changed)
    return PlainTextResponse(content="")

@router.post("/torrents/setForceStart")
async def qb_torrents_set_force_start(request: Request):
    """qBittorrent: force-start torrent(s) (no-op — tasks are always started)."""
    _require_auth(request)

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content="")

@router.post("/torrents/delete")
async def qb_torrents_delete(request: Request):
    """qBittorrent: delete torrent(s). Form field: hashes (comma-separated)."""
    _require_auth(request)

    from backend.routers.downloads import _active, _events, _save_tasks, _tasks

    body = await request.body()
    form = _parse_form(body)
    hashes = (form.get("hashes", [""])[0]).strip()

    if not hashes:
        raise HTTPException(status_code=400, detail="No hashes provided")

    hash_set = {h.strip() for h in hashes.split(",") if h.strip()}
    deleted = 0
    for h in hash_set:
        t = _resolve_task(h)
        if t:
            stop_event = _events.get(t["id"])
            if stop_event:
                stop_event.set()
            if t["id"] in _active:
                _active[t["id"]].cancel()
                del _active[t["id"]]
            _tasks.remove(t)
            _save_tasks()
            logger.info("qBittorrent: deleted task %s (%s)", h, t.get("name", "?"))
            deleted += 1

    return {"deleted": deleted, "total_requested": len(hash_set)}


@router.post("/torrents/pause")
async def qb_torrents_pause(request: Request):
    """qBittorrent: pause torrent(s)."""
    _require_auth(request)

    from backend.routers.downloads import _events, _set_status

    body = await request.body()
    form = _parse_form(body)
    hashes = {h.strip() for h in (form.get("hashes", [""])[0]).split(",") if h.strip()}
    paused = 0
    for h in hashes:
        t = _resolve_task(h)
        if t and t["status"] in ("waiting", "downloading", "debrid_downloading", "debrid_selecting"):
            stop_event = _events.get(t["id"])
            if stop_event:
                stop_event.set()
            _set_status(t, "paused")
            paused += 1
    return {"paused": paused}


@router.post("/torrents/resume")
async def qb_torrents_resume(request: Request):
    """qBittorrent: resume torrent(s)."""
    _require_auth(request)

    from backend.routers.downloads import _pump, _set_status

    body = await request.body()
    form = _parse_form(body)
    hashes = {h.strip() for h in (form.get("hashes", [""])[0]).split(",") if h.strip()}
    resumed = 0
    for h in hashes:
        t = _resolve_task(h)
        if t and t["status"] == "paused":
            _set_status(t, "waiting")
            resumed += 1
            await _pump()
    return {"resumed": resumed}


@router.get("/sync/maindata")
async def qb_sync_maindata(request: Request, rid: str = ""):
    """qBittorrent: sync data (Radarr/Sonarr poll this for torrent state updates)."""
    _require_auth(request)

    from backend.routers.downloads import _tasks
    torrents = [_qb_torrent_obj(t) for t in _tasks]
    active_speed = sum(int(t.get("speed") or 0) for t in _tasks
                       if t["status"] in ("downloading", "debrid_downloading"))
    return {
        "rid": str(int(rid or "0") + 1),
        "torrents": torrents,
        "transfer_info": {
            "download_payload_rate": active_speed,
            "upload_payload_rate": 0,
            "download_rate": active_speed,
            "upload_rate": 0,
        },
        "cache": {},
    }


@router.get("/transfer/info")
async def qb_transfer_info(request: Request):
    """qBittorrent: global transfer info."""
    _require_auth(request)

    from backend.routers.downloads import _tasks
    active_speed = sum(int(t.get("speed") or 0) for t in _tasks
                       if t["status"] in ("downloading", "debrid_downloading"))
    return {
        "download_payload_rate": active_speed,
        "upload_payload_rate": 0,
        "download_rate": active_speed,
        "upload_rate": 0,
    }


# Categories created via createCategory. In-memory only — NzbDrone re-creates
# them on startup if missing (it checks GetCategories before adding torrents).
_created_categories: set = set()


@router.get("/torrents/categories")
async def qb_torrents_categories(request: Request):
    """qBittorrent: list categories with their save paths."""
    _require_auth(request)

    from backend.routers.downloads import _load_config, _tasks
    configured = {str(c.get("name")): str(c.get("save_path") or "")
                  for c in (_load_config().get("categories") or [])}
    implicit = {t.get("_qb_category", "") for t in _tasks if t.get("_qb_category")}
    names = sorted(set(configured) | implicit | _created_categories)
    # Return as a dict (qBittorrent v2 format) — NzbDrone deserializes into
    # a Dictionary<string, QBittorrentLabel>, not a JSON array.
    return {name: {"name": name, "savePath": configured.get(name, "")} for name in names}


@router.post("/torrents/createCategory")
async def qb_torrents_create_category(request: Request):
    """qBittorrent: create/update a category; its savePath is persisted in config."""
    _require_auth(request)

    from fastapi.responses import PlainTextResponse
    from backend.routers.downloads import _load_config, _upsert_category
    form = await request.form()
    category = str(form.get("category", "")).strip()
    save_path = str(form.get("savePath", "") or "").strip()
    if not category:
        return PlainTextResponse(content="Category name cannot be empty")
    if save_path and (os.path.isabs(save_path) or save_path.startswith("~")):
        # This endpoint is API-key authenticated, not user-authenticated: an
        # absolute savePath must stay inside the configured download dir or it
        # would let any qB client write anywhere the service user can.
        dl_dir = os.path.abspath(str(_load_config().get("download_dir") or ""))
        sp = os.path.abspath(os.path.expanduser(save_path))
        if not dl_dir or not (sp == dl_dir or sp.startswith(dl_dir + os.sep)):
            raise HTTPException(
                status_code=403,
                detail="savePath must be inside the configured download directory",
            )
        save_path = sp
    _created_categories.add(category)
    _upsert_category(category, save_path)
    return PlainTextResponse(content="")


@router.delete("/torrents/deleteCategory")
async def qb_torrents_delete_category(request: Request):
    """qBittorrent: delete a category (and its persisted savePath)."""
    _require_auth(request)

    from fastapi.responses import PlainTextResponse
    from backend.routers.downloads import _remove_category
    form = await request.form()
    category = str(form.get("category", "")).strip()
    if not category:
        return PlainTextResponse(content="Category name cannot be empty")
    _created_categories.discard(category)
    _remove_category(category)
    return PlainTextResponse(content="")


# ── v2 Router (same handlers under /v2/ prefix) ───────────────────────────────
# Radarr's QBittorrentProxyV2 calls /api/v2/… first. If we register the same
# handlers under /v2/, both v1 and v2 paths resolve.

v2_router = APIRouter()


@v2_router.get("/app/version")
async def qb_v2_app_version():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content="4.3.5")


@v2_router.get("/app/webapiVersion")
async def qb_v2_webapi_version():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content="2.0")


@v2_router.get("/app/preferences")
async def qb_v2_app_preferences(request: Request):
    return await qb_app_preferences(request)


@v2_router.post("/auth/login")
async def qb_v2_auth_login(request: Request):
    return await qb_auth_login(request)


@v2_router.post("/auth/logout")
async def qb_v2_auth_logout(request: Request):
    return await qb_auth_logout(request)


@v2_router.post("/torrents/add")
async def qb_v2_torrents_add(request: Request):
    return await qb_torrents_add(request)


@v2_router.get("/torrents/info")
async def qb_v2_torrents_info(request: Request, hash: str = "", hashes: str = "", category: str = ""):
    # Radarr polls with `category=movies` — the v1 handler takes (hash, hashes, category),
    # so forward all three explicitly.
    return await qb_torrents_info(request, hash, hashes, category)

@v2_router.get("/torrents/properties")
async def qb_v2_torrents_properties(request: Request, hash: str = ""):
    return await qb_torrents_properties(request, hash)

@v2_router.get("/torrents/files")
async def qb_v2_torrents_files(request: Request, hash: str = ""):
    return await qb_torrents_files(request, hash)

@v2_router.post("/torrents/setCategory")
async def qb_v2_torrents_set_category(request: Request):
    return await qb_torrents_set_category(request)

@v2_router.post("/torrents/setShareLimits")
async def qb_v2_torrents_set_share_limits(request: Request):
    return await qb_torrents_set_share_limits(request)

@v2_router.post("/torrents/topPrio")
async def qb_v2_torrents_top_prio(request: Request):
    return await qb_torrents_top_prio(request)

@v2_router.post("/torrents/setForceStart")
async def qb_v2_torrents_set_force_start(request: Request):
    return await qb_torrents_set_force_start(request)


@v2_router.post("/torrents/delete")
async def qb_v2_torrents_delete(request: Request):
    return await qb_torrents_delete(request)


@v2_router.post("/torrents/pause")
async def qb_v2_torrents_pause(request: Request):
    return await qb_torrents_pause(request)


@v2_router.post("/torrents/resume")
async def qb_v2_torrents_resume(request: Request):
    return await qb_torrents_resume(request)


@v2_router.get("/sync/maindata")
async def qb_v2_sync_maindata(request: Request, rid: str = ""):
    return await qb_sync_maindata(request, rid)


@v2_router.get("/transfer/info")
async def qb_v2_transfer_info(request: Request):
    return await qb_transfer_info(request)


@v2_router.get("/torrents/categories")
async def qb_v2_torrents_categories(request: Request):
    return await qb_torrents_categories(request)


@v2_router.post("/torrents/createCategory")
async def qb_v2_torrents_create_category(request: Request):
    return await qb_torrents_create_category(request)


@v2_router.delete("/torrents/deleteCategory")
async def qb_v2_torrents_delete_category(request: Request):
    return await qb_torrents_delete_category(request)
