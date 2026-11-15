"""Shared helpers for the catalog builders (radio + podcasts).

Both builders are *offline* tools: they run on a developer machine (or CI) with
network access, query public directories (radio-browser.info, iTunes), validate
what they find and write deterministic JSON snapshots into ``backend/``. Nothing
here runs at request time — the NAS only reads the generated files.

Run from the repo root:

    .venv/bin/python scripts/build_radio_catalog.py
    .venv/bin/python scripts/build_podcast_catalog.py
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python scripts/<tool>.py`
    sys.path.insert(0, str(ROOT))

from backend.services.music_search import dedupe_keep_order, fold  # noqa: E402

logger = logging.getLogger("ethos-nas.scripts.catalogs")

USER_AGENT = "EthOS-NAS-catalog-builder/1.0 (+https://github.com/ethos-nas)"


def setup_logging(verbose: bool = False) -> None:
    """Console logging in the same ``ethos-nas`` hierarchy as the server."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def stable_id(value: str) -> str:
    """16-char SHA-256 prefix — the id scheme shared by NAS and both clients."""
    return hashlib.sha256(str(value or "").strip().encode()).hexdigest()[:16]


def get_json(url: str, *, timeout: float = 25.0, retries: int = 3) -> Any:
    """GET JSON with a UA, retries and exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, socket.timeout, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                delay = 1.5 * attempt
                logger.warning("GET %s failed (%s) — retry in %.1fs", url, exc, delay)
                time.sleep(delay)
    assert last_error is not None
    raise last_error


def fetch_text(url: str, *, timeout: float = 12.0, limit: int = 5 * 1024 * 1024) -> str:
    """GET a document (RSS feed) as text with a size cap."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(limit)
    return raw.decode("utf-8", "replace")


def probe_stream(url: str, *, timeout: float = 7.0) -> tuple[bool, str]:
    """Check that a live stream answers with audio (or an HLS playlist).

    radio-browser already records ``lastcheckok``, so this is a second opinion —
    useful because the NAS user and the phone may be on a different network than
    the directory's prober.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "audio/*, application/vnd.apple.mpegurl, application/x-mpegURL, */*",
            "Range": "bytes=0-2047",
            "Icy-MetaData": "1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            content_type = (response.headers.get("Content-Type") or "").lower()
            head = response.read(2048)
        if status >= 400:
            return False, f"HTTP {status}"
        looks_audio = (
            "audio" in content_type
            or "mpegurl" in content_type
            or "ogg" in content_type
            or head.lstrip().startswith(b"#EXTM3U")
        )
        return (True, content_type or "audio") if looks_audio else (False, f"content-type {content_type or '?'}")
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        return False, str(exc)


def write_json(path: Path, payload: dict, *, indent: int = 2) -> None:
    """Atomic write with UTF-8 Polish characters kept readable.

    The file must be **world-readable** (0644): the NAS service runs as the
    ``ethos-nas`` user while the generator runs as the developer. ``mkstemp``
    creates 0600 files, which made ``GET /api/music/podcasts/catalog`` answer
    500 with ``PermissionError`` (and unified search failed with it).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=indent) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        os.chmod(path, 0o644)
    finally:
        tmp.unlink(missing_ok=True)
    logger.info("Wrote %s (%d bytes)", path, path.stat().st_size)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Cannot read %s: %s", path, exc)
        return default


def dedupe(items: Iterable[Any], key) -> list[Any]:
    """Order-preserving de-duplication (see ``music_search.dedupe_keep_order``)."""
    return dedupe_keep_order(items, key)


def log_counts(label: str, items: Iterable[dict], key: str) -> None:
    counts: dict[str, int] = {}
    for item in items:
        counts[str(item.get(key) or "?")] = counts.get(str(item.get(key) or "?"), 0) + 1
    summary = ", ".join(f"{name}: {count}" for name, count in sorted(counts.items(), key=lambda kv: -kv[1]))
    logger.info("%s: %d total (%s)", label, sum(counts.values()), summary)


__all__ = [
    "ROOT", "USER_AGENT", "fold", "stable_id", "setup_logging", "get_json", "fetch_text",
    "probe_stream", "write_json", "read_json", "dedupe", "log_counts",
]
