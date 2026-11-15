"""Podcast RSS/Atom feed parsing (TODO.md P2-04).

The previous parser was a hand-rolled regex port of the Android client: it split
`<item>` blocks with a `.*?` regex (breaks on a CDATA body that contains a
closing item tag), could not follow nested `<image><url>` reliably, and mattered for
anything a real podcast host emits. This uses `xml.etree.ElementTree` and works
on *local* tag names, so `itunes:`, `podcast:` and any other namespace prefix
behave the same.

Two feed dialects are handled:

  RSS 2.0   <rss><channel><item>…   pubDate, enclosure url, guid, itunes:duration
  Atom      <feed><entry>…          published/updated, link rel="enclosure"

Output (unchanged from the router version, so both clients keep working):

  {"title", "description", "cover_url",
   "episodes": [{"id", "title", "artist", "thumb", "dur", "file_url", "date"}]}

Any feed the parser cannot understand raises `FeedError`; the router turns that
into a 422 with the same message the UI already shows. Episodes without an audio
URL are dropped (they cannot be played) and a feed with no playable episode is an
error, not an empty list.
"""

import hashlib
import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

logger = logging.getLogger("ethos-nas.services.podcast_service")

MAX_EPISODES = 50


class FeedError(Exception):
    """The document is not a usable RSS/Atom feed (message is user-facing)."""


def _local(tag: str) -> str:
    """`{http://www.itunes.com/…}duration` → `duration`."""
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _text(el: ET.Element | None) -> str:
    """All text inside an element (CDATA and nested markup included)."""
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _child(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in el if _local(child.tag) == name]


def _attr(el: ET.Element | None, *names: str) -> str:
    """First matching attribute by local name (`href`, `url`, …)."""
    if el is None:
        return ""
    for key, value in el.attrib.items():
        if _local(key) in names and value:
            return value.strip()
    return ""


def clean_html(s: str) -> str:
    """Flatten an HTML description to plain text (single line, no tags)."""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"</p\s*>", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_duration(s: str) -> int:
    """`3723`, `62:03` or `1:02:03` → seconds (0 when unparseable)."""
    t = (s or "").strip()
    if not t:
        return 0
    if t.isdigit():
        return int(t)
    parts = []
    for part in t.split(":"):
        if not part.strip().isdigit():
            return 0
        parts.append(int(part))
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def parse_rfc822(s: str) -> int:
    """RFC 822 / RFC 2822 date (RSS `pubDate`) → epoch seconds (0 on failure)."""
    if not (s or "").strip():
        return 0
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return 0


def parse_iso8601(s: str) -> int:
    """ISO 8601 date (Atom `published`) → epoch seconds (0 on failure)."""
    if not (s or "").strip():
        return 0
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return 0


def _cover_url(channel: ET.Element) -> str:
    """`<itunes:image href>` → `<image><url>` → `<logo>`/`<icon>`."""
    image = _child(channel, "image")
    for candidate in (_attr(image, "href", "url"), _attr(_child(channel, "image"), "href", "url")):
        if candidate:
            return candidate
    url = _text(_child(image, "url"))
    if url:
        return url
    return _text(_child(channel, "logo")) or _text(_child(channel, "icon"))


def _episode_media_url(item: ET.Element, is_atom: bool) -> str:
    """Audio URL: `enclosure url`, then Atom `link rel="enclosure" href`."""
    url = _attr(_child(item, "enclosure"), "url")
    if url:
        return url
    for link in _children(item, "link"):
        rel = _attr(link, "rel").lower()
        if rel == "enclosure":
            href = _attr(link, "href")
            if href:
                return href
    # Some hosts put the media URL in <media:content url="…">
    for child in item:
        if _local(child.tag) == "content":
            candidate = _attr(child, "url")
            if candidate:
                return candidate
    # RSS 2.0 without an enclosure: <link> is the episode page, not audio.
    return ""


def _episode_link(item: ET.Element, is_atom: bool) -> str:
    if is_atom:
        for link in _children(item, "link"):
            if _attr(link, "rel").lower() in ("", "alternate"):
                href = _attr(link, "href")
                if href:
                    return href
        return _text(_child(item, "id"))
    return _text(_child(item, "link")) or _text(_child(item, "guid"))


def _episode_date(item: ET.Element, is_atom: bool) -> int:
    rfc = parse_rfc822(_text(_child(item, "pubdate")))
    if rfc:
        return rfc
    for name in ("published", "updated", "date"):
        iso = parse_iso8601(_text(_child(item, name)))
        if iso:
            return iso
    return 0


def parse_feed(xml_text: str, max_episodes: int = MAX_EPISODES) -> dict:
    """Parse an RSS 2.0 or Atom document into the EthOS podcast shape.

    Raises `FeedError` when the document is not a feed or has no playable episode.
    """
    raw = (xml_text or "").strip()
    if not raw:
        raise FeedError("Pusty dokument — to nie jest feed RSS/Atom")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning("Podcast feed is not valid XML: %s", exc)
        raise FeedError("To nie wygląda na feed RSS/Atom") from exc

    root_name = _local(root.tag)
    if root_name == "rss":
        channel = _child(root, "channel")
        if channel is None:
            raise FeedError("Feed RSS bez kanału")
        is_atom = False
    elif root_name == "feed":
        channel = root
        is_atom = True
    elif root_name == "rdf":  # RSS 1.0
        channel = _child(root, "channel") or root
        is_atom = False
    else:
        raise FeedError("To nie wygląda na feed RSS/Atom")

    title = (_text(_child(channel, "title")) or _text(_child(channel, "itunes:title"))
             or "Podcast")
    description = clean_html(
        _text(_child(channel, "description"))
        or _text(_child(channel, "summary"))
        or _text(_child(channel, "subtitle"))
    )[:300]
    cover_url = _cover_url(channel)

    items = _children(channel, "entry" if is_atom else "item")
    if not items:
        # Be tolerant: some feeds omit <channel> wrapper or use the other dialect.
        items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
        is_atom = bool(items) and _local(items[0].tag) == "entry"
    if not items:
        raise FeedError("Feed nie zawiera odcinków")

    episodes: list[dict] = []
    for item in items:
        media_url = _episode_media_url(item, is_atom)
        if not media_url:
            continue  # no audio file — the episode cannot be played
        ep_title = _text(_child(item, "title")) or "Odcinek"
        link = _episode_link(item, is_atom)
        ep_id = (_text(_child(item, "guid")) or _text(_child(item, "id")) or link
                 or hashlib.sha256(ep_title.encode()).hexdigest()[:16])
        episodes.append({
            "id": ep_id,
            "title": ep_title,
            "artist": title,
            "thumb": cover_url,
            "dur": parse_duration(_text(_child(item, "duration"))),
            "file_url": media_url,
            "date": _episode_date(item, is_atom),
        })

    if not episodes:
        raise FeedError("Feed nie zawiera odcinków z plikiem audio")

    episodes.sort(key=lambda e: e["date"], reverse=True)
    return {
        "title": title,
        "description": description,
        "cover_url": cover_url,
        "episodes": episodes[:max(1, max_episodes)],
    }
