"""EthOS NAS — Public Share Links API

Authenticated CRUD for share links + public token endpoints that serve the
linked content without a JWT (the prefix /api/share-links/public is added to
main.py's public_paths).

Routes:
  Authenticated (mounted at /api/share-links):
    GET    /                      → {links:[...]} newest first, with status
    POST   /                      → create link {path, name?, expires_in_days?, max_downloads?}
                                     or multi-file {paths:[...], ...} → ONE link
    DELETE /{link_id}             → revoke a link

  Public (no JWT):
    GET    /public/{token}        → file: attachment download; dir: JSON listing;
                                    multi: self-contained HTML gallery page
    GET    /public/{token}/download?file=<rel> → one file inside a shared dir,
                                    or by display name for multi links
    GET    /public/{token}/thumb?file=<name>   → WebP thumbnail (multi links)
    GET    /public/{token}/zip                  → all files of a multi link as .zip
"""

import asyncio
import html
import io
import json
import logging
import os
import re
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from backend.config import settings
from backend.services import path_scope
from backend.services import share_links_service as svc
from backend.services.notifications_service import emit_event
from backend.services.thumbnails import render_video_thumb

logger = logging.getLogger("ethos-nas.routers.share_links")

router = APIRouter(dependencies=[Depends(path_scope.scope_request)])

# Temp dir for on-the-fly zip builds (0700, like gallery's private tmp).
_ZIP_TMP_DIR = Path(settings.DATA_DIR) / ".sharetmp"
MAX_ZIP_SOURCE_BYTES = 8 * 1024 ** 3   # refuse to zip more than this total
THUMB_SIZE = 640
_THUMB_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_THUMB_MAX_ENTRIES = 300

class ShareLinkCreate(BaseModel):
    path: str | None = Field(None, description="Absolute filesystem path of the file or folder to share")
    paths: list[str] | None = Field(
        None, max_length=svc.MAX_MULTI_FILES,
        description="Multiple files → ONE link with a shared gallery page (Photo Station multi-share)")
    name: str | None = Field(None, max_length=200, description="Display label (defaults to basename / 'N photos')")
    expires_in_days: float | None = Field(None, gt=0, description="Optional expiry in days")
    max_downloads: int | None = Field(None, gt=0, description="Optional download cap")

def _public_url(request: Request, token: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/share-links/public/{token}"

@router.get("")
async def list_links() -> dict:
    """All share links, newest first."""
    return {"links": svc.list_links()}

@router.post("")
async def create_link(body: ShareLinkCreate, request: Request) -> dict:
    """Create a public share link for a file/folder, or ONE link covering many files.

    `paths` (multi-file) takes precedence over `path`. Multi links serve a
    self-contained gallery page: recipients browse the photos and download
    them individually or all at once as .zip — no account needed.
    """
    username, role = path_scope.current_identity()
    if body.paths:
        try:
            clean_paths = [path_scope.sanitize(p) for p in body.paths]
            link = svc.create_multi_link(
                clean_paths,
                name=body.name,
                expires_in_days=body.expires_in_days,
                max_downloads=body.max_downloads,
                owner=username,
                owner_role=role,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    else:
        if not body.path:
            raise HTTPException(status_code=400, detail="Provide 'path' or 'paths'")
        try:
            link = svc.create_link(
                path_scope.sanitize(body.path),
                name=body.name,
                expires_in_days=body.expires_in_days,
                max_downloads=body.max_downloads,
                owner=username,
                owner_role=role,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    emit_event("info", "Share link created", f'"{link["name"]}" — {_public_url(request, link["token"])}', source="share-links")
    out = svc.with_status(link)
    out["url"] = _public_url(request, link["token"])
    return out

@router.delete("/{link_id}")
async def delete_link(link_id: str) -> dict:
    """Revoke (delete) a share link."""
    if not svc.delete_link(link_id):
        raise HTTPException(status_code=404, detail="Link not found")
    emit_event("info", "Share link revoked", f"link {link_id[:8]}… removed", source="share-links")
    return {"ok": True}

def _resolve_link(token: str) -> dict:
    """Fetch the link for a public token and enforce expiry / download cap."""
    link = svc.get_by_token(token)
    if not link:
        raise HTTPException(status_code=404, detail="Invalid or revoked share link")
    status = svc.link_status(link)
    if status == "expired":
        raise HTTPException(status_code=410, detail="This share link has expired")
    if status == "limit_reached":
        raise HTTPException(status_code=410, detail="Download limit reached for this share link")
    if not _link_in_scope(link):
        # Paths are re-validated against the creator's current scope on every
        # public request: a link cannot serve a path its creator may not read.
        logger.warning("Share link %s rejected: path outside the creator's scope", link.get("id"))
        raise HTTPException(status_code=410, detail="This share link is no longer available")
    return link


def _link_paths(link: dict) -> list[str]:
    files = link.get("files")
    if isinstance(files, list) and files:
        return [str(p) for p in files]
    path = str(link.get("path") or "")
    return [path] if path else []


def _link_in_scope(link: dict) -> bool:
    """True when every stored path is still readable by the link's creator."""
    paths = _link_paths(link)
    if not paths:
        return False
    owner = str(link.get("owner") or "")
    role = str(link.get("owner_role") or "user")
    try:
        for p in paths:
            if owner:
                path_scope.sanitize_for(p, owner, role)
            else:
                # Links created before ownership existed: keep the old shared
                # roots only, never an arbitrary system path.
                real = os.path.realpath(p)
                if not any(real == r or real.startswith(r + os.sep)
                           for r in ("/home", "/mnt", "/media")):
                    return False
        return True
    except HTTPException:
        return False

def _safe_child(root: str, rel: str) -> str:
    """Join a relative path to the shared root without allowing traversal."""
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        raise HTTPException(status_code=400, detail="Invalid file path")
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, rel))
    if target != root_real and not target.startswith(root_real + os.sep):
        raise HTTPException(status_code=400, detail="Path escapes the shared folder")
    return target

# ---------------------------------------------------------------------------
# Multi-file links (Photo Station multi-share)
# ---------------------------------------------------------------------------

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv"}

def _multi_entries(link: dict) -> list[dict]:
    """Existing files of a multi link → [{name, path, size, video}] in share order.

    Display names are basenames; collisions get a " (2)" suffix so every file
    stays addressable via ?file=<name>.
    """
    counts: dict[str, int] = {}
    out: list[dict] = []
    for p in link.get("files") or []:
        try:
            st = os.stat(p)
        except OSError:
            continue  # file vanished since the link was created
        if not os.path.isfile(p):
            continue
        base = os.path.basename(p)
        n = counts.get(base, 0) + 1
        counts[base] = n
        name = base
        if n > 1:
            stem, ext = os.path.splitext(base)
            name = f"{stem} ({n}){ext}"
        out.append({
            "name": name,
            "path": p,
            "size": int(st.st_size),
            "video": os.path.splitext(p)[1].lower() in VIDEO_EXTS,
        })
    return out

def _multi_entry_by_name(link: dict, file: str) -> dict | None:
    for e in _multi_entries(link):
        if e["name"] == file:
            return e
    return None

_FALLBACK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="{s}" height="{s}">'
    '<rect width="100%" height="100%" fill="#1e293b"/>'
    '<text x="50%" y="52%" font-family="sans-serif" font-size="{fs}" '
    'fill="#64748b" text-anchor="middle">no preview</text></svg>'
)

def _fallback_svg(status_code: int = 200) -> Response:
    return Response(
        content=_FALLBACK_SVG.format(s=THUMB_SIZE, fs=32).encode(),
        media_type="image/svg+xml", status_code=status_code)

def _thumb_cache_get(key: tuple) -> bytes | None:
    data = _THUMB_CACHE.get(key)
    if data is not None:
        _THUMB_CACHE.move_to_end(key)
    return data

def _thumb_cache_put(key: tuple, data: bytes) -> None:
    _THUMB_CACHE[key] = data
    _THUMB_CACHE.move_to_end(key)
    while len(_THUMB_CACHE) > _THUMB_MAX_ENTRIES:
        _THUMB_CACHE.popitem(last=False)

def _image_thumb(path: str) -> bytes | None:
    """Image → square-ish WebP thumbnail (in-memory cache only)."""
    from PIL import Image
    try:
        with Image.open(path) as img:
            if img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "WEBP", quality=82)
            return buf.getvalue()
    except Exception as exc:
        logger.warning("Thumbnail failed for %s: %s", path, exc)
        return None

def _video_thumb(path: str) -> bytes | None:
    """Video → one WebP frame (files.py helper; honors custom covers), read back in memory."""
    _ZIP_TMP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_ZIP_TMP_DIR, 0o700)
    tmp = _ZIP_TMP_DIR / f"thumb-{os.getpid()}-{time.time_ns()}.webp"
    try:
        if not render_video_thumb(path, THUMB_SIZE, tmp):
            return None
        with open(tmp, "rb") as f:
            return f.read()
    except Exception as exc:
        logger.warning("Video thumbnail failed for %s: %s", path, exc)
        return None
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass  # best-effort cleanup — nothing to report

@router.get("/public/{token}", response_model=None)
async def public_link(token: str):
    """Serve a share link without authentication.

    Files → attachment download (Range-capable via FileResponse).
    Directories → JSON listing; individual files are fetched through
    /public/{token}/download?file=<relative path>.
    Multi-file links → self-contained HTML gallery page (browse + per-photo
    download + "download all" .zip), no account or external assets needed.
    """
    link = _resolve_link(token)

    if link.get("files"):
        return HTMLResponse(_multi_page_html(link))

    root = link["path"]

    if os.path.isfile(root):
        svc.record_download(link["id"])
        return FileResponse(root, filename=os.path.basename(root))

    def _scan() -> list[dict]:
        out = []
        try:
            entries = sorted(
                os.scandir(root),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
        except PermissionError:
            raise
        for e in entries[:2000]:
            try:
                is_dir = e.is_dir()
                size = None if is_dir else e.stat().st_size
            except OSError:
                continue
            out.append({"name": e.name, "is_dir": is_dir, "size": size})
        return out

    try:
        # Shared folders can be huge; scanning them belongs in a worker thread.
        files = await asyncio.to_thread(_scan)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Permission denied") from exc
    return {
        "kind": "directory",
        "name": link["name"],
        "path": root,
        "files": files,
    }

@router.get("/public/{token}/download")
async def public_link_file(token: str, file: str = Query(...)) -> FileResponse:
    """Download one file from a shared directory (traversal-safe) or by display
    name from a multi-file link."""
    link = _resolve_link(token)

    if link.get("files"):
        entry = _multi_entry_by_name(link, file)
        if not entry:
            raise HTTPException(status_code=404, detail="File not found in this share")
        svc.record_download(link["id"])
        return FileResponse(entry["path"], filename=entry["name"])

    if not os.path.isdir(link["path"]):
        raise HTTPException(status_code=404, detail="This share link is not a folder")
    target = _safe_child(link["path"], file)
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="File not found in shared folder")
    svc.record_download(link["id"])
    return FileResponse(target, filename=os.path.basename(target))

@router.get("/public/{token}/thumb", response_model=None)
async def public_link_thumb(token: str, file: str = Query(...)):
    """WebP thumbnail for one file of a multi-file link (images via PIL,
    videos via ffmpeg frame grab). Falls back to a placeholder SVG."""
    link = _resolve_link(token)
    if not link.get("files"):
        raise HTTPException(status_code=404, detail="This share link has no files")
    entry = _multi_entry_by_name(link, file)
    if not entry:
        raise HTTPException(status_code=404, detail="File not found in this share")

    try:
        mtime = os.stat(entry["path"]).st_mtime
    except OSError:
        return _fallback_svg(status_code=404)

    key = (entry["path"], mtime)
    data = _thumb_cache_get(key)
    if data is None:
        make = _video_thumb if entry["video"] else _image_thumb
        data = await asyncio.to_thread(make, entry["path"])
        if data is None:
            return _fallback_svg()
        _thumb_cache_put(key, data)
    return Response(content=data, media_type="image/webp",
                    headers={"Cache-Control": "private, max-age=86400"})

def _zip_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")
    return slug[:80] or "photos"

def _build_zip(entries: list[dict], dest: str) -> None:
    """Write all entries into a zip file (runs in a worker thread)."""
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED) as zf:
        for e in entries:
            zf.write(e["path"], arcname=e["name"])

def _cleanup_stale_zips() -> None:
    """Best-effort removal of zip temp files older than an hour (aborted clients)."""
    try:
        now = time.time()
        for p in _ZIP_TMP_DIR.iterdir():
            if p.suffix == ".zip" and now - p.stat().st_mtime > 3600:
                p.unlink(missing_ok=True)
    except OSError:
        pass  # best-effort cleanup — nothing to report

@router.get("/public/{token}/zip")
async def public_link_zip(token: str):
    """All files of a multi-file link as one .zip download."""
    link = _resolve_link(token)
    if not link.get("files"):
        raise HTTPException(status_code=404, detail="This share link has no files")
    entries = _multi_entries(link)
    if not entries:
        raise HTTPException(status_code=410, detail="No files left in this share")

    total = sum(e["size"] for e in entries)
    if total > MAX_ZIP_SOURCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Share is too large to download as a zip ({total / 1024**3:.1f} GiB)")

    _ZIP_TMP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_ZIP_TMP_DIR, 0o700)
    _cleanup_stale_zips()
    dest = _ZIP_TMP_DIR / f"{link['id']}-{int(time.time())}.zip"
    await asyncio.to_thread(_build_zip, entries, str(dest))
    svc.record_download(link["id"])
    return FileResponse(
        str(dest), media_type="application/zip",
        filename=f"{_zip_slug(link['name'])}.zip",
        background=BackgroundTask(os.unlink, str(dest)))

# ---------------------------------------------------------------------------
# Multi-link gallery page (self-contained: no external assets, works offline)
# ---------------------------------------------------------------------------

_MULTI_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — shared via EthOS NAS</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background:#0b1220; color:#e2e8f0; font-family:system-ui,-apple-system,'Segoe UI',sans-serif; min-height:100vh; }}
  header {{ display:flex; align-items:center; gap:14px; flex-wrap:wrap; padding:16px 20px; border-bottom:1px solid #1e293b; position:sticky; top:0; background:rgba(11,18,32,.92); backdrop-filter:blur(6px); z-index:5; }}
  header .t {{ font-size:17px; font-weight:700; }}
  header .s {{ font-size:12.5px; color:#94a3b8; }}
  header .grow {{ flex:1; }}
  a.zipbtn {{ display:inline-flex; align-items:center; gap:7px; background:#0e7490; color:#fff; text-decoration:none; font-size:13.5px; font-weight:600; padding:9px 16px; border-radius:9px; }}
  a.zipbtn:hover {{ background:#0891b2; }}
  main {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:12px; padding:18px 20px 40px; max-width:1500px; margin:0 auto; }}
  .card {{ background:#111a2c; border:1px solid #1e293b; border-radius:11px; overflow:hidden; cursor:pointer; transition:border-color .15s, transform .15s; }}
  .card:hover {{ border-color:#0891b2; transform:translateY(-2px); }}
  .card .ph {{ position:relative; aspect-ratio:4/3; background:#0f172a; }}
  .card img {{ width:100%; height:100%; object-fit:cover; display:block; }}
  .card .badge {{ position:absolute; right:8px; bottom:8px; background:rgba(0,0,0,.65); color:#e2e8f0; font-size:11px; padding:2px 7px; border-radius:6px; }}
  .card .nm {{ padding:8px 10px; font-size:12.5px; color:#cbd5e1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .empty {{ grid-column:1/-1; text-align:center; color:#64748b; padding:70px 20px; font-size:14.5px; line-height:1.6; }}
  /* lightbox */
  #lb {{ position:fixed; inset:0; background:rgba(2,6,17,.94); display:flex; align-items:center; justify-content:center; z-index:50; }}
  #lb[hidden] {{ display:none; }}
  #lb .stage {{ max-width:min(92vw,1400px); max-height:82vh; display:flex; align-items:center; justify-content:center; }}
  #lb img, #lb video {{ max-width:100%; max-height:82vh; border-radius:8px; box-shadow:0 18px 60px rgba(0,0,0,.6); background:#000; }}
  #lb .bar {{ position:absolute; top:0; left:0; right:0; display:flex; align-items:center; gap:12px; padding:14px 18px; }}
  #lb .bar .nm {{ font-size:13.5px; color:#e2e8f0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:50vw; }}
  #lb .bar .cnt {{ font-size:12.5px; color:#94a3b8; flex:none; }}
  #lb .bar .grow {{ flex:1; }}
  #lb button {{ background:rgba(255,255,255,.09); border:none; color:#e2e8f0; font-size:14px; padding:9px 15px; border-radius:9px; cursor:pointer; flex:none; }}
  #lb button:hover {{ background:rgba(255,255,255,.17); }}
  a.dlbtn {{ display:inline-block; background:rgba(255,255,255,.09); color:#e2e8f0; font-size:14px; padding:9px 15px; border-radius:9px; text-decoration:none; flex:none; }}
  a.dlbtn:hover {{ background:rgba(255,255,255,.17); }}
  #lb .nav {{ position:absolute; top:50%; transform:translateY(-50%); width:46px; height:46px; border-radius:50%; padding:0; display:flex; align-items:center; justify-content:center; font-size:20px; }}
  #lb .prev {{ left:18px; }} #lb .next {{ right:18px; }}
  @media (max-width:640px) {{ main {{ grid-template-columns:repeat(auto-fill,minmax(130px,1fr)); padding:12px; gap:9px; }} #lb .nav {{ width:38px;height:38px;font-size:17px; }} }}
</style>
</head>
<body>
<header>
  <div class="t">{title}</div>
  <div class="s" id="sub"></div>
  <div class="grow"></div>
  {zipbtn}
</header>
<main id="grid"></main>

<div id="lb" hidden>
  <div class="bar">
    <button onclick="closeLb()" title="Close (Esc)">✕</button>
    <span class="nm" id="lbName"></span>
    <span class="cnt" id="lbCnt"></span>
    <div class="grow"></div>
    <a id="lbDl" class="dlbtn" href="#" download>⬇ Download</a>
  </div>
  <button class="nav prev" onclick="step(-1)" title="Previous (←)">‹</button>
  <div class="stage" id="lbStage"></div>
  <button class="nav next" onclick="step(1)" title="Next (→)">›</button>
</div>

<script>
const ITEMS = {items};
const fmtSize = b => b >= 1073741824 ? (b/1073741824).toFixed(1)+' GB' : b >= 1048576 ? (b/1048576).toFixed(1)+' MB' : b >= 1024 ? Math.round(b/1024)+' KB' : b+' B';
document.getElementById('sub').textContent = ITEMS.length + ' file' + (ITEMS.length===1?'':'s') + ' · shared via EthOS NAS';

const grid = document.getElementById('grid');
if (!ITEMS.length) {{
  grid.innerHTML = '<div class="empty">This share is currently empty — the files may have been moved or deleted.</div>';
}} else {{
  ITEMS.forEach((it, i) => {{
    const c = document.createElement('div');
    c.className = 'card';
    c.title = it.name;
    c.innerHTML = '<div class="ph"><img loading="lazy" src="' + it.thumb + '" alt="" onerror="this.style.opacity=.25">' + (it.video ? '<span class="badge">▶ video</span>' : '') + '</div><div class="nm"></div>';
    c.querySelector('.nm').textContent = it.name;
    c.onclick = () => openLb(i);
    grid.appendChild(c);
  }});
}}

let idx = -1;
const lb = document.getElementById('lb'), stage = document.getElementById('lbStage');
function render() {{
  const it = ITEMS[idx];
  if (!it) return;
  stage.innerHTML = '';
  let el;
  if (it.video) {{
    el = document.createElement('video');
    el.src = it.dl; el.controls = true; el.autoplay = true;
  }} else {{
    el = document.createElement('img');
    el.src = it.dl; el.alt = it.name;
  }}
  stage.appendChild(el);
  document.getElementById('lbName').textContent = it.name + ' · ' + fmtSize(it.size);
  document.getElementById('lbCnt').textContent = (idx+1) + ' / ' + ITEMS.length;
  const a = document.getElementById('lbDl');
  a.href = it.dl; a.download = it.name;
}}
function openLb(i) {{ idx = i; render(); lb.hidden = false; }}
function closeLb() {{ idx = -1; stage.innerHTML = ''; lb.hidden = true; }}
function step(d) {{ if (idx < 0) return; idx = (idx + d + ITEMS.length) % ITEMS.length; render(); }}
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') closeLb();
  else if (e.key === 'ArrowLeft') step(-1);
  else if (e.key === 'ArrowRight') step(1);
}});
lb.onclick = e => {{ if (e.target === lb) closeLb(); }};
</script>
</body>
</html>
"""

def _multi_page_html(link: dict) -> str:
    """Render the self-contained gallery page for a multi-file link."""
    base = f"/api/share-links/public/{link['token']}"
    items = []
    for e in _multi_entries(link):
        # URL-encode the display name for ?file= (spaces, parens, unicode…)
        fn = quote(e["name"], safe="")
        items.append({
            "name": e["name"],
            "size": e["size"],
            "video": e["video"],
            "thumb": f"{base}/thumb?file={fn}",
            "dl": f"{base}/download?file={fn}",
        })
    payload = json.dumps(items, ensure_ascii=False).replace("</", "<\\/")
    title = html.escape(link.get("name") or "Shared photos")
    zipbtn = (f'<a class="zipbtn" href="{base}/zip">⬇ Download all (.zip)</a>'
              if items else "")
    return _MULTI_PAGE.format(title=title, zipbtn=zipbtn, items=payload)
