"""Per-role filesystem scoping shared by every router that touches disks.

The service user runs with broad ACLs (see docs/service-user.md), so an
unscoped absolute path is effectively root read/write for any authenticated
account. `files.py`, `sync.py` and `file_ops.py` all resolve request paths
through this module so the rule cannot drift between routers.
"""

import contextvars
import logging
import os

from fastapi import HTTPException, Request

logger = logging.getLogger("ethos-nas.services.path_scope")

# Shared areas every authenticated account may use. Anything else (system
# dirs, other users' homes, /etc) is admin-only.
ALLOWED_ROOTS = ("/home", "/mnt", "/media")

# Role of the caller for the current request, published by scope_request().
CURRENT_ROLE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "files_role", default="admin")


async def scope_request(request: Request) -> None:
    """Publish the caller's role for sanitize().

    Must be `async def`: FastAPI runs sync dependencies in a worker thread,
    and a contextvar set there does not propagate back to the request context
    (the handler would keep the permissive default and skip the scope check).

    Use as a router dependency: ``APIRouter(dependencies=[Depends(scope_request)])``.
    Without it the contextvar keeps its ``admin`` default, which is only safe
    for routers mounted behind an admin dependency.
    """
    user = getattr(request.state, "current_user", None) or {}
    CURRENT_ROLE.set(str(user.get("role") or "user"))


def sanitize(path: str) -> str:
    """Resolve to an absolute path and enforce the per-role scope.

    ``..`` is normalised by abspath; symlinks are resolved for the scope check
    so a link cannot be used to hop outside the allowed roots. Non-admin
    accounts are limited to ALLOWED_ROOTS; admins keep full access.
    """
    if not path or not isinstance(path, str):
        raise HTTPException(status_code=400, detail="Path is required")
    abs_path = os.path.abspath(os.path.expanduser(path))
    if CURRENT_ROLE.get() != "admin":
        real = os.path.realpath(abs_path)
        if not any(real == r or real.startswith(r + os.sep) for r in ALLOWED_ROOTS):
            logger.warning("Path rejected outside allowed roots: %s", abs_path)
            raise HTTPException(
                status_code=403,
                detail="Access denied: your account is limited to /home, /mnt and /media",
            )
    return abs_path
