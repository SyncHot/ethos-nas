#!/usr/bin/env python3
"""Unit tests for backend/services/download_config.py (isolated data dir).

Run:  .venv/bin/python tests/test_download_config.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before backend.config is imported (settings reads it at import).
_DATA = tempfile.mkdtemp(prefix="ethos-dlcfg-test-")
os.environ["ETHOS_DATA_DIR"] = _DATA

from backend.services import download_config as cfg  # noqa: E402
from backend.services import path_scope  # noqa: E402

_failures = 0


def check(name, ok, detail=""):
    global _failures
    if not ok:
        _failures += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    # Engine-level config unit test: no request identity is involved, so use
    # the same privileged identity as the background pump.
    path_scope.CURRENT_ROLE.set("admin")
    path_scope.CURRENT_USER.set("system")
    print("download_config")
    try:
        # ── Small parsers ────────────────────────────────────────────────────
        check("parse_hhmm accepts 00:00", cfg.parse_hhmm("00:00") == 0)
        check("parse_hhmm accepts 23:59", cfg.parse_hhmm("23:59") == 1439)
        check("parse_hhmm rejects 24:00", cfg.parse_hhmm("24:00") is None)
        check("parse_hhmm rejects garbage", cfg.parse_hhmm("noon") is None)

        # ── Normalizers ──────────────────────────────────────────────────────
        cats = cfg.norm_categories([
            {"name": "movies", "save_path": "/mnt/movies"},
            {"name": "MOVIES", "save_path": "/tmp/ignored"},
            {"name": "", "save_path": "/x"},
            "not-a-dict",
        ])
        check("categories are deduped case-insensitively", len(cats) == 1, str(cats))
        check("category save_path is kept", cats[0]["save_path"] == "/mnt/movies")
        check("category list is capped at 50",
              len(cfg.norm_categories([{"name": f"c{i}"} for i in range(60)])) == 50)

        feeds = cfg.norm_rss_feeds([
            {"id": "a", "url": "https://indexer/rss", "interval_minutes": 1},
            {"id": "b", "url": "ftp://nope", "pattern": "x"},
            {"url": "http://indexer2/rss", "pattern": "y"},
        ])
        check("non-http RSS feeds are dropped", len(feeds) == 2, str(feeds))
        check("RSS interval is clamped to 5 minutes", feeds[0]["interval_minutes"] == 5)
        check("missing feed id is generated", bool(feeds[1]["id"]))

        # ── Defaults on an empty data dir ────────────────────────────────────
        base = cfg.load_config()
        check("defaults: 3 concurrent downloads", base["max_concurrent"] == 3)
        check("defaults: schedule disabled", base["schedule"]["enabled"] is False)
        check("defaults: watch folder placeholder", len(base["watch_folders"]) == 1)
        check("defaults: no hoster-link provider", base["debrid"]["link_provider"] == "")
        check("defaults: tb7 credentials empty",
              base["debrid"]["tb7_username"] == "" and base["debrid"]["tb7_password"] == "")

        # ── v1 → v5 migration ────────────────────────────────────────────────
        Path(_DATA, "downloads_config.json").write_text(json.dumps({
            "version": 1, "watch_folder": "/mnt/torrents", "max_concurrent": 99,
        }), encoding="utf-8")
        migrated = cfg.load_config()
        check("legacy watch_folder becomes a watch_folders entry",
              migrated["watch_folders"][0]["folder"] == "/mnt/torrents",
              str(migrated["watch_folders"]))
        check("max_concurrent is clamped on read", migrated["max_concurrent"] == 16)
        check("schedule/speed_limit defaults added by migration",
              migrated["schedule"]["start"] == "01:00" and migrated["speed_limit"]["global_bps"] == 0)
        check("categories/rss_feeds exist after migration",
              migrated["categories"] == [] and migrated["rss_feeds"] == [])

        # ── Save/load roundtrip + category destination ───────────────────────
        migrated["download_dir"] = _DATA
        migrated["categories"] = [
            {"name": "movies", "save_path": "movies"},
            {"name": "vr", "save_path": ""},
        ]
        migrated["schedule"] = {"enabled": True, "start": "22:00", "end": "03:00"}
        cfg.save_config(migrated)
        reloaded = cfg.load_config()
        check("saved schedule is reloaded",
              reloaded["schedule"]["enabled"] is True and reloaded["schedule"]["start"] == "22:00")
        check("relative category path resolves under download_dir",
              cfg.category_target(reloaded, "movies") == os.path.join(_DATA, "movies"),
              cfg.category_target(reloaded, "movies"))
        check("blank category path resolves to <download_dir>/<name>",
              cfg.category_target(reloaded, "vr") == os.path.join(_DATA, "vr"),
              cfg.category_target(reloaded, "vr"))
        check("blank category path keeps the configured name case",
              cfg.category_target(
                  {**reloaded, "categories": [{"name": "VR", "save_path": ""}]}, "vr")
              == os.path.join(_DATA, "VR"))
        for hostile, expected in (("../evil", os.path.join(_DATA, "evil")), ("..", "")):
            target = cfg.category_target(
                {**reloaded, "categories": [{"name": hostile, "save_path": ""}]}, hostile)
            check(f"category name {hostile!r} cannot escape download_dir", target == expected, target)
        check("unknown category has no destination", cfg.category_target(reloaded, "nope") == "")

        # ── v8 → v9: the link-provider slot keeps AllDebrid routing ─────────
        Path(_DATA, "downloads_config.json").write_text(json.dumps({
            "version": 8,
            "debrid": {"provider": "alldebrid", "alldebrid_api_key": "key"},
        }), encoding="utf-8")
        migrated9 = cfg.load_config()
        check("v8→v9: AllDebrid becomes the link provider too",
              migrated9["debrid"]["link_provider"] == "alldebrid",
              str(migrated9["debrid"]))
        check("v8→v9: tb7 fields default to empty",
              migrated9["debrid"]["tb7_username"] == "" and migrated9["debrid"]["tb7_password"] == "")
        Path(_DATA, "downloads_config.json").write_text(json.dumps({
            "version": 8,
            "debrid": {"provider": "torbox", "torbox_api_key": "key"},
        }), encoding="utf-8")
        migrated9b = cfg.load_config()
        check("v8→v9: a torrent-only provider gets no link slot",
              migrated9b["debrid"]["link_provider"] == "", str(migrated9b["debrid"]))

        # ── qB key lifecycle ──────────────────────────────────────────────────
        key1 = cfg.qb_get_api_key()
        check("qB key is generated with the ethos_ prefix", key1.startswith("ethos_"), key1[:10])
        check("qB key is stable across calls", cfg.qb_get_api_key() == key1)

        # ── Category upsert/remove (qB shim path) ────────────────────────────
        cfg.upsert_category("tv", "/mnt/tv")
        check("upsert_category persists",
              any(c["name"] == "tv" for c in cfg.load_config()["categories"]))
        cfg.remove_category("tv")
        check("remove_category deletes it",
              not any(c["name"] == "tv" for c in cfg.load_config()["categories"]))
    finally:
        shutil.rmtree(_DATA, ignore_errors=True)

    print(f"\n{'FAIL' if _failures else 'OK'} — {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
