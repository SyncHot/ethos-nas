"""EthOS NAS — Disk Cleaner Router

REST:
- GET  /api/disk-clean/scan           — directory size tree for a path (depth-limited)
- GET  /api/disk-clean/top-files      — largest individual files under a path
- GET  /api/disk-clean/suggestions    — cleanup candidates (trash, temp, caches, logs, journal)
- POST /api/disk-clean/delete         — delete file(s)/dir(s), report freed bytes
- POST /api/disk-clean/vacuum-journal — vacuum systemd journal older than N days

Design notes:
- Path handling mirrors files.py::_sanitize (abspath/expanduser, no allowlist —
  OS-level access control). Deletion additionally refuses a small set of
  critical system roots so the UI cannot brick the box.
- All filesystem walks are capped (max entries) and run in worker threads;
  pseudo-filesystems (/proc, /sys, /dev) are pruned.
"""

import asyncio
import heapq
import logging
import os
import re
import shutil
import subprocess
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.routers.auth import require_admin

logger = logging.getLogger("ethos-nas.routers.disk_clean")
# Cleaner deletes whatever the scans find — admin-only.
router = APIRouter(dependencies=[Depends(require_admin)])

# ── Constants ────────────────────────────────────────────────────────────────

MAX_WALK_ENTRIES = 400_000   # hard cap on directory entries per scan
SCAN_TIMEOUT_S = 120         # wall-clock budget for a full scan
MIN_SUGGESTION_BYTES = 1 * 1024 * 1024  # hide junk items smaller than 1 MB
MAX_ITEMS_PER_CATEGORY = 30

# Roots that must never be deleted through the UI.
PROTECTED_ROOTS = {
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64",
    "/proc", "/root/.ssh", "/run", "/sbin", "/sys", "/usr",
}

# Pseudo-filesystems to prune from walks.
PRUNE_PATHS = {"/proc", "/sys", "/dev"}

_ROTATED_LOG_RE = re.compile(r"\.(gz|bz2|xz|zst|old|log\.\d+|\d+)$")


def _sanitize(path: str) -> str:
    """Resolve and return an absolute path (same policy as files.py)."""
    if not path or not isinstance(path, str):
        raise HTTPException(status_code=400, detail="Path is required")
    return os.path.abspath(os.path.expanduser(path))


def _is_protected(abs_path: str) -> bool:
    p = abs_path.rstrip("/") or "/"
    return p in PROTECTED_ROOTS


def _prune(abs_path: str) -> bool:
    for pp in PRUNE_PATHS:
        if abs_path == pp or abs_path.startswith(pp + "/"):
            return True
    return False


# ── Walk helpers (synchronous — always called via asyncio.to_thread) ─────────

def _walk_dir_sizes(root: str):
    """Single-pass walk returning {dir_path: total_size_incl_descendants}.

    Returns (sizes, entry_count, truncated).
    """
    direct = {}
    count = 0
    truncated = False
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                count += 1
                if count > MAX_WALK_ENTRIES:
                    truncated = True
                    break
                try:
                    if e.is_dir(follow_symlinks=False):
                        ap = os.path.abspath(e.path)
                        if _prune(ap):
                            continue
                        direct.setdefault(ap, 0)
                        stack.append(ap)
                    elif e.is_file(follow_symlinks=False):
                        sz = e.stat(follow_symlinks=False).st_size
                        direct[d] = direct.get(d, 0) + sz
                except OSError:
                    continue
        if truncated:
            break

    # Aggregate bottom-up (longest paths first so children precede parents).
    for p in sorted(direct.keys(), key=len, reverse=True):
        parent = os.path.dirname(p)
        if parent and parent != p:
            direct[parent] = direct.get(parent, 0) + direct[p]
    return direct, count, truncated


def _entry_sort_size(e, sizes: dict) -> int:
    try:
        if e.is_dir(follow_symlinks=False):
            return sizes.get(os.path.abspath(e.path), 0)
        return e.stat(follow_symlinks=False).st_size
    except OSError:
        return 0


def _build_tree(root: str, sizes: dict, depth: int, max_children: int = 100):
    """Build a nested tree from aggregated dir sizes.

    `depth` = number of directory levels expanded (root's entries are level 1).
    """
    def node(path: str, levels_left: int) -> dict:
        base = {
            "name": os.path.basename(root.rstrip("/")) if path == root else (os.path.basename(path) or "/"),
            "path": path,
            "size": sizes.get(path, 0),
            "is_dir": True,
        }
        if levels_left <= 0:
            base["children"] = None
            return base
        children = []
        try:
            entries = sorted(os.scandir(path), key=lambda e: _entry_sort_size(e, sizes), reverse=True)
        except OSError:
            entries = []
        for e in entries[:max_children]:
            ap = os.path.abspath(e.path)
            try:
                if e.is_dir(follow_symlinks=False):
                    if _prune(ap):
                        continue
                    children.append(node(ap, levels_left - 1))
                elif e.is_file(follow_symlinks=False):
                    children.append({
                        "name": e.name, "path": ap,
                        "size": e.stat(follow_symlinks=False).st_size,
                        "is_dir": False, "children": None,
                    })
            except OSError:
                continue
        base["count"] = len(children)
        base["children"] = children
        return base

    return node(root, depth)


def _top_files(root: str, limit: int):
    """Return the `limit` largest files under root using a bounded heap."""
    h: list[tuple[int, str]] = []
    count = 0
    truncated = False
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                count += 1
                if count > MAX_WALK_ENTRIES:
                    truncated = True
                    break
                try:
                    if e.is_dir(follow_symlinks=False):
                        ap = os.path.abspath(e.path)
                        if _prune(ap):
                            continue
                        stack.append(ap)
                    elif e.is_file(follow_symlinks=False):
                        sz = e.stat(follow_symlinks=False).st_size
                        if len(h) < limit:
                            heapq.heappush(h, (sz, e.path))
                        elif sz > h[0][0]:
                            heapq.heapreplace(h, (sz, e.path))
                except OSError:
                    continue
        if truncated:
            break
    files = [{"path": p, "name": os.path.basename(p), "size": s} for s, p in h]
    files.sort(key=lambda f: f["size"], reverse=True)
    return files, count, truncated


def _entry_size(path: str) -> int:
    """Size of a file or total size of a directory tree (capped walk)."""
    try:
        if os.path.isdir(path):
            sizes, _, _ = _walk_dir_sizes(path)
            return sizes.get(path, 0)
        if os.path.isfile(path):
            return os.stat(path).st_size
    except OSError:
        pass  # probe failure means 'not present'
    return 0


# ── Suggestion collectors ───────────────────────────────────────────────────

def _user_homes() -> list[str]:
    homes = []
    try:
        for u in sorted(os.listdir("/home")):
            p = os.path.join("/home", u)
            if os.path.isdir(p):
                homes.append(p)
    except OSError:
        pass  # probe failure means 'not present'
    if os.path.isdir("/root"):
        homes.append("/root")
    return homes


def _collect_suggestions() -> dict:
    cats: dict[str, list[dict]] = {
        "trash": [], "temp": [], "cache": [], "logs": [], "var_cache": [],
    }

    def add(cat: str, path: str, safety: str, description: str):
        if _prune(path) or not os.path.exists(path):
            return
        size = _entry_size(path)
        if size < MIN_SUGGESTION_BYTES and cat != "journal":
            return
        cats[cat].append({
            "path": path,
            "name": os.path.basename(path.rstrip("/")) or "/",
            "size": size,
            "safety": safety,          # safe | caution
            "description": description,
        })

    for home in _user_homes():
        add("trash", f"{home}/.local/share/Trash", "safe", "User trash — permanently deleted on removal")
        add("cache", f"{home}/.cache", "safe", "Application cache (thumbnails, browser caches, pip/npm caches)")

    for tmp in ("/tmp", "/var/tmp"):
        try:
            entries = sorted(os.scandir(tmp), key=lambda e: _entry_size(e.path), reverse=True)
        except OSError:
            continue
        for e in entries[:MAX_ITEMS_PER_CATEGORY]:
            if not e.is_dir(follow_symlinks=False) and not e.is_file(follow_symlinks=False):
                continue
            add("temp", e.path, "safe", f"Temporary file in {tmp}")

    # /var/log — rotated logs are safe, active logs need caution.
    try:
        log_entries = sorted(os.scandir("/var/log"), key=lambda e: _entry_size(e.path), reverse=True)
    except OSError:
        log_entries = []
    for e in log_entries[:MAX_ITEMS_PER_CATEGORY]:
        ap = os.path.abspath(e.path)
        if e.is_dir(follow_symlinks=False):
            add("logs", ap, "caution", "Log directory")
        else:
            rotated = bool(_ROTATED_LOG_RE.search(e.name)) or e.name.startswith(("syslog.", "kern.")) and "." in e.name[6:]
            safety = "safe" if (rotated or not e.name.endswith(".log")) else "caution"
            add("logs", ap, safety, "Rotated log file" if rotated else "Active system log")

    # /var/cache top-level dirs (apt, etc.) — caution.
    try:
        vc_entries = sorted(os.scandir("/var/cache"), key=lambda e: _entry_size(e.path), reverse=True)
    except OSError:
        vc_entries = []
    for e in vc_entries[:MAX_ITEMS_PER_CATEGORY]:
        if e.is_dir(follow_symlinks=False):
            add("var_cache", os.path.abspath(e.path), "caution", "System package cache")

    # systemd journal size (not deletable via /delete — has its own vacuum action)
    journal = {"size": 0, "human": "", "available": False}
    try:
        out = subprocess.run(["journalctl", "--disk-usage"], capture_output=True, text=True, timeout=10).stdout
        m = re.search(r"([0-9.]+)\s*([KMGT]i?B?)\b", out)
        if m:
            val, unit = float(m.group(1)), m.group(2).upper().rstrip("IB")
            mult = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit, 1)
            journal = {"size": int(val * mult), "human": f"{m.group(1)} {unit}", "available": True}
    except Exception:
        pass  # intentional: a failure here must not break the caller

    for cat in cats.values():
        cat.sort(key=lambda i: i["size"], reverse=True)
        del cat[MAX_ITEMS_PER_CATEGORY:]

    return {"categories": cats, "journal": journal}


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/scan")
async def scan(
    path: str = Query(default="/home", description="Root directory to analyze"),
    depth: int = Query(default=2, ge=1, le=5),
):
    """Directory size tree for `path`, expanded `depth` levels."""
    root = _sanitize(path)
    if not os.path.isdir(root):
        raise HTTPException(status_code=404, detail=f"Not a directory: {root}")

    def work():
        sizes, count, truncated = _walk_dir_sizes(root)
        tree = _build_tree(root, sizes, depth)
        return {"path": root, "total_size": sizes.get(root, 0), "file_count": count,
                "truncated": truncated, "tree": tree}

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=SCAN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Scan timed out — try a smaller path or lower depth")


@router.get("/top-files")
async def top_files(
    path: str = Query(default="/home", description="Root directory to search"),
    limit: int = Query(default=25, ge=1, le=100),
):
    """Largest individual files under `path`."""
    root = _sanitize(path)
    if not os.path.isdir(root):
        raise HTTPException(status_code=404, detail=f"Not a directory: {root}")

    def work():
        files, count, truncated = _top_files(root, limit)
        return {"path": root, "files": files, "scanned_entries": count, "truncated": truncated}

    try:
        return await asyncio.wait_for(asyncio.to_thread(work), timeout=SCAN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Scan timed out — try a smaller path")


@router.get("/suggestions")
async def suggestions():
    """Cleanup candidates across the system (trash, temp, caches, logs, journal)."""
    return await asyncio.to_thread(_collect_suggestions)


class DeleteRequest(BaseModel):
    paths: list[str] = Field(..., max_length=50)


@router.post("/delete")
async def delete_paths(req: DeleteRequest):
    """Delete files/directories. Returns freed bytes per path."""
    if not req.paths:
        raise HTTPException(status_code=400, detail="No paths provided")

    resolved = []
    for p in req.paths:
        ap = _sanitize(p)
        if _is_protected(ap):
            raise HTTPException(status_code=403, detail=f"Refusing to delete protected path: {ap}")
        if not os.path.exists(ap):
            continue
        resolved.append(ap)

    deleted, errors, freed_total = [], [], 0

    def work():
        nonlocal freed_total
        for ap in resolved:
            try:
                size = _entry_size(ap)
                if os.path.isdir(ap) and not os.path.islink(ap):
                    shutil.rmtree(ap)
                else:
                    os.remove(ap)
                deleted.append({"path": ap, "freed_bytes": size})
                freed_total += size
            except Exception as exc:  # noqa: BLE001 — report per-path errors, keep going
                logger.warning("disk-clean delete failed for %s: %s", ap, exc)
                errors.append({"path": ap, "error": str(exc)})

    await asyncio.to_thread(work)
    return {"deleted": deleted, "errors": errors, "freed_bytes": freed_total}


class VacuumRequest(BaseModel):
    days: int = Field(default=7, ge=1, le=3650)


@router.post("/vacuum-journal")
async def vacuum_journal(req: VacuumRequest):
    """Remove systemd journal entries older than `days` days."""

    def work():
        r = subprocess.run(
            ["journalctl", f"--vacuum-time={req.days}d"],
            capture_output=True, text=True, timeout=120,
        )
        return (r.stdout or "") + (f"\n{r.stderr}" if r.returncode else "")

    try:
        out = await asyncio.to_thread(work)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"journalctl failed: {exc}")
    return {"output": out.strip() or f"Journal vacuumed (older than {req.days} days)."}
