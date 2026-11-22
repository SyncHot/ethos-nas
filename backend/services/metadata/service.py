"""Provider chain, metadata cache and refresh orchestration.

The router owns filesystem walking/path scope; this service owns everything
else: provider choice, fallback, normalization, positive/negative caching,
artwork lifecycle and configuration sanitising. No router imports.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from backend.config import settings
from backend.services import json_store
from backend.services.metadata.base import (
    MatchRequest,
    MetadataConfigError,
    MetadataError,
    MetadataMatch,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
)
from backend.services.metadata.cinemeta import CinemetaProvider
from backend.services.metadata.images import IMAGE_DIR, image_media_type, image_path, sweep_images
from backend.services.metadata.matching import GENERIC_SHOW_NAMES, clean_query, extract_imdb_id
from backend.services.metadata.tmdb import TmdbProvider
from backend.services.metadata.tvmaze import TvmazeProvider
from backend.services.video_metadata import parse_video_name

logger = logging.getLogger("ethos-nas.services.metadata")

DATA_DIR = settings.DATA_DIR
MATCHES_FILE = DATA_DIR / "video_metadata_matches.json"
LEGACY_MATCHES_FILE = DATA_DIR / "tmdb_matches.json"
MISSES_FILE = DATA_DIR / "video_metadata_misses.json"

PROVIDER_IDS = ("auto", "keyless", "cinemeta", "tvmaze", "tmdb", "off")
PROVIDER_LABELS = {
    "auto": "Automatic",
    "keyless": "Keyless (Cinemeta + TVmaze)",
    "cinemeta": "Cinemeta (Stremio)",
    "tvmaze": "TVmaze",
    "tmdb": "TMDB",
    "off": "Off",
}
DEFAULT_PROVIDER = "auto"
DEFAULT_TMDB = {"api_key": "", "auto_fetch": True, "language": "pl-PL", "provider": "auto"}
LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z]{2,4})?")

MATCH_FIELDS = (
    "kind", "source", "source_id", "source_label", "source_url",
    "tmdb_id", "imdb_id", "title", "year", "rating", "overview",
    "poster_file", "backdrop_file", "genres", "cast", "runtime", "language",
)
# Backwards-compatible alias for imports/tests from the TMDB-only era.
TMDB_MATCH_FIELDS = MATCH_FIELDS

MISS_TTL = 14 * 24 * 3600
MAX_MATCHES = 4000
MAX_MISSES = 4000


def _migrate_matches_store(from_version: int, to_version: int, data: dict) -> dict:
    """Accept the old path→match map and wrap it in {version, matches}."""
    if not isinstance(data, dict):
        raise ValueError("metadata cache is not an object")
    if isinstance(data.get("matches"), dict):
        return data
    legacy = {k: v for k, v in data.items()
              if k != "version" and isinstance(k, str) and k.startswith("/")
              and isinstance(v, dict)}
    return {"matches": legacy}


_MATCHES_STORE = json_store.get(
    MATCHES_FILE, dict, version=2, migrate=_migrate_matches_store,
)
_LEGACY_MATCHES_STORE = json_store.get(LEGACY_MATCHES_FILE, dict)
_MISSES_STORE = json_store.get(MISSES_FILE, dict, version=1)

_matches_memo: tuple[int, int, dict] | None = None
_legacy_checked = False


# ── providers / configuration ────────────────────────────────────────────────

CINEMETA = CinemetaProvider()
TVMAZE = TvmazeProvider()
_PROVIDERS = {
    "cinemeta": CINEMETA,
    "tvmaze": TVMAZE,
    "tmdb": TmdbProvider(),
}


def sanitize_config(tmdb_cfg, prev: dict | None = None) -> dict:
    """Validate the metadata config; partial patches keep previous values."""
    previous = {**DEFAULT_TMDB, **(prev or {})}
    out = {
        "api_key": str(previous.get("api_key") or ""),
        "auto_fetch": bool(previous.get("auto_fetch", True)),
        "language": previous.get("language") or DEFAULT_TMDB["language"],
        "provider": previous.get("provider") or DEFAULT_PROVIDER,
    }
    if not isinstance(tmdb_cfg, dict):
        return out
    if tmdb_cfg.get("clear_key"):
        out["api_key"] = ""
    key = tmdb_cfg.get("api_key")
    if isinstance(key, str) and key.strip():
        out["api_key"] = key.strip()[:64]
    if tmdb_cfg.get("auto_fetch") is not None:
        out["auto_fetch"] = bool(tmdb_cfg.get("auto_fetch"))
    language = tmdb_cfg.get("language")
    if isinstance(language, str) and LANGUAGE_RE.fullmatch(language.strip()):
        out["language"] = language.strip()
    provider = tmdb_cfg.get("provider")
    if isinstance(provider, str) and provider.strip().lower() in PROVIDER_IDS:
        out["provider"] = provider.strip().lower()
    return out


def mask_config(tmdb_cfg: dict) -> dict:
    """Config-safe view; the raw key never leaves the server."""
    cfg = {**DEFAULT_TMDB, **(tmdb_cfg or {})}
    key = str(cfg.get("api_key") or "")
    provider = cfg.get("provider") if cfg.get("provider") in PROVIDER_IDS else DEFAULT_PROVIDER
    return {
        "api_key_set": bool(key),
        "api_key_masked": ("…" + key[-4:]) if key else "",
        "auto_fetch": bool(cfg.get("auto_fetch", True)),
        "language": cfg.get("language") or DEFAULT_TMDB["language"],
        "provider": provider,
        "provider_label": PROVIDER_LABELS.get(provider, provider),
        "key_required": provider == "tmdb",
        "source_mode": ("off" if provider == "off"
                        else "tmdb" if provider == "tmdb" or (provider == "auto" and key)
                        else "keyless"),
    }


def provider_order(provider: str, key: str, kind: str) -> list[str]:
    provider = provider if provider in PROVIDER_IDS else DEFAULT_PROVIDER
    if provider == "off":
        return []
    if provider == "auto":
        order = (["tmdb"] if key else []) + ["cinemeta"]
        if kind == "tv":
            order.append("tvmaze")
        return order
    if provider == "keyless":
        return ["cinemeta"] + (["tvmaze"] if kind == "tv" else [])
    return [provider]


def provider_status(key: str = "") -> list[dict]:
    out = []
    for provider_id in ("cinemeta", "tvmaze", "tmdb"):
        provider = _PROVIDERS[provider_id]
        health = getattr(getattr(provider, "http", None), "health", None)
        out.append({
            "id": provider.id,
            "label": provider.label,
            "supports": list(provider.supports),
            "requires_key": provider.requires_key,
            "configured": bool(key) if provider.requires_key else True,
            "attribution": provider.attribution,
            "homepage": provider.homepage,
            "health": health.snapshot() if health else None,
        })
    return out


@dataclass
class ResolveResult:
    match: Optional[MetadataMatch] = None
    errors: int = 0
    transient: bool = False
    attempts: list[str] = field(default_factory=list)


def resolve(config: dict, request: MatchRequest) -> ResolveResult:
    key = str((config or {}).get("api_key") or "")
    provider = (config or {}).get("provider") or DEFAULT_PROVIDER
    if provider == "tmdb" and not key:
        raise MetadataConfigError("TMDB API key is not configured")
    if provider == "off":
        return ResolveResult()
    result = ResolveResult()
    for provider_id in provider_order(provider, key, request.kind):
        source = _PROVIDERS.get(provider_id)
        if source is None or request.kind not in source.supports:
            continue
        if source.requires_key and not key:
            if provider == "tmdb":
                raise MetadataConfigError("TMDB API key is not configured")
            continue
        client = getattr(source, "http", None)
        if client is not None and client.health.blocked():
            result.attempts.append(f"{provider_id}:blocked")
            result.transient = True
            continue
        try:
            match = source.fetch(request, key)
        except ProviderAuthError as exc:
            result.errors += 1
            result.attempts.append(f"{provider_id}:auth")
            logger.warning("Metadata provider %s rejected the key", provider_id)
            if provider == provider_id:
                raise MetadataConfigError(str(exc)) from exc
            continue
        except ProviderRateLimited as exc:
            result.errors += 1
            result.transient = True
            result.attempts.append(f"{provider_id}:rate-limited")
            logger.warning("Metadata provider %s rate limited: %s", provider_id, exc)
            continue
        except ProviderError as exc:
            result.errors += 1
            result.transient = True
            result.attempts.append(f"{provider_id}:error")
            logger.warning("Metadata provider %s failed: %s", provider_id, exc)
            continue
        except MetadataError as exc:
            result.errors += 1
            result.transient = True
            result.attempts.append(f"{provider_id}:error")
            logger.warning("Metadata provider %s failed: %s", provider_id, exc)
            continue
        except Exception as exc:   # a provider bug must not abort the whole batch
            result.errors += 1
            result.transient = True
            result.attempts.append(f"{provider_id}:crash")
            logger.warning("Metadata provider %s crashed: %s", provider_id, exc, exc_info=True)
            continue
        if match:
            result.match = match
            return result
        result.attempts.append(f"{provider_id}:no-match")
    return result


# ── query derivation ─────────────────────────────────────────────────────────

def query_for_item(item: dict):
    """(kind, query, year, imdb_id) or None when a search is not worth it."""
    imdb_id = extract_imdb_id(os.path.basename(item.get("path", "")))
    if item.get("kind") == "episode":
        show = (item.get("show") or "").strip()
        if len(show) < 3 or show.lower() in GENERIC_SHOW_NAMES:
            return None
        return ("tv", show, None, imdb_id)
    title = (item.get("title") or "").strip()
    if not title:
        return None
    return ("movie", title, item.get("year"), imdb_id)


def query_for_path(path: str):
    item = {"path": path, **parse_video_name(
        os.path.basename(path), folder=os.path.basename(os.path.dirname(path)) or None,
    )}
    return query_for_item(item)


def build_groups(videos: Iterable[dict], matches: dict | None = None,
                 *, max_groups: int | None = None,
                 only_missing: bool = False) -> list[list[tuple[str, tuple]]]:
    """Group files by title so a whole season costs one metadata lookup."""
    known = matches if matches is not None else load_matches()
    groups: dict[tuple, list[tuple[str, tuple]]] = {}
    for video in videos:
        path = video.get("path")
        if not path:
            continue
        if only_missing and isinstance(known.get(path), dict) and known[path].get("genres"):
            continue
        query = query_for_item(video)
        if not query:
            continue
        kind, title, year, imdb_id = query
        key = (kind, title.lower(), year, imdb_id)
        groups.setdefault(key, []).append((path, query))
        if max_groups and len(groups) >= max_groups:
            break
    return list(groups.values())


# ── match cache ──────────────────────────────────────────────────────────────

def _normalise_match(hit: dict) -> dict:
    out = dict(hit or {})
    source = str(out.get("source") or ("tmdb" if out.get("tmdb_id") else "unknown"))
    out["source"] = source
    out.setdefault("source_label", {
        "tmdb": "TMDB", "cinemeta": "Cinemeta (Stremio)", "tvmaze": "TVmaze",
    }.get(source, source))
    out.setdefault("source_id", str(out.get("tmdb_id") or out.get("imdb_id") or ""))
    out.setdefault("source_url", "")
    out.setdefault("language", "")
    out["schema"] = 2
    return out


def _extract_matches(data) -> dict:
    if not isinstance(data, dict):
        return {}
    raw = data.get("matches") if isinstance(data.get("matches"), dict) else data
    return {str(k): _normalise_match(v) for k, v in raw.items()
            if isinstance(k, str) and k.startswith("/") and isinstance(v, dict)}


def _stat_key() -> tuple[int, int]:
    try:
        stat = os.stat(MATCHES_FILE)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (0, 0)


def load_matches() -> dict:
    """Path→match map, memoized on the cache file mtime + size."""
    global _matches_memo, _legacy_checked
    key = _stat_key()
    if _matches_memo and (_matches_memo[0], _matches_memo[1]) == key:
        return _matches_memo[2]
    data = _MATCHES_STORE.load()
    matches = _extract_matches(data)
    if not matches and not _legacy_checked:
        _legacy_checked = True
        legacy = _extract_matches(_LEGACY_MATCHES_STORE.load())
        if legacy:
            logger.info("Migrating %d legacy TMDB match(es) to metadata cache", len(legacy))
            _MATCHES_STORE.save({"matches": legacy})
            matches = legacy
            key = _stat_key()
    _matches_memo = (key[0], key[1], matches)
    return matches


def save_matches(matches: dict) -> None:
    global _matches_memo
    if len(matches) > MAX_MATCHES:
        oldest = sorted(matches.items(),
                        key=lambda kv: (kv[1].get("matched_at") or 0))
        for path, _ in oldest[:len(matches) - 3500]:
            matches.pop(path, None)
    _MATCHES_STORE.save({"matches": matches})
    _matches_memo = None


def attach(videos: list[dict]) -> None:
    """Attach cached metadata to library items (cache read only — no network)."""
    matches = load_matches()
    if not matches:
        return
    for video in videos:
        hit = matches.get(video.get("path"))
        if isinstance(hit, dict):
            video["tmdb"] = {k: hit[k] for k in MATCH_FIELDS if k in hit}


def match_counts() -> dict:
    return dict(Counter(str(h.get("source") or "unknown")
                        for h in load_matches().values() if isinstance(h, dict)))


# ── negative cache ───────────────────────────────────────────────────────────

def _load_misses() -> dict:
    data = _MISSES_STORE.load()
    raw = data.get("misses") if isinstance(data, dict) else None
    return raw if isinstance(raw, dict) else {}


def _save_misses(misses: dict) -> None:
    if len(misses) > MAX_MISSES:
        oldest = sorted(misses.items(), key=lambda kv: (kv[1].get("at") or 0))
        for key, _ in oldest[:len(misses) - 3500]:
            misses.pop(key, None)
    _MISSES_STORE.save({"misses": misses})


def miss_key(provider: str, key: str, query: tuple) -> str:
    kind, title, year, imdb_id = query
    mode = "key" if key else "nokey"
    return f"{provider}:{mode}|{kind}|{title.lower()}|{year or ''}|{imdb_id or ''}"


def is_miss(misses: dict, key: str) -> bool:
    entry = misses.get(key)
    return isinstance(entry, dict) and (entry.get("at") or 0) > time.time() - MISS_TTL


# ── refresh ──────────────────────────────────────────────────────────────────

def refresh(groups: list[list[tuple[str, tuple]]], config: dict,
            *, force: bool = False, max_groups: int | None = None) -> dict:
    """Fetch metadata for grouped files and persist matches + artwork.

    `force` re-queries cached entries (they are replaced only on success), which
    is what a provider switch or "Re-fetch all" needs. Transient failures are
    never cached as misses, so a temporary outage retries next time.
    """
    stats = {
        "titles_checked": 0, "titles_matched": 0, "files_matched": 0,
        "errors": 0, "skipped": 0, "kept": 0, "by_source": {},
    }
    provider = (config or {}).get("provider") or DEFAULT_PROVIDER
    key = str((config or {}).get("api_key") or "")
    if provider == "off":
        stats["disabled"] = True
        return stats
    if provider == "tmdb" and not key:
        raise MetadataConfigError("TMDB API key is not configured")

    matches = load_matches()
    misses = _load_misses()
    misses_dirty = False
    by_source: Counter = Counter()
    language = (config or {}).get("language") or DEFAULT_TMDB["language"]

    try:
        for entries in groups:
            if not entries:
                continue
            if max_groups is not None and stats["titles_checked"] >= max_groups:
                break
            paths = [path for path, _ in entries]
            query = entries[0][1]
            existing = next((matches.get(path) for path in paths if matches.get(path)), None)
            if existing and existing.get("genres") and not force:
                stats["skipped"] += 1
                continue
            key_miss = miss_key(provider, key, query)
            if not force and is_miss(misses, key_miss):
                stats["skipped"] += 1
                continue
            request = MatchRequest(
                kind=query[0], query=query[1], year=query[2], imdb_id=query[3],
                language=language, image_dir=IMAGE_DIR,
            )
            stats["titles_checked"] += 1
            result = resolve(config, request)
            stats["errors"] += result.errors
            if result.match:
                data = result.match.to_dict()
                data["matched_at"] = time.time()
                data["language"] = result.match.language or language
                data["schema"] = 2
                for path in paths:
                    matches[path] = data
                stats["files_matched"] += len(paths)
                stats["titles_matched"] += 1
                by_source[result.match.source] += 1
                if key_miss in misses:
                    misses.pop(key_miss, None)
                    misses_dirty = True
                try:
                    save_matches(matches)
                except OSError as exc:
                    logger.error("Could not persist metadata matches: %s", exc, exc_info=True)
            else:
                if result.transient:
                    pass   # transient failure: retry next time, do not cache a miss
                else:
                    misses[key_miss] = {"at": time.time(), "provider": provider}
                    misses_dirty = True
                if existing and force:
                    stats["kept"] += 1
    finally:
        if misses_dirty:
            try:
                _save_misses(misses)
            except OSError as exc:
                logger.error("Could not persist metadata misses: %s", exc, exc_info=True)

    stats["by_source"] = dict(by_source)
    if (stats["titles_checked"] and not stats["titles_matched"]
            and stats["errors"] >= stats["titles_checked"]):
        stats["aborted"] = ("Metadata sources are unavailable — check Settings → Metadata "
                            "or add a TMDB key")
    try:
        sweep_images(matches)
    except OSError as exc:
        logger.warning("Metadata image sweep failed: %s", exc)
    return stats


def refresh_single(path: str, config: dict, *, force: bool = False) -> dict:
    query = query_for_path(path)
    if not query:
        return {"titles_checked": 0, "titles_matched": 0, "files_matched": 0,
                "errors": 0, "skipped": 0, "kept": 0, "by_source": {},
                "match": load_matches().get(path)}
    group = [[(path, query)]]
    stats = refresh(group, config, force=force, max_groups=1)
    stats["match"] = load_matches().get(path)
    return stats


def test_providers(config: dict) -> dict:
    provider = (config or {}).get("provider") or DEFAULT_PROVIDER
    key = str((config or {}).get("api_key") or "")
    if provider == "auto":
        ids = (["tmdb"] if key else []) + ["cinemeta", "tvmaze"]
    elif provider == "keyless":
        ids = ["cinemeta", "tvmaze"]
    elif provider in ("cinemeta", "tvmaze", "tmdb"):
        ids = [provider]
    else:
        ids = []
    results = []
    for provider_id in ids:
        source = _PROVIDERS.get(provider_id)
        if not source:
            continue
        if source.requires_key and not key:
            results.append({
                "id": source.id, "label": source.label, "ok": False,
                "message": "API key is not configured", "requires_key": True,
                "configured": False, "latency_ms": 0,
            })
            continue
        try:
            results.append(source.test(key))
        except Exception as exc:   # a test endpoint must never 500 the UI
            logger.warning("Metadata test failed for %s: %s", provider_id, exc)
            results.append({
                "id": source.id, "label": source.label, "ok": False,
                "message": "Health check failed", "requires_key": source.requires_key,
                "configured": True, "latency_ms": 0,
            })
    return {
        "ok": any(item.get("ok") for item in results),
        "provider": provider,
        "provider_label": PROVIDER_LABELS.get(provider, provider),
        "results": results,
    }


def status(config: dict) -> dict:
    masked = mask_config(config)
    matches = load_matches()
    return {
        **masked,
        "matched_count": len(matches),
        "by_source": dict(Counter(str(h.get("source") or "unknown")
                                  for h in matches.values() if isinstance(h, dict))),
        "providers": provider_status(str((config or {}).get("api_key") or "")),
    }


__all__ = [
    "MATCH_FIELDS", "TMDB_MATCH_FIELDS", "PROVIDER_IDS", "PROVIDER_LABELS",
    "DEFAULT_PROVIDER", "DEFAULT_TMDB", "IMAGE_DIR",
    "sanitize_config", "mask_config", "provider_order", "provider_status",
    "resolve", "query_for_item", "query_for_path", "build_groups",
    "load_matches", "save_matches", "attach", "match_counts",
    "refresh", "refresh_single", "test_providers", "status",
    "image_path", "image_media_type", "sweep_images",
]
