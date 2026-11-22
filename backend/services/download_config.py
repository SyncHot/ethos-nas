"""Downloads configuration: defaults, migration, normalization and I/O.

Extracted from `backend/routers/downloads.py` (TODO.md P2-01). The router and
the qBittorrent shim both use these functions; the store is `json_store`, so a
corrupt config is quarantined and a newer schema is never overwritten.
"""

import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Optional

from backend.config import settings
from backend.services import json_store, path_scope, user_data
from backend.services.download_providers import (
    CAP_LINK_UNLOCK,
    CAP_TORRENTS,
    ids_by_capability,
    link_provider,
    primary_secret,
    provider_credentials,
    torrent_provider,
)

logger = logging.getLogger("ethos-nas.services.download_config")

# Torrent-capable providers (legacy name kept for importers); hoster-link
# providers are tracked separately since config v9.
DEBRID_PROVIDERS = ids_by_capability(CAP_TORRENTS)
LINK_PROVIDERS = ids_by_capability(CAP_LINK_UNLOCK)

DATA_DIR = settings.DATA_DIR
CONFIG_FILE = DATA_DIR / "downloads_config.json"

DEFAULT_CONFIG = {
    "download_dir": str(Path.home() / "Downloads"),
    "max_concurrent": 3,
    "auto_download_debrid": True,   # auto-pull cached debrid files to the NAS
    "auto_retry": True,            # re-queue transient failures with backoff
    "retry_max": 3,                # retries after the first failure (0 = never)
    "retry_delay": 60,             # base seconds; doubled per attempt
    "duplicate_policy": "rename",  # rename | skip | overwrite when the target file exists
    "auto_extract": False,          # unpack completed archives next to the download
    "auto_extract_delete": False,   # remove archives after a successful extraction
    "watch_folder": "",             # legacy single watch folder — migrated into watch_folders on load
    "watch_enabled": False,         # master switch for the watch-folder scanners
    "watch_folders": [              # watched dirs; entry: {name, folder, destination}
        {"name": "Default", "folder": "", "destination": ""},  # "" destination = save into the watched folder itself
    ],
    "schedule": {                   # only start transfers inside this daily window
        "enabled": False,
        "start": "01:00",           # HH:MM, local time
        "end": "06:00",             # may be earlier than start → overnight window
    },
    "speed_limit": {                # 0 = unlimited
        "global_bps": 0,
        "per_task_bps": 0,
    },
    "package_limits": {},           # {package_key: bps} — a shared cap for one multi-part package
    "categories": [],               # [{name, save_path}] — "" = <download_dir>/<name>
    "rss_feeds": [],                # [{id,url,enabled,pattern,exclude,category,destination,interval_minutes}]
    "debrid": {
        "provider": "",             # torrent slot: "" | realdebrid | alldebrid | torbox
        "link_provider": "",        # hoster-link slot: "" | alldebrid | tb7
        "realdebrid_api_token": "",
        "alldebrid_api_key": "",
        "torbox_api_key": "",
        "tb7_username": "",
        "tb7_password": "",
    },
}


def _migrate_config(from_version: int, to_version: int, data: dict) -> dict:
    """v1 → v2: fold the legacy single `watch_folder` into `watch_folders`.

    The rename used to happen on every read (a silent in-memory patch); doing it
    once, here, means the file on disk actually converges and the reader stays a
    plain merge (TODO.md P2-09).
    """
    if from_version < 2:
        legacy = str(data.pop("watch_folder", "") or "").strip()
        folders = data.get("watch_folders")
        folders = folders if isinstance(folders, list) else []
        if legacy:
            expanded = os.path.abspath(os.path.expanduser(legacy))
            if not any(isinstance(e, dict) and e.get("folder") == expanded for e in folders):
                folders = [e for e in folders if isinstance(e, dict) and e.get("folder")]
                folders.insert(0, {"name": "Default", "folder": expanded, "destination": ""})
        data["watch_folders"] = folders
    if from_version < 3:
        data.setdefault("schedule", json.loads(json.dumps(DEFAULT_CONFIG["schedule"])))
        data.setdefault("speed_limit", json.loads(json.dumps(DEFAULT_CONFIG["speed_limit"])))
    if from_version < 4:
        data.setdefault("categories", [])
    if from_version < 5:
        data.setdefault("rss_feeds", [])
    if from_version < 6:
        data.setdefault("auto_retry", True)
        data.setdefault("retry_max", 3)
        data.setdefault("retry_delay", 60)
        data.setdefault("duplicate_policy", "rename")
    if from_version < 7:
        data.setdefault("auto_extract", False)
        data.setdefault("auto_extract_delete", False)
    if from_version < 8:
        data.setdefault("package_limits", {})
    if from_version < 9:
        # Two independent provider slots: AllDebrid used to be the only
        # hoster-link provider, so keep its previous routing behaviour.
        db = data.get("debrid")
        db = dict(db) if isinstance(db, dict) else {}
        db.setdefault("link_provider", "alldebrid" if db.get("provider") == "alldebrid" else "")
        db.setdefault("tb7_username", "")
        db.setdefault("tb7_password", "")
        data["debrid"] = db
    return data


# Fields that belong to the NAS-wide engine and may only be changed by an
# admin. Everything else is stored in the caller's per-user slice.
ENGINE_FIELDS = (
    "max_concurrent", "auto_download_debrid", "auto_retry", "retry_max",
    "retry_delay", "schedule", "speed_limit", "package_limits", "debrid",
    "qb_api_key",
)
USER_FIELDS = (
    "download_dir", "categories", "watch_enabled", "watch_folders",
    "rss_feeds", "duplicate_policy", "auto_extract", "auto_extract_delete",
)


def _default_download_dir() -> str:
    """<user home>/Downloads — inside the account's own path scope."""
    home = path_scope.home_dir(user_data.current_username()) or str(Path.home())
    return str(Path(home) / "Downloads")


_CONFIG_STORE = json_store.get(CONFIG_FILE, version=9, migrate=_migrate_config)


def parse_hhmm(value) -> Optional[int]:
    """'HH:MM' → minutes since midnight, or None when malformed."""
    try:
        hour, minute = str(value).split(":", 1)
        hour, minute = int(hour), int(minute)
    except (ValueError, AttributeError):
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def default_debrid() -> dict:
    return json.loads(json.dumps(DEFAULT_CONFIG["debrid"]))


def norm_categories(raw) -> list:
    """Normalize the category list: unique names, bounded length, string paths."""
    out: list[dict] = []
    seen: set = set()
    for item in (raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:64]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "save_path": str(item.get("save_path") or "").strip()[:4096]})
        if len(out) >= 50:
            break
    return out


def category_target(cfg: dict, name: str) -> str:
    """Destination directory for a category ("" when unknown or no save_path)."""
    wanted = str(name or "").strip().lower()
    if not wanted:
        return ""
    for cat in cfg.get("categories") or []:
        if str(cat.get("name") or "").lower() != wanted:
            continue
        path = str(cat.get("save_path") or "").strip()
        if not path:
            return ""
        if os.path.isabs(path) or path.startswith("~"):
            return path_scope.sanitize(path)
        return path_scope.sanitize(os.path.join(cfg["download_dir"], path))
    return ""


def upsert_category(name: str, save_path: str = "", view: "str | None" = None) -> list:
    """Create/replace a category (used by the qBittorrent shim).

    ``view`` writes into another identity's slice — the *arr API key is not an
    account, so its categories belong to the account that maintains the queue.
    """
    if view and view != user_data.current_username():
        with path_scope.system_scope(view):
            return upsert_category(name, save_path)
    cfg = load_config()
    cats = [c for c in (cfg.get("categories") or [])
            if str(c.get("name") or "").lower() != str(name).strip().lower()]
    cats.append({"name": name, "save_path": save_path})
    cfg["categories"] = norm_categories(cats)
    save_config(cfg)
    return cfg["categories"]


def remove_category(name: str, view: "str | None" = None) -> list:
    if view and view != user_data.current_username():
        with path_scope.system_scope(view):
            return remove_category(name)
    cfg = load_config()
    cfg["categories"] = [c for c in (cfg.get("categories") or [])
                         if str(c.get("name") or "").lower() != str(name).strip().lower()]
    save_config(cfg)
    return cfg["categories"]


def norm_rss_feeds(raw) -> list:
    """Normalize RSS rules: http(s) URLs only, unique ids, bounded intervals."""
    out: list[dict] = []
    seen: set = set()
    for item in (raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()[:4096]
        if not url.lower().startswith(("http://", "https://")):
            continue
        feed_id = str(item.get("id") or "").strip()[:40] or f"rss{len(out) + 1}"
        if feed_id in seen:
            continue
        seen.add(feed_id)
        try:
            interval = max(5, min(1440, int(item.get("interval_minutes") or 15)))
        except (TypeError, ValueError):
            interval = 15
        out.append({
            "id": feed_id,
            "url": url,
            "enabled": bool(item.get("enabled")),
            "pattern": str(item.get("pattern") or "").strip()[:512],
            "exclude": str(item.get("exclude") or "").strip()[:512],
            "category": str(item.get("category") or "").strip()[:64],
            "destination": str(item.get("destination") or "").strip()[:4096],
            "interval_minutes": interval,
        })
        if len(out) >= 20:
            break
    return out


def norm_watch_entry(e) -> Optional[dict]:
    """Normalize one watch-folder entry; None when it is not a usable dict."""
    if not isinstance(e, dict):
        return None
    name = str(e.get("name") or "").strip()[:40] or "Watch"
    folder = str(e.get("folder") or "").strip()
    dest = str(e.get("destination") or "").strip()
    return {
        "name": name,
        "folder": os.path.abspath(os.path.expanduser(folder)) if folder else "",
        "destination": os.path.abspath(os.path.expanduser(dest)) if dest else "",
    }


def norm_watch_folders(cfg: dict) -> None:
    """Normalize cfg["watch_folders"] in place; migrate the legacy single watch_folder."""
    raw = cfg.get("watch_folders")
    entries = [e for e in (norm_watch_entry(x) for x in (raw if isinstance(raw, list) else [])) if e]
    legacy = str(cfg.get("watch_folder") or "").strip()
    if legacy:
        lp = os.path.abspath(os.path.expanduser(legacy))
        if not any(e["folder"] == lp for e in entries):
            # Legacy value wins over empty placeholder rows.
            entries = [e for e in entries if e["folder"]]
            entries.insert(0, {"name": "Default", "folder": lp, "destination": ""})
    cfg["watch_folders"] = (entries or [{"name": "Default", "folder": "", "destination": ""}])[:8]


PACKAGE_LIMIT_MAX = 10 * 1000 * 1000 * 1000  # 10 Gbit/s is "effectively unlimited"
PACKAGE_LIMIT_ENTRIES = 50


def norm_package_limits(raw) -> dict:
    """Normalize `package_limits`: {12-hex package_key: bytes/s}, bounded.

    An entry with a 0/negative/absurd value is dropped instead of storing a
    no-op — the key only exists while a limit is actually in force.
    """
    out: dict = {}
    for key, value in (raw.items() if isinstance(raw, dict) else []):
        name = str(key or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{6,32}", name):
            continue
        try:
            bps = int(value or 0)
        except (TypeError, ValueError):
            continue
        if bps <= 0:
            continue
        out[name] = min(PACKAGE_LIMIT_MAX, bps)
        if len(out) >= PACKAGE_LIMIT_ENTRIES:
            break
    return out


def load_config(view: "str | None" = None) -> dict:
    """Merged download config for the calling identity.

    ``view`` reads the config as another identity. The qBittorrent shim needs
    it: Radarr/Sonarr authenticate with a NAS-wide API key, and that key is
    **not** a real account, so the per-user default would be a phantom
    ``/home/<key-id>/Downloads`` instead of the folder the admin configured in
    Download Station.
    """
    if not view or view == user_data.current_username():
        return _load_config()
    with path_scope.system_scope(view):
        return _load_config()


def _load_config() -> dict:
    try:
        # json_store gives us quarantine-on-corruption, the migration and the
        # downgrade guard; it never raises for a missing/damaged file.
        global_data = _CONFIG_STORE.load()
        if not isinstance(global_data, dict):
            global_data = {}
        username = user_data.current_username()
        if username == "system":
            # Background engine: use the legacy/global view (engine + its own
            # former user fields) so the pump and workers keep running.
            user_slice = global_data
        else:
            raw = user_data.user_store("downloads.json").load()
            user_slice = raw if isinstance(raw, dict) else {}
        merged = {**DEFAULT_CONFIG, **{
            k: v for k, v in global_data.items() if k in ENGINE_FIELDS or username == "system"
        }, **user_slice}
        if not global_data and not user_slice:
            # First boot (no config yet) is normal, not a warning: this function
            # is called on every engine tick and by several endpoints.
            return json.loads(json.dumps(DEFAULT_CONFIG))
        if username != "system" and "download_dir" not in user_slice:
            merged["download_dir"] = _default_download_dir()
        merged["max_concurrent"] = max(1, min(16, int(merged.get("max_concurrent", 3))))
        merged["download_dir"] = os.path.abspath(os.path.expanduser(str(merged.get("download_dir", DEFAULT_CONFIG["download_dir"]))))
        if not isinstance(merged.get("auto_download_debrid"), bool):
            merged["auto_download_debrid"] = True
        if not isinstance(merged.get("auto_retry"), bool):
            merged["auto_retry"] = True
        try:
            merged["retry_max"] = max(0, min(10, int(merged.get("retry_max", 3))))
        except (TypeError, ValueError):
            merged["retry_max"] = 3
        try:
            merged["retry_delay"] = max(10, min(3600, int(merged.get("retry_delay", 60))))
        except (TypeError, ValueError):
            merged["retry_delay"] = 60
        if merged.get("duplicate_policy") not in ("rename", "skip", "overwrite"):
            merged["duplicate_policy"] = "rename"
        for flag in ("auto_extract", "auto_extract_delete"):
            if not isinstance(merged.get(flag), bool):
                merged[flag] = False
        for key in ("schedule", "speed_limit"):
            raw = merged.get(key)
            merged[key] = {**DEFAULT_CONFIG[key], **(raw if isinstance(raw, dict) else {})}
        sched = merged["schedule"]
        sched["enabled"] = bool(sched.get("enabled"))
        for edge in ("start", "end"):
            value = str(sched.get(edge) or DEFAULT_CONFIG["schedule"][edge])
            sched[edge] = value if parse_hhmm(value) is not None else DEFAULT_CONFIG["schedule"][edge]
        limit = merged["speed_limit"]
        for edge in ("global_bps", "per_task_bps"):
            try:
                limit[edge] = max(0, int(limit.get(edge) or 0))
            except (TypeError, ValueError):
                limit[edge] = 0
        merged["categories"] = norm_categories(merged.get("categories"))
        merged["rss_feeds"] = norm_rss_feeds(merged.get("rss_feeds"))
        merged["package_limits"] = norm_package_limits(merged.get("package_limits"))
        db = default_debrid()
        user_db = merged.get("debrid") or {}
        if isinstance(user_db, dict):
            for k in db:
                v = user_db.get(k)
                if isinstance(v, str):
                    db[k] = v.strip()
        merged["debrid"] = db
        norm_watch_folders(merged)
        return merged
    except (FileNotFoundError, ValueError, TypeError, OSError) as exc:
        logger.warning("Download config unreadable (%s) — using defaults", exc)
        return json.loads(json.dumps(DEFAULT_CONFIG))


def qb_get_api_key() -> str:
    """Return the global qBittorrent API key, generating one on first use."""
    global_data = _CONFIG_STORE.load()
    global_data = global_data if isinstance(global_data, dict) else {}
    key = str(global_data.get("qb_api_key") or "").strip()
    if not key:
        key = "ethos_" + secrets.token_hex(24)
        global_data["qb_api_key"] = key
        _CONFIG_STORE.save(global_data)
        logger.info("Generated new qBittorrent API key")
    return key


def active_debrid(cfg: dict) -> tuple:
    """Return (provider_name, primary_secret) for the torrent/debrid slot."""
    name, creds = torrent_provider(cfg)
    if not name:
        return "", ""
    return name, primary_secret(name, creds)


def active_link_provider(cfg: dict) -> tuple:
    """Return (provider_name, credentials) for the hoster-link slot."""
    return link_provider(cfg)


def credentials_for(cfg: dict, provider: str) -> dict:
    """Credential values for any provider id, read from the config."""
    return provider_credentials(cfg, provider)


def save_config(cfg: dict) -> None:
    """Persist engine fields globally and the caller's slice per user.

    The `system` identity keeps the legacy behaviour: the whole config is the
    global document (the background engine and the config unit tests use it).
    """
    username = user_data.current_username()
    global_data = _CONFIG_STORE.load()
    global_data = global_data if isinstance(global_data, dict) else {}
    if username == "system":
        global_data.update(cfg)
        _CONFIG_STORE.save(global_data)
        return
    for field in ENGINE_FIELDS:
        if field in cfg:
            global_data[field] = cfg[field]
    _CONFIG_STORE.save(global_data)
    store = user_data.user_store("downloads.json")
    existing = store.load()
    existing = existing if isinstance(existing, dict) else {}
    existing.update({k: cfg[k] for k in USER_FIELDS if k in cfg})
    store.save(existing)
