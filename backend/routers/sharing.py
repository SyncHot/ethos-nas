"""EthOS NAS — user-to-user sharing (view-only grants).

Mounted at ``/api/sharing``. Every route requires a JWT (global middleware);
the router publishes the caller identity with ``scope_request`` so grant
targets are validated with the same path rules as the rest of the app.

A grant never grants writes: recipients read through ``sanitize_read`` only.
See ``docs/sharing.md`` for the model and the app integration matrix.
"""

import logging
import os
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.services import path_scope, sharing_resources, sharing_service, users_store

logger = logging.getLogger("ethos-nas.routers.sharing")

router = APIRouter(dependencies=[Depends(path_scope.scope_request)])

_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_RESERVED = {"root", "ethos-nas", "nobody", "system"}


class GrantRequest(BaseModel):
    kind: Literal["path", "album", "playlist"]
    resource: str = Field(min_length=1, max_length=1024)
    label: str = Field(default="", max_length=120)
    recipients: list[str] = Field(default_factory=list)
    all: bool = False


class GrantUpdate(BaseModel):
    recipients: Optional[list[str]] = None
    all: Optional[bool] = None


def _caller(request: Request) -> tuple[str, str]:
    payload = getattr(request.state, "current_user", None) or {}
    username = str(payload.get("username") or "")
    role = str(payload.get("role") or "user")
    if not username:
        raise HTTPException(status_code=401, detail="Authentication required")
    return username, role


def _known_recipients(raw: list[str] | None, owner: str) -> list[str]:
    """Validate recipients are active, real accounts (no typos, no tombstones)."""
    out: list[str] = []
    for value in raw or []:
        name = str(value or "").strip()
        if not name or name == owner or name in out:
            continue
        record = users_store.find_username(name)
        if record is None or not users_store.is_active(record):
            raise HTTPException(status_code=400, detail=f"Unknown or inactive user: {name}")
        out.append(name)
        if len(out) > sharing_service.MAX_RECIPIENTS:
            raise HTTPException(status_code=400, detail="Too many recipients")
    return out


def _validated_resource(owner: str, kind: str, resource: str) -> str:
    """Normalise the resource and prove the caller owns it."""
    if kind == "path":
        target = path_scope.sanitize_share(resource)
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail="File or folder not found")
        return target
    if kind in ("album", "playlist"):
        if not sharing_resources.resource_exists(owner, kind, resource):
            raise HTTPException(status_code=404, detail=f"Unknown {kind}")
        return resource
    raise HTTPException(status_code=400, detail="Unsupported resource kind")


def _grant_or_404(grant_id: str, owner: str) -> dict:
    grant = sharing_service.get_grant_by_id(grant_id, owner)
    if grant is None:
        raise HTTPException(status_code=404, detail="Share not found")
    return grant


@router.get("/users")
def list_users(request: Request) -> dict:
    """Minimal directory for the recipient picker.

    Deliberately returns only active usernames and roles — no uid, home,
    api_key or login timestamps. Admin-only ``/api/users`` stays untouched.
    """
    caller, _role = _caller(request)
    users = []
    for record in users_store.load_cached():
        name = str(record.get("username") or "")
        if not _USERNAME_RE.match(name) or name in _RESERVED:
            continue
        if not users_store.is_active(record):
            continue
        users.append({"username": name, "role": str(record.get("role") or "user")})
    users.sort(key=lambda item: item["username"].lower())
    return {"users": users, "self": caller}


@router.get("/incoming")
def incoming(request: Request, kind: Optional[str] = Query(default=None)) -> dict:
    caller, _role = _caller(request)
    if kind and kind not in sharing_resources.KINDS:
        raise HTTPException(status_code=400, detail="Unsupported resource kind")
    grants = sharing_service.list_incoming(caller)
    if kind:
        grants = [grant for grant in grants if grant.get("kind") == kind]
    return {"grants": grants}


@router.get("/outgoing")
def outgoing(request: Request, kind: Optional[str] = Query(default=None)) -> dict:
    caller, _role = _caller(request)
    if kind and kind not in sharing_resources.KINDS:
        raise HTTPException(status_code=400, detail="Unsupported resource kind")
    grants = sharing_service.list_outgoing(caller)
    if kind:
        grants = [grant for grant in grants if grant.get("kind") == kind]
    return {"grants": grants}


@router.get("/resource")
def resource_grant(
    request: Request,
    kind: str = Query(...),
    resource: str = Query(..., max_length=1024),
) -> dict:
    caller, _role = _caller(request)
    if kind not in sharing_resources.KINDS:
        raise HTTPException(status_code=400, detail="Unsupported resource kind")
    return {"grant": sharing_service.get_grant(caller, kind, resource)}


@router.post("/grants")
def create_grant(body: GrantRequest, request: Request) -> dict:
    owner, _role = _caller(request)
    resource = _validated_resource(owner, body.kind, body.resource)
    recipients = [] if body.all else _known_recipients(body.recipients, owner)
    if not body.all and not recipients:
        raise HTTPException(status_code=400, detail="Select at least one user")
    try:
        grant, added = sharing_service.upsert_grant(
            owner, body.kind, resource,
            recipients=recipients, share_all=body.all, label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    sharing_service.notify_share(owner, grant, added)
    logger.info("Share %s: %s shared %s %s with %s", grant["id"], owner, body.kind,
                resource, "all users" if body.all else recipients)
    return {"ok": True, "grant": grant}


@router.patch("/grants/{grant_id}")
def update_grant(grant_id: str, body: GrantUpdate, request: Request) -> dict:
    owner, _role = _caller(request)
    current = _grant_or_404(grant_id, owner)
    share_all = current["all"] if body.all is None else body.all
    raw_recipients = current["recipients"] if body.recipients is None else body.recipients
    recipients = [] if share_all else _known_recipients(raw_recipients, owner)
    if not share_all and not recipients:
        raise HTTPException(status_code=400, detail="Select at least one user")
    grant, added = sharing_service.upsert_grant(
        owner, current["kind"], current["resource"],
        recipients=recipients, share_all=share_all, label=current["label"],
    )
    sharing_service.notify_share(owner, grant, added)
    logger.info("Share %s updated: %s → %s", grant_id, owner,
                "all users" if share_all else recipients)
    return {"ok": True, "grant": grant}


@router.delete("/grants/{grant_id}")
def revoke_grant(grant_id: str, request: Request) -> dict:
    owner, _role = _caller(request)
    if not sharing_service.delete_grant(grant_id, owner):
        raise HTTPException(status_code=404, detail="Share not found")
    logger.info("Share %s revoked by %s", grant_id, owner)
    return {"ok": True}
