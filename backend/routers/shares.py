"""EthOS NAS — Samba Share Management Router

REST:
- GET    /api/shares          — list all Samba shares
- GET    /api/shares/status   — Samba service status
- POST   /api/shares          — add a new share
- PUT    /api/shares/{name}   — update an existing share
- DELETE /api/shares/{name}   — delete a share

Functionality:
- Reads/writes /etc/samba/smb.conf via configparser
- Validates config with testparm before applying
- Reloads smbd/nmbd after changes
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.routers.auth import require_admin
from backend.services.notifications_service import emit_event
from backend.services.shares_service import (
    SambaUnavailable,
    get_shares,
    add_share,
    update_share,
    delete_share,
    get_status,
)

logger = logging.getLogger("ethos-nas.routers.shares")
router = APIRouter()


class ShareCreate(BaseModel):
    """Request body for POST /api/shares."""

    name: str
    path: str
    comment: str = ""
    read_only: bool = False
    browsable: bool = True
    guest_ok: bool = False
    valid_users: str = ""
    create_mask: str = "0755"
    directory_mask: str = "0755"
    model_config = {"extra": "forbid"}


class ShareUpdate(BaseModel):
    """Request body for PUT /api/shares/{name}.

    All fields optional — only provided values are updated.
    """

    path: Optional[str] = None
    comment: Optional[str] = None
    read_only: Optional[bool] = None
    browsable: Optional[bool] = None
    guest_ok: Optional[bool] = None
    valid_users: Optional[str] = None
    create_mask: Optional[str] = None
    directory_mask: Optional[str] = None
    model_config = {"extra": "forbid"}


@router.get("")
async def api_list_shares() -> dict:
    """List all Samba shares (excluding special sections)."""
    try:
        shares = await asyncio.to_thread(get_shares)
        return {"shares": shares, "total": len(shares)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def api_share_status() -> dict:
    """Get Samba service status (active, enabled, config path)."""
    try:
        return await asyncio.to_thread(get_status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("", dependencies=[Depends(require_admin)])
async def api_add_share(body: ShareCreate) -> dict:
    """Add a new Samba share.

    Validates path exists, config via testparm, then reloads smbd.
    """
    try:
        result = await asyncio.to_thread(add_share,
            name=body.name,
            path=body.path,
            comment=body.comment,
            read_only=body.read_only,
            browsable=body.browsable,
            guest_ok=body.guest_ok,
            valid_users=body.valid_users,
            create_mask=body.create_mask,
            directory_mask=body.directory_mask,
        )
        emit_event("success", "Samba share added", f"{body.name} → {body.path}", source="shares")
        return result
    except SambaUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=(
                "Permission denied — cannot write /etc/samba/smb.conf. "
                "Run setup_scripts/setup_service.sh as root to install the NOPASSWD rules."
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{name}", dependencies=[Depends(require_admin)])
async def api_update_share(name: str, body: ShareUpdate) -> dict:
    """Update an existing Samba share.

    Only provided fields are modified. Validates new path if provided.
    """
    try:
        result = await asyncio.to_thread(update_share,
            name=name,
            path=body.path,
            comment=body.comment,
            read_only=body.read_only,
            browsable=body.browsable,
            guest_ok=body.guest_ok,
            valid_users=body.valid_users,
            create_mask=body.create_mask,
            directory_mask=body.directory_mask,
        )
        emit_event("info", "Samba share updated", name, source="shares")
        return result
    except SambaUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=(
                "Permission denied — cannot write /etc/samba/smb.conf. "
                "Run setup_scripts/setup_service.sh as root to install the NOPASSWD rules."
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{name}", dependencies=[Depends(require_admin)])
async def api_delete_share(name: str) -> dict:
    """Delete a Samba share by name."""
    try:
        result = await asyncio.to_thread(delete_share, name)
        emit_event("warning", "Samba share removed", name, source="shares")
        return result
    except SambaUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail=(
                "Permission denied — cannot write /etc/samba/smb.conf. "
                "Run setup_scripts/setup_service.sh as root to install the NOPASSWD rules."
            ),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
