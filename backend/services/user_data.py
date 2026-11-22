"""Per-user state directories: ``<DATA_DIR>/users/<username>/``.

User-facing libraries keep their state here so two accounts never share a
folder list, an index, a download queue or a playback position. The files
themselves stay wherever the user points them (their home or the shared
``/mnt`` / ``/media`` mounts); only settings and indexes live in DATA_DIR.

Upgrading an existing install: the pre-multi-user stores at the DATA_DIR root
are adopted by the admin exactly once (``adopt_all`` from the app lifespan),
so the admin keeps the current library and every other account starts empty.
"""

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from backend.config import settings
from backend.services import json_store, path_scope, users_store

logger = logging.getLogger("ethos-nas.services.user_data")

# Stores that lived at the DATA_DIR root before the per-user split.
LEGACY_STORES = (
    "gallery_config.json",
    "gallery_index.db",
    "albums.json",
    "music_config.json",
    "video_station_config.json",
    "video_station_resume.json",
    "editor.json",
)

_UNSAFE = re.compile(r"[^A-Za-z0-9._@+-]")
_MAX_NAME = 64


def safe_username(username: str) -> str:
    """Filesystem-safe account name (never empty, never a path)."""
    cleaned = _UNSAFE.sub("_", str(username or "").strip())[:_MAX_NAME].strip("._")
    return cleaned or "default"


def current_username() -> str:
    """Username of the request/background identity (never empty)."""
    username, _role = path_scope.current_identity()
    return safe_username(username) if username else "default"


def users_root() -> Path:
    return settings.DATA_DIR / "users"


def user_dir(username: str | None = None) -> Path:
    return users_root() / safe_username(username or current_username())


def user_path(filename: str, username: str | None = None) -> Path:
    return user_dir(username) / filename


def user_store(
    filename: str,
    default_factory: Callable[[], Any] = dict,
    *,
    version: int | None = None,
    migrate: Callable[[int, int, Any], Any] | None = None,
    username: str | None = None,
) -> json_store.Store:
    """A locked JSON store in the current user's (or `username`'s) directory."""
    return json_store.get(
        user_path(filename, username),
        default_factory,
        version=version,
        migrate=migrate,
    )


def admin_username() -> str:
    """Username of the first admin account (fallback used by the migration)."""
    try:
        users = users_store.load()
    except Exception:  # noqa: BLE001 — a broken account store must not block startup
        logger.error("Could not read users.json for the admin migration", exc_info=True)
        return "admin"
    for user in users:
        if str(user.get("role") or "") != "admin":
            continue
        if users_store.status_of(user) != users_store.STATUS_ACTIVE:
            continue
        return safe_username(str(user.get("username") or ""))
    return "admin"


def user_role(username: str) -> str:
    """Role for an account, defaulting to `user` when the store is unreadable."""
    wanted = safe_username(username)
    try:
        for user in users_store.load():
            if safe_username(str(user.get("username") or "")) == wanted:
                return str(user.get("role") or "user")
    except Exception:  # noqa: BLE001 — a broken account store must not block scans
        logger.error("Could not read users.json for %s", wanted, exc_info=True)
    return "user"


def iter_user_slices(filename: str):
    """Yield (username, data) for every account that has `filename` stored."""
    root = users_root()
    if not root.is_dir():
        return
    for path in sorted(root.glob(f"*/{filename}")):
        name = path.parent.name
        data = json_store.get(path, dict).load()
        if isinstance(data, dict):
            yield name, data


def adopt_legacy(filename: str) -> bool:
    """Move a pre-multi-user store into the admin's directory once."""
    source = settings.DATA_DIR / filename
    if not source.exists():
        return False
    target = user_path(filename, admin_username())
    if target.exists():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
    except OSError:
        logger.error("Could not migrate %s → %s", source, target, exc_info=True)
        return False
    logger.info("Migrated legacy store %s → users/%s/%s",
                filename, admin_username(), filename)
    return True


def adopt_all() -> None:
    """Adopt every known legacy store (idempotent; called at startup)."""
    for filename in LEGACY_STORES:
        adopt_legacy(filename)
    _split_legacy_music_library()
    _adopt_download_config()


def _adopt_download_config() -> None:
    """Copy the user-facing half of the legacy downloads config to the admin.

    ``downloads_config.json`` stays in place as the global engine config; its
    former user fields (download dir, categories, watch folders, RSS) become
    the admin's per-user slice once.
    """
    source = settings.DATA_DIR / "downloads_config.json"
    target = user_path("downloads.json", admin_username())
    if not source.is_file() or target.exists():
        return
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.error("Legacy downloads config unreadable — leaving it untouched", exc_info=True)
        return
    if not isinstance(data, dict):
        return
    fields = ("download_dir", "categories", "watch_enabled", "watch_folders",
              "rss_feeds", "duplicate_policy", "auto_extract", "auto_extract_delete")
    user_slice = {k: data[k] for k in fields if k in data}
    if not user_slice:
        return
    try:
        json_store.get(target, dict).save(user_slice)
        logger.info("Adopted the legacy downloads config for %s", admin_username())
    except Exception:  # noqa: BLE001 — startup must continue with defaults
        logger.error("Could not adopt the legacy downloads config", exc_info=True)


def _split_legacy_music_library() -> None:
    """Split the old ``{user_id: state}`` music library into per-user files.

    The pre-multi-user store was one JSON document keyed by the JWT ``sub``.
    Each account now owns ``users/<username>/music_library.json``; the old
    file is renamed to ``music_library.legacy.json`` so nothing is lost.
    """
    source = settings.DATA_DIR / "music_library.json"
    if not source.is_file():
        return
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.error("Legacy music library unreadable — leaving it untouched", exc_info=True)
        return
    if not isinstance(data, dict):
        return
    names: dict[str, str] = {}
    try:
        for user in users_store.load():
            username = safe_username(str(user.get("username") or ""))
            if username:
                names[str(user.get("id"))] = username
                names[str(user.get("username"))] = username
    except Exception:  # noqa: BLE001 — a broken account store must not block startup
        logger.error("Could not read users.json for the music library split", exc_info=True)
    moved = 0
    for key, state in data.items():
        name = names.get(str(key))
        if not name or not isinstance(state, dict):
            continue
        target = user_path("music_library.json", name)
        if target.exists():
            continue
        try:
            json_store.get(target, dict).save(state)
            moved += 1
        except Exception:  # noqa: BLE001 — keep migrating the other accounts
            logger.error("Could not write legacy music state for %s", name, exc_info=True)
    try:
        source.replace(settings.DATA_DIR / "music_library.legacy.json")
    except OSError:
        logger.error("Could not archive the legacy music library", exc_info=True)
    logger.info("Split legacy music library into %d per-user file(s)", moved)
