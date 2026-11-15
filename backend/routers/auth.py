"""EthOS NAS — Auth & User Management Router

Authenticates users against Linux PAM (/etc/pam.d/login). No passwords stored locally.

REST:
- POST /api/auth/login  — login with Linux credentials, returns JWT token
- GET  /api/auth/me     — current user (requires Bearer token)
- GET  /api/users       — list users (admin only)
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel

from backend.config import settings
from backend.routers.auth_pam import (
    pam_authenticate, seed_admin_from_system, ensure_user_registered, update_last_login,
    ensure_api_key, find_user_by_api_key, regenerate_api_key,
)
from backend.services import users_store

logger = logging.getLogger("ethos-nas.routers.auth")
router = APIRouter()


def require_admin(request: Request) -> dict:
    """Dependency: allow only authenticated users whose role is `admin`.

    Attach per route with `dependencies=[Depends(require_admin)]`, or per router
    with `APIRouter(dependencies=[Depends(require_admin)])`. The auth middleware
    in main.py populates `request.state.current_user`.
    """
    user = getattr(request.state, "current_user", None) or {}
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if str(user.get("role") or "user") != "admin":
        logger.warning("Admin access denied for %s on %s %s",
                       user.get("username", "?"), request.method, request.url.path)
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_role(*roles: str):
    """Dependency factory for endpoints restricted to a set of roles."""
    allowed = {r.lower() for r in roles} or {"admin"}

    def _dependency(request: Request) -> dict:
        user = getattr(request.state, "current_user", None) or {}
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if str(user.get("role") or "user").lower() not in allowed:
            logger.warning("Access denied for %s on %s %s (needs %s)",
                           user.get("username", "?"), request.method,
                           request.url.path, ", ".join(sorted(allowed)))
            raise HTTPException(
                status_code=403,
                detail=f"{', '.join(sorted(allowed)).title()} access required",
            )
        return user

    return _dependency


# ── JWT (custom HMAC-SHA256) ─────────────────────────────────────────────────

import base64 as _b64


def _b64url(data: bytes) -> str:
    return _b64.urlsafe_b64encode(data).decode().rstrip("=")


def _create_token(user_id: str, username: str, role: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({
        "sub": user_id, "username": username, "role": role,
        "exp": int(time.time()) + settings.JWT_EXPIRY_HOURS * 3600,
        "iat": int(time.time()),
    }).encode())
    sig = _b64url(hmac.new(
        settings.JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256,
    ).digest())
    return f"{header}.{payload}.{sig}"


def _verify_token(token: str) -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, payload, sig = parts
        expected_sig = _b64url(hmac.new(
            settings.JWT_SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256,
        ).digest())
        if not hmac.compare_digest(sig, expected_sig):
            return None
        padding = 4 - len(payload) % 4
        decoded = json.loads(_b64.urlsafe_b64decode(payload + "=" * padding))
        if decoded.get("exp", 0) < int(time.time()):
            return None
        return decoded
    except Exception:
        return None


# ── Endpoints ────────────────────────────────────────────────────────────────

class _LoginRequest(BaseModel):
    username: str = ""
    password: str = ""
    api_key: str = ""  # alternative to username/password (EthOS Photos app)
    model_config = {"extra": "forbid"}


@router.post("/login")
async def login(req: _LoginRequest) -> dict:
    """Authenticate and issue a JWT.

    Two modes:
      • username + password — validated against Linux PAM. Seeds admin from
        system on first boot; auto-registers new users in users.json.
      • api_key — looked up in users.json (no PAM). Used by the EthOS
        Photos Android app; the key is generated per user automatically.
    """
    if req.api_key:
        user = find_user_by_api_key(req.api_key)
        if not user:
            logger.warning("Login failed: unknown API key")
            raise HTTPException(status_code=401, detail="Invalid API key")
        token = _create_token(user["id"], user["username"], user.get("role", "user"))
        logger.info("User %s logged in via API key", user["username"])
        return {
            "token": token,
            "user": {"id": user["id"], "username": user["username"], "role": user.get("role", "user")},
        }

    if not req.username or not req.password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    # Seed admin from system on first boot (users.json absent)
    seed_admin_from_system()

    if not pam_authenticate(req.username, req.password):
        logger.warning("PAM authentication failed for user %s", req.username)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Check existing registration in the account store.
    users_data = users_store.load()

    user = next((u for u in users_data if u.get("username") == req.username), None)
    if not user:
        # Auto-register new PAM-validated Linux user with "user" role
        user = ensure_user_registered(req.username, "user")
    else:
        logger.info("User %s already registered", req.username)

    update_last_login(req.username)
    # Make sure the per-user API key exists so it can be copied from Settings.
    ensure_api_key(req.username)
    token = _create_token(user["id"], user["username"], user["role"])
    return {
        "token": token,
        "user": {"id": user["id"], "username": user["username"], "role": user["role"]},
    }


@router.get("/me")
async def get_me(authorization: str = Header(default="")) -> dict:
    """Return the authenticated user's profile."""
    token = authorization.removeprefix("Bearer ").strip()
    payload = _verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return {"id": payload["sub"], "username": payload["username"], "role": payload["role"]}


@router.get("/api-key")
async def get_api_key(authorization: str = Header(default="")) -> dict:
    """Return the current user's API key (generated automatically if missing).

    The EthOS Photos Android app can log in with this key instead of an
    password. Copy it from Settings → Security on the NAS web UI.
    """
    token = authorization.removeprefix("Bearer ").strip()
    payload = _verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    username = payload["username"]
    key = ensure_api_key(username)
    return {"username": username, "api_key": key}


@router.post("/api-key/regenerate")
async def regen_api_key(authorization: str = Header(default="")) -> dict:
    """Replace the current user's API key with a fresh one.

    The old key stops working immediately (e.g. after a phone is lost).
    """
    token = authorization.removeprefix("Bearer ").strip()
    payload = _verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    username = payload["username"]
    key = regenerate_api_key(username)
    return {"username": username, "api_key": key}


@router.get("/users")
async def list_users(request: Request) -> dict:
    """List all users (admin only)."""
    caller = getattr(request.state, "current_user", None) or {}
    if caller.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    if not users_store.load():
        return {"users": [], "total": 0}
    users = users_store.load()

    safe = [
        {
            "id": u["id"], "username": u["username"], "role": u.get("role", "user"),
            "created_at": u.get("created_at"), "last_login": u.get("last_login"),
        }
        for u in users
    ]
    return {"users": safe, "total": len(safe)}
