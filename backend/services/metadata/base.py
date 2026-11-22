"""Shared types for the video metadata provider chain."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable


class MetadataError(Exception):
    """Base class for metadata failures (never contains credentials)."""


class MetadataConfigError(MetadataError):
    """The selected provider cannot run with the current configuration."""


class ProviderError(MetadataError):
    """A provider failed (network/HTTP/JSON) — the chain may fall back."""


class ProviderNotFound(ProviderError):
    """The provider answered correctly but has no matching title."""


class ProviderRateLimited(ProviderError):
    """HTTP 429 / explicit retry-after — back off and try another provider."""


class ProviderAuthError(ProviderError):
    """The provider rejected the configured API key."""


@dataclass
class MatchRequest:
    """One normalized lookup request derived from a video file name."""

    kind: str                       # "movie" | "tv"
    query: str
    year: Optional[int] = None
    imdb_id: Optional[str] = None
    language: Optional[str] = None
    image_dir: Optional[Path] = None


@dataclass
class MetadataMatch:
    """Provider-agnostic match consumed by the cache and the package UI."""

    kind: str
    source: str
    source_id: str = ""
    source_label: str = ""
    source_url: str = ""
    title: str = ""
    year: Optional[int] = None
    rating: Optional[float] = None
    overview: str = ""
    genres: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    runtime: Optional[int] = None
    imdb_id: Optional[str] = None
    tmdb_id: Optional[int] = None
    poster_file: str = ""
    backdrop_file: str = ""
    language: str = ""
    # Remote URLs are internal only — never persisted in the API payload.
    poster_url: str = ""
    backdrop_url: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("poster_url", None)
        data.pop("backdrop_url", None)
        return data


@runtime_checkable
class Provider(Protocol):
    """Contract every metadata source implements."""

    id: str
    label: str
    supports: tuple[str, ...]
    requires_key: bool
    attribution: str
    homepage: str

    def fetch(self, request: MatchRequest, api_key: str = "") -> Optional[MetadataMatch]:
        """Return a normalized match or None; raise ProviderError on failure."""

    def test(self, api_key: str = "") -> dict:
        """Cheap health check; never raises."""
