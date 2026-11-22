"""Offline tests for the provider-agnostic video metadata pipeline.

Covers candidate gating, Cinemeta/TVmaze normalization, provider fallback,
configuration, legacy cache migration, negative cache, artwork lookup and the
HTTP host allowlist. No live network calls — every provider uses fixtures.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-video-metadata-"))
os.environ["ETHOS_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))

from backend.services.metadata import service
from backend.services.metadata.base import (
    MatchRequest,
    MetadataConfigError,
    MetadataMatch,
    ProviderError,
    ProviderNotFound,
)
from backend.services.metadata.cinemeta import CinemetaProvider
from backend.services.metadata.http import HttpClient
from backend.services.metadata.matching import (
    parse_runtime,
    parse_year,
    pick_candidate,
    strip_html,
)
from backend.services.metadata.tvmaze import TvmazeProvider
from backend.services.video_metadata import parse_video_name

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "metadata"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def fixture(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


class _Health:
    def blocked(self) -> bool:
        return False

    def snapshot(self) -> dict:
        return {"blocked": False, "fail_streak": 0}


class FakeHttp:
    """Duck-typed HttpClient with fixture routes and fake image writes."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[str] = []
        self.health = _Health()

    def get_json(self, url: str, *, force: bool = False):
        self.calls.append(url)
        for marker, payload in self.routes.items():
            if marker in url:
                if isinstance(payload, Exception):
                    raise payload
                return copy.deepcopy(payload)
        raise ProviderNotFound(f"no fixture for {url}")

    def download_image(self, url: str, dest_dir: Path, prefix: str) -> str:
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = f"fake-{abs(hash(url))}.jpg"
        (dest_dir / name).write_bytes(b"\xff\xd8fake-jpeg")
        return name


class FakeProvider:
    supports = ("movie", "tv")

    def __init__(self, provider_id: str, *, match=None, error=None, requires_key=False):
        self.id = provider_id
        self.label = provider_id
        self.requires_key = requires_key
        self.attribution = ""
        self.homepage = ""
        self._match = match
        self._error = error
        self.calls = 0

    def fetch(self, request, api_key=""):
        self.calls += 1
        if self._error:
            raise self._error
        return self._match

    def test(self, api_key=""):
        return {"id": self.id, "label": self.label, "ok": self._error is None,
                "message": "", "requires_key": self.requires_key,
                "configured": True, "latency_ms": 1}


def reset_cache() -> None:
    for path in (service.MATCHES_FILE, service.LEGACY_MATCHES_FILE, service.MISSES_FILE):
        path.unlink(missing_ok=True)
    service._matches_memo = None
    service._legacy_checked = False


def main() -> int:
    # ── A. matching helpers ──────────────────────────────────────────────
    print("A. matching helpers")
    check("year from releaseInfo", parse_year("2014") == 2014)
    check("year from series range", parse_year("2022–") == 2022 and parse_year("2022-") == 2022)
    check("runtime from minutes", parse_runtime("169 min") == 169)
    check("runtime from int", parse_runtime(49) == 49)
    check("html summary stripped", strip_html("<p>Hello &amp; <b>world</b></p>") == "Hello & world")
    candidates = [
        {"title": "John Wick", "year": 2014, "source_id": "a"},
        {"title": "John Wick: Chapter 4", "year": 2023, "source_id": "b"},
    ]
    check("exact title + year wins", pick_candidate(candidates, "John Wick", 2014)["source_id"] == "a")
    check("no overlap → None", pick_candidate(
        [{"title": "Alpha Beta", "year": 2020}, {"title": "Gamma Delta", "year": 2020}],
        "Zeta Omega", 2020) is None)
    check("lone result accepted", pick_candidate([{"title": "Interstellar", "year": 2014}],
                                                 "Interstella", 2014) is not None)

    # ── B. Cinemeta normalization ───────────────────────────────────────
    print("B. Cinemeta")
    fake = FakeHttp({
        "catalog/movie/top/": fixture("cinemeta_search_movie.json"),
        "meta/movie/tt0816692": fixture("cinemeta_meta_movie.json"),
        "catalog/series/top/": fixture("cinemeta_search_series.json"),
        "meta/series/tt11280740": fixture("cinemeta_meta_series.json"),
        "manifest.json": {},
    })
    cinemeta = CinemetaProvider(http=fake)
    art = TMP / "art"
    movie = cinemeta.fetch(MatchRequest("movie", "Interstellar", 2014, image_dir=art))
    check("movie matched", bool(movie and movie.title == "Interstellar"))
    check("movie fields", bool(movie and movie.year == 2014 and movie.rating == 8.7
                               and movie.runtime == 169 and movie.genres == ["Adventure", "Sci-Fi"]
                               and len(movie.cast) == 5))
    check("movie artwork cached", bool(movie and movie.poster_file and movie.backdrop_file))
    check("movie source", bool(movie and movie.source == "cinemeta" and movie.source_label))
    series = cinemeta.fetch(MatchRequest("tv", "Severance", image_dir=art))
    check("series matched", bool(series and series.title == "Severance" and series.kind == "tv"))
    check("series fields", bool(series and series.year == 2022 and series.rating == 8.6
                               and series.runtime == 49 and len(series.cast) >= 3))
    fake_imdb = FakeHttp({"meta/movie/tt0816692": fixture("cinemeta_meta_movie.json")})
    exact = CinemetaProvider(http=fake_imdb).fetch(
        MatchRequest("movie", "ignored", imdb_id="tt0816692"),
    )
    check("IMDb lookup skips search", bool(exact and exact.title == "Interstellar"
                                           and len(fake_imdb.calls) == 1))
    check("health test", cinemeta.test().get("ok") is True)

    # ── C. TVmaze normalization ─────────────────────────────────────────
    print("C. TVmaze")
    tv_fake = FakeHttp({
        "search/shows?q=Severance": fixture("tvmaze_search.json"),
        "/shows/44933?embed=cast": fixture("tvmaze_show_cast.json"),
        "lookup/shows?imdb=tt11280740": fixture("tvmaze_show_cast.json"),
        "/shows/1": {"id": 1, "name": "Under the Dome"},
    })
    tvmaze = TvmazeProvider(http=tv_fake)
    show = tvmaze.fetch(MatchRequest("tv", "Severance", image_dir=art))
    check("show matched", bool(show and show.title == "Severance" and show.year == 2022))
    check("show fields", bool(show and show.rating == 7.7 and show.runtime == 49
                              and len(show.cast) == 5 and "<p>" not in show.overview))
    check("show artwork cached", bool(show and show.poster_file))
    lookup = tvmaze.fetch(MatchRequest("tv", "ignored", imdb_id="tt11280740"))
    check("IMDb lookup works", bool(lookup and lookup.title == "Severance"))
    check("TVmaze ignores movies", tvmaze.fetch(MatchRequest("movie", "Interstellar")) is None)
    check("TVmaze health", tvmaze.test().get("ok") is True)

    # ── D. provider chain ───────────────────────────────────────────────
    print("D. provider chain")
    check("auto order with key", service.provider_order("auto", "k", "tv") == ["tmdb", "cinemeta", "tvmaze"])
    check("auto order without key", service.provider_order("auto", "", "movie") == ["cinemeta"])
    check("keyless order", service.provider_order("keyless", "", "tv") == ["cinemeta", "tvmaze"])
    check("off order", service.provider_order("off", "k", "movie") == [])
    original = dict(service._PROVIDERS)
    try:
        tmdb_match = MetadataMatch(kind="movie", source="tmdb", title="TMDB Hit")
        cinemeta_match = MetadataMatch(kind="movie", source="cinemeta", title="Cinemeta Hit")
        tvmaze_match = MetadataMatch(kind="tv", source="tvmaze", title="TVmaze Hit")
        service._PROVIDERS["tmdb"] = FakeProvider("tmdb", match=tmdb_match, requires_key=True)
        service._PROVIDERS["cinemeta"] = FakeProvider("cinemeta", match=cinemeta_match)
        service._PROVIDERS["tvmaze"] = FakeProvider("tvmaze", match=tvmaze_match)
        req = MatchRequest("movie", "Interstellar", 2014)
        result = service.resolve({"provider": "auto", "api_key": ""}, req)
        check("keyless auto picks Cinemeta", result.match is cinemeta_match)
        result = service.resolve({"provider": "auto", "api_key": "key"}, req)
        check("TMDB preferred with key", result.match is tmdb_match)
        service._PROVIDERS["cinemeta"] = FakeProvider("cinemeta", error=ProviderError("down"))
        result = service.resolve({"provider": "keyless", "api_key": ""},
                                 MatchRequest("tv", "Severance"))
        check("Cinemeta failure falls back to TVmaze", result.match is tvmaze_match)
        try:
            service.resolve({"provider": "tmdb", "api_key": ""}, req)
            check("explicit TMDB without key raises", False)
        except MetadataConfigError:
            check("explicit TMDB without key raises", True)
        service._PROVIDERS["cinemeta"] = original["cinemeta"]
    finally:
        service._PROVIDERS.clear()
        service._PROVIDERS.update(original)

    # ── E. configuration ────────────────────────────────────────────────
    print("E. configuration")
    prev = {"api_key": "stored", "auto_fetch": False, "language": "pl-PL", "provider": "auto"}
    cfg = service.sanitize_config({"provider": "keyless"}, prev)
    check("provider patch keeps key", cfg["api_key"] == "stored" and cfg["provider"] == "keyless")
    check("clear_key removes key", service.sanitize_config({"clear_key": True}, prev)["api_key"] == "")
    check("invalid provider keeps previous",
          service.sanitize_config({"provider": "nope"}, prev)["provider"] == "auto")
    masked = service.mask_config({"api_key": "abcdef", "provider": "auto"})
    check("key masked", masked["api_key_set"] and masked["api_key_masked"] == "…cdef")
    check("auto+key mode", masked["source_mode"] == "tmdb")
    check("keyless mode", service.mask_config({"provider": "keyless"})["source_mode"] == "keyless")
    check("off mode", service.mask_config({"provider": "off"})["source_mode"] == "off")

    # ── F. cache migration + attach ─────────────────────────────────────
    print("F. cache")
    reset_cache()
    legacy = {"/videos/Old.mkv": {
        "kind": "movie", "tmdb_id": 123, "title": "Old", "year": 2000,
        "genres": ["Drama"], "matched_at": time.time(),
    }}
    service.LEGACY_MATCHES_FILE.write_text(json.dumps(legacy), encoding="utf-8")
    matches = service.load_matches()
    migrated = matches.get("/videos/Old.mkv", {})
    check("legacy cache migrated", migrated.get("source") == "tmdb" and migrated.get("title") == "Old")
    check("new cache file written", service.MATCHES_FILE.is_file())
    videos = [{"path": "/videos/Old.mkv"}]
    service.attach(videos)
    check("metadata attached", videos[0].get("tmdb", {}).get("source") == "tmdb")
    check("negative cache key stable",
          service.miss_key("auto", "", ("movie", "X", 2020, None))
          == service.miss_key("auto", "", ("movie", "x", 2020, None)))
    misses = {service.miss_key("auto", "", ("movie", "X", 2020, None)): {"at": time.time()}}
    check("fresh miss detected", service.is_miss(misses, service.miss_key("auto", "", ("movie", "X", 2020, None))))
    check("old miss expires", not service.is_miss(misses, "unknown"))

    # ── G. grouping + refresh ───────────────────────────────────────────
    print("G. grouping/refresh")
    reset_cache()
    episode = parse_video_name("Severance.S01E02.1080p.mkv", folder="Severance")
    videos = [
        {"path": "/videos/Severance/Severance.S01E01.1080p.mkv",
         **parse_video_name("Severance.S01E01.1080p.mkv", folder="Severance")},
        {"path": "/videos/Severance/Severance.S01E02.1080p.mkv", **episode},
        {"path": "/videos/Interstellar.2014.1080p.mkv",
         **parse_video_name("Interstellar.2014.1080p.mkv", folder="videos")},
    ]
    groups = service.build_groups(videos)
    check("season grouped by show", len(groups) == 2
          and max(len(group) for group in groups) == 2)
    original = dict(service._PROVIDERS)
    try:
        hit = MetadataMatch(kind="tv", source="cinemeta", title="Severance",
                            genres=["Drama"], poster_file="fake.jpg")
        service._PROVIDERS["cinemeta"] = FakeProvider("cinemeta", match=hit)
        stats = service.refresh(groups, {"provider": "keyless", "api_key": "",
                                         "language": "pl-PL"}, force=True)
        check("refresh matched groups", stats["titles_matched"] == 2 and stats["files_matched"] == 3,
              str(stats))
        check("refresh source counters", stats["by_source"].get("cinemeta") == 2, str(stats))
        check("cache persisted", len(service.load_matches()) == 3)
        stats = service.refresh(groups, {"provider": "keyless", "api_key": ""}, force=False)
        check("fresh entries skipped", stats["titles_checked"] == 0 and stats["skipped"] == 2, str(stats))
        stats = service.refresh(groups, {"provider": "off", "api_key": ""}, force=True)
        check("off provider disabled", stats.get("disabled") is True)
    finally:
        service._PROVIDERS.clear()
        service._PROVIDERS.update(original)

    # ── H. artwork helpers + HTTP allowlist ─────────────────────────────
    print("H. artwork/http")
    service.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    referenced = service.IMAGE_DIR / "ref.jpg"
    orphan = service.IMAGE_DIR / "orphan.jpg"
    referenced.write_bytes(b"\xff\xd8ref")
    orphan.write_bytes(b"\xff\xd8orphan")
    check("image lookup", service.image_path("ref.jpg") == referenced)
    check("image mime", service.image_media_type(referenced) == "image/jpeg")
    check("unsafe image name rejected", service.image_path("../ref.jpg") is None)
    service.sweep_images({"/x": {"poster_file": "ref.jpg"}})
    check("sweep keeps referenced", referenced.is_file())
    check("sweep removes orphan", not orphan.exists())
    client = HttpClient("test", {"example.com"})
    for label, url in (
        ("https allowed", "https://example.com/a.json"),
    ):
        try:
            client.check_url(url)
            check(label, True)
        except ProviderError:
            check(label, False)
    for label, url in (
        ("http rejected", "http://example.com/a.json"),
        ("foreign host rejected", "https://evil.example/a.json"),
        ("non-443 rejected", "https://example.com:8443/a.json"),
    ):
        try:
            client.check_url(url)
            check(label, False)
        except ProviderError:
            check(label, True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
