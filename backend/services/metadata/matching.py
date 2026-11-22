"""Provider-agnostic title matching helpers.

The gated candidate picking (exact title + year > same-year overlap > exact
any year > overlap > lone result) was already proven for TMDB. Keyless sources
are normalized into the same candidate shape and reuse it, so a fuzzy search hit
can never silently become a wrong poster.
"""

from __future__ import annotations

import html
import re
from typing import Optional

from backend.services import tmdb_service

GENERIC_SHOW_NAMES = tmdb_service.GENERIC_SHOW_NAMES
NOISE_TOKENS = tmdb_service.NOISE_TOKENS
extract_imdb_id = tmdb_service.extract_imdb_id
clean_query = tmdb_service._clean_query
query_variants = tmdb_service._query_variants

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_YEAR_RE = re.compile(r"(?<!\d)(19\d{2}|20[0-3]\d)(?!\d)")
_RUNTIME_RE = re.compile(r"(\d{1,3})\s*(?:min|minutes?)\b", re.IGNORECASE)


def norm_title(value: str) -> str:
    text = (value or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def token_set(value: str) -> set[str]:
    return {t for t in norm_title(value).split() if t}


def overlap(query: str, title: str) -> float:
    q = token_set(query)
    if not q:
        return 0.0
    return len(q & token_set(title)) / len(q)


def strip_html(value, limit: int = 600) -> str:
    """TVmaze summaries are HTML; the UI renders plain text only."""
    if value is None:
        return ""
    text = html.unescape(_HTML_TAG_RE.sub(" ", str(value)))
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit]


def parse_year(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1900 <= value <= 2039 else None
    text = str(value or "")
    match = _YEAR_RE.search(text)
    return int(match.group(1)) if match else None


def parse_runtime(value) -> Optional[int]:
    """'169 min', '49', 'PT1H29M'-free input → minutes."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 1000 else None
    text = str(value or "")
    match = _RUNTIME_RE.search(text)
    if match:
        minutes = int(match.group(1))
        return minutes if 1 <= minutes <= 1000 else None
    match = re.search(r"\b(\d{1,3})\b", text)
    if match:
        minutes = int(match.group(1))
        return minutes if 1 <= minutes <= 1000 else None
    return None


def as_candidate(
    *,
    title: str,
    year=None,
    source_id: str = "",
    poster_url: str = "",
    backdrop_url: str = "",
    rating=None,
    popularity=None,
    raw: dict | None = None,
) -> dict:
    """Normalize a provider search hit for `pick_candidate`."""
    return {
        "title": (title or "").strip(),
        "year": parse_year(year),
        "source_id": str(source_id or ""),
        "poster_url": poster_url or "",
        "backdrop_url": backdrop_url or "",
        "rating": rating,
        "popularity": popularity or 0,
        "raw": raw or {},
    }


def pick_candidate(candidates: list[dict], query: str,
                   year: Optional[int] = None, kind: str = "movie") -> Optional[dict]:
    """Pick the safest candidate using the existing gated TMDB logic.

    Candidates are mapped into TMDB-shaped dictionaries so there is exactly one
    matching implementation to test and maintain. Missing beats a wrong poster.
    """
    if not candidates:
        return None
    shaped: list[dict] = []
    for candidate in candidates:
        raw = candidate.get("raw") or {}
        if kind == "tv":
            shaped.append({
                "name": candidate.get("title") or raw.get("name") or "",
                "original_name": raw.get("original_name"),
                "popularity": candidate.get("popularity") or 0,
                "vote_average": candidate.get("rating") or 0,
                "_candidate": candidate,
            })
        else:
            year_value = candidate.get("year")
            shaped.append({
                "title": candidate.get("title") or raw.get("title") or "",
                "original_title": raw.get("original_title"),
                "release_date": f"{year_value}-01-01" if year_value else None,
                "popularity": candidate.get("popularity") or 0,
                "vote_average": candidate.get("rating") or 0,
                "_candidate": candidate,
            })
    picked = (tmdb_service._pick_tv(shaped, query) if kind == "tv"
              else tmdb_service._pick_movie(shaped, query, year))
    return picked.get("_candidate") if picked else None
