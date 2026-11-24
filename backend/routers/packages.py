"""EthOS NAS — Packages API (signed repositories, compose packages).

Static routes are defined before the ``/{pkg_id}`` catch-all; keep it that way.
Mutations are admin-only; catalog reads are available to any authenticated user.
"""

import asyncio
import logging
import mimetypes
import re
import shutil
import threading
import urllib.error
import urllib.request
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel

from backend.config import settings
from backend.routers.auth import require_admin
from backend.services import (
    json_store, package_cleanup, package_installer, package_jobs, package_repo, package_state,
    packages_store, system_deps,
)
from backend.services.notifications_service import emit_event
from backend.services.package_schema import PackageError, check_pkg_id, safe_relative_path

logger = logging.getLogger("ethos-nas.routers.packages")
router = APIRouter()

DESKTOP_LAYOUTS_FILE = settings.DATA_DIR / "desktop_layouts.json"
_LAYOUT_STORE = json_store.get(DESKTOP_LAYOUTS_FILE, mode=0o660)
_LAYOUTS_LOCK = threading.Lock()

_PKG_ID_CORE = r"[a-z0-9][a-z0-9-]{1,48}[a-z0-9]"
PKG_ICON_RE = re.compile(rf"^pkg-{_PKG_ID_CORE}$")

# Must match frontend/static/core/registry.js and desktop-layout.js.
APP_REGISTRY_KEYS = [
    "overview", "storage", "files", "trash",
    "terminal", "network", "security", "services", "settings",
    "processes", "logs", "packages", "smart",
    "cron",
    "search", "shared-links", "sharing",
]

# Built-in apps that moved to Packages. Existing per-user layouts keep the
# icon position after the upgrade.
LEGACY_LAYOUT_MAP = {
    # System Services merged into Service Manager (2.29.0); a pinned icon keeps
    # its position and opens the merged window.
    "systemd": "services",
    "backup": "pkg-backup-manager",
    "downloads": "pkg-download-manager",
    # Kanban moved to Packages (docs/app-store.md): a pinned icon keeps its
    # position and points at the package.
    "kanban": "pkg-ethos-kanban",
    # Docker Manager is now a package too; the compose runtime keeps living in core.
    "docker": "pkg-ethos-docker",
    "gallery": "pkg-ethos-photos",
    "music": "pkg-ethos-music",
    "videos": "pkg-ethos-video",
    "surveillance": "pkg-ethos-surveillance",
    "shares": "pkg-ethos-shares",
    "editor": "pkg-ethos-editor",
    "git-diff": "pkg-ethos-git-diff",
    "cleaner": "pkg-ethos-cleaner",
}


def _http_error(exc: PackageError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


class LayoutRequest(BaseModel):
    model_config = {"extra": "forbid"}
    order: list[str] = []
    visible: list[str] = []
    positions: dict[str, dict] = {}


class InstallRequest(BaseModel):
    model_config = {"extra": "forbid"}
    package_id: str
    source: Optional[str] = None
    # Values for the manifest's optional `settings` fields (admin login, etc.).
    settings: dict[str, str] = {}


class AddSourceRequest(BaseModel):
    model_config = {"extra": "forbid"}
    url: str
    name: str = ""
    public_key: str = ""
    trust: str = "unverified"
    enabled: bool = True


class UpdateSourceRequest(BaseModel):
    model_config = {"extra": "forbid"}
    name: Optional[str] = None
    public_key: Optional[str] = None
    trust: Optional[str] = None
    enabled: Optional[bool] = None


class BulkDeleteRequest(BaseModel):
    model_config = {"extra": "forbid"}
    ids: list[str]
    remove_volumes: bool = False
    remove_images: bool = False
    remove_data: bool = False


class ActionRequest(BaseModel):
    model_config = {"extra": "forbid"}
    action: str


class SettingsRequest(BaseModel):
    model_config = {"extra": "forbid"}
    # Only the keys the user edited; omitted fields keep their stored value.
    values: dict[str, str] = {}


# ── Installed packages / desktop icons ──────────────────────────────────────

def _record_view(pkg: dict) -> dict:
    """Installed record for API clients: secret settings never leave the NAS.

    Text values are returned so the settings form can prefill them; password
    values are always blank and the UI keeps the stored one unless the user
    types a new one (an omitted key means "unchanged" to the backend).
    """
    values = pkg.get("settings_values") or {}
    defs = pkg.get("settings") or []
    masked = {
        str(d["key"]): "" if d.get("type") == "password" else str(values.get(str(d["key"]), ""))
        for d in defs if isinstance(d, dict) and d.get("key")
    }
    return {**pkg, "settings_values": masked}


@router.get("")
async def list_packages() -> dict:
    items = [_record_view(p) for p in packages_store.load_installed()]
    return {"packages": items, "items": items, "total": len(items)}


def _public_icon_url(value) -> str:
    """Only absolute HTTPS icons are exposed on the unauthenticated endpoint."""
    url = str(value or "").strip()
    return url if url.startswith("https://") and len(url) <= 500 else ""


@router.get("/desktop-icons")
async def desktop_icons() -> list[dict]:
    """Public: icons for installed packages (never exposes paths or manifests).

    Engine packages have no UI (no `ui` block) and therefore no desktop icon.
    """
    return [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "icon": p.get("icon") or "📦",
            "icon_url": _public_icon_url(p.get("icon_url")),
        }
        for p in packages_store.load_installed()
        if p.get("id") and p.get("ui")
    ]


# ── Catalog / sources ───────────────────────────────────────────────────────

@router.get("/registry/sources")
async def list_registry_sources() -> dict:
    return {"sources": package_repo.list_sources()}


@router.post("/registry/sources", dependencies=[Depends(require_admin)])
async def add_registry_source(req: AddSourceRequest) -> dict:
    try:
        sources = package_repo.add_source(
            req.url, name=req.name, public_key=req.public_key,
            trust=req.trust, enabled=req.enabled,
        )
    except PackageError as exc:
        raise _http_error(exc) from None
    return {"status": 201, "sources": sources}


@router.patch("/registry/sources/{index}", dependencies=[Depends(require_admin)])
async def update_registry_source(index: int, req: UpdateSourceRequest) -> dict:
    try:
        sources = package_repo.update_source(index, **req.model_dump(exclude_none=True))
    except PackageError as exc:
        raise _http_error(exc) from None
    return {"ok": True, "sources": sources}


@router.delete("/registry/sources/{index}", dependencies=[Depends(require_admin)])
async def remove_registry_source(index: int) -> dict:
    try:
        removed = package_repo.remove_source(index)
    except PackageError as exc:
        raise _http_error(exc) from None
    return {"ok": True, "removed": removed.get("url")}


@router.get("/registry/browse")
async def browse_registry(q: str = "", category: str = "", channel: str = "") -> dict:
    try:
        data = await package_repo.browse()
    except PackageError as exc:
        raise _http_error(exc) from None
    packages = data["packages"]
    if q:
        needle = q.lower()
        packages = [p for p in packages if needle in " ".join(
            str(p.get(k, "")) for k in ("id", "name", "description", "category", "author")
        ).lower()]
    if category:
        packages = [p for p in packages if p.get("category") == category]
    if channel:
        packages = [p for p in packages if p.get("channel", "stable") == channel]
    return {**data, "packages": packages}


@router.post("/registry/refresh", dependencies=[Depends(require_admin)])
async def refresh_registry() -> dict:
    try:
        return await package_repo.browse(force=True)
    except PackageError as exc:
        raise _http_error(exc) from None


@router.get("/catalog/{pkg_id}")
async def catalog_detail(pkg_id: str) -> dict:
    try:
        entry = await package_repo.get_catalog_entry(pkg_id)
        manifest = await package_repo.fetch_manifest(entry)
    except PackageError as exc:
        raise _http_error(exc) from None
    # Preflight for the Install button: which host binaries the package needs and
    # whether this NAS has them. Probing is a few stat() calls, no subprocess.
    return {**entry, "manifest": manifest,
            "system_status": system_deps.probe(manifest.get("system_requires"))}


@router.get("/updates")
async def list_updates() -> dict:
    try:
        data = await package_repo.browse()
    except PackageError as exc:
        raise _http_error(exc) from None
    updates = [p for p in data["packages"] if p.get("update_available")]
    return {"updates": updates, "total": len(updates)}


def _data_inventory(pkg_id: str) -> dict:
    """Size of the state a package keeps under `<DATA_DIR>`.

    That is `<DATA_DIR>/packages/data/<id>` for every runtime, plus the core
    state owned by a UI-only package (Cameras stores its cameras and settings in
    the core — see `services/package_state.py`). `remove_data=1` deletes both.
    """
    path = packages_store.data_dir(pkg_id)
    package_exists = path.exists()
    inventory = {"path": str(path), "paths": [str(path)], "exists": package_exists,
                 "size_bytes": packages_store.dir_size(path),
                 "has_package_data": package_exists}
    core = package_state.describe(pkg_id)
    if core:
        inventory["label"] = core["label"]
        inventory["core"] = core
        inventory["exists"] = package_exists or core["exists"]
        inventory["size_bytes"] += core["size_bytes"]
        if core["exists"]:
            inventory["paths"].extend(core["paths"])
        if not package_exists:
            inventory["path"] = core["path"]
    return inventory


@router.get("/orphans", dependencies=[Depends(require_admin)])
async def list_orphan_data() -> dict:
    """Package state left behind by uninstalled packages.

    Uninstall keeps `packages/data/<id>` on purpose (a reinstall resumes with its
    scan logs, certificates and settings), but a removed package has no UI to
    reclaim it. This lists those directories so the operator can purge them.
    """
    installed = {str(p.get("id")) for p in packages_store.load_installed()}
    root = packages_store.DATA_DIR
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        logger.warning("Cannot list the package data directory %s", root, exc_info=True)
        entries = []
    orphans = []
    for entry in entries:
        if entry.name in installed or not entry.is_dir():
            continue
        if not re.fullmatch(_PKG_ID_CORE, entry.name):
            continue
        try:
            modified = int(entry.stat().st_mtime)
        except OSError:
            modified = 0
        orphans.append({"id": entry.name, "path": str(entry),
                        "size_bytes": packages_store.dir_size(entry), "modified": modified})
    # UI-only packages keep their state in the core, not in packages/data/<id>.
    by_id = {item["id"]: item for item in orphans}
    for pkg_id in package_state.known_packages():
        if pkg_id in installed:
            continue
        core = package_state.describe(pkg_id)
        if not core or not core["exists"]:
            continue
        item = by_id.get(pkg_id)
        if item:
            # Both roots exist: one row, one Delete button, the full total.
            item["size_bytes"] += core["size_bytes"]
            item["path"] = f"{item['path']}, {core['path']}"
            item["core"] = True
            item["label"] = core["label"]
            item["modified"] = max(int(item.get("modified") or 0), int(core["modified"]))
            continue
        orphans.append({"id": pkg_id, "path": core["path"], "core": True,
                        "size_bytes": core["size_bytes"], "modified": core["modified"],
                        "label": core["label"]})
    return {"orphans": orphans, "total_bytes": sum(item["size_bytes"] for item in orphans)}


@router.delete("/orphans/{pkg_id}", dependencies=[Depends(require_admin)])
async def delete_orphan_data(pkg_id: str) -> dict:
    try:
        check_pkg_id(pkg_id)
        if packages_store.get_package(pkg_id):
            raise PackageError(
                f"Package {pkg_id!r} is installed; uninstall it with remove_data=1 instead", 409)
    except PackageError as exc:
        raise _http_error(exc) from None
    path = packages_store.data_dir(pkg_id)
    freed = packages_store.dir_size(path)
    shutil.rmtree(path, ignore_errors=True)
    if path.exists():
        raise HTTPException(500, "Could not delete every file in the package data directory")
    core = package_state.purge(pkg_id)
    if core:
        freed += core["size_bytes"]
        if core["errors"]:
            raise HTTPException(500, "Could not delete every file in the core state")
    logger.info("Deleted orphan package data for %s (%d bytes)", pkg_id, freed)
    emit_event("info", "Package data deleted",
               f"{pkg_id}: removed {freed // 1024} KiB left behind by an uninstalled package",
               source="packages", dedupe_key=f"pkg-orphan-purge:{pkg_id}:{freed}")
    return {"deleted": pkg_id, "size_bytes": freed}


# ── Jobs ────────────────────────────────────────────────────────────────────

def _start_job(kind: str, pkg_id: str, work) -> dict:
    job = package_jobs.create(kind, pkg_id)
    if not package_jobs.start(job, work):
        raise HTTPException(status_code=500, detail="Could not start the package task")
    return {"job_id": job["id"], "job": job}


@router.post("/install", dependencies=[Depends(require_admin)])
async def install_package(req: InstallRequest) -> dict:
    try:
        entry = await package_repo.get_catalog_entry(req.package_id)
        if req.source and entry.get("source") != req.source:
            raise PackageError("Package is not available from the selected source", 409)
    except PackageError as exc:
        raise _http_error(exc) from None
    return _start_job("install", req.package_id,
                      lambda job_id: package_installer.install(entry, job_id, req.settings))


@router.get("/jobs")
async def list_jobs() -> dict:
    return {"jobs": package_jobs.list_jobs()}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = package_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/bulk-delete", dependencies=[Depends(require_admin)])
async def bulk_uninstall_packages(req: BulkDeleteRequest) -> dict:
    jobs, errors = [], []
    for pkg_id in dict.fromkeys(req.ids or []):
        try:
            check_pkg_id(pkg_id)
            if not packages_store.get_package(pkg_id):
                errors.append({"id": pkg_id, "error": "not installed"})
                continue
            jobs.append(_start_job(
                "uninstall", pkg_id,
                lambda job_id, pid=pkg_id: package_installer.uninstall(
                    pid, job_id, req.remove_volumes, req.remove_images, req.remove_data,
                ),
            ))
        except (PackageError, HTTPException) as exc:
            errors.append({"id": pkg_id, "error": getattr(exc, "detail", str(exc))})
    return {"ok": True, "jobs": jobs, "errors": errors}


# ── Desktop layout (per-user icon order/visibility) ─────────────────────────

def _current_username(authorization: str) -> Optional[str]:
    from backend.routers.auth import _verify_token
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    payload = _verify_token(token)
    if not payload:
        return None
    username = str(payload.get("username") or "").strip()
    return username[:64] or None


def _load_layouts() -> dict:
    data = _LAYOUT_STORE.load()
    return data if isinstance(data, dict) else {}


def _filter_layout_ids(ids: list[str]) -> list[str]:
    allowed = set(APP_REGISTRY_KEYS)
    out, seen = [], set()
    for value in ids or []:
        if not isinstance(value, str):
            continue
        value = LEGACY_LAYOUT_MAP.get(value, value)
        if (value in allowed or PKG_ICON_RE.match(value)) and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _filter_positions(raw: dict) -> dict:
    allowed = set(APP_REGISTRY_KEYS)
    out: dict[str, dict] = {}
    for app_id, pos in (raw or {}).items():
        if not isinstance(app_id, str) or not isinstance(pos, dict):
            continue
        app_id = LEGACY_LAYOUT_MAP.get(app_id, app_id)
        if not (app_id in allowed or PKG_ICON_RE.match(app_id)):
            continue
        try:
            col, row = int(pos.get("c", 0)), int(pos.get("r", 0))
        except (TypeError, ValueError):
            continue
        out[app_id] = {"c": max(0, min(col, 200)), "r": max(0, min(row, 200))}
    return out


@router.get("/desktop/layout")
async def get_desktop_layout(authorization: str = Header(default="")) -> dict:
    username = _current_username(authorization)
    if not username:
        raise HTTPException(401, "Authentication required")
    with _LAYOUTS_LOCK:
        layout = _load_layouts().get(username)
    if layout is None or not isinstance(layout, dict):
        raise HTTPException(404, "No saved desktop layout for this user")
    migrated = {
        "order": _filter_layout_ids(layout.get("order") or []),
        "visible": _filter_layout_ids(layout.get("visible") or []),
        "positions": _filter_positions(layout.get("positions") or {}),
    }
    if migrated != {"order": layout.get("order"), "visible": layout.get("visible"),
                     "positions": layout.get("positions")}:
        with _LAYOUTS_LOCK:
            data = _load_layouts()
            data[username] = migrated
            _LAYOUT_STORE.save(data)
        logger.info("Desktop layout for %s migrated to package ids", username)
    return {"username": username, **migrated}


@router.post("/desktop/layout")
async def save_desktop_layout(req: LayoutRequest, authorization: str = Header(default="")) -> dict:
    username = _current_username(authorization)
    if not username:
        raise HTTPException(401, "Authentication required")
    order = _filter_layout_ids(req.order) or list(APP_REGISTRY_KEYS)
    visible = _filter_layout_ids(req.visible)
    positions = _filter_positions(req.positions)
    with _LAYOUTS_LOCK:
        data = _load_layouts()
        data[username] = {"order": order, "visible": visible, "positions": positions}
        _LAYOUT_STORE.save(data)
    return {"status": "ok", "username": username, "order": order,
            "visible": visible, "positions": positions}


# ── Per-package endpoints (catch-all — keep LAST) ───────────────────────────

def _native_ui(pkg_id: str) -> tuple[dict, dict]:
    try:
        check_pkg_id(pkg_id)
    except PackageError as exc:
        raise _http_error(exc) from None
    pkg = packages_store.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, f"Package {pkg_id!r} not found")
    ui = pkg.get("ui") or {}
    if ui.get("mode") != "native":
        raise HTTPException(404, "Package does not provide a native UI")
    if pkg.get("trust") != "pinned":
        raise HTTPException(403, "Native UI requires a pinned package source")
    return pkg, ui


@router.get("/{pkg_id}/ui/{asset_path:path}")
async def package_ui_asset(pkg_id: str, asset_path: str) -> Response:
    _pkg, ui = _native_ui(pkg_id)
    try:
        relative = safe_relative_path(asset_path, "ui asset")
    except PackageError as exc:
        raise _http_error(exc) from None
    root = packages_store.package_dir(pkg_id).resolve()
    target = (root / relative).resolve()
    declared = {str(item.get("path")) for item in (_pkg.get("files") or [])}
    if relative not in declared:
        raise HTTPException(404, "UI asset is not declared by the package")
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "UI asset not found")
    if target.stat().st_size > 4 * 1024 * 1024:
        raise HTTPException(413, "UI asset is too large")
    payload = target.read_bytes()
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return Response(content=payload, media_type=media_type,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

def _proxy_identity(request: Request) -> tuple[str, str]:
    """Username and role of the caller, taken from the verified JWT only."""
    from backend.routers.auth import _verify_token

    authorization = request.headers.get("authorization", "")
    token = authorization.removeprefix("Bearer ").strip() if authorization else ""
    payload = _verify_token(token) or {}
    username = str(payload.get("username") or "").strip()
    role = str(payload.get("role") or "user").strip() or "user"
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    return username, role

@router.api_route("/{pkg_id}/proxy/{proxy_path:path}",
                  methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def package_ui_proxy(pkg_id: str, proxy_path: str, request: Request) -> Response:
    """Forward native package UI requests to the package's loopback service.

    Only the port declared by the installed package is reachable; the proxy never
    accepts a target from the client. Standard REST verbs are forwarded so a
    package service can expose PUT/DELETE resources. Admin-only unless the
    manifest declares `user_access: true` (pinned sources only), in which case
    every signed-in account may call it and the server forwards their identity
    as `X-Ethos-User` / `X-Ethos-Role`.
    """
    _pkg, ui = _native_ui(pkg_id)
    username, role = _proxy_identity(request)
    if role != "admin" and not _pkg.get("user_access"):
        raise HTTPException(status_code=403, detail="Admin access required for this package")
    port = int(ui.get("port") or ((_pkg.get("python") or {}).get("port") or 0))
    if not port:
        raise HTTPException(404, "Package has no proxy target")
    path = "/" + str(proxy_path or "").lstrip("/")
    url = f"http://127.0.0.1:{port}{path}"
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body() if request.method in ("POST", "PUT", "PATCH") else None
    headers = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["Content-Type"] = content_type
    # Conditional GET: the package service answers 304 when nothing changed, so
    # a phone start does not re-download a large library every time.
    if request.headers.get("if-none-match"):
        headers["If-None-Match"] = request.headers["if-none-match"]
    # Identity is server-derived: client-supplied X-Ethos-* headers are never copied.
    headers["X-Ethos-User"] = username[:64]
    headers["X-Ethos-Role"] = role[:16]
    headers["X-Ethos-Package"] = pkg_id

    def _call() -> tuple[int, bytes, str, str]:
        req = urllib.request.Request(url, data=body, headers=headers, method=request.method)
        try:
            with urllib.request.urlopen(req, timeout=1800) as resp:
                return (resp.status, resp.read(16 * 1024 * 1024), resp.headers.get(
                    "Content-Type", "application/octet-stream"), resp.headers.get("ETag", ""))
        except urllib.error.HTTPError as exc:
            return (exc.code, exc.read(1024 * 1024), exc.headers.get(
                "Content-Type", "application/json"), exc.headers.get("ETag", ""))
        except OSError as exc:
            # The exception text is a urllib/socket message ("<urlopen error [Errno 111]
            # Connection refused>"); the UI prints `detail` verbatim, so it must stay
            # a sentence the operator can act on. The real cause goes to the logs.
            logger.error("Package service for %s is unreachable on 127.0.0.1:%s (%s %s): %s",
                         pkg_id, port, request.method, path, exc, exc_info=True)
            raise PackageError(
                f"The {pkg_id} service is not responding. Start it in Package Center "
                "and try again.", 503) from None

    try:
        status, payload, media_type, etag = await asyncio.to_thread(_call)
    except PackageError as exc:
        raise _http_error(exc) from None
    response_headers = {"Cache-Control": "no-store"}
    if etag:
        # Private + revalidate: only this account's browser may keep a copy, and
        # it must check with us before reusing it.
        response_headers = {"Cache-Control": "private, no-cache", "ETag": etag}
    return Response(content=payload, status_code=status, media_type=media_type,
                    headers=response_headers)


@router.get("/{pkg_id}")
async def get_package(pkg_id: str) -> dict:
    try:
        check_pkg_id(pkg_id)
    except PackageError as exc:
        raise _http_error(exc) from None
    pkg = packages_store.get_package(pkg_id)
    if not pkg:
        raise HTTPException(404, f"Package {pkg_id!r} not found")
    return pkg


@router.get("/{pkg_id}/status")
async def get_package_status(pkg_id: str) -> dict:
    try:
        return await package_installer.package_status(pkg_id)
    except PackageError as exc:
        raise _http_error(exc) from None


@router.get("/{pkg_id}/uninstall-preview", dependencies=[Depends(require_admin)])
async def uninstall_preview(pkg_id: str) -> dict:
    """What an uninstall can remove: containers, project volumes and images.

    Images still used by another container are reported separately so the UI
    can offer the others and explain why these stay.
    """
    try:
        check_pkg_id(pkg_id)
        record = packages_store.get_package(pkg_id)
        if not record:
            raise PackageError(f"Package {pkg_id!r} not found", 404)
        if str(record.get("runtime") or "compose") == "venv":
            # A venv package owns no containers, volumes or images.
            return {
                "id": pkg_id, "name": record.get("name") or pkg_id,
                "containers": [], "volumes": [], "images": [], "shared_images": [],
                "has_compose": False, "docker": True, "runtime": "venv",
                "data": _data_inventory(pkg_id),
            }
        inventory = await package_cleanup.preview(pkg_id, package_installer.compose_file(pkg_id))
    except PackageError as exc:
        raise _http_error(exc) from None
    return {"id": pkg_id, "name": record.get("name") or pkg_id, "data": _data_inventory(pkg_id), **inventory}


@router.post("/{pkg_id}/action", dependencies=[Depends(require_admin)])
async def package_action(pkg_id: str, req: ActionRequest) -> dict:
    try:
        return await package_installer.compose_action(pkg_id, req.action)
    except PackageError as exc:
        raise _http_error(exc) from None


@router.post("/{pkg_id}/repair-ports", dependencies=[Depends(require_admin)])
async def repair_package_ports(pkg_id: str) -> dict:
    """Re-publish a package on a free host port and restart it.

    The port is picked at install time; a later boot order (another compose
    project grabbing it first) leaves the container unable to bind. This re-plans
    the mapping, rewrites the generated override and recreates the service.
    """
    try:
        return await package_installer.repair_ports(pkg_id)
    except PackageError as exc:
        raise _http_error(exc) from None


@router.post("/{pkg_id}/settings", dependencies=[Depends(require_admin)])
async def update_package_settings(pkg_id: str, req: SettingsRequest) -> dict:
    """Change an installed package's install-time settings (login, password, …).

    The service is recreated so the new values take effect now; values the form
    omits keep their stored value (that is how a password stays secret).
    """
    try:
        return await package_installer.apply_settings(pkg_id, req.values)
    except PackageError as exc:
        raise _http_error(exc) from None


@router.post("/{pkg_id}/update", dependencies=[Depends(require_admin)])
async def update_package_endpoint(pkg_id: str) -> dict:
    try:
        check_pkg_id(pkg_id)
        if not packages_store.get_package(pkg_id):
            raise PackageError(f"Package {pkg_id!r} not found", 404)
    except PackageError as exc:
        raise _http_error(exc) from None
    return _start_job("update", pkg_id,
                      lambda job_id: package_installer.update(pkg_id, job_id))


@router.delete("/{pkg_id}", dependencies=[Depends(require_admin)])
async def uninstall_package(pkg_id: str, remove_volumes: bool = False,
                            remove_images: bool = False,
                            remove_data: bool = False) -> dict:
    """Uninstall a package.

    ``remove_volumes`` also deletes the package's named volumes,
    ``remove_images`` deletes its images once no container uses them, and
    ``remove_data`` deletes its app state (`packages/data/<id>`). All three are
    off by default because package state usually survives a reinstall.
    """
    try:
        check_pkg_id(pkg_id)
        if not packages_store.get_package(pkg_id):
            raise PackageError(f"Package {pkg_id!r} not found", 404)
    except PackageError as exc:
        raise _http_error(exc) from None
    return _start_job("uninstall", pkg_id,
                      lambda job_id: package_installer.uninstall(
                          pkg_id, job_id, remove_volumes, remove_images, remove_data))
