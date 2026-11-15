"""Versioned package stores and the migration to packages/apps/<id>.

Old state lived in ``packages/*.json`` as bare lists and package files in
``<DATA_DIR>/<pkg_id>``. The new layout keeps the JSON files but wraps them in
``{version: 1, items|sources|...}`` and moves package payloads under
``packages/apps/<pkg_id>`` so a package id can never delete another subsystem.
"""

import base64
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from backend.config import settings
from backend.services import json_store
from backend.services.package_schema import PackageError, check_pkg_id

logger = logging.getLogger("ethos-nas.services.packages_store")

PACKAGES_ROOT: Path = settings.DATA_DIR / "packages"
APPS_DIR: Path = PACKAGES_ROOT / "apps"
STAGING_DIR: Path = PACKAGES_ROOT / ".staging"
BACKUP_DIR: Path = PACKAGES_ROOT / ".backup"

INSTALLED_FILE = PACKAGES_ROOT / "installed.json"
SOURCES_FILE = PACKAGES_ROOT / "sources.json"
OLD_SOURCES_FILE = PACKAGES_ROOT / "registry_sources.json"
CACHE_FILE = PACKAGES_ROOT / "cache.json"
OLD_CACHE_FILE = PACKAGES_ROOT / "registry_cache.json"

_INSTALLED = json_store.get(INSTALLED_FILE, dict, version=1)
_SOURCES = json_store.get(SOURCES_FILE, dict, version=1)
_CACHE = json_store.get(CACHE_FILE, dict, version=1)


def _items(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("items") or []
    return []


def _ensure_installed_dict() -> None:
    data = _INSTALLED.load()
    if not isinstance(data, dict):
        _INSTALLED.save({"items": data if isinstance(data, list) else []})


def load_installed() -> list[dict]:
    return [p for p in _items(_INSTALLED.load()) if isinstance(p, dict)]


def get_package(pkg_id: str) -> Optional[dict]:
    for pkg in load_installed():
        if pkg.get("id") == pkg_id:
            return pkg
    return None


def add_package(record: dict) -> dict:
    check_pkg_id(str(record.get("id") or ""))
    _ensure_installed_dict()

    def _add(data: dict) -> dict:
        items = data.setdefault("items", [])
        if any(p.get("id") == record["id"] for p in items):
            raise PackageError(f"Package {record['id']!r} already installed", 409)
        items.append(record)
        return record

    return _INSTALLED.update(_add)


def replace_package(record: dict) -> dict:
    check_pkg_id(str(record.get("id") or ""))
    _ensure_installed_dict()

    def _replace(data: dict) -> dict:
        items = data.setdefault("items", [])
        for index, pkg in enumerate(items):
            if pkg.get("id") == record["id"]:
                items[index] = record
                return record
        raise PackageError(f"Package {record['id']!r} not found", 404)

    return _INSTALLED.update(_replace)


def remove_package(pkg_id: str) -> bool:
    check_pkg_id(pkg_id)
    _ensure_installed_dict()
    removed = {"value": False}

    def _remove(data: dict) -> None:
        items = data.setdefault("items", [])
        remaining = [p for p in items if p.get("id") != pkg_id]
        removed["value"] = len(remaining) != len(items)
        data["items"] = remaining

    _INSTALLED.update(_remove)
    return removed["value"]


def package_dir(pkg_id: str) -> Path:
    check_pkg_id(pkg_id)
    return APPS_DIR / pkg_id


# ── Sources ─────────────────────────────────────────────────────────────────

def load_sources() -> list[dict]:
    data = _SOURCES.load()
    items = data.get("sources") if isinstance(data, dict) else None
    if isinstance(items, list):
        return [s for s in items if isinstance(s, dict)]
    # Legacy list[str] (or a fresh default).
    raw = data if isinstance(data, list) else []
    return [
        {"url": url, "name": url, "public_key": "", "key_id": "", "trust": "unverified", "enabled": True}
        for url in raw if isinstance(url, str)
    ]


def save_sources(sources: list[dict]) -> list[dict]:
    _SOURCES.save({"sources": sources})
    return sources


def load_cache() -> dict:
    data = _CACHE.load()
    if isinstance(data, dict) and isinstance(data.get("sources"), dict):
        return data["sources"]
    return data if isinstance(data, dict) else {}


def save_cache(cache: dict) -> None:
    _CACHE.save({"sources": cache})


def ensure_default_source() -> bool:
    """Seed or upgrade the official signed source.

    The key is pinned from EthOS configuration, never fetched from the repository
    itself; otherwise a compromised repo could replace both index and key.
    """
    if not settings.PACKAGES_DEFAULT_SOURCE:
        return False
    url = str(settings.PACKAGES_DEFAULT_SOURCE_URL or "").strip()
    key = str(settings.PACKAGES_DEFAULT_SOURCE_KEY or "").strip()
    if not url or not key:
        return False
    try:
        raw_key = base64.b64decode(key, validate=True)
    except (ValueError, TypeError):
        logger.error("ETHOS_PACKAGES_DEFAULT_SOURCE_KEY is not valid base64")
        return False
    key_id = hashlib.sha256(raw_key).hexdigest()[:16]
    sources = load_sources()
    if not sources:
        save_sources([{
            "url": url,
            "name": str(settings.PACKAGES_DEFAULT_SOURCE_NAME or "Official packages"),
            "public_key": key,
            "key_id": key_id,
            "trust": "pinned",
            "enabled": True,
        }])
        logger.info("Seeded pinned package source %s", url)
        return True

    changed = False
    official = next((s for s in sources if s.get("url") == url), None)
    if official is None:
        sources.append({
            "url": url,
            "name": str(settings.PACKAGES_DEFAULT_SOURCE_NAME or "Official packages"),
            "public_key": key,
            "key_id": key_id,
            "trust": "pinned",
            "enabled": True,
        })
        changed = True
    elif official.get("trust") != "pinned" or official.get("public_key") != key:
        official.update({
            "trust": "pinned",
            "public_key": key,
            "key_id": key_id,
            "name": official.get("name") or str(
                settings.PACKAGES_DEFAULT_SOURCE_NAME or "Official packages"),
        })
        changed = True
    if changed:
        save_sources(sources)
        logger.info("Ensured pinned official package source %s", url)
    return changed


# ── Migration ───────────────────────────────────────────────────────────────

def _archive_legacy(path: Path) -> None:
    """Rename a migrated legacy store so it cannot be re-imported later."""
    target = path.with_name(path.name + ".migrated")
    try:
        if target.exists():
            target.unlink()
        path.rename(target)
        logger.info("Archived legacy store %s", path.name)
    except OSError:
        logger.error("Could not archive legacy store %s", path, exc_info=True)


def migrate_legacy() -> dict:
    """Idempotently migrate old state and package directories.

    - bare-list ``installed.json`` → ``{"items": [...]}`` (via Store.update)
    - ``registry_sources.json`` → ``sources.json`` with dict entries
    - ``registry_cache.json`` → ``cache.json``
    - ``<DATA_DIR>/<pkg_id>`` → ``packages/apps/<pkg_id>``
    """
    PACKAGES_ROOT.mkdir(parents=True, exist_ok=True)
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0

    if INSTALLED_FILE.exists():
        raw = _INSTALLED.load()
        if isinstance(raw, list):
            _INSTALLED.save({"items": raw})
            logger.info("Migrated %d installed package record(s)", len(raw))

    if OLD_SOURCES_FILE.exists():
        try:
            raw = json.loads(OLD_SOURCES_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, list) and raw and not load_sources():
            save_sources([
                {"url": url, "name": url, "public_key": "", "key_id": "",
                 "trust": "unverified", "enabled": True}
                for url in raw if isinstance(url, str)
            ])
            logger.info("Migrated %d legacy registry source(s)", len(raw))
        if isinstance(raw, list):
            _archive_legacy(OLD_SOURCES_FILE)

    if OLD_CACHE_FILE.exists():
        try:
            raw = json.loads(OLD_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict) and raw and not load_cache():
            save_cache(raw)
            logger.info("Migrated legacy registry cache")
        if isinstance(raw, dict):
            _archive_legacy(OLD_CACHE_FILE)

    for pkg in load_installed():
        pkg_id = str(pkg.get("id") or "")
        try:
            check_pkg_id(pkg_id)
        except PackageError:
            logger.warning("Skipping migration for invalid package id %r", pkg_id)
            continue
        old_dir = settings.DATA_DIR / pkg_id
        new_dir = APPS_DIR / pkg_id
        if old_dir.is_dir() and not new_dir.exists():
            try:
                shutil.move(str(old_dir), str(new_dir))
                moved += 1
                logger.info("Migrated package %s to %s", pkg_id, new_dir)
            except OSError:
                logger.error("Could not migrate package directory %s", old_dir, exc_info=True)
        elif old_dir.is_dir() and new_dir.exists():
            logger.warning("Both legacy and new package dirs exist for %s; leaving legacy in place", pkg_id)

    default_source = ensure_default_source()
    return {"moved": moved, "installed": len(load_installed()),
            "sources": len(load_sources()), "default_source": default_source}
