#!/usr/bin/env python3
"""Music search — Polish text folding, ranking, de-duplication and catalog match.

Pure unit checks plus an in-process exercise of the router helpers with yt-dlp
monkey-patched (no server, no network).

    .venv/bin/python tests/test_music_search_service.py
"""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Importing the router registers JSON stores — keep them out of backend/data/.
os.environ.setdefault("ETHOS_DATA_DIR", tempfile.mkdtemp(prefix="ethos-music-search-"))

from backend.services import music_search as ms  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def test_fold() -> None:
    print("\n[fold]")
    check("polish diacritics folded", ms.fold("Żółw Śpiewa – Łódź!") == "zolw spiewa lodz", ms.fold("Żółw Śpiewa – Łódź!"))
    check("case and punctuation", ms.fold("  RMF  FM!! ") == "rmf fm", ms.fold("  RMF  FM!! "))
    check("empty input", ms.fold(None) == "" and ms.fold("!!!") == "")
    check("tokens", ms.tokens("Kryminatorium — odcinek 12") == ["kryminatorium", "odcinek", "12"])


def test_titles() -> None:
    print("\n[artist/title parsing]")
    check("dash split", ms.split_artist_title("Doda - Nie żałuję (Official Video)") == ("Doda", "Nie żałuję"))
    check("en dash split", ms.split_artist_title("Mata – Patointeligencja") == ("Mata", "Patointeligencja"))
    check("no separator", ms.split_artist_title("Kryminatorium #12") == ("", "Kryminatorium #12"))
    check("brackets removed", ms.strip_noise("Utwór [Official Audio]") == "Utwór")
    check("trailing noise removed", ms.strip_noise("Utwór - Official Music Video") == "Utwór")
    check("soundtrack bracket kept", ms.strip_noise("Piosenka (z filmu)") == "Piosenka (z filmu)")
    sig = ms.track_signature("Doda - Nie żałuję", "Doda - Topic")
    check("signature strips topic suffix", sig == ("nie zaluje", "doda"), str(sig))
    check("signature drops feat", ms.track_signature("Utwór feat. Ktoś", "")[0] == "utwor")


def test_ranking() -> None:
    print("\n[ranking]")
    query = "doda nie żałuję"
    items = [
        {"id": "a", "title": "Doda - Nie żałuję (Official Video)", "artist": "Doda", "duration": 210},
        {"id": "b", "title": "Doda - Nie żałuję (Cover)", "artist": "Karaoke Fan", "duration": 215},
        {"id": "c", "title": "Doda - Nie żałuję [Official Audio]", "artist": "Doda - Topic", "duration": 208},
        {"id": "d", "title": "Doda - Nie żałuję 10 hours", "artist": "Loops", "duration": 36000},
    ]
    ranked = ms.rank_tracks(query, items)
    check("official upload first", ranked[0]["id"] == "c", str([r["id"] for r in ranked]))
    check("duplicate recordings collapsed", len(ranked) == 1, str([r["title"] for r in ranked]))
    check("compilation dropped", all(r["id"] != "d" for r in ranked))
    check("cover dropped when an official match exists", all(r["id"] != "b" for r in ranked))

    diacritic = ms.rank_tracks("zolw", [
        {"id": "x", "title": "Żółw", "artist": "Zespół", "duration": 180},
        {"id": "y", "title": "Inny utwór", "artist": "Ktoś", "duration": 180},
    ])
    check("diacritics-free query ranks matching title first", diacritic[0]["id"] == "x", str(diacritic))


def test_catalog_match() -> None:
    print("\n[catalog search]")
    stations = [
        {"id": "1", "name": "Trójka", "category": "Publiczne", "tags": ["public"], "city": ""},
        {"id": "2", "name": "RMF FM", "category": "Komercyjne", "tags": ["pop"], "city": "Kraków"},
        {"id": "3", "name": "Radio Maryja", "category": "Religijne", "tags": [], "city": ""},
    ]
    check("diacritics-free query finds Trójka", [s["id"] for s in ms.search_catalog(stations, "trojka")] == ["1"])
    check("prefix query finds Trójka", [s["id"] for s in ms.search_catalog(stations, "troj")] == ["1"])
    check("brand prefix tolerated", [s["id"] for s in ms.search_catalog(stations, "maryja")] == ["3"])
    check("category is searchable", [s["id"] for s in ms.search_catalog(stations, "religijne")] == ["3"])
    check("no match returns empty", ms.search_catalog(stations, "zzzzz") == [])
    check("empty query returns everything", len(ms.search_catalog(stations, "")) == 3)
    check("limit respected", len(ms.search_catalog(stations, "", limit=2)) == 2)

    podcasts = [{"id": "p", "name": "Kryminatorium", "author": "Gazeta.pl", "category": "True crime", "description": ""}]
    check("podcast typo tolerated", [p["id"] for p in ms.search_catalog(podcasts, "kryminatoriu")] == ["p"])


def test_variants_and_helpers() -> None:
    print("\n[helpers]")
    variants = ms.query_variants("Żółw")
    check("folded fallback variant", variants == ["Żółw", "zolw"], str(variants))
    check("plain query has one variant", ms.query_variants("rammstein") == ["rammstein"])
    check("dedupe keeps first", ms.dedupe_keep_order([1, 2, 1, 3], key=lambda x: x) == [1, 2, 3])


def test_router_pipeline() -> None:
    """Exercise _search_tracks/_search_all with yt-dlp replaced by a stub."""
    print("\n[router pipeline]")
    from backend.routers import music as router

    calls: list[tuple[str, int]] = []

    def fake_search(query: str, limit: int) -> list[dict]:
        calls.append((query, limit))
        return [
            {"id": "v1", "title": "Test Utwór (Official Video)", "artist": "Wykonawca", "duration": 200, "thumbnail": ""},
            {"id": "v2", "title": "Test Utwór (Cover)", "artist": "Fan", "duration": 200, "thumbnail": ""},
            {"id": "v3", "title": "Test inny", "artist": "Ktoś", "duration": 190, "thumbnail": ""},
            {"id": "v4", "title": "Test kolejny", "artist": "Ktoś inny", "duration": 180, "thumbnail": ""},
            {"id": "v5", "title": "Test jeszcze", "artist": "Ktoś trzeci", "duration": 170, "thumbnail": ""},
        ]

    router._search_sync = fake_search
    router._search_cache.clear()
    router._search_locks.clear()

    first = asyncio.run(router._search_tracks("test utwor", 2, 0))
    check("first page size", len(first["items"]) == 2, str(len(first["items"])))
    check("official upload ranked first", first["items"][0]["id"] == "v1", str([i["id"] for i in first["items"]]))
    check("dedupe removed cover", all("Cover" not in i["title"] for i in first["items"]))
    check("second page advertised", first["has_more"] is True)
    check("not cached on miss", first["cached"] is False)

    second = asyncio.run(router._search_tracks("test utwor", 2, 0))
    check("cache hit", second["cached"] is True)
    check("yt-dlp called once for identical query", len(calls) == 1, str(calls))

    last = asyncio.run(router._search_tracks("test utwor", 2, 2))
    check("offset page", len(last["items"]) == 2, str(len(last["items"])))
    check("no more pages", last["has_more"] is False)

    # type=all merges the catalogs without touching the network.
    router._load_podcast_catalog = lambda: {
        "categories": ["True crime"], "total": 1, "generated_at": "",
        "podcasts": [{"id": "p1", "name": "Testowy Podcast", "author": "Autor", "category": "True crime",
                      "description": "", "rss_url": "https://example.org/rss", "cover_url": "",
                      "language": "pl", "episode_count": 10, "last_episode": "2026-11-01"}],
    }
    router._load_radio_catalog = lambda: {
        "categories": ["Publiczne"], "total": 1, "generated_at": "",
        "stations": [{"id": "r1", "name": "Testowa Trójka", "category": "Publiczne", "stream_url": "https://example.org/s",
                      "tags": [], "city": "", "language": "pl", "logo": "", "codec": "", "bitrate": 0,
                      "hls": False, "homepage": "", "verified": True}],
    }
    router._library_items_cached = lambda: [
        {"name": "test.m4a", "title": "Test lokalny", "artist": "Wykonawca", "video_id": "", "url": ""}
    ]
    request = types.SimpleNamespace(state=types.SimpleNamespace(current_user={"sub": "1"}))

    combined = asyncio.run(router._search_all("test", 5, 0, request))
    check("all: tracks section", "tracks" in combined and "items" in combined["tracks"], str(combined.keys()))
    check("all: podcast matched", combined["podcasts"]["total"] == 1)
    check("all: radio matched", combined["radio"]["total"] == 1)
    check("all: library matched", combined["library"]["total"] == 1)

    suggestions = asyncio.run(router.search_suggest(request, "test", 6))
    types_seen = {item["type"] for item in suggestions["items"]}
    check("suggest returns typed items", {"podcast", "radio", "library"} <= types_seen, str(types_seen))


def test_search_all_resilience() -> None:
    """Uszkodzony/niedostępny katalog nie może wywalić całego wyszukiwania."""
    print("\n[unified search resilience]")
    from backend.routers import music as router

    router._search_sync = lambda q, n: [
        {"id": "v1", "title": "Amy Winehouse - Rehab", "artist": "Amy Winehouse",
         "duration": 214, "thumbnail": ""},
    ]
    router._search_cache.clear()
    router._search_locks.clear()

    def broken_catalog():
        raise PermissionError("[Errno 13] Permission denied: podcast_catalog.json")

    router._load_podcast_catalog = broken_catalog
    router._load_radio_catalog = broken_catalog
    router._library_items_cached = lambda: []
    request = types.SimpleNamespace(state=types.SimpleNamespace(current_user={"sub": "1"}))

    combined = asyncio.run(router._search_all("amy winehouse", 5, 0, request))
    check("tracks survive a broken catalog", len(combined["tracks"]["items"]) == 1,
          str(combined["tracks"]))
    check("podcast section degrades to empty", combined["podcasts"]["total"] == 0)
    check("radio section degrades to empty", combined["radio"]["total"] == 0)


def main() -> int:
    test_fold()
    test_titles()
    test_ranking()
    test_catalog_match()
    test_variants_and_helpers()
    test_router_pipeline()
    test_search_all_resilience()

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
