"""RSS feeds + rules for the Download Station.

A background loop polls each enabled feed at its own interval, matches items
against an include/exclude regex and queues the resulting magnet/``.torrent``
as a normal Downloads task. Seen item ids are persisted (bounded, 5000) so a
restart does not re-queue the same release.

Extracted from ``routers/downloads.py`` (TODO.md P2-01): the router supplies
the pieces that belong to it — the per-identity config loader, the enqueue
path (``AddTaskRequest``) and the bencode parser — so this module stays free of
router imports (``scripts/check_boundaries.py``).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import urllib.request
from typing import Awaitable, Callable, Optional

from backend.config import settings
from backend.services import json_store, path_scope, rss_service, user_data

logger = logging.getLogger("ethos-nas.services.download_rss")

RSS_SEEN_FILE = settings.DATA_DIR / "downloads_rss_seen.json"
RSS_SEEN_MAX = 5000

SeenKeys = list[str]
EnqueueFn = Callable[[str, str, Optional[str], Optional[str]], Awaitable[None]]

_seen_store = json_store.get(RSS_SEEN_FILE, list)
_seen_cache: set = set(_seen_store.load() or [])
_last_run: dict = {}
_seen_lock = threading.Lock()
_scan_lock = asyncio.Lock()


def mark_seen(keys: list) -> None:
    """Remember item ids (bounded ring) so a restart does not re-queue them."""
    if not keys:
        return
    with _seen_lock:
        _seen_cache.update(keys)
        if len(_seen_cache) > RSS_SEEN_MAX:
            for extra in list(_seen_cache)[: len(_seen_cache) - RSS_SEEN_MAX]:
                _seen_cache.discard(extra)
        _seen_store.save(sorted(_seen_cache))


def seen_count() -> int:
    return len(_seen_cache)


def reset_state() -> None:
    """Drop the in-memory seen set + run timers (tests)."""
    with _seen_lock:
        _seen_cache.clear()
        _last_run.clear()


def fetch_bytes_bounded(url: str, cap: int) -> bytes:
    """GET an http(s) URL with a hard size cap (feeds, .torrent links)."""
    if not str(url).lower().startswith(("http://", "https://")):
        raise ValueError(f"unsupported URL scheme: {str(url)[:32]}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "EthOS-NAS-DownloadStation/2.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 — scheme checked above
        data = resp.read(cap + 1)
    if len(data) > cap:
        raise ValueError(f"response is larger than {cap // (1024 * 1024)} MiB")
    return data


async def scan(
    *,
    load_config: Callable[[], dict],
    enqueue: EnqueueFn,
    parse_torrent: Callable[[bytes], dict],
    feed_ids: Optional[set] = None,
    force: bool = False,
) -> dict:
    """Poll due feeds and queue matching items. Returns ``{feeds, queued}``.

    The caller must already be inside ``path_scope.system_scope()`` (the whole
    scan runs as the service identity); each user's slice is then scanned
    inside that user's own identity so a queued task lands in the right
    library and is path-checked against the right home.
    """
    async with _scan_lock:
        checked = 0
        queued = 0
        for username, _slice in list(user_data.iter_user_slices("downloads.json")):
            role = user_data.user_role(username)
            with path_scope.identity_scope(username, role):
                cfg = load_config()
                now = time.time()
                for feed in cfg.get("rss_feeds") or []:
                    if feed_ids is not None and feed.get("id") not in feed_ids:
                        continue
                    if not feed.get("enabled") or not feed.get("pattern"):
                        continue
                    run_key = (username, feed["id"])
                    last = _last_run.get(run_key, 0)
                    interval = int(feed.get("interval_minutes") or 15) * 60
                    if not force and now - last < interval:
                        continue
                    _last_run[run_key] = now
                    checked += 1
                    try:
                        raw = await asyncio.to_thread(
                            fetch_bytes_bounded, feed["url"], 2 * 1024 * 1024)
                        items = await asyncio.to_thread(
                            rss_service.parse_feed, raw.decode("utf-8", "replace"))
                    except Exception as exc:
                        logger.warning("RSS feed %s failed: %s", feed["url"], exc)
                        continue
                    new_keys = []
                    for item in items:
                        key = str(item.get("id") or "")[:512]
                        seen_key = f"{username}:{key}"
                        if not key or seen_key in _seen_cache:
                            continue
                        new_keys.append(seen_key)
                        pattern = feed.get("pattern") or ""
                        exclude = feed.get("exclude") or ""
                        if not rss_service.matches(item, pattern, exclude):
                            continue
                        url = item.get("magnet") or item.get("link") or ""
                        if not url:
                            continue
                        name = (item.get("title") or "rss-download")[:255]
                        try:
                            if url.lower().startswith("magnet:"):
                                pass
                            elif url.lower().split("?")[0].endswith(".torrent"):
                                data = await asyncio.to_thread(
                                    fetch_bytes_bounded, url, 10 * 1024 * 1024)
                                info = await asyncio.to_thread(parse_torrent, data)
                                url = info["magnet"]
                                name = (item.get("title") or info["name"])[:255]
                            await enqueue(url, name,
                                          feed.get("destination") or None,
                                          feed.get("category") or None)
                            queued += 1
                            logger.info("RSS: queued %r from %s for %s",
                                        name, feed["id"], username)
                        except Exception as exc:
                            logger.warning("RSS item %r could not be queued: %s",
                                           key[:80], exc)
                    mark_seen(new_keys)
        return {"feeds": checked, "queued": queued}
