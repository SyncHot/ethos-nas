#!/usr/bin/env python3
"""File search service — walk, filters, scopes and result merge (no server).

Extracted from `routers/files.py` in P2-03; this locks the behaviour the two
search endpoints rely on.

Run:  .venv/bin/python tests/test_search_service.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services import search_service  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def _touch(path: Path, size: int = 4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ethos-search-test-"))
    try:
        _touch(tmp / "alpha.txt", 10)
        _touch(tmp / "beta.MP4", 100)
        _touch(tmp / "sub" / "alpha.log", 20)
        _touch(tmp / "deep" / "nested" / "gamma.png", 30)
        _touch(tmp / "node_modules" / "alpha.txt", 40)
        _touch(tmp / ".git" / "alpha.txt", 50)

        print("walk_search")
        results, truncated = search_service.walk_search(str(tmp), "alpha", 100)
        names = sorted(item["path"].replace(str(tmp) + "/", "") for item in results)
        check("finds matches recursively",
              names == ["alpha.txt", "sub/alpha.log"], str(names))
        check("not truncated", truncated is False)

        check("skips node_modules",
              not any("node_modules" in item["path"] for item in results))
        check("skips .git",
              not any("/.git/" in item["path"] for item in results))

        results, _ = search_service.walk_search(str(tmp), "alpha", 100, exts={"log"})
        check("extension filter", [item["name"] for item in results] == ["alpha.log"],
              str([item["name"] for item in results]))

        results, trunc = search_service.walk_search(str(tmp), "", 1)
        check("limit caps the result list", len(results) == 1, str(len(results)))
        check("truncated flag set", trunc is True)

        results, _ = search_service.walk_search(str(tmp), "beta", 100, exts={"mp4"})
        check("extension match is case-insensitive",
              [item["name"] for item in results] == ["beta.MP4"],
              str([item["name"] for item in results]))

        results, _ = search_service.walk_search(str(tmp), "gamma", 100, min_size=10_000)
        check("min_size filters small files", results == [])

        results, _ = search_service.walk_search(str(tmp), "sub", 100)
        check("directories are returned without filters",
              any(item["is_dir"] for item in results), str(results))

        print("type filters")
        check("'all' has no extension filter", search_service.type_extensions("all") is None)
        check("images include png", "png" in (search_service.type_extensions("images") or set()))
        check("unknown type has no filter", search_service.type_extensions("nope") is None)

        print("merge_results")
        merged, stats = search_service.merge_results([
            (str(tmp), ([{"name": "old", "modified": 1}], False)),
            (str(tmp / "sub"), ([{"name": "new", "modified": 9}], True)),
        ])
        check("newest first", [item["name"] for item in merged] == ["new", "old"],
              str([item["name"] for item in merged]))
        check("root attached", merged[1]["root"] == str(tmp))
        check("root stats match", [s["count"] for s in stats] == [1, 1] and stats[1]["truncated"])

        check("skip dirs are the documented set",
              {"node_modules", ".git", "venv", "__pycache__"} <= search_service.SEARCH_SKIP_DIRS)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("All search service checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
