"""TMDB adapter — optional, keyed provider with the best localization."""

from __future__ import annotations

from typing import Optional

from backend.services import tmdb_service
from backend.services.metadata.base import (
    MatchRequest,
    MetadataMatch,
    ProviderAuthError,
    ProviderError,
)
from backend.services.metadata.matching import strip_html


class TmdbProvider:
    id = "tmdb"
    label = "TMDB"
    supports = ("movie", "tv")
    requires_key = True
    attribution = "This product uses the TMDB API but is not endorsed or certified by TMDB."
    homepage = "https://www.themoviedb.org/"

    def fetch(self, request: MatchRequest, api_key: str = "") -> Optional[MetadataMatch]:
        try:
            data = tmdb_service.fetch_match(
                api_key,
                request.kind,
                request.query,
                request.year,
                request.image_dir,
                language=request.language,
                imdb_id=request.imdb_id,
            )
        except tmdb_service.TmdbError as exc:
            message = str(exc)
            if "401" in message or "403" in message:
                raise ProviderAuthError("TMDB rejected the API key") from exc
            raise ProviderError("TMDB request failed") from exc
        if not data:
            return None
        tmdb_id = data.get("tmdb_id")
        return MetadataMatch(
            kind=data.get("kind") or request.kind,
            source=self.id,
            source_id=str(tmdb_id or ""),
            source_label=self.label,
            source_url=(f"https://www.themoviedb.org/{data.get('kind') or request.kind}/"
                        f"{tmdb_id}") if tmdb_id else self.homepage,
            title=str(data.get("title") or "").strip(),
            year=data.get("year"),
            rating=data.get("rating"),
            overview=strip_html(data.get("overview")),
            genres=list(data.get("genres") or []),
            cast=list(data.get("cast") or []),
            runtime=data.get("runtime"),
            imdb_id=request.imdb_id,
            tmdb_id=tmdb_id if isinstance(tmdb_id, int) else None,
            poster_file=str(data.get("poster_file") or ""),
            backdrop_file=str(data.get("backdrop_file") or ""),
            language=request.language or "en",
        )

    def test(self, api_key: str = "") -> dict:
        if not api_key:
            return {
                "id": self.id, "label": self.label, "ok": False,
                "message": "TMDB API key is not configured",
                "requires_key": True, "configured": False, "latency_ms": 0,
            }
        result = tmdb_service.test_connection(api_key)
        return {
            "id": self.id, "label": self.label,
            "ok": bool(result.get("ok")),
            "message": str(result.get("message") or ""),
            "requires_key": True, "configured": True, "latency_ms": 0,
        }
