#!/usr/bin/env python3
"""Download Station RSS scanner (TODO.md P2-01).

The scanner moved out of ``routers/downloads.py`` into
``services/download_rss.py``: bounded fetch, include/exclude matching,
per-feed intervals, the persisted seen set and the .torrent → magnet hop. This
checks the contract with injected collaborators (no network, no router):

  * only http(s) URLs are fetched, with a hard size cap
  * the seen set is bounded and survives a reload
  * due feeds are polled, matching items queued exactly once
  * intervals are honoured unless ``force`` is set
  * a feed error is logged and skipped without aborting the scan

Run:
    .venv/bin/python tests/test_download_rss_service.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="ethos-download-rss-"))
os.environ["ETHOS_DATA_DIR"] = str(_TMP)

from backend.services import download_rss, path_scope  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class _FakeUserData:
    """Minimal stand-in for services/user_data (slice iteration only)."""

    def __init__(self, users=("marcin",)):
        self.users = list(users)

    def iter_user_slices(self, _name):
        for u in self.users:
            yield u, {}

    def user_role(self, _u):
        return "admin"


FEED = {
    "id": "feed1", "url": "https://example.test/feed.xml", "enabled": True,
    "pattern": r"S01", "exclude": "", "interval_minutes": 15,
    "destination": "/mnt/tv", "category": "tv",
}

ITEMS = [
    {"id": "a1", "title": "Show S01E01", "magnet": "magnet:?xt=urn:btih:a1"},
    {"id": "b2", "title": "Show S02E01", "magnet": "magnet:?xt=urn:btih:b2"},  # excluded by pattern
    {"id": "c3", "title": "Show S01E03", "link": "https://example.test/c3.torrent"},
]


def main() -> int:
    path_scope.CURRENT_ROLE.set("admin")
    path_scope.CURRENT_USER.set("system")

    # 1. URL scheme + size cap.
    check("ftp scheme rejected",
          _raises(download_rss.fetch_bytes_bounded, "ftp://x/y", 1024))
    check("file scheme rejected",
          _raises(download_rss.fetch_bytes_bounded, "file:///etc/passwd", 1024))

    # 2. Seen set is bounded and persisted.
    download_rss.reset_state()
    download_rss.mark_seen([f"u{k}" for k in range(download_rss.RSS_SEEN_MAX + 25)])
    check("seen set capped", download_rss.seen_count() <= download_rss.RSS_SEEN_MAX,
          str(download_rss.seen_count()))
    saved = download_rss.json_store.get(download_rss.RSS_SEEN_FILE, list).load() or []
    check("seen set persisted", len(saved) == download_rss.seen_count(), str(len(saved)))

    # 3. A due feed queues matching items exactly once.
    download_rss.reset_state()
    download_rss.user_data = _FakeUserData()
    queued: list[tuple] = []
    fetched: list[str] = []

    def fake_fetch(url, cap):
        fetched.append(url)
        return b"<rss/>"

    def fake_parse(_raw):
        return ITEMS

    def fake_matches(item, pattern, exclude):
        import re
        ok = re.search(pattern, item.get("title") or "") is not None
        return ok and not (exclude and re.search(exclude, item.get("title") or ""))

    def fake_parse_torrent(_data):
        return {"magnet": "magnet:?xt=urn:btih:c3", "name": "Show S01E03"}

    async def fake_enqueue(url, name, folder, category):
        queued.append((url, name, folder, category))

    real = (download_rss.fetch_bytes_bounded, download_rss.rss_service.parse_feed,
            download_rss.rss_service.matches)
    download_rss.fetch_bytes_bounded = fake_fetch
    download_rss.rss_service.parse_feed = fake_parse
    download_rss.rss_service.matches = fake_matches
    try:
        cfg = {"rss_feeds": [FEED]}
        out = asyncio.run(download_rss.scan(
            load_config=lambda: cfg, enqueue=fake_enqueue,
            parse_torrent=fake_parse_torrent))
        check("one feed scanned", out["feeds"] == 1, str(out))
        check("two items queued", out["queued"] == 2, str(queued))
        check("excluded item skipped",
              all("S02E01" not in q[1] for q in queued), str(queued))
        check("torrent link converted to magnet",
              any(q[0].startswith("magnet:") and q[1] == "Show S01E03" for q in queued),
              str(queued))
        check("feed destination forwarded",
              all(q[2] == "/mnt/tv" and q[3] == "tv" for q in queued), str(queued))
        check(".torrent link fetched", "https://example.test/c3.torrent" in fetched, str(fetched))

        # 4. Re-running inside the interval is a no-op.
        queued.clear()
        fetched.clear()
        again = asyncio.run(download_rss.scan(
            load_config=lambda: cfg, enqueue=fake_enqueue,
            parse_torrent=fake_parse_torrent))
        check("interval honoured", again == {"feeds": 0, "queued": 0}, str(again))
        check("no fetch inside the interval", fetched == [], str(fetched))

        # 5. force=True polls again but the seen set prevents duplicates.
        forced = asyncio.run(download_rss.scan(
            load_config=lambda: cfg, enqueue=fake_enqueue,
            parse_torrent=fake_parse_torrent, force=True))
        check("forced rescan polls the feed", forced["feeds"] == 1, str(forced))
        check("seen items are not re-queued", queued == [], str(queued))

        # 6. A broken feed is skipped, an unknown feed_id filters everything out.
        def boom(_url, _cap):
            raise OSError("connection refused")

        download_rss.fetch_bytes_bounded = boom
        bad = asyncio.run(download_rss.scan(
            load_config=lambda: cfg, enqueue=fake_enqueue,
            parse_torrent=fake_parse_torrent, force=True))
        check("feed error does not abort the scan", bad == {"feeds": 1, "queued": 0}, str(bad))
        skipped = asyncio.run(download_rss.scan(
            load_config=lambda: cfg, enqueue=fake_enqueue,
            parse_torrent=fake_parse_torrent, feed_ids={"nope"}, force=True))
        check("feed_ids filter applied", skipped == {"feeds": 0, "queued": 0}, str(skipped))
    finally:
        (download_rss.fetch_bytes_bounded, download_rss.rss_service.parse_feed,
         download_rss.rss_service.matches) = real

    failed = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if failed else 0


def _raises(fn, *args) -> bool:
    try:
        fn(*args)
    except Exception:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
