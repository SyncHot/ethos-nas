"""Derived-media cache helpers (thumbnails, covers, transcodes).

Every cached artifact (image thumbnail, video master frame, per-size slot,
custom cover, H.264 transcode) stores a *sidecar* meta file next to it with the
source file's mtime + size. Freshness is therefore a pure mtime/size comparison
— no database, no rescan, and a restart (or a cache directory copied to another
disk) keeps working.

Shared by ``services/thumbnails.py``, ``services/transcode.py`` and
``routers/files.py`` so the three pipelines cannot drift apart (TODO.md
P2-03).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.media_cache")

# Cache roots — derived media never mixes with user-visible data.
THUMB_DIR: Path = settings.DATA_DIR / "thumbnails"
TRANSCODE_DIR: Path = settings.DATA_DIR / "transcodes"

META_SUFFIX = ".meta.json"


def set_cache_root(root: Path) -> None:
    """Point thumbnails (+ transcodes) at another root (tests, migrations)."""
    global THUMB_DIR, TRANSCODE_DIR
    THUMB_DIR = Path(root) / "thumbnails"
    TRANSCODE_DIR = Path(root) / "transcodes"


def ensure_thumb_dir() -> None:
    THUMB_DIR.mkdir(parents=True, exist_ok=True)


def make_thumb_key(abs_path: str, size: int) -> str:
    h = hashlib.sha256(f"{abs_path}:{size}".encode()).hexdigest()[:16]
    return f"{h}_{size}"


def thumb_meta_file(cache_file: Path) -> Path:
    """Sidecar file storing source mtime+size the thumbnail was built from."""
    return cache_file.with_name(cache_file.name + META_SUFFIX)


def cache_is_fresh(cache_file: Path, abs_path: str) -> bool:
    """True if a cached artifact matches the current source (mtime AND size)."""
    meta = thumb_meta_file(cache_file)
    try:
        st = os.stat(abs_path)
        data = json.loads(meta.read_text())
        return (abs(st.st_mtime - float(data.get("src_mtime", 0))) < 1.0
                and int(data.get("src_size", -1)) == st.st_size)
    except Exception:
        # No meta / unreadable → treat as stale; regenerate once to rebuild it.
        return False


def write_thumb_meta(cache_file: Path, abs_path: str, extra: dict | None = None):
    """Record the source mtime+size a cached artifact was built from.

    ``extra`` merges derived artifacts' own lineage (e.g. the mtime of the
    master frame a per-size slot was rendered from).
    """
    try:
        st = os.stat(abs_path)
        data = {"src_mtime": st.st_mtime, "src_size": st.st_size}
        if extra:
            data.update(extra)
        thumb_meta_file(cache_file).write_text(json.dumps(data))
    except OSError:
        pass  # probe failure means 'not present'


def read_thumb_meta(cache_file: Path) -> dict:
    """Return the sidecar meta dict, or {} when missing/unreadable."""
    try:
        return json.loads(thumb_meta_file(cache_file).read_text())
    except Exception:
        return {}


def cleanup_stale_thumb_artifacts() -> None:
    """Remove orphaned thumbnail artifacts (no sidecar meta) — one-time sweep."""
    try:
        for f in THUMB_DIR.iterdir():
            if not f.is_file():
                continue
            if "_vid_v2" in f.name:
                # Previous video pipeline (one ffmpeg run per size) — superseded
                # by the master-frame cache; drop it so it stops eating the cap.
                with contextlib.suppress(OSError):
                    f.unlink()
                continue
            if f.suffix != ".webp":
                continue
            if thumb_meta_file(f).exists():
                continue
            # Orphaned (legacy cache without meta) — safe to drop.
            with contextlib.suppress(OSError):
                f.unlink()
    except OSError:
        pass  # best-effort cleanup — nothing to report


def probe_duration(abs_path: str) -> float:
    """Probe video duration with ffprobe. Returns 0 on failure."""
    try:
        probe = subprocess.run(
            ["nice", "-n", "19", "ffprobe", "-v", "error",
             "-probesize", "1000000", "-analyzeduration", "1000000",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", abs_path],
            capture_output=True, text=True, timeout=6,
        )
        return float(probe.stdout.strip()) if probe.stdout.strip() else 0.0
    except Exception:
        return 0.0


def append_bytes(path: str, data: bytes) -> None:
    """Append one upload chunk (runs in a worker thread)."""
    with open(path, "ab") as fh:
        fh.write(data)
