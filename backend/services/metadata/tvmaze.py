"""TVmaze provider — official, keyless series metadata (CC BY-SA 4.0)."""

from __future__ import annotations

import logging
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

logger = logging.getLogger("ethos-nas.services.metadata.tvmaze")

API_BASE = "https://api.tvmaze.com"
API_HOST = "api.tvmaze.com"
IMAGE_HOST = "static.tvmaze.com"


class TvmazeProvider:
    id = "tvmaze"
    label = "TVmaze"
    supports = ("tv",)
    requires_key = False
    attribution = "Series data from TVmaze, licensed CC BY-SA 4.0."
    homepage = "https://www.tvmaze.com/"

    def __init__(self, http: Optional[HttpClient] = None):
        self.http = http or HttpClient(
            "tvmaze", {API_HOST, IMAGE_HOST}, min_interval=0.5,
        )

    def _search(self, query: str) -> list[dict]:
        if not query:
            return []
        url = f"{API_BASE}/search/shows?q={urllib.parse.quote(query, safe='')}"
        data = self.http.get_json(url)
        out: list[dict] = []
        for item in (data or [])[:20]:
            show = item.get("show") if isinstance(item, dict) else None
            if not isinstance(show, dict) or not show.get("id"):
                continue
            image = show.get("image") or {}
            out.append(as_candidate(
                title=show.get("name") or "",
                year=show.get("premiered"),
                source_id=str(show.get("id")),
                poster_url=image.get("original") or image.get("medium") or "",
                rating=(show.get("rating") or {}).get("average"),
                popularity=show.get("weight") or 0,
                raw=show,
            ))
        return out

    def _lookup_imdb(self, imdb_id: str) -> Optional[dict]:
        if not imdb_id:
            return None
        url = f"{API_BASE}/lookup/shows?imdb={urllib.parse.quote(imdb_id)}"
        data = self.http.get_json(url)
        return data if isinstance(data, dict) and data.get("id") else None

    def _detail(self, show_id) -> Optional[dict]:
        try:
            data = self.http.get_json(f"{API_BASE}/shows/{int(show_id)}?embed=cast")
            return data if isinstance(data, dict) else None
        except ProviderNotFound:
            return None
        except ProviderError as exc:
            logger.warning("TVmaze detail failed for show %s: %s", show_id, exc)
            return None

    def fetch(self, request: MatchRequest, api_key: str = "") -> Optional[MetadataMatch]:
        if request.kind != "tv":
            return None
        query = clean_query(request.query, request.kind)[:120]
        if not query and not request.imdb_id:
            return None

        show: Optional[dict] = None
        if request.imdb_id:
            try:
                show = self._lookup_imdb(request.imdb_id)
            except ProviderNotFound:
                show = None
            except ProviderError as exc:
                logger.warning("TVmaze IMDb lookup failed for %s: %s", request.imdb_id, exc)

        if not show and query:
            for attempt in [query, *query_variants(query)]:
                try:
                    candidates = self._search(attempt)
                except ProviderNotFound:
                    candidates = []
                if not candidates:
                    continue
                picked = pick_candidate(candidates, attempt, None, "tv")
                if picked:
                    show = picked.get("raw")
                    break

        if not show:
            return None
        detail = self._detail(show.get("id")) or show
        image = detail.get("image") or show.get("image") or {}
        poster_url = image.get("original") or image.get("medium") or ""
        cast: list[str] = []
        embedded = detail.get("_embedded") or {}
        for item in (embedded.get("cast") or [])[:5]:
            person = (item or {}).get("person") or {}
            name = person.get("name")
            if isinstance(name, str) and name:
                cast.append(name)
        externals = detail.get("externals") or show.get("externals") or {}
        show_id = detail.get("id") or show.get("id")
        show_url = detail.get("url") or show.get("url") or f"https://www.tvmaze.com/shows/{show_id}"

        match = MetadataMatch(
            kind="tv",
            source=self.id,
            source_id=str(show_id or ""),
            source_label=self.label,
            source_url=str(show_url),
            title=str(detail.get("name") or show.get("name") or "").strip(),
            year=parse_year(detail.get("premiered") or show.get("premiered")),
            rating=((detail.get("rating") or show.get("rating") or {}).get("average")),
            overview=strip_html(detail.get("summary") or show.get("summary")),
            genres=[g for g in (detail.get("genres") or show.get("genres") or [])
                    if isinstance(g, str) and g],
            cast=cast,
            runtime=parse_runtime(detail.get("averageRuntime") or detail.get("runtime")
                                  or show.get("averageRuntime") or show.get("runtime")),
            imdb_id=externals.get("imdb"),
            language="en",
            poster_url=poster_url,
        )
        if not match.rating:
            match.rating = None
        if request.image_dir is not None:
            cache_match_images(self.http, match, poster_urls=[poster_url])
        return match if match.title else None

    def test(self, api_key: str = "") -> dict:
        started = time.monotonic()
        try:
            self.http.get_json(f"{API_BASE}/shows/1", force=True)
            return {
                "id": self.id, "label": self.label, "ok": True,
                "message": "TVmaze reachable", "requires_key": False,
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
