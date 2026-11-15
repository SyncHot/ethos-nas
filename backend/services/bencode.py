"""BitTorrent metadata helpers — bencode decoder/encoder, magnet and .torrent parsing.

Extracted from `backend/routers/downloads.py` (TODO.md P2-01) so the HTTP
router and the qBittorrent shim share one implementation and it can be unit
tested without booting the app.

No third-party dependencies: a minimal bencode decoder is enough for
`.torrent` metadata and keeps the install lean.
"""

import hashlib
import logging
import os
import urllib.parse

logger = logging.getLogger("ethos-nas.services.bencode")

__all__ = [
    "bencode_any",
    "bdecode",
    "parse_magnet",
    "parse_torrent",
    "torrent_files",
]


def parse_magnet(url: str) -> dict:
    """Extract info-hash and display name from a magnet link."""
    import re

    m = re.search(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", url or "")
    h = m.group(1).lower() if m else ""
    dn = ""
    try:
        q = urllib.parse.urlparse(url).query
        for part in q.split("&"):
            if part.startswith("dn="):
                dn = urllib.parse.unquote(part[3:])
                break
    except Exception:
        pass  # intentional: a failure here must not break the caller
    return {"hash": h, "name": dn}


def bencode_any(value) -> bytes:
    """Encode any supported value (containers included)."""
    if isinstance(value, bool):
        raise ValueError("cannot bencode bool")
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, str):
        b = value.encode("utf-8")
        return f"{len(b)}:".encode() + b
    if isinstance(value, bytes):
        return f"{len(value)}:".encode() + value
    if isinstance(value, (list, tuple)):
        out = b"l"
        for item in value:
            out += bencode_any(item)
        return out + b"e"
    if isinstance(value, dict):
        def _kb(x):
            return x if isinstance(x, bytes) else str(x).encode("utf-8")
        out = b"d"
        for k in sorted(value.keys(), key=_kb):
            kb = _kb(k)
            out += f"{len(kb)}:".encode() + kb + bencode_any(value[k])
        return out + b"e"
    raise ValueError(f"cannot bencode {type(value).__name__}")


def _bdecode_at(data: bytes, i: int):
    """Decode one bencoded value starting at offset i. Returns (value, next_offset)."""
    c = data[i:i + 1]
    if not c:
        raise ValueError("truncated bencode")
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        items, j = [], i + 1
        while data[j:j + 1] != b"e":
            v, j = _bdecode_at(data, j)
            items.append(v)
        return items, j + 1
    if c == b"d":
        d, j = {}, i + 1
        while data[j:j + 1] != b"e":
            k, j = _bdecode_at(data, j)
            v, j = _bdecode_at(data, j)
            d[k] = v  # keys stay bytes → canonical re-encoding for the info-hash
        return d, j + 1
    if c.isdigit():
        colon = data.index(b":", i)
        n = int(data[i:colon])
        start, end = colon + 1, colon + 1 + n
        if end > len(data):
            raise ValueError("truncated bencoded string")
        return data[start:end], end
    raise ValueError(f"invalid bencode at byte {c!r}")


def bdecode(data: bytes) -> dict:
    """Decode a full .torrent file (top-level dictionary)."""
    if not data or data[0:1] != b"d":
        raise ValueError("not a torrent file")
    value, _ = _bdecode_at(data, 0)
    return value


def _s(v) -> str:
    """bytes/str → str (bencoded strings are bytes)."""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v or "")


def torrent_files(info: dict) -> list:
    """Flatten a torrent's info dict into [{name, size, path}, ...] (path = relative)."""
    if b"length" in info and isinstance(info.get(b"length"), int):
        name = _s(info.get(b"name")).replace("\\", "/").strip("/")
        return [{"name": os.path.basename(name) or "file.bin",
                 "size": int(info[b"length"]), "path": ""}]
    out = []
    for f in info.get(b"files") or []:
        if not isinstance(f, dict):
            continue
        parts = [_s(p).replace("\\", "/").strip("/") for p in (f.get(b"path") or [])]
        rel = "/".join(p for p in parts if p)
        out.append({
            "name": os.path.basename(rel) or "file.bin",
            "size": int(f.get(b"length") or 0),
            "path": rel,
        })
    return out


def _raw_info_span(data: bytes):
    """Byte offsets (start, end) of the 'info' dict inside a torrent file.

    Walks the top-level dictionary with the real decoder so non-string values
    (integers like `creation date`, lists, nested dicts) are skipped correctly.
    """
    if not data or data[0:1] != b"d":
        return None
    i = 1
    while data[i:i + 1] != b"e":
        key, j = _bdecode_at(data, i)   # keys are bytes
        vstart = j
        _, vend = _bdecode_at(data, vstart)
        if key == b"info":
            return vstart, vend
        i = vend
    return None


def parse_torrent(data: bytes) -> dict:
    """Parse .torrent bytes → {magnet, hash, name, size, files} (raises ValueError)."""
    meta = bdecode(data)
    if not isinstance(meta, dict):
        raise ValueError("not a torrent file")
    info = meta.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("torrent has no info dictionary")
    # Info-hash = SHA-1 of the raw bencoded 'info' dictionary (guaranteed canonical).
    span = _raw_info_span(data)
    try:
        if span:
            infohash = hashlib.sha1(data[span[0]:span[1]]).hexdigest()
        else:  # fallback: re-encode the decoded dict canonically
            infohash = hashlib.sha1(bencode_any(info)).hexdigest()
    except (TypeError, ValueError):
        raise ValueError("torrent info dict is not encodable")
    files = torrent_files(info)
    name = _s(meta.get(b"name")).strip() or _s(info.get(b"name")).strip()
    if not name:
        multi = len(files) > 1 or any(f["path"] for f in files)
        name = (files[0]["name"] if files and not multi else None) or f"torrent-{infohash[:12]}"
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn={urllib.parse.quote(name)}"
    return {"magnet": magnet, "hash": infohash, "name": name,
            "size": sum(f["size"] for f in files), "files": files}
