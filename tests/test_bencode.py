#!/usr/bin/env python3
"""Unit tests for backend/services/bencode.py (no server).

Run:  .venv/bin/python tests/test_bencode.py
"""

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.services.bencode import (  # noqa: E402
    bencode_any,
    bdecode,
    parse_magnet,
    parse_torrent,
)

_failures = 0


def check(name, ok, detail=""):
    global _failures
    if not ok:
        _failures += 1
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    print("bencode service")

    # ── Encoder/decoder roundtrip ────────────────────────────────────────────
    value = {b"a": 1, b"b": [b"x", {b"c": b"y"}], b"d": b"bytes"}
    encoded = bencode_any(value)
    check("encoder sorts dictionary keys",
          encoded.startswith(b"d1:ai1e1:b") and encoded.endswith(b"e"))
    check("decoder roundtrips containers", bdecode(encoded) == value)

    # ── Single-file torrent ──────────────────────────────────────────────────
    info = {b"name": b"movie.mkv", b"length": 12345}
    raw_info = bencode_any(info)
    torrent = b"d4:info" + raw_info + b"e"
    parsed = parse_torrent(torrent)
    check("info-hash is the SHA-1 of the raw info dict",
          parsed["hash"] == hashlib.sha1(raw_info).hexdigest(), parsed["hash"])
    check("name comes from the top-level dict", parsed["name"] == "movie.mkv")
    check("size is the file length", parsed["size"] == 12345)
    check("magnet carries hash + display name",
          parsed["magnet"].startswith("magnet:?xt=urn:btih:") and "dn=movie.mkv" in parsed["magnet"])

    # ── Multi-file torrent ───────────────────────────────────────────────────
    info_multi = {
        b"name": b"Show",
        b"files": [
            {b"length": 10, b"path": [b"Season 1", b"a.mkv"]},
            {b"length": 20, b"path": [b"Season 1", b"b.srt"]},
        ],
    }
    raw_multi = bencode_any(info_multi)
    parsed_multi = parse_torrent(b"d4:info" + raw_multi + b"e")
    check("multi-file paths are flattened",
          [f["path"] for f in parsed_multi["files"]] == ["Season 1/a.mkv", "Season 1/b.srt"])
    check("multi-file total size is summed", parsed_multi["size"] == 30)
    check("multi-file torrent keeps its name", parsed_multi["name"] == "Show")

    # ── Malformed input ──────────────────────────────────────────────────────
    for bad, label in ((b"garbage", "non-bencode"), (b"d4:infoi1ee", "info is not a dict")):
        try:
            parse_torrent(bad)
        except ValueError:
            check(f"{label} → ValueError", True)
        else:
            check(f"{label} → ValueError", False)

    # ── Magnet parsing ───────────────────────────────────────────────────────
    m = parse_magnet("magnet:?xt=urn:btih:" + "A" * 40 + "&dn=My%20File")
    check("magnet hash is lowercased", m["hash"] == "a" * 40)
    check("magnet display name is URL-decoded", m["name"] == "My File")
    check("magnet without dn still returns a hash",
          parse_magnet("magnet:?xt=urn:btih:" + "b" * 40)["name"] == "")

    print(f"\n{'FAIL' if _failures else 'OK'} — {_failures} failure(s)")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
