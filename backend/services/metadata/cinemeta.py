"""Cinemeta (Stremio) provider — keyless movies and series.

Cinemeta is a public Stremio addon endpoint, not an official metadata API. It
returns IMDb-derived metadata and Metahub artwork, which makes it the only
practical zero-key source that covers both movies and series with posters and
backdrops. It is labelled as unofficial in Settings and can be disabled.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import Optional

from backend.services.metadata.base import (
    MatchRequest,
    MetadataMatch,
    ProviderError,
    ProviderNotFound,
)
from backend.services.metadata.http import HttpClient
from backend.services.metadata.images import cache_match_images
from backend.services.metadata.matching import (
    as_candidate,
    clean_query,
    parse_runtime,
    parse_year,
    pick_candidate,
    query_variants,
    strip_html,
)

logger = logging.getLogger("ethos-nas.services.metadata.cinemeta")

API_BASE = "https://v3-cinemeta.strem.io"
API_HOST = "v3-cinemeta.strem.io"
IMAGE_HOST = "images.metahub.space"
_IMDB_RE = re.compile(r"^tt\d{7,9}$")


class CinemetaProvider:
    id = "cinemeta"
    label = "Cinemeta (Stremio)"
    supports = ("movie", "tv")
    requires_key = False
    attribution = "Metadata via the unofficial Stremio Cinemeta addon."
    homepage = "https://www.stremio.com/"

    def __init__(self, http: Optional[HttpClient] = None):
        self.http = http or HttpClient(
            "cinemeta", {API_HOST, IMAGE_HOST}, min_interval=0.25,
        )

    @staticmethod
    def _type(kind: str) -> str:
        return "series" if kind == "tv" else "movie"

    def _search(self, kind: str, query: str) -> list[dict]:
        if not query:
            return []
        url = (f"{API_BASE}/catalog/{self._type(kind)}/top/"
               f"search={urllib.parse.quote(query, safe='')}.json")
        data = self.http.get_json(url)
        metas = data.get("metas") if isinstance(data, dict) else None
        out: list[dict] = []
        for raw in (metas or [])[:20]:
            if not isinstance(raw, dict):
                continue
            imdb = str(raw.get("id") or raw.get("imdb_id") or "")
            if not _IMDB_RE.fullmatch(imdb):
                continue
            out.append(as_candidate(
                title=raw.get("name") or "",
                year=raw.get("releaseInfo"),
                source_id=imdb,
                poster_url=raw.get("poster") or "",
                backdrop_url=raw.get("background") or "",
                rating=None,
                raw=raw,
            ))
        return out

    def _meta(self, kind: str, imdb_id: str) -> Optional[dict]:
        if not _IMDB_RE.fullmatch(imdb_id or ""):
            return None
        url = f"{API_BASE}/meta/{self._type(kind)}/{urllib.parse.quote(imdb_id)}.json"
        data = self.http.get_json(url)
        meta = data.get("meta") if isinstance(data, dict) else None
        return meta if isinstance(meta, dict) else None

    @staticmethod
    def _upgrade_poster(url: str) -> str:
        return (url or "").replace("/poster/small/", "/poster/medium/")

    @staticmethod
    def _upgrade_backdrop(url: str) -> str:
        return (url or "").replace("/background/medium/", "/background/large/")

    def fetch(self, request: MatchRequest, api_key: str = "") -> Optional[MetadataMatch]:
        query = clean_query(request.query, request.kind)[:120]
        if not query and not request.imdb_id:
            return None

        meta: Optional[dict] = None
        candidate: Optional[dict] = None
        if request.imdb_id:
            try:
                meta = self._meta(request.kind, request.imdb_id)
            except ProviderNotFound:
                meta = None
            except ProviderError as exc:
                logger.warning("Cinemeta IMDb lookup failed for %s: %s", request.imdb_id, exc)

        if not meta and query:
            for attempt in [query, *query_variants(query)]:
                try:
                    candidates = self._search(request.kind, attempt)
                except ProviderNotFound:
                    candidates = []
                if not candidates:
                    continue
                candidate = pick_candidate(candidates, attempt, request.year, request.kind)
                if not candidate:
                    continue
                try:
                    meta = self._meta(request.kind, candidate["source_id"])
                except (ProviderNotFound, ProviderError) as exc:
                    logger.warning("Cinemeta detail failed for %s: %s", candidate["source_id"], exc)
                    meta = None
                break

        if not meta and not candidate:
            return None

        data = meta or candidate or {}
        title = str(data.get("name") or (candidate or {}).get("title") or "").strip()
        if not title:
            return None
        imdb_id = (str(data.get("imdb_id") or data.get("id") or
                        (candidate or {}).get("source_id") or "") or None)
        poster_url = str(data.get("poster") or (candidate or {}).get("poster_url") or "")
        backdrop_url = str(data.get("background") or (candidate or {}).get("backdrop_url") or "")
        rating = None
        try:
            raw_rating = data.get("imdbRating")
            if raw_rating not in (None, ""):
                rating = round(float(raw_rating), 1)
        except (TypeError, ValueError):
            rating = None
        genres = [g for g in (data.get("genres") or data.get("genre") or [])
                  if isinstance(g, str) and g]
        cast = [c for c in (data.get("cast") or []) if isinstance(c, str) and c][:5]

        match = MetadataMatch(
            kind="tv" if self._type(request.kind) == "series" else "movie",
            source=self.id,
            source_id=str(imdb_id or ""),
            source_label=self.label,
            source_url=self.homepage,
            title=title,
            year=parse_year(data.get("year") or data.get("releaseInfo")),
            rating=rating,
            overview=strip_html(data.get("description")),
            genres=list(dict.fromkeys(genres)),
            cast=cast,
            runtime=parse_runtime(data.get("runtime")),
            imdb_id=str(imdb_id) if imdb_id else None,
            language="en",
            poster_url=poster_url,
            backdrop_url=backdrop_url,
        )
        if request.image_dir is not None:
            cache_match_images(
                self.http,
                match,
                poster_urls=[self._upgrade_poster(poster_url), poster_url],
                backdrop_urls=[self._upgrade_backdrop(backdrop_url), backdrop_url],
            )
        return match

    def test(self, api_key: str = "") -> dict:
        started = time.monotonic()
        try:
            self.http.get_json(f"{API_BASE}/manifest.json", force=True)
            return {
                "id": self.id, "label": self.label, "ok": True,
                "message": "Cinemeta reachable", "requires_key": False,
                "configured": True,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        except ProviderError as exc:
            return {
                "id": self.id, "label": self.label, "ok": False,
                "message": str(exc), "requires_key": False,
                "configured": True,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
