"""Per-user filesystem scoping shared by every router that touches disks.

The service user runs with broad ACLs (see docs/service-user.md), so an
unscoped absolute path is effectively root read/write for any authenticated
account. Every router that touches disks mounts ``scope_request`` and passes
each path through ``sanitize`` so the rule cannot drift between modules.

Admins keep unrestricted access (documented decision — they already own the
host). Every other account is limited to its own home directory plus the
shared data mounts ``/mnt`` and ``/media``. The EthOS state directory is never
in scope: ``users.json`` (which stores every API key), configuration stores and
logs must not be reachable through ``/api/files`` even when DATA_DIR lives
under ``/home``.
"""

import contextvars
import logging
import os
import pwd

from fastapi import HTTPException, Request

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.path_scope")

# Shared data mounts every authenticated account may use.
SHARED_ROOTS = ("/mnt", "/media")
HOME_PREFIX = "/home"

# Identity of the caller for the current request, published by scope_request().
# Fail closed: a router that forgets the dependency denies instead of allowing
# admin-level access. Background workers wrap their work in system_scope().
CURRENT_ROLE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "files_role", default="user")
CURRENT_USER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "files_user", default="")


def home_dir(username: str) -> str:
    """Home directory for `username` (fallback: /home/<name>)."""
    if not username:
        return ""
    try:
        return os.path.realpath(pwd.getpwnam(username).pw_dir)
    except (KeyError, OSError):
        return os.path.realpath(os.path.join(HOME_PREFIX, username))


def allowed_roots(username: str = "", role: str = "user") -> tuple[str, ...]:
    """Filesystem roots the identity may use. Admin → ("/",) (unrestricted)."""
    if role == "admin":
        return ("/",)
    home = home_dir(username)
    return ((home,) if home else ()) + SHARED_ROOTS


def current_identity() -> tuple[str, str]:
    """(username, role) published by scope_request() / system_scope()."""
    return CURRENT_USER.get(), CURRENT_ROLE.get()


async def scope_request(request: Request) -> None:
    """Publish the caller's identity for sanitize().

    Must be `async def`: FastAPI runs sync dependencies in a worker thread,
    and a contextvar set there does not propagate back to the request context
    (the handler would keep the permissive default and skip the scope check).

    Use as a router dependency: ``APIRouter(dependencies=[Depends(scope_request)])``.
    """
    user = getattr(request.state, "current_user", None) or {}
    CURRENT_ROLE.set(str(user.get("role") or "user"))
    CURRENT_USER.set(str(user.get("username") or ""))


class system_scope:
    """Privileged identity for background work outside a request.

    Workers that continue request-created state (gallery scans, download
    engine, thumbnail warmers) wrap their work explicitly: the fail-closed
    default has no home directory and would reject every state path.
    """

    def __init__(self, username: str = "system", role: str = "admin"):
        self._username = username
        self._role = role
        self._role_token: contextvars.Token | None = None
        self._user_token: contextvars.Token | None = None

    def __enter__(self) -> "system_scope":
        self._role_token = CURRENT_ROLE.set(self._role)
        self._user_token = CURRENT_USER.set(self._username)
        return self

    def __exit__(self, *exc_info) -> bool:
        if self._role_token is not None:
            CURRENT_ROLE.reset(self._role_token)
        if self._user_token is not None:
            CURRENT_USER.reset(self._user_token)
        return False


def _under(path: str, root: str) -> bool:
    root = root.rstrip(os.sep) or root
    return path == root or path.startswith(root + os.sep)


def sanitize(path: str) -> str:
    """Resolve to an absolute path and enforce the per-identity scope.

    ``..`` is normalised by abspath; symlinks are resolved for the scope check
    so a link cannot be used to hop outside the allowed roots. ``~`` expands to
    the caller's home (not the service account's HOME). Admins keep full
    access; every other account is limited to its own home, /mnt and /media,
    and the EthOS DATA_DIR is never user-facing.
    """
    if not path or not isinstance(path, str):
        raise HTTPException(status_code=400, detail="Path is required")
    username, role = current_identity()
    if path == "~" or path.startswith("~/"):
        home = home_dir(username)
        if home:
            path = home + path[1:]
    abs_path = os.path.abspath(os.path.expanduser(path))
    if role == "admin":
        return abs_path
    real = os.path.realpath(abs_path)
    data_dir = os.path.realpath(str(settings.DATA_DIR))
    if _under(real, data_dir):
        logger.warning("Path rejected inside the EthOS state directory: %s", abs_path)
        raise HTTPException(
            status_code=403,
            detail="Access denied: the EthOS state directory is not user-facing",
        )
    if not any(_under(real, root) for root in allowed_roots(username, role)):
        logger.warning("Path rejected outside allowed roots: %s", abs_path)
        raise HTTPException(
            status_code=403,
            detail="Access denied: your account is limited to its home directory, /mnt and /media",
        )
    return abs_path


def sanitize_for(path: str, username: str, role: str) -> str:
    """Sanitize `path` as an explicit identity.

    Used by public endpoints and background jobs that act on behalf of a
    stored owner (share links, download tasks) outside the request context.
    """
    role_token = CURRENT_ROLE.set(role or "user")
    user_token = CURRENT_USER.set(username or "")
    try:
        return sanitize(path)
    finally:
        CURRENT_ROLE.reset(role_token)
        CURRENT_USER.reset(user_token)
