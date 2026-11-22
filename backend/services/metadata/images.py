"""Local artwork cache shared by all metadata providers.

The directory name is generic (`metadata_images`); files downloaded by older
TMDB-only builds stay in `tmdb_images` and are still served/looked up, so an
upgrade never breaks existing cached posters.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Iterable

from backend.config import settings
from backend.services.metadata.base import MetadataMatch, ProviderError
from backend.services.metadata.http import HttpClient

logger = logging.getLogger("ethos-nas.services.metadata.images")

DATA_DIR = settings.DATA_DIR
IMAGE_DIR = DATA_DIR / "metadata_images"
LEGACY_IMAGE_DIR = DATA_DIR / "tmdb_images"
IMAGE_DIRS = (IMAGE_DIR, LEGACY_IMAGE_DIR)

_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]{1,80}")
_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def cache_image(client: HttpClient, urls: Iterable[str], prefix: str) -> str:
    """Try each candidate URL in order; return the first cached filename."""
    for url in urls:
        if not url:
            continue
        try:
            name = client.download_image(url, IMAGE_DIR, prefix)
            if name:
                return name
        except ProviderError as exc:
            logger.warning("Artwork download failed for %s: %s", prefix, exc)
    return ""


def cache_match_images(client: HttpClient, match: MetadataMatch,
                       poster_urls: Iterable[str] | None = None,
                       backdrop_urls: Iterable[str] | None = None) -> None:
    """Download poster/backdrop for a match; never raises."""
    poster_urls = list(poster_urls if poster_urls is not None else [match.poster_url])
    backdrop_urls = list(backdrop_urls if backdrop_urls is not None else [match.backdrop_url])
    match.poster_file = cache_image(client, poster_urls, f"{match.source}:poster")
    match.backdrop_file = cache_image(client, backdrop_urls, f"{match.source}:backdrop")
    if not match.poster_file and match.backdrop_file:
        match.poster_file = match.backdrop_file


def image_path(filename: str) -> Path | None:
    """Resolve a cached image from the new or the legacy TMDB directory."""
    if not filename or not _SAFE_NAME.fullmatch(filename):
        return None
    if Path(filename).name != filename:
        return None
    for directory in IMAGE_DIRS:
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def image_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_MEDIA_TYPES:
        return _IMAGE_MEDIA_TYPES[suffix]
    guessed = mimetypes.guess_type(str(path))[0] or ""
    return guessed if guessed.startswith("image/") else "image/jpeg"


def referenced_files(matches: dict) -> set[str]:
    referenced: set[str] = set()
    for hit in (matches or {}).values():
        if not isinstance(hit, dict):
            continue
        for key in ("poster_file", "backdrop_file"):
            value = hit.get(key)
            if value:
                referenced.add(str(value))
    return referenced


def sweep_images(matches: dict) -> int:
    """Delete cached artwork no longer referenced by any match."""
    referenced = referenced_files(matches)
    removed = 0
    for directory in IMAGE_DIRS:
        try:
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not path.is_file():
                    continue
                if path.name.endswith(".tmp"):
                    path.unlink(missing_ok=True)
                    continue
                if path.name not in referenced:
                    path.unlink(missing_ok=True)
                    removed += 1
        except OSError as exc:
            logger.warning("Metadata image sweep failed for %s: %s", directory, exc)
    if removed:
        logger.info("Metadata image sweep: removed %d unreferenced file(s)", removed)
    return removed
