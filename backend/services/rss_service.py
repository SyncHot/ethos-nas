"""RSS/Atom feed parsing and rule matching for Downloads.

Pure functions only (no network, no storage) so the router can unit-test the
dangerous part — deciding whether an item matches a rule — without a feed
server. The router owns fetching, dedupe storage and task creation.
"""

import logging
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger("ethos-nas.services.rss_service")

DEFAULT_LIMIT = 100


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()


def _text(el) -> str:
    if el is None:
        return ""
    return "".join(el.itertext()).strip()


def _children(el, name: str) -> list:
    return [c for c in list(el) if _local(c.tag) == name]


def _first(el, name: str):
    for child in list(el):
        if _local(child.tag) == name:
            return child
    return None


def _attr(el, *names: str) -> str:
    if el is None:
        return ""
    for name in names:
        value = el.get(name)
        if value:
            return str(value).strip()
    return ""


def _enclosure(el) -> str:
    enc = _first(el, "enclosure")
    if enc is None:
        return ""
    url = _attr(enc, "url")
    kind = _attr(enc, "type").lower()
    if url and ("torrent" in kind or kind in ("", "application/octet-stream")):
        return url
    return url if url else ""


def _link(el) -> str:
    link = _first(el, "link")
    if link is not None:
        href = _attr(link, "href")
        if href:
            return href
        text = _text(link)
        if text:
            return text
    for child in list(el):
        if _local(child.tag) == "link":
            href = _attr(child, "href")
            if href:
                return href
    return ""


def _guid(el) -> str:
    for name in ("guid", "id"):
        node = _first(el, name)
        if node is not None and _text(node):
            return _text(node)
    return ""


def parse_feed(xml_text: str, limit: int = DEFAULT_LIMIT) -> list:
    """Parse RSS 2.0 or Atom into [{id, title, link, magnet, published}].

    Malformed XML raises `ET.ParseError`; the router logs and skips the feed.
    """
    root = ET.fromstring(xml_text)
    items = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    out = []
    for el in items[:limit]:
        link = _link(el)
        magnet = ""
        for node in el.iter():
            tag = _local(node.tag)
            if tag in ("magneturl", "magnet"):
                magnet = _text(node)
            elif tag in ("link", "url") and _attr(node, "href").startswith("magnet:"):
                magnet = _attr(node, "href")
            elif tag == "enclosure":
                url = _attr(node, "url")
                if url.startswith("magnet:"):
                    magnet = url
        enclosure = _enclosure(el)
        if not magnet and enclosure.startswith("magnet:"):
            magnet = enclosure
        published = ""
        for name in ("pubdate", "published", "updated"):
            node = _first(el, name)
            if node is not None:
                published = _text(node)
                break
        out.append({
            "id": _guid(el) or link or enclosure or _text(_first(el, "title")),
            "title": _text(_first(el, "title")),
            "link": link or enclosure,
            "magnet": magnet,
            "published": published,
        })
    return out


def compile_rule(pattern: str, exclude: str = "") -> tuple:
    """Compile the include/exclude patterns (invalid regex → ValueError)."""
    inc = re.compile(pattern, re.IGNORECASE) if pattern else None
    exc = re.compile(exclude, re.IGNORECASE) if exclude else None
    return inc, exc


def matches(item: dict, pattern: str, exclude: str = "") -> bool:
    """True when the item matches `pattern` and not `exclude`.

    Enabled feeds require a pattern (the router enforces it) — an empty pattern
    would queue the whole feed.
    """
    if not pattern:
        return False
    haystack = " ".join(str(item.get(k) or "") for k in ("title", "link", "magnet"))
    try:
        inc, exc = compile_rule(pattern, exclude)
    except re.error:
        return False
    if inc is None or not inc.search(haystack):
        return False
    return not (exc is not None and exc.search(haystack))
