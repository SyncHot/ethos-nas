"""Offline contract checks for the generated music catalogs.

Used by ``tests/test_music_catalogs.py`` and as a pre-commit gate after
regenerating ``backend/radio_stations.json`` / ``backend/podcast_catalog.json``.
No network access.

    .venv/bin/python scripts/verify_catalogs.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RADIO_FILE = ROOT / "backend" / "radio_stations.json"
PODCAST_FILE = ROOT / "backend" / "podcast_catalog.json"

MIN_RADIO = 150
MIN_PODCASTS = 120

PINNED_FEEDS = (
    "https://anchor.fm/s/13a6bdec/podcast/rss",
    "https://anchor.fm/s/ff12104c/podcast/rss",
    "https://podcasts.files.bbci.co.uk/p02pc9tn.rss",
)


def stable_id(value: str) -> str:
    return hashlib.sha256(str(value or "").strip().encode()).hexdigest()[:16]


class Checker:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def fail(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _load(path: Path, checker: Checker) -> dict:
    if not path.exists():
        checker.fail(f"{path.name}: missing")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        checker.fail(f"{path.name}: unreadable ({exc})")
        return {}
    if not isinstance(data, dict):
        checker.fail(f"{path.name}: top-level value is not an object")
        return {}
    return data


def check_radio(data: dict, checker: Checker) -> None:
    stations = data.get("stations") or []
    categories = data.get("categories") or []
    if not isinstance(stations, list) or not stations:
        checker.fail("radio_stations.json: no stations")
        return
    if not categories or not all(isinstance(c, str) and c for c in categories):
        checker.fail("radio_stations.json: categories must be a non-empty list of strings")
    if len(stations) < MIN_RADIO:
        checker.fail(f"radio_stations.json: {len(stations)} stations, need >= {MIN_RADIO}")

    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    unverified = 0
    for index, station in enumerate(stations):
        if not isinstance(station, dict):
            checker.fail(f"radio_stations.json: station #{index} is not an object")
            continue
        sid = str(station.get("id") or "")
        name = str(station.get("name") or "").strip()
        url = str(station.get("stream_url") or "").strip()
        category = str(station.get("category") or "")
        if not sid:
            checker.fail(f"radio_stations.json: station #{index} has no id")
        elif sid in seen_ids:
            checker.fail(f"radio_stations.json: duplicate id {sid} ({name})")
        seen_ids.add(sid)
        if sid and url and sid != stable_id(url):
            checker.fail(f"radio_stations.json: id/url mismatch for {name!r} ({sid} != {stable_id(url)})")
        if not name:
            checker.fail(f"radio_stations.json: station #{index} has no name")
        if not url.startswith(("http://", "https://")):
            checker.fail(f"radio_stations.json: invalid stream_url for {name!r}: {url!r}")
        if url in seen_urls:
            checker.fail(f"radio_stations.json: duplicate stream_url for {name!r}")
        seen_urls.add(url)
        if categories and category not in categories:
            checker.fail(f"radio_stations.json: {name!r} has unknown category {category!r}")
        if not station.get("verified"):
            unverified += 1

    if unverified:
        checker.warn(f"radio_stations.json: {unverified}/{len(stations)} stations not verified by a probe")
    tags_total = sum(1 for s in stations if s.get("tags"))
    if tags_total < len(stations) * 0.5:
        checker.warn(f"radio_stations.json: only {tags_total}/{len(stations)} stations have tags")


def check_podcasts(data: dict, checker: Checker) -> None:
    podcasts = data.get("podcasts") or []
    categories = data.get("categories") or []
    if not isinstance(podcasts, list) or not podcasts:
        checker.fail("podcast_catalog.json: no podcasts")
        return
    if len(podcasts) < MIN_PODCASTS:
        checker.fail(f"podcast_catalog.json: {len(podcasts)} podcasts, need >= {MIN_PODCASTS}")
    if not categories:
        checker.fail("podcast_catalog.json: categories must be non-empty")

    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    pl = 0
    for index, podcast in enumerate(podcasts):
        if not isinstance(podcast, dict):
            checker.fail(f"podcast_catalog.json: entry #{index} is not an object")
            continue
        pid = str(podcast.get("id") or "")
        name = str(podcast.get("name") or "").strip()
        rss = str(podcast.get("rss_url") or "").strip()
        category = str(podcast.get("category") or "")
        if not pid:
            checker.fail(f"podcast_catalog.json: entry #{index} has no id")
        elif pid in seen_ids:
            checker.fail(f"podcast_catalog.json: duplicate id {pid} ({name})")
        seen_ids.add(pid)
        if pid and rss and pid != stable_id(rss):
            checker.fail(f"podcast_catalog.json: id/rss mismatch for {name!r}")
        if not name:
            checker.fail(f"podcast_catalog.json: entry #{index} has no name")
        if not rss.startswith(("http://", "https://")):
            checker.fail(f"podcast_catalog.json: invalid rss_url for {name!r}: {rss!r}")
        if rss in seen_urls:
            checker.fail(f"podcast_catalog.json: duplicate rss_url for {name!r}")
        seen_urls.add(rss)
        if categories and category not in categories:
            checker.fail(f"podcast_catalog.json: {name!r} has unknown category {category!r}")
        if podcast.get("language") == "pl":
            pl += 1
        if not podcast.get("cover_url"):
            checker.warn(f"podcast_catalog.json: {name!r} has no cover_url")

    missing = [feed for feed in PINNED_FEEDS if feed not in seen_urls]
    if missing:
        checker.fail(f"podcast_catalog.json: pinned feeds missing (ids would break): {missing}")
    if pl < len(podcasts) * 0.9:
        checker.warn(f"podcast_catalog.json: only {pl}/{len(podcasts)} entries are Polish")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    checker = Checker()
    # The NAS service runs as the `ethos-nas` user; a 0600 file from a dev shell
    # made every catalog request answer 500 (PermissionError).
    for path in (RADIO_FILE, PODCAST_FILE):
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            continue
        if not mode & 0o004:
            checker.fail(f"{path.name}: mode {mode:o} is not world-readable — the ethos-nas service cannot read it")

    check_radio(_load(RADIO_FILE, checker), checker)
    check_podcasts(_load(PODCAST_FILE, checker), checker)

    for warning in checker.warnings:
        print(f"[WARN] {warning}")
    for error in checker.errors:
        print(f"[FAIL] {error}")

    radio = _load_safe(RADIO_FILE)
    podcasts = _load_safe(PODCAST_FILE)
    print(
        f"[INFO] radio: {len(radio.get('stations') or [])} stations, "
        f"podcasts: {len(podcasts.get('podcasts') or [])} entries"
    )
    if checker.errors:
        return 1
    if not args.quiet:
        print("[PASS] catalog contracts")
    return 0


def _load_safe(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


if __name__ == "__main__":
    sys.exit(main())
