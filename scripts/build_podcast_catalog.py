"""Build the Polish podcast catalog served at ``GET /api/music/podcasts/catalog``.

Discovery uses the public iTunes Search/Lookup API restricted to the Polish
storefront (``country=PL``), plus the official Apple top-100 podcast chart for
popularity. Every candidate feed is then fetched and parsed with the *same*
parser the NAS uses at runtime (``services/podcast_service.parse_feed``), so the
catalog only contains feeds that actually work and have playable episodes.

The pinned list below is the 20-entry catalog that shipped in the Android and web
apps. Feed URLs must not change: podcast ids are ``sha256(rss_url)[:16]`` and
existing subscriptions reference them.

    .venv/bin/python scripts/build_podcast_catalog.py --limit 130
"""

from __future__ import annotations

import argparse
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from catalog_common import (
    ROOT,
    dedupe,
    fetch_text,
    fold,
    get_json,
    log_counts,
    read_json,
    setup_logging,
    stable_id,
    write_json,
)
from backend.services.podcast_service import FeedError, parse_feed

logger = logging.getLogger("ethos-nas.scripts.build_podcast_catalog")

PODCAST_FILE = ROOT / "backend" / "podcast_catalog.json"

# (name, category, rss_url) — the original catalog, preserved verbatim.
PINNED: list[tuple[str, str, str]] = [
    ("Raport o stanie świata Dariusza Rosiaka", "Aktualności", "https://anchor.fm/s/13a6bdec/podcast/rss"),
    ("Żurnalista - Rozmowy bez kompromisów", "Rozmowy", "https://anchor.fm/s/5d7d5d68/podcast/rss"),
    ("OSW - Ośrodek Studiów Wschodnich", "Geopolityka", "https://anchor.fm/s/5c81fea0/podcast/rss"),
    ("Podcast Historyczny", "Historia", "https://pinecast.com/feed/podcast-historyczny"),
    ("Mroczne Wieki", "Historia", "https://anchor.fm/s/101e14644/podcast/rss"),
    ("Podcast Wojenne Historie", "Historia", "https://anchor.fm/s/62c2781c/podcast/rss"),
    ("Kryminatorium", "True crime", "https://anchor.fm/s/ff12104c/podcast/rss"),
    ("7 metrów pod ziemią", "True crime", "https://anchor.fm/s/cff58c1c/podcast/rss"),
    ("Piąte: Nie zabijaj", "True crime", "https://anchor.fm/s/e1b27dc0/podcast/rss"),
    ("Radio Naukowe", "Nauka", "https://www.spreaker.com/show/4638772/episodes/feed"),
    ("Nauka To Lubię", "Nauka", "https://feeds.castos.com/d4vo5"),
    ("Dwa Kilo Mózgu", "Psychologia", "https://anchor.fm/s/11123bd58/podcast/rss"),
    ("Finanse Bardzo Osobiste", "Finanse", "https://rss.libsyn.com/shows/60678/destinations/234534.xml"),
    ("Podcast o technologii", "Technologia", "https://anchor.fm/s/eb4e9d28/podcast/rss"),
    ("Cyprian Majcher", "Rozmowy", "https://anchor.fm/s/f2269128/podcast/rss"),
    ("Imponderabilia - Karol Paciorek", "Rozmowy", "https://anchor.fm/s/5a81754/podcast/rss"),
    ("Przemek Górczyk Podcast", "Rozmowy", "https://anchor.fm/s/f49a65c4/podcast/rss"),
    ("Podcastex - podcast o latach 90. i 00.", "Kultura", "https://anchor.fm/s/5dc9d530/podcast/rss"),
    ("Kwadrans na angielski", "Język angielski", "https://www.spreaker.com/show/3011148/episodes/feed"),
    ("6 Minute English (BBC)", "Język angielski", "https://podcasts.files.bbci.co.uk/p02pc9tn.rss"),
]

# Search terms → category hint. Order is priority: earlier results rank higher.
SEARCH_TERMS: list[tuple[str, str]] = [
    ("kryminał", "True crime"),
    ("true crime", "True crime"),
    ("reportaż", "Reportaż"),
    ("historia Polski", "Historia"),
    ("historia", "Historia"),
    ("nauka", "Nauka"),
    ("technologia", "Technologia"),
    ("sztuczna inteligencja", "Technologia"),
    ("programowanie", "Technologia"),
    ("biznes", "Biznes"),
    ("finanse", "Finanse"),
    ("gospodarka", "Biznes"),
    ("psychologia", "Psychologia"),
    ("rozwój osobisty", "Rozwój osobisty"),
    ("zdrowie", "Zdrowie"),
    ("medycyna", "Zdrowie"),
    ("sport", "Sport"),
    ("piłka nożna", "Sport"),
    ("kultura", "Kultura"),
    ("film", "Film"),
    ("serial", "Film"),
    ("muzyka", "Muzyka"),
    ("komedia", "Komedia"),
    ("stand-up", "Komedia"),
    ("rozmowy", "Rozmowy"),
    ("wywiady", "Rozmowy"),
    ("polityka", "Polityka"),
    ("geopolityka", "Polityka"),
    ("prawo", "Prawo"),
    ("edukacja", "Edukacja"),
    ("matura", "Edukacja"),
    ("dla dzieci", "Dla dzieci"),
    ("bajki", "Dla dzieci"),
    ("rodzice", "Rodzina"),
    ("kuchnia", "Kuchnia"),
    ("podróże", "Podróże"),
    ("motoryzacja", "Motoryzacja"),
    ("gry", "Gry"),
    ("książki", "Literatura"),
    ("język angielski", "Język angielski"),
    ("astronomia", "Nauka"),
    ("mitologia", "Historia"),
    ("popkultura", "Kultura"),
    ("religia", "Religia"),
    ("śmiech", "Komedia"),
]

GENRE_MAP = {
    "True Crime": "True crime",
    "News": "Aktualności",
    "Politics": "Polityka",
    "Government": "Polityka",
    "History": "Historia",
    "Science": "Nauka",
    "Natural Sciences": "Nauka",
    "Social Sciences": "Nauka",
    "Technology": "Technologia",
    "Business": "Biznes",
    "Economics": "Biznes",
    "Comedy": "Komedia",
    "Society & Culture": "Kultura",
    "Arts": "Kultura",
    "Design": "Kultura",
    "Music": "Muzyka",
    "TV & Film": "Film",
    "Health & Fitness": "Zdrowie",
    "Sports": "Sport",
    "Education": "Edukacja",
    "Kids & Family": "Rodzina",
    "Religion & Spirituality": "Religia",
    "Fiction": "Literatura",
    "Leisure": "Lifestyle",
    "Food": "Kuchnia",
    "Travel": "Podróże",
    "Games": "Gry",
    "Medicine": "Zdrowie",
    "Documentary": "Reportaż",
}

CATEGORY_ORDER = [
    "Aktualności", "True crime", "Reportaż", "Historia", "Polityka", "Geopolityka",
    "Nauka", "Technologia", "Biznes", "Finanse", "Psychologia", "Rozwój osobisty",
    "Zdrowie", "Sport", "Kultura", "Film", "Muzyka", "Komedia", "Rozmowy",
    "Literatura", "Edukacja", "Język angielski", "Dla dzieci", "Rodzina", "Kuchnia",
    "Podróże", "Motoryzacja", "Gry", "Prawo", "Religia", "Lifestyle",
]

_NON_LATIN = re.compile(r"[\u0370-\u03FF\u0400-\u04FF\u0590-\u06FF\u0E00-\u0E7F\u3040-\u9FFF\uAC00-\uD7AF]")
_WS = re.compile(r"\s+")
_PL_CHARS = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
_PL_WORDS = re.compile(
    r"\b(?:się|jest|nie|czy|dla|oraz|przez|który|która|które|odcinek|odcinki|"
    r"rozmow\w*|histor\w*|opowie\w*|opowiad\w*|wiedzy|świat\w*|ludzi|życia|pracy)\b",
    re.IGNORECASE,
)
_FEED_LANG = re.compile(r"<language[^>]*>\s*([a-zA-Z]{2,3}(?:[-_][a-zA-Z]{2,4})?)", re.IGNORECASE)


def _clean(text: Any, limit: int = 160) -> str:
    value = _WS.sub(" ", str(text or "")).strip()
    return value[:limit]


def _is_plausible_name(name: str) -> bool:
    return bool(name) and not _NON_LATIN.search(name)


def _looks_polish(name: str, description: str) -> bool:
    """The Polish storefront also lists foreign shows — keep only Polish ones.

    Two cheap signals: Polish diacritics, or Polish function words in the feed
    description. Pinned entries bypass this check (they are curated by hand).
    """
    sample = f"{name} {description}"
    return bool(_PL_CHARS.search(sample) or _PL_WORDS.search(sample))


def _norm_feed(url: str) -> str:
    return str(url or "").strip()


def search_itunes(term: str, per_term: int) -> list[dict]:
    url = (
        "https://itunes.apple.com/search?media=podcast&entity=podcast&country=PL"
        f"&limit={per_term}&term={quote_plus(term)}"
    )
    data = get_json(url)
    results = data.get("results") if isinstance(data, dict) else []
    return [r for r in results or [] if isinstance(r, dict) and r.get("feedUrl")]


def top_chart() -> list[dict]:
    """Apple's official PL top-100, resolved to feeds via a batched lookup."""
    chart = get_json("https://rss.applemarketingtools.com/api/v2/pl/podcasts/top/100/podcasts.json")
    entries = ((chart or {}).get("feed") or {}).get("results") or []
    ids = [str(e.get("id") or "") for e in entries if e.get("id")]
    if not ids:
        return []
    out: list[dict] = []
    for start in range(0, len(ids), 100):
        chunk = ",".join(ids[start:start + 100])
        data = get_json(f"https://itunes.apple.com/lookup?id={chunk}&country=pl&entity=podcast")
        for result in (data or {}).get("results") or []:
            if isinstance(result, dict) and result.get("feedUrl"):
                out.append(result)
    logger.info("Apple PL top chart: %d feeds resolved", len(out))
    return out


def candidates(per_term: int) -> list[dict]:
    rows: list[dict] = []

    for name, category, rss in PINNED:
        rows.append({"feedUrl": rss, "collectionName": name, "artistName": "", "primaryGenreName": "",
                     "hint": category, "pinned": True, "popularity": len(rows)})

    try:
        for index, result in enumerate(top_chart()):
            rows.append({
                "feedUrl": _norm_feed(result.get("feedUrl")),
                "collectionName": _clean(result.get("collectionName")),
                "artistName": _clean(result.get("artistName")),
                "primaryGenreName": _clean(result.get("primaryGenreName")),
                "trackCount": int(result.get("trackCount") or 0),
                "artworkUrl600": str(result.get("artworkUrl600") or result.get("artworkUrl100") or ""),
                "itunes_id": str(result.get("collectionId") or ""),
                "hint": "",
                "pinned": False,
                "popularity": 100 + index,
            })
    except Exception as exc:  # chart is a nice-to-have; search still works
        logger.warning("Top chart unavailable: %s", exc)

    for term_index, (term, hint) in enumerate(SEARCH_TERMS):
        try:
            results = search_itunes(term, per_term)
        except Exception as exc:
            logger.warning("iTunes search %r failed: %s", term, exc)
            continue
        logger.info("iTunes %-24s %3d results", term, len(results))
        for index, result in enumerate(results):
            rows.append({
                "feedUrl": _norm_feed(result.get("feedUrl")),
                "collectionName": _clean(result.get("collectionName")),
                "artistName": _clean(result.get("artistName")),
                "primaryGenreName": _clean(result.get("primaryGenreName")),
                "trackCount": int(result.get("trackCount") or 0),
                "artworkUrl600": str(result.get("artworkUrl600") or result.get("artworkUrl100") or ""),
                "itunes_id": str(result.get("collectionId") or ""),
                "hint": hint,
                "pinned": False,
                # Round-robin across terms: without this the first terms (true
                # crime, historia) fill the whole catalog and the rest never runs.
                "popularity": 1000 + index * len(SEARCH_TERMS) + term_index,
            })

    rows = dedupe(rows, key=lambda r: _norm_feed(r["feedUrl"]).lower())
    rows.sort(key=lambda r: (0 if r.get("pinned") else 1, int(r.get("popularity") or 0)))
    logger.info("Candidate feeds: %d (after URL de-duplication)", len(rows))
    return rows


def validate_one(row: dict) -> tuple[dict | None, str, str]:
    """Fetch and parse a feed; returns (parsed_feed, error, declared_language)."""
    try:
        xml = fetch_text(row["feedUrl"])
        parsed = parse_feed(xml, max_episodes=5)
        match = _FEED_LANG.search(xml[:20000])
        return parsed, "", (match.group(1).lower() if match else "")
    except FeedError as exc:
        return None, str(exc), ""
    except Exception as exc:  # network, TLS, timeouts — skip the feed
        return None, str(exc)[:160], ""


def build(limit: int, per_term: int, validate_limit: int, verify: bool) -> dict:
    rows = candidates(per_term)
    if not verify:
        logger.warning("Verification disabled — catalog may contain broken feeds")

    picked: list[dict] = []
    seen_names: set[str] = set()
    checked = 0
    batch_size = 40

    for start in range(0, min(len(rows), validate_limit), batch_size):
        batch = rows[start:start + batch_size]
        if verify:
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(validate_one, batch))
        else:
            results = [({}, "", "") for _ in batch]

        for row, (parsed, error, feed_lang) in zip(batch, results):
            checked += 1
            if verify and parsed is None:
                logger.info("SKIP %-45s %s", row.get("collectionName") or row["feedUrl"], error)
                continue
            pinned = bool(row.get("pinned"))
            name = _clean(row.get("collectionName") if pinned else ((parsed or {}).get("title") or row.get("collectionName")), 120)
            description = _clean((parsed or {}).get("description"), 300)
            if not _is_plausible_name(name):
                continue
            is_polish = pinned or feed_lang.startswith("pl") or _looks_polish(name, description)
            if not is_polish:
                logger.info("SKIP %-45s not Polish (lang=%s)", name, feed_lang or "?")
                continue
            key = fold(name)
            if key in seen_names:
                continue
            seen_names.add(key)

            genre = row.get("primaryGenreName") or ""
            category = row.get("hint") or GENRE_MAP.get(genre) or "Rozmowy"
            if category == "Aktualności" and row.get("hint"):
                category = row["hint"]
            if category not in CATEGORY_ORDER:
                category = "Rozmowy"

            episodes = (parsed or {}).get("episodes") or []
            last_epoch = max((int(e.get("date") or 0) for e in episodes), default=0)
            language = "en" if "english" in fold(name) or "bbci.co.uk" in row["feedUrl"] else "pl"

            picked.append({
                "id": stable_id(row["feedUrl"]),
                "name": name,
                "author": _clean(row.get("artistName"), 80),
                "category": category,
                "description": description,
                "rss_url": row["feedUrl"],
                "cover_url": str((parsed or {}).get("cover_url") or row.get("artworkUrl600") or ""),
                "language": language,
                "episode_count": int(row.get("trackCount") or len(episodes)),
                "last_episode": (
                    datetime.fromtimestamp(last_epoch, tz=timezone.utc).strftime("%Y-%m-%d")
                    if last_epoch else ""
                ),
                "itunes_id": str(row.get("itunes_id") or ""),
                "popularity": int(row.get("popularity") or 0),
                "verified": True,
                "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            if len(picked) >= limit:
                break
        if len(picked) >= limit:
            break

    picked.sort(key=lambda p: (0 if p["rss_url"] in {rss for _, _, rss in PINNED} else 1, p["popularity"]))
    categories = [c for c in CATEGORY_ORDER if any(p["category"] == c for p in picked)]

    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "iTunes Search/Lookup (country=PL) + Apple PL top chart + curated EthOS list",
        "categories": categories,
        "podcasts": picked,
    }
    logger.info("Checked %d feeds, kept %d podcasts", checked, len(picked))
    log_counts("Podcast catalog", picked, "category")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=130, help="target number of podcasts (default 130)")
    parser.add_argument("--per-term", type=int, default=50, help="iTunes results per search term")
    parser.add_argument("--validate-limit", type=int, default=320, help="max feeds to verify (network heavy)")
    parser.add_argument("--no-verify", action="store_true", help="skip feed fetching/parsing")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    payload = build(
        limit=max(20, args.limit),
        per_term=max(10, args.per_term),
        validate_limit=max(40, args.validate_limit),
        verify=not args.no_verify,
    )
    if args.dry_run:
        print(f"DRY RUN: {len(payload['podcasts'])} podcasts, {len(payload['categories'])} categories")
        return 0
    write_json(PODCAST_FILE, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
