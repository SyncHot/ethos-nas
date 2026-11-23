"""EthOS NAS — file search service (TODO.md P2-03).

Name-based search used by two endpoints in ``routers/files.py``:

- ``GET /api/files/search``          — one root, optional filters
- ``GET /api/files/universal-search`` — many roots in parallel + type/size filters

Extracted from the router so the walk, the extension catalogue and the default
scope policy are testable without HTTP (``tests/test_search_service.py``) and
so ``files.py`` keeps shrinking. The endpoints stay responsible for auth, path
scope (``sanitize``) and HTTP error shapes.
"""

import os
from typing import Iterable

from backend.services import path_scope

#: Directories never worth crawling on a NAS search (VCS metadata, dependency
#: trees, caches, virtualenvs). They dominate the walk time and almost never
#: hold the file the user is looking for.
SEARCH_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".cache",
    ".npm", ".yarn", ".gradle", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", "site-packages", ".tox", ".next", ".nuxt", "dist",
    ".@Recycle",
}

#: File-type filters for universal search → allowed extensions.
SEARCH_TYPE_EXTS: dict[str, set[str]] = {
    "images": {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "heic", "avif", "tiff", "tif"},
    "video": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "ts", "m2ts"},
    "audio": {"mp3", "ogg", "oga", "flac", "wav", "aac", "m4a", "opus", "wma", "mid", "midi"},
    "docs": {"pdf", "doc", "docx", "odt", "txt", "md", "rtf", "xls", "xlsx", "ods", "csv",
             "ppt", "pptx", "odp", "epub", "html", "htm"},
    "archives": {"zip", "tar", "gz", "tgz", "bz2", "tbz", "xz", "txz", "7z", "rar", "zst", "lz4"},
    "code": {"py", "js", "ts", "jsx", "tsx", "c", "cpp", "cc", "h", "hpp", "java", "go",
             "rs", "rb", "php", "sh", "bash", "sql", "json", "yaml", "yml", "toml",
             "xml", "css", "html", "htm", "vue", "svelte"},
}

#: Filesystems that never hold user data — excluded from auto search scopes.
PSEUDO_FSTYPES = {
    "tmpfs", "squashfs", "overlay", "proc", "procfs", "sysfs", "devtmpfs",
    "devpts", "cgroup", "cgroup2", "securityfs", "debugfs", "tracefs", "pstore",
    "bpf", "configfs", "fusectl", "mqueue", "hugetlbfs", "ramfs", "autofs",
    "binfmt_misc", "nsfs", "efivarfs", "iso9660",
}


def walk_search(root_path: str, term: str, limit: int,
                exts: set[str] | None = None, min_size: int = 0) -> tuple[list[dict], bool]:
    """Recursively collect items whose names contain term (case-insensitive).

    With ``exts`` set, only regular files with a matching extension are
    returned; with ``min_size`` > 0, smaller files are skipped. When either
    filter is active, directories are excluded from the results.
    Well-known heavy directories (``SEARCH_SKIP_DIRS``) are not descended into.

    Returns ``(results, truncated)``; blocking — call it through
    ``asyncio.to_thread``.
    """
    results: list[dict] = []
    truncated = False
    filtered = exts is not None or min_size > 0
    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = [d for d in dirnames if d not in SEARCH_SKIP_DIRS]
        names = sorted(filenames) if filtered else sorted(filenames + dirnames)
        for name in names:
            if term not in name.lower():
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            is_dir = os.path.isdir(full)
            if filtered and is_dir:
                continue
            size = 0 if is_dir else st.st_size
            if exts is not None:
                e = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if e not in exts:
                    continue
            if min_size > 0 and size < min_size:
                continue
            results.append({
                "name": name,
                "path": full,
                "is_dir": is_dir,
                "size": size,
                "modified": st.st_mtime,
            })
            if len(results) >= limit:
                truncated = True
                break
        if truncated:
            break
    return results, truncated


def real_mountpoints() -> list[str]:
    """Readable real (non-pseudo) mountpoints, sorted and deduped."""
    try:
        import psutil
        parts = psutil.disk_partitions(all=False)
    except Exception:  # noqa: BLE001 — psutil missing or platform quirk
        return []
    out: list[str] = []
    for p in parts:
        if p.fstype in PSEUDO_FSTYPES:
            continue
        mp = p.mountpoint
        try:
            if not os.path.isdir(mp) or not os.access(mp, os.R_OK):
                continue
        except OSError:
            continue
        out.append(mp)
    return sorted(set(out))


def default_search_roots() -> list[dict]:
    """Default universal-search scopes for the current identity.

    Admins get /home plus the real data mounts; every other account gets its
    own home plus the shared /mnt and /media mounts. Nested duplicates are
    dropped (shallowest wins) so results are not counted twice. The root
    filesystem itself is never a default scope — searching it means crawling
    the entire disk and must be an explicit admin choice.
    """
    username, role = path_scope.current_identity()
    kept: list[str] = []
    for mp in real_mountpoints():  # sorted → parents precede children
        if mp == "/":
            continue
        if any(mp.startswith(k + os.sep) for k in kept):
            continue
        kept.append(mp)
    if role == "admin":
        roots = [{"path": "/home", "label": "Home"}]
    else:
        home = path_scope.home_dir(username)
        roots = [{"path": home, "label": "Home"}] if home else []
    for mp in kept:
        if mp.startswith(("/mnt/", "/media/")):
            label = os.path.basename(mp.rstrip("/")) or mp
            roots.append({"path": mp, "label": f"{label} ({mp})"})
    return roots


def type_extensions(file_type: str) -> set[str] | None:
    """Extension filter for a universal-search type; ``None`` means 'no filter'."""
    if file_type == "all":
        return None
    return SEARCH_TYPE_EXTS.get(file_type)


def merge_results(outcomes: Iterable[tuple[str, tuple[list[dict], bool]]]) -> tuple[list[dict], list[dict]]:
    """Attach each result's root, merge, sort newest-first and cap at 2000.

    Returns ``(merged, root_stats)`` — the shape ``/universal-search`` returns.
    """
    merged: list[dict] = []
    stats: list[dict] = []
    for root, (results, truncated) in outcomes:
        stats.append({"root": root, "ok": True, "count": len(results), "truncated": truncated})
        for item in results:
            item["root"] = root
        merged.extend(results)
    merged.sort(key=lambda x: x.get("modified", 0), reverse=True)
    return merged[:2000], stats
