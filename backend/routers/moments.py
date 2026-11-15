"""EthOS NAS — Highlights router (curated feed over the media library).

Highlights is a read-only *curation* layer over the same media library the
gallery indexes. It does not own media; it reuses the gallery router's
discovery helpers (walk + config) and produces:
- a month timeline (counts + a few representative items per month)
- curated "highlights" (newest, largest, one-per-folder, recent bursts)
- aggregate stats (totals, by type, by month, by folder)

Everything is computed live from the gallery index (fast, in-memory) so
there is nothing extra to scan or persist.
"""

import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

from fastapi import APIRouter, HTTPException, Query

# Reuse the gallery router's media discovery + config so the two apps always
# agree on what "the library" is.
from backend.routers.gallery import (
    _load_config as gallery_load_config,
    _walk_folder_sync,
    _index_state as gallery_index,
    _ensure_fresh_index,
)

logger = logging.getLogger("ethos-nas.routers.moments")

router = APIRouter()


# ── Shared helpers ────────────────────────────────────────────────────────────

def _all_items() -> List[dict]:
    """Media items from the gallery index (fresh if needed).

    Private items are excluded: they are encrypted at rest and only visible
    through the PIN-gated Private section, so a curation feed must not leak
    them (their raw URLs would fail anyway).  The gallery's "Include videos"
    option is honoured too — Highlights must show the same library as the rest
    of the Photos app.
    """
    cfg = gallery_load_config()
    if cfg.get("folders"):
        _ensure_fresh_index()
    items = [it for it in gallery_index["items"] if not it.get("private")]
    if not cfg.get("options", {}).get("include_videos", True):
        items = [it for it in items if it.get("type") != "video"]
    return items


def _month_key(epoch: float) -> str:
    # Local time — must match the gallery timeline and UI date labels.
    d = datetime.fromtimestamp(epoch or 0.0)
    return d.strftime("%Y-%m")


def _month_label(key: str) -> str:
    y, m = key.split("-")
    names = ["January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]
    return f"{names[int(m) - 1]} {y}"


def _eff_date(it: dict) -> float:
    """EXIF taken date when known, else file mtime (chronological ordering)."""
    return it.get("taken") or it.get("modified") or 0.0


def _public_item(it: dict) -> dict:
    return {
        "path": it["path"],
        "name": it["name"],
        "folder": it["folder"],
        "size": int(it.get("size") or 0),
        "modified": it.get("modified", 0),
        "taken": it.get("taken"),
        "type": it.get("type", "image"),
        # Keep the tile/lightbox controls working in the Highlights view: the
        # star state, video duration and EXIF camera all come from the index.
        "favorite": bool(it.get("favorite")),
        "camera": it.get("camera") or "",
        "duration": it.get("duration"),
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/library")
async def library():
    """Folders in the shared library (same source of truth as the gallery)."""
    cfg = gallery_load_config()
    items = _all_items()
    by_folder: Dict[str, int] = defaultdict(int)
    for it in items:
        by_folder[it["folder"]] += 1
    return {
        "folders": cfg.get("folders", []),
        "total": len(items),
        "counts": {f: c for f, c in sorted(by_folder.items(), key=lambda kv: -kv[1])},
    }


@router.get("/timeline")
async def timeline(per_month: int = Query(default=3, ge=1, le=12)):
    """Group items by month (newest first), each with a few samples.

    The feed leads with a chronological timeline, so the months are the
    backbone and the sample items seed each month's preview strip.
    """
    items = _all_items()
    if not items:
        return {"months": [], "total": 0}

    buckets: Dict[str, List[dict]] = defaultdict(list)
    for it in items:
        buckets[_month_key(_eff_date(it))].append(it)

    months = []
    for key in sorted(buckets, reverse=True):
        group = sorted(buckets[key], key=_eff_date, reverse=True)
        months.append({
            "month": key,
            "label": _month_label(key),
            "count": len(group),
            "videos": sum(1 for g in group if g.get("type") == "video"),
            "images": sum(1 for g in group if g.get("type") != "video"),
            "samples": [_public_item(g) for g in group[:per_month]],
        })
    return {"months": months, "total": len(items)}


@router.get("/highlights")
async def highlights(limit: int = Query(default=24, ge=4, le=120)):
    """Curated picks: a mix that looks good as a front-page feed.

    Heuristics (no ML — deterministic, fast, explainable):
    - newest items
    - largest files (proxy for resolution/quality)
    - one representative item per folder (coverage)
    - a couple of recent videos (moments favour motion)
    De-duplicated, capped at `limit`.
    """
    items = _all_items()
    if not items:
        return {"highlights": [], "total": 0}

    by_newest = sorted(items, key=_eff_date, reverse=True)
    by_size = sorted(items, key=lambda x: x.get("size", 0), reverse=True)
    videos = sorted([x for x in items if x.get("type") == "video"],
                    key=_eff_date, reverse=True)

    # One per folder, most recent wins.
    per_folder: Dict[str, dict] = {}
    for it in by_newest:
        per_folder.setdefault(it["folder"], it)

    picked: List[dict] = []
    seen = set()
    def _add(it):
        if it["path"] in seen:
            return
        seen.add(it["path"])
        picked.append(_public_item(it))

    # Interleave: newest, a video, largest, a folder-representative…
    queues = [
        iter(by_newest),
        iter(videos),
        iter(by_size),
        iter(list(per_folder.values())),
    ]
    while len(picked) < limit:
        progressed = False
        for q in queues:
            if len(picked) >= limit:
                break
            try:
                _add(next(q))
                progressed = True
            except StopIteration:
                continue
        if not progressed:
            break
    return {"highlights": picked, "total": len(items)}


@router.get("/stats")
async def stats():
    """Aggregate numbers for the Highlights header / summary card."""
    items = _all_items()
    by_type = {"image": 0, "video": 0}
    by_month: Dict[str, int] = defaultdict(int)
    by_folder: Dict[str, int] = defaultdict(int)
    total_size = 0
    for it in items:
        by_type["video" if it.get("type") == "video" else "image"] += 1
        by_month[_month_key(_eff_date(it))] += 1
        by_folder[it["folder"]] += 1
        total_size += int(it.get("size") or 0)
    return {
        "total": len(items),
        "images": by_type["image"],
        "videos": by_type["video"],
        "total_size": total_size,
        "folders": len(by_folder),
        "months": {k: by_month[k] for k in sorted(by_month, reverse=True)},
        "top_folders": sorted(
            ({"folder": f, "count": c} for f, c in by_folder.items()),
            key=lambda x: -x["count"],
        )[:8],
    }


@router.get("/month")
async def month(month: str = Query(..., description="YYYY-MM"), limit: int = Query(default=120, ge=1, le=500)):
    """All items in one calendar month (for expanding a timeline row)."""
    if not re.fullmatch(r"\d{4}-\d{2}", month or ""):
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")
    items = [it for it in _all_items() if _month_key(_eff_date(it)) == month]
    items.sort(key=_eff_date, reverse=True)
    return {
        "month": month,
        "label": _month_label(month),
        "count": len(items),
        "items": [_public_item(it) for it in items[:limit]],
    }


@router.get("/item/{path:path}")
async def item(path: str):
    """A single media item by absolute path (for the detail / player view)."""
    abs_path = os.path.abspath(os.path.expanduser(path or ""))
    for it in _all_items():
        if it["path"] == abs_path:
            return _public_item(it)
    raise HTTPException(status_code=404, detail="Item not in library")
