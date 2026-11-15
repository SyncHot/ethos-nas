"""Build the Polish radio catalog served at ``GET /api/music/radio/stations``.

Source of truth: radio-browser.info (free, community-maintained directory). We
take Polish stations that their own prober marked healthy, then:

1. keep every station that is already in the repo catalog (device favourites are
   keyed by ``sha256(stream_url)[:16]`` — rewriting a URL would lose them),
2. classify into user-facing categories (Publiczne, Komercyjne, Regionalne,
   Religijne, Muzyka, Kultura, Sport),
3. de-duplicate by folded name and URL,
4. probe the selected streams and record ``verified``/``verified_at``,
5. write ``backend/radio_stations.json`` (schema v2) atomically.

The file is read on every request by the NAS, so regenerating it needs no restart.

    .venv/bin/python scripts/build_radio_catalog.py --limit 150
"""

from __future__ import annotations

import argparse
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_common import (
    ROOT,
    dedupe,
    fold,
    log_counts,
    probe_stream,
    read_json,
    setup_logging,
    stable_id,
    write_json,
    get_json,
)

logger = logging.getLogger("ethos-nas.scripts.build_radio_catalog")

RADIO_FILE = ROOT / "backend" / "radio_stations.json"

# Mirrors are interchangeable; the first one that answers wins.
API_MIRRORS = (
    "https://de1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
)

CATEGORIES = [
    "Publiczne",
    "Komercyjne",
    "Regionalne",
    "Religijne",
    "Muzyka",
    "Kultura",
    "Sport",
]

PUBLIC_BRANDS = {
    "jedynka", "dwojka", "trojka", "czworka", "piatka", "polskie radio",
    "polskie radio 24", "pr24", "radio poland", "polish radio",
}

RELIGIOUS_KEYWORDS = (
    "relig", "katolick", "kosciol", "maryja", "ewangelick", "chrzescijan",
    "nadzieja", "radio plus", "radioplus", "wiara", "biblia", "jezus", "ewangelia",
)

CULTURE_KEYWORDS = (
    "kultur", "teatr", "literack", "ksiazk", "muzeum", "filharmon", "opera", "poezj",
)

SPORT_KEYWORDS = ("sport", "ekstraklasa", "futbol", "pilka nozna", "zuzel")

MUSIC_KEYWORDS = (
    "disco polo", "dance", "techno", "house", "trance", "rock", "metal", "jazz",
    "klasyczn", "classic", "chopin", "barok", "chill", "lounge", "ambient", "rap",
    "hip hop", "hip-hop", "reggae", "blues", "folk", "country", "pop", "hity",
    "przeboje", "muzyka", "music", "polskie przeboje", "oldies", "party",
)

REGIONAL_KEYWORDS = (
    "regionaln", "lokaln", "radio krakow", "radio lodz", "radio lublin", "radio wroclaw",
    "radio poznan", "radio gdansk", "radio katowice", "radio szczecin", "radio bydgoszcz",
    "radio bialystok", "radio olsztyn", "radio kielce", "radio rzeszow", "radio opole",
    "radio plock", "radio pik", "radio zachod", "radio dla ciebie", "radio zet warszawa",
)

CITIES = (
    "warszawa", "krakow", "lodz", "wroclaw", "poznan", "gdansk", "szczecin",
    "bydgoszcz", "lublin", "bialystok", "katowice", "gdynia", "czestochowa", "radom",
    "rzeszow", "torun", "kielce", "olsztyn", "opole", "zielona gora", "gorzow",
    "plock", "tychy", "bielsko", "rybnik", "sosnowiec", "elblag", "tarnow", "koszalin",
    "slupsk", "nowy sacz", "siedlce", "pila", "konin", "legnica", "walbrzych",
    "jelenia gora", "przemysl", "zamosc", "suwalki", "lomza", "jasna gora",
)


# "(AAC+)", "[128 kbps]", trailing "AAC" / "128 Kbps" — stream descriptors, not the name.
_CODEC_NOISE_RE = re.compile(
    r"\s*[\(\[]\s*(?:aac\+?|mp3|hls|m3u8|ogg|opus|flac|\d+\s*kbps|\d+\s*k)\s*[\)\]]",
    re.IGNORECASE,
)
_CODEC_SUFFIX_RE = re.compile(
    r"(?:\s+(?:aac\+?|mp3|hls|m3u8|ogg|opus)\b.*|\s+\d+\s*kbps\b.*|\s+\d+\s*k\b.*|"
    r"\s+(?:64|96|128|160|192|256|320)\s*)$",
    re.IGNORECASE,
)
_ALL_BRACKETS_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_NON_LATIN_RE = re.compile(r"[\u0370-\u03FF\u0400-\u04FF\u0590-\u06FF\u3040-\u9FFF\uAC00-\uD7AF]")
_BITRATE_TOKEN_RE = re.compile(r"\b(?:64|96|128|160|192|256|320)\s*(?:kbps|k)\b", re.IGNORECASE)

# Prefixes/suffixes that do not distinguish two relays of the same station.
_DEDUPE_PREFIXES = ("moje polskie radio", "polskie radio", "radio", "radia", "pr")
_DEDUPE_SUFFIXES = ("polska", "pl", "fm", "online", "live", "na zywo", "hd")


def _clean_name(raw: Any) -> str:
    """Display name without stream descriptors (codec/bitrate brackets)."""
    name = " ".join(str(raw or "").split())
    name = _CODEC_NOISE_RE.sub("", name)
    name = _CODEC_SUFFIX_RE.sub("", name)
    name = name.strip(" -–—|·•")
    return name[:120]


def _dedupe_name(raw: Any) -> str:
    """Name key for de-duplication: brackets, bitrates, brand prefixes/suffixes dropped.

    “Polskie Radio - Czwórka (Program 4) (AAC+)”, “Radio Czwórka” and “Czwórka”
    are the same station relayed three times; “Anty Radio” and “Antyradio” too.
    """
    name = " ".join(str(raw or "").split())
    name = _ALL_BRACKETS_RE.sub(" ", name)
    name = _BITRATE_TOKEN_RE.sub(" ", name)
    key = fold(name)
    for prefix in _DEDUPE_PREFIXES:
        if key.startswith(prefix + " "):
            key = key[len(prefix) + 1:]
            break
    changed = True
    while changed:
        changed = False
        for suffix in _DEDUPE_SUFFIXES:
            if key.endswith(" " + suffix) and len(key) > len(suffix) + 2:
                key = key[: -len(suffix) - 1]
                changed = True
            elif key.endswith(suffix) and not key.endswith(" " + suffix) and len(key) > len(suffix) + 2:
                # “TOKFM” vs “TOK FM”; keep short names like “PR” intact.
                key = key[: -len(suffix)]
                changed = True
    # “Anty Radio” vs “Antyradio”, “TokFM” vs “TOK FM”.
    key = re.sub(r"\s+", "", key)
    key = re.sub(r"(.)\1{2,}", r"\1", key)  # MAXXX → MAXX
    return key.strip()


def _is_plausible_name(name: str) -> bool:
    return bool(name) and not _NON_LATIN_RE.search(name)


def _tags(raw: Any) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").split(","):
        tag = part.strip().lower()
        if tag and tag not in out and len(out) < 6:
            out.append(tag)
    return out


def classify(name: str, tags: list[str], homepage: str) -> str:
    """Pick one UI category for a station (order matters: most specific first)."""
    folded = fold(name)
    haystack = folded + " " + " ".join(fold(t) for t in tags)

    if any(word in haystack for word in RELIGIOUS_KEYWORDS):
        return "Religijne"
    if any(word in haystack for word in SPORT_KEYWORDS):
        return "Sport"
    if any(word in haystack for word in CULTURE_KEYWORDS):
        return "Kultura"
    # Regional stations of the public broadcaster (“Polskie Radio Katowice”) are
    # regional for the listener, so city detection beats the “Polskie Radio” brand.
    if any(city in folded for city in CITIES) or any(word in folded for word in REGIONAL_KEYWORDS):
        return "Regionalne"
    if folded in PUBLIC_BRANDS or "polskie radio" in folded or "polskieradio.pl" in (homepage or ""):
        return "Publiczne"
    if any(word in folded for word in MUSIC_KEYWORDS):
        return "Muzyka"
    return "Komercyjne"


def fetch_pl_stations(limit: int) -> list[dict]:
    last_error: Exception | None = None
    for base in API_MIRRORS:
        url = (
            f"{base}/json/stations/search?countrycode=PL&hidebroken=true"
            f"&order=clickcount&reverse=true&limit={limit}"
        )
        try:
            data = get_json(url)
            if isinstance(data, list):
                logger.info("radio-browser (%s): %d Polish stations", base, len(data))
                return data
            last_error = RuntimeError(f"unexpected payload: {type(data).__name__}")
        except Exception as exc:  # network/JSON — try the next mirror
            last_error = exc
            logger.warning("Mirror %s failed: %s", base, exc)
    raise RuntimeError(f"No radio-browser mirror answered: {last_error}")


def existing_entries() -> list[dict]:
    data = read_json(RADIO_FILE, default={"stations": []}) or {}
    out = []
    for st in data.get("stations") or []:
        if not isinstance(st, dict) or not st.get("stream_url"):
            continue
        out.append(st)
    logger.info("Existing catalog: %d stations (kept as-is to preserve favourites)", len(out))
    return out


def build(limit: int, probe: bool) -> dict:
    existing = existing_entries()
    existing_urls = {str(s["stream_url"]) for s in existing}
    existing_names = {_dedupe_name(s.get("name")) for s in existing if s.get("name")}

    raw = fetch_pl_stations(limit=max(limit * 4, 600))

    candidates: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("lastcheckok") not in (1, True):
            continue
        url = str(entry.get("url_resolved") or entry.get("url") or "").strip()
        if not url.startswith(("http://", "https://")) or url in existing_urls:
            continue
        name = _clean_name(entry.get("name"))
        if not name or len(name) < 2 or "test" in fold(name) or not _is_plausible_name(name):
            continue
        tags = _tags(entry.get("tags"))
        candidates.append({
            "id": stable_id(url),
            "name": name,
            "category": classify(name, tags, str(entry.get("homepage") or "")),
            "tags": tags,
            "city": " ".join(str(entry.get("state") or "").split())[:60],
            "language": "pl",
            "logo": str(entry.get("favicon") or "").strip(),
            "stream_url": url,
            "codec": str(entry.get("codec") or "").strip().upper(),
            "bitrate": int(entry.get("bitrate") or 0),
            "hls": bool(entry.get("hls")),
            "homepage": str(entry.get("homepage") or "").strip(),
            "votes": int(entry.get("votes") or 0),
            "clicks": int(entry.get("clickcount") or 0),
            "verified": False,
            "verified_at": "",
        })

    # Popularity order first: the best-known stream of a brand ("RMF FM") should
    # win over an obscure relay. Name de-duplication happens *after* probing so a
    # working relay is never thrown away in favour of a dead duplicate.
    candidates = dedupe(candidates, key=lambda c: str(c["stream_url"]))
    candidates.sort(key=lambda c: (-c["clicks"], -c["votes"], fold(c["name"])))
    candidates = [c for c in candidates if _dedupe_name(c["name"]) not in existing_names]

    # Existing stations are always kept (device favourites are keyed by id). New
    # candidates are over-selected because probing will discard broken relays.
    pool = list(existing) + candidates[: max(limit * 3, limit)]
    logger.info(
        "Probing %d stations (%d existing + %d candidates)",
        len(pool), len(existing), len(pool) - len(existing),
    )

    if probe:
        probe_stations(pool)
        pinned_urls = {str(s["stream_url"]) for s in existing}
        alive = [
            s for s in pool
            if s.get("verified") or str(s.get("stream_url")) in pinned_urls
        ]
        survivors = len(alive)
        alive = dedupe(alive, key=lambda s: _dedupe_name(s.get("name")))
        selected = alive[:limit]
        logger.info(
            "Probe: %d alive, %d dropped, %d after name dedupe, %d selected",
            survivors, len(pool) - survivors, len(alive), len(selected),
        )
    else:
        selected = dedupe(pool, key=lambda s: _dedupe_name(s.get("name")))[:limit]

    for station in selected:
        for key in ("_drop", "probe_error", "votes", "clicks"):
            station.pop(key, None)
        station.setdefault("tags", [])
        station.setdefault("city", "")
        station.setdefault("language", "pl")
        station.setdefault("logo", "")
        station.setdefault("codec", "")
        station.setdefault("bitrate", 0)
        station.setdefault("hls", False)
        station.setdefault("homepage", "")
        station.setdefault("verified", False)
        station.setdefault("verified_at", "")
        if station.get("category") not in CATEGORIES:
            station["category"] = classify(str(station.get("name") or ""), list(station.get("tags") or []), str(station.get("homepage") or ""))

    order = {name: index for index, name in enumerate(CATEGORIES)}
    selected.sort(key=lambda s: (order.get(str(s.get("category")), 99), fold(s.get("name"))))

    payload = {
        "version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "radio-browser.info + curated EthOS list",
        "categories": CATEGORIES,
        "stations": selected,
    }
    log_counts("Radio catalog", selected, "category")
    return payload


def probe_stations(stations: list[dict]) -> list[dict]:
    """Probe every stream concurrently; mark dead 404/410 streams for removal."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(probe_stream, str(s["stream_url"])): s for s in stations}
        for future in as_completed(futures):
            station = futures[future]
            try:
                ok, detail = future.result()
            except Exception as exc:  # unexpected probe failure is never fatal
                ok, detail = False, str(exc)
                logger.debug("Probe crashed for %s: %s", station.get("name"), exc)
            station["verified"] = bool(ok)
            station["verified_at"] = now if ok else ""
            if ok:
                logger.debug("OK   %-40s %s", station.get("name"), detail)
            else:
                logger.info("FAIL %-40s %s", station.get("name"), detail)
                station["probe_error"] = detail
    return stations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=150, help="target number of stations (default 150)")
    parser.add_argument("--no-probe", action="store_true", help="skip stream probing (fast, offline-ish)")
    parser.add_argument("--dry-run", action="store_true", help="print the summary without writing the file")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    payload = build(limit=max(20, args.limit), probe=not args.no_probe)
    if args.dry_run:
        print(f"DRY RUN: {len(payload['stations'])} stations, categories={payload['categories']}")
        return 0
    write_json(RADIO_FILE, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
