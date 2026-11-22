"""Admin user management: create, edit, lock and delete EthOS accounts.

Spec: docs/user-management.md. Every route is admin-only; guards that protect
the appliance (no self-delete, never the last active admin, system accounts)
live in `services/user_accounts.py` so the API cannot be bypassed by the UI.

Identity model: a real Linux account (PAM) plus its record in users.json
(role/status/API key). Deletion keeps a tombstone so an already-issued JWT or
API key stops working immediately.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.routers.auth import require_admin
from backend.services import user_accounts

logger = logging.getLogger("ethos-nas.routers.users")
router = APIRouter()


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"
    samba_password: str = ""
    model_config = {"extra": "forbid"}


class PatchUserRequest(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None
    model_config = {"extra": "forbid"}


class PasswordRequest(BaseModel):
    password: str
    model_config = {"extra": "forbid"}


class DeleteUserRequest(BaseModel):
    """DELETE carries its options in the query string; this model documents them."""

    remove_home: bool = False
    remove_ethos_state: bool = True


def _actor(request: Request) -> str:
    user = getattr(request.state, "current_user", None) or {}
    return str(user.get("username") or "")


def _http_error(exc: Exception, action: str) -> HTTPException:
    if isinstance(exc, user_accounts.UserAccountNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, user_accounts.UserAccountConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, user_accounts.UserPolicyError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, user_accounts.UserAccountUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    logger.error("User account %s failed", action, exc_info=True)
    return HTTPException(status_code=500, detail="Account operation failed")


@router.get("")
async def list_users(request: Request, include_deleted: bool = False,
                     _admin: dict = Depends(require_admin)) -> dict:
    """Registered accounts merged with Linux uid/home and Samba state."""
    try:
        return user_accounts.list_accounts(include_deleted=include_deleted)
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc, "list") from None


@router.post("", status_code=201)
async def create_user(req: CreateUserRequest, request: Request,
                      _admin: dict = Depends(require_admin)) -> dict:
    """Create a Linux account + its registry record."""
    try:
        return user_accounts.create_user(
            req.username, req.password, role=req.role,
            samba_password=req.samba_password, actor=_actor(request),
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc, "create") from None


@router.patch("/{username}")
async def patch_user(username: str, req: PatchUserRequest, request: Request,
                     _admin: dict = Depends(require_admin)) -> dict:
    """Change the role and/or the enabled state of an account."""
    if req.role is None and req.status is None:
        raise HTTPException(status_code=422, detail="Nothing to update")
    try:
        user = None
        if req.role is not None:
            user = user_accounts.set_role(username, req.role, actor=_actor(request))
        if req.status is not None:
            user = user_accounts.set_status(username, req.status, actor=_actor(request))
        return {"user": user}
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc, "patch") from None


@router.post("/{username}/password")
async def reset_password(username: str, req: PasswordRequest, request: Request,
                         _admin: dict = Depends(require_admin)) -> dict:
    """Admin password reset (the password is never returned or logged)."""
    try:
        return user_accounts.set_password(username, req.password, actor=_actor(request))
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc, "password") from None


@router.delete("/{username}")
async def delete_user(username: str, request: Request,
                      remove_home: bool = False, remove_ethos_state: bool = True,
                      _admin: dict = Depends(require_admin)) -> dict:
    """Delete the Linux account, its Samba credentials and (optionally) its state."""
    try:
        return user_accounts.delete_user(
            username, remove_home=remove_home,
            remove_ethos_state=remove_ethos_state, actor=_actor(request),
        )
    except Exception as exc:  # noqa: BLE001
        raise _http_error(exc, "delete") from None
