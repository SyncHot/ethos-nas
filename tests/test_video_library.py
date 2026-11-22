#!/usr/bin/env python3
"""Library walk/cache/home payload checks for the Videos router."""

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="ethos-video-lib-test-")
os.environ["ETHOS_DATA_DIR"] = os.path.join(_TMP, "data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.routers import video_station as vs  # noqa: E402

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"[PASS] {name}")
    else:
        _failed += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    try:
        root = os.path.join(_TMP, "media")
        os.makedirs(root)
        Path(root, "Movie.One.2020.mp4").write_bytes(b"")
        Path(root, "Movie.Two.2021.mp4").write_bytes(b"")

        listing = vs._walk_all([root])
        videos = listing.get("videos", [])
        check("walk finds both videos", len(videos) == 2, str(len(videos)))
        check("walk is not truncated", listing.get("truncated") is False)
        check("parsed title is attached", any(v.get("title") for v in videos))

        resumes = {
            videos[0]["path"]: {"time": 120.0, "duration": 600.0, "updated_at": 10.0},
            videos[1]["path"]: {"time": 600.0, "duration": 600.0, "updated_at": 20.0},
        }
        home = vs._home_payload(videos, resumes, limit=10)
        check("unfinished item is in Continue Watching",
              [v["path"] for v in home["continue_watching"]] == [videos[0]["path"]])
        check("finished item is not in Continue Watching",
              all(v["path"] != videos[1]["path"] for v in home["continue_watching"]))
        check("recently added includes both", len(home["recently_added"]) == 2)

        first = asyncio.run(vs._walk_all_cached([root]))
        check("cached walk returns both", len(first.get("videos", [])) == 2)
        Path(root, "Movie.Three.2022.mp4").write_bytes(b"")
        second = asyncio.run(vs._walk_all_cached([root]))
        check("cache hides a new file inside the TTL", len(second.get("videos", [])) == 2,
              str(len(second.get("videos", []))))
        time.sleep(0.02)
        refreshed = asyncio.run(vs._walk_all_cached([root], refresh=True))
        check("refresh sees the new file", len(refreshed.get("videos", [])) == 3,
              str(len(refreshed.get("videos", []))))

        filtered = asyncio.run(vs._walk_all_cached([root], "three", refresh=True))
        check("query filters by filename", len(filtered.get("videos", [])) == 1)

        check("home payload is deterministic",
              vs._home_payload([], {}, 5) == {"continue_watching": [], "recently_added": []})
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
