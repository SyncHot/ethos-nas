#!/usr/bin/env python3
"""Unit tests for backend/services/rss_service.py (no network).

Run:  .venv/bin/python tests/test_rss_service.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.rss_service import matches, parse_feed  # noqa: E402

_failures = 0

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Indexer</title>
  <item>
    <title>Some.Show.S01E01.1080p.WEB-DL</title>
    <link>https://indexer/download/1</link>
    <guid>guid-1</guid>
    <enclosure url="magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa&amp;dn=Some.Show" type="application/x-bittorrent"/>
    <pubDate>Mon, 13 Nov 2026 10:00:00 +0000</pubDate>
  </item>
  <item>
    <title>Other.Show.S01E02.2160p</title>
    <link>https://indexer/file/2.torrent</link>
    <guid>guid-2</guid>
  </item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Indexer</title>
  <entry>
    <title>Movie.2025.1080p</title>
    <id>atom-1</id>
    <link href="https://indexer/atom/1.torrent"/>
    <updated>2026-11-13T10:00:00Z</updated>
  </entry>
</feed>"""


def check(name, ok, detail=""):
    global _failures
    if not ok:
        _failures += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    print("rss_service")

    items = parse_feed(RSS)
    check("RSS 2.0 parses both items", len(items) == 2, str(len(items)))
    check("title/link/guid are extracted",
          items[0]["title"].startswith("Some.Show") and items[0]["link"] == "https://indexer/download/1"
          and items[0]["id"] == "guid-1")
    check("magnet enclosure becomes the magnet",
          items[0]["magnet"].startswith("magnet:?xt=urn:btih:aaaa"))
    check(".torrent enclosure stays as the link", items[1]["link"].endswith(".torrent"))
    check("pubDate is captured", "2026" in items[0]["published"])

    atom = parse_feed(ATOM)
    check("Atom entry parses", len(atom) == 1 and atom[0]["id"] == "atom-1", str(atom))
    check("Atom link href is used", atom[0]["link"].endswith("/atom/1.torrent"))

    check("include regex matches the title", matches(items[0], r"S01E01"))
    check("include regex is case-insensitive", matches(items[0], r"some\.show"))
    check("exclude regex vetoes a match",
          not matches(items[0], r"1080p", r"WEB-DL"))
    check("non-matching item is rejected", not matches(items[1], r"S01E01"))
    check("empty pattern never matches", not matches(items[0], ""))
    check("invalid regex is treated as no match", not matches(items[0], "("))
    check("link is part of the searchable text", matches(items[1], r"/file/2"))

    print(f"\n{'FAIL' if _failures else 'OK'} — {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
