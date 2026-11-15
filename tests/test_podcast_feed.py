#!/usr/bin/env python3
"""Podcast feed parser (TODO.md P2-04) — RSS 2.0 + Atom edge cases.

Runs in-process, no server needed:

    .venv/bin/python tests/test_podcast_feed.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services.podcast_service import (  # noqa: E402
    FeedError, clean_html, parse_duration, parse_feed, parse_iso8601, parse_rfc822,
)

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Testowy Podcast</title>
    <description><![CDATA[<p>Opis &amp; wi&#281;cej</p>]]></description>
    <itunes:image href="https://cdn.example/cover.jpg"/>
    <image><url>https://cdn.example/fallback.jpg</url></image>
    <item>
      <title>Odcinek 1</title>
      <guid>ep-1</guid>
      <pubDate>Tue, 10 Nov 2026 08:00:00 +0100</pubDate>
      <itunes:duration>1:02:03</itunes:duration>
      <description><![CDATA[ not an item end ]]></description>
      <enclosure url="https://cdn.example/ep1.mp3" type="audio/mpeg" length="123"/>
    </item>
    <item>
      <title>Odcinek 2 (nowszy)</title>
      <guid>ep-2</guid>
      <pubDate>Wed, 11 Nov 2026 09:30:00 +0000</pubDate>
      <itunes:duration>3723</itunes:duration>
      <enclosure url="https://cdn.example/ep2.mp3" type="audio/mpeg" length="123"/>
    </item>
    <item>
      <title>Bez audio</title>
      <guid>ep-3</guid>
      <pubDate>Thu, 12 Nov 2026 09:30:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Podcast</title>
  <subtitle>Sub</subtitle>
  <logo>https://cdn.example/atom.png</logo>
  <entry>
    <title>Atom ep 1</title>
    <id>urn:uuid:1</id>
    <published>2026-11-01T10:00:00Z</published>
    <link rel="alternate" href="https://example.org/ep1"/>
    <link rel="enclosure" type="audio/mpeg" href="https://cdn.example/a1.mp3"/>
  </entry>
</feed>
"""


def main() -> int:
    feed = parse_feed(RSS, 10)
    check("rss title", feed["title"] == "Testowy Podcast", feed["title"])
    check("rss description flattened", feed["description"] == "Opis & więcej", feed["description"])
    check("itunes:image wins", feed["cover_url"] == "https://cdn.example/cover.jpg", feed["cover_url"])
    titles = [e["title"] for e in feed["episodes"]]
    check("unplayable episode dropped", "Bez audio" not in titles, str(titles))
    check("newest first", titles[0] == "Odcinek 2 (nowszy)", str(titles))
    check("CDATA containing a closing tag is safe", len(feed["episodes"]) == 2, str(titles))
    by_id = {e["id"]: e for e in feed["episodes"]}
    check("hh:mm:ss duration", by_id["ep-1"]["dur"] == 3723, str(by_id["ep-1"]["dur"]))
    check("bare seconds duration", by_id["ep-2"]["dur"] == 3723, str(by_id["ep-2"]["dur"]))
    check("pubDate parsed", by_id["ep-1"]["date"] == 1794294000, str(by_id["ep-1"]["date"]))
    check("artist is the channel title", by_id["ep-1"]["artist"] == "Testowy Podcast")
    check("limit respected", len(parse_feed(RSS, 1)["episodes"]) == 1)

    atom = parse_feed(ATOM, 5)
    check("atom title", atom["title"] == "Atom Podcast", atom["title"])
    check("atom description from subtitle", atom["description"] == "Sub", atom["description"])
    check("atom cover from logo", atom["cover_url"] == "https://cdn.example/atom.png", atom["cover_url"])
    check("atom enclosure link", atom["episodes"][0]["file_url"] == "https://cdn.example/a1.mp3",
          atom["episodes"][0]["file_url"])
    check("atom id", atom["episodes"][0]["id"] == "urn:uuid:1", atom["episodes"][0]["id"])
    check("atom published parsed", atom["episodes"][0]["date"] == 1793527200,
          str(atom["episodes"][0]["date"]))

    for label, payload in (("empty", ""), ("html", "<html><body>no</body></html>"),
                           ("broken", "<rss><channel><title>x</title>")):
        try:
            parse_feed(payload, 5)
            check(f"{label} document rejected", False, "no FeedError")
        except FeedError:
            check(f"{label} document rejected", True)

    try:
        parse_feed("<rss><channel><title>t</title><item><title>a</title></item></channel></rss>", 5)
        check("feed without audio rejected", False, "no FeedError")
    except FeedError:
        check("feed without audio rejected", True)

    check("clean_html unwraps entities", clean_html("a &lt;b&gt; <br/> c") == "a <b> c",
          clean_html("a &lt;b&gt; <br/> c"))
    check("duration variants", (parse_duration("62:03"), parse_duration("1:02:03"),
                                parse_duration(""), parse_duration("abc")) == (3723, 3723, 0, 0))
    check("rfc822 invalid → 0", parse_rfc822("not a date") == 0)
    check("iso8601 invalid → 0", parse_iso8601("nope") == 0)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
