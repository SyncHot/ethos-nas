"""Explicit user-to-user sharing grants (view-only).

One store, one grant shape, one enforcement point. A grant says “owner shares
this resource with these active accounts (or everyone) for reading”. It never
grants writes: ``path_scope.sanitize`` remains own-scope only, and only the
read-only ``sanitize_read`` consults this module.

Resources are resolved live by ``sharing_resources`` (albums/playlists can be
edited after a grant was created). Grants are re-validated on every read:
the owner must still be active and the paths must still be shareable, so a
disabled account, a deleted home or an admin demotion takes effect without a
cleanup job.

Store: ``<DATA_DIR>/sharing.json`` — ``{"version": 1, "grants": [...]}``.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from backend.config import settings
from backend.services import json_store, sharing_resources, user_data, users_store
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.sharing_service")

DATA_FILE = settings.DATA_DIR / "sharing.json"
SCHEMA_VERSION = 1
MAX_GRANTS = 5000
MAX_RECIPIENTS = 200
CACHE_TTL = 5.0

_CACHE_LOCK = threading.Lock()
_INCOMING_CACHE: dict[str, tuple[float, list[dict]]] = {}
_PATH_CACHE: dict[tuple[str, str, str], tuple[float, frozenset[str]]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_store() -> dict:
    return {"version": SCHEMA_VERSION, "grants": []}


def _migrate(from_version: int, to_version: int, data: Any) -> Any:
    if not isinstance(data, dict) or not isinstance(data.get("grants"), list):
        raise ValueError("sharing store is not an object with a grants list")
    data["grants"] = [g for g in data["grants"] if isinstance(g, dict) and g.get("id")]
    return data


_STORE = json_store.get(
    DATA_FILE,
    _default_store,
    version=SCHEMA_VERSION,
    migrate=_migrate,
)


def _bump() -> None:
    with _CACHE_LOCK:
        _INCOMING_CACHE.clear()
        _PATH_CACHE.clear()


def _clean_name(value: Any) -> str:
    return str(value or "").strip()


def _clean_recipients(recipients: Any) -> list[str]:
    out: list[str] = []
    if not isinstance(recipients, (list, tuple)):
        return out
    for raw in recipients:
        name = _clean_name(raw)
        if not name or name in out:
            continue
        out.append(name)
        if len(out) >= MAX_RECIPIENTS:
            break
    return out


def _grants() -> list[dict]:
    data = _STORE.load()
    grants = data.get("grants") if isinstance(data, dict) else None
    return [dict(g) for g in grants] if isinstance(grants, list) else []


def _under(path: str, root: str) -> bool:
    root = root.rstrip(os.sep) or root
    return path == root or path.startswith(root + os.sep)


def _owner_record(owner: str) -> dict | None:
    return users_store.find_username(owner)


def _owner_role(owner: str) -> str:
    record = _owner_record(owner)
    return str((record or {}).get("role") or "user")


def _owner_active(owner: str) -> bool:
    """Unknown owners are tolerated (tests mint JWTs); disabled ones are not."""
    record = _owner_record(owner)
    return True if record is None else users_store.is_active(record)


def _owner_allows_path(owner: str, path: str) -> bool:
    """The owner must still be allowed the path under its *current* scope."""
    if not owner or not path or not sharing_resources.is_shareable_path(path):
        return False
    role = _owner_role(owner)
    if role == "admin":
        return True
    from backend.services import path_scope  # local import: path_scope → sharing (lazy)
    real = os.path.realpath(path)
    return any(_under(real, root) for root in path_scope.allowed_roots(owner, role))


def _recipient_ok(grant: dict, username: str) -> bool:
    if not username:
        return False
    if _clean_name(grant.get("owner")) == username:
        return False
    if grant.get("all"):
        return True
    return username in (grant.get("recipients") or [])


def _owner_ok(grant: dict) -> bool:
    owner = _clean_name(grant.get("owner"))
    if not owner or not _owner_active(owner):
        return False
    kind = _clean_name(grant.get("kind"))
    resource = _clean_name(grant.get("resource"))
    if kind == "path":
        return _owner_allows_path(owner, resource) and os.path.exists(resource)
    return True


def _label(grant: dict) -> str:
    stored = _clean_name(grant.get("label"))
    if stored:
        return stored
    return sharing_resources.label(
        _clean_name(grant.get("owner")),
        _clean_name(grant.get("kind")),
        _clean_name(grant.get("resource")),
    )


def _public(grant: dict) -> dict:
    return {
        "id": _clean_name(grant.get("id")),
        "owner": _clean_name(grant.get("owner")),
        "kind": _clean_name(grant.get("kind")),
        "resource": _clean_name(grant.get("resource")),
        "label": _label(grant),
        "recipients": list(grant.get("recipients") or []),
        "all": bool(grant.get("all")),
        "created_at": _clean_name(grant.get("created_at")),
        "updated_at": _clean_name(grant.get("updated_at")),
    }


def _sort_key(grant: dict) -> str:
    return _clean_name(grant.get("updated_at") or grant.get("created_at"))


def list_outgoing(owner: str) -> list[dict]:
    """Grants created by `owner`, newest first."""
    return [_public(g) for g in sorted(
        (g for g in _grants() if _clean_name(g.get("owner")) == owner),
        key=_sort_key, reverse=True,
    )]


def list_incoming(username: str) -> list[dict]:
    """Grants addressed to `username` from active owners, newest first."""
    rows = []
    for grant in _grants():
        if not _recipient_ok(grant, username) or not _owner_ok(grant):
            continue
        rows.append(_public(grant))
    rows.sort(key=lambda g: g.get("updated_at") or g.get("created_at") or "", reverse=True)
    return rows


def get_grant(owner: str, kind: str, resource: str) -> dict | None:
    for grant in _grants():
        if (_clean_name(grant.get("owner")) == owner
                and _clean_name(grant.get("kind")) == kind
                and _clean_name(grant.get("resource")) == resource):
            return _public(grant)
    return None


def get_grant_by_id(grant_id: str, owner: str = "") -> dict | None:
    for grant in _grants():
        if _clean_name(grant.get("id")) != grant_id:
            continue
        if owner and _clean_name(grant.get("owner")) != owner:
            return None
        return _public(grant)
    return None


def upsert_grant(owner: str, kind: str, resource: str, *,
                 recipients: list[str] | None = None,
                 share_all: bool = False,
                 label: str = "") -> tuple[dict, list[str]]:
    """Create or replace the grant for one resource.

    Returns ``(grant, newly_added_recipients)`` so the caller can notify only
    the accounts that gained access in this change.
    """
    clean_recipients = _clean_recipients(recipients)
    clean_label = _clean_name(label)[:120]
    created = _now_iso()
    added: list[str] = []

    def _mutate(store: dict) -> dict:
        nonlocal added
        grants = store.setdefault("grants", [])
        existing = next((g for g in grants
                         if _clean_name(g.get("owner")) == owner
                         and _clean_name(g.get("kind")) == kind
                         and _clean_name(g.get("resource")) == resource), None)
        if existing is None:
            if len(grants) >= MAX_GRANTS:
                raise ValueError("too many sharing grants")
            existing = {
                "id": f"shr_{os.urandom(6).hex()}",
                "owner": owner,
                "kind": kind,
                "resource": resource,
                "created_at": created,
            }
            grants.append(existing)
            before: list[str] = []
        else:
            before = list(existing.get("recipients") or [])
        existing["label"] = clean_label or sharing_resources.label(owner, kind, resource)
        existing["recipients"] = [] if share_all else clean_recipients
        existing["all"] = bool(share_all)
        existing["updated_at"] = created
        after = [] if share_all else clean_recipients
        added = [name for name in after if name not in before]
        return dict(existing)

    grant = _STORE.update(_mutate)
    _bump()
    return _public(grant), added


def delete_grant(grant_id: str, owner: str) -> bool:
    """Revoke one grant. Only its owner may delete it."""
    def _mutate(store: dict) -> bool:
        grants = store.get("grants") or []
        target = next((g for g in grants if _clean_name(g.get("id")) == grant_id), None)
        if target is None or _clean_name(target.get("owner")) != owner:
            return False
        store["grants"] = [g for g in grants if _clean_name(g.get("id")) != grant_id]
        return True

    removed = _STORE.update(_mutate)
    if removed:
        _bump()
    return removed


def _incoming_grants(username: str) -> list[dict]:
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _INCOMING_CACHE.get(username)
        if hit and hit[0] > now:
            return hit[1]
    # Cache only the recipient match; owner status is re-checked per request so
    # disabling/re-enabling an account takes effect without waiting out the TTL.
    grants = [g for g in _grants() if _recipient_ok(g, username)]
    with _CACHE_LOCK:
        _INCOMING_CACHE[username] = (now + CACHE_TTL, grants)
        if len(_INCOMING_CACHE) > 64:
            for stale in [k for k, v in _INCOMING_CACHE.items() if v[0] <= now]:
                _INCOMING_CACHE.pop(stale, None)
    return grants


def _resource_paths(owner: str, kind: str, resource: str) -> frozenset[str]:
    key = (owner, kind, resource)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _PATH_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    resolved = {
        path for path in sharing_resources.shareable_paths(owner, kind, resource)
        if _owner_allows_path(owner, path)
    }
    frozen = frozenset(resolved)
    with _CACHE_LOCK:
        _PATH_CACHE[key] = (now + CACHE_TTL, frozen)
        # Keyed by (owner, kind, resource) — one entry per shared root ever
        # looked at; sweep the expired ones so it cannot grow forever.
        if len(_PATH_CACHE) > 256:
            for stale in [k for k, v in _PATH_CACHE.items() if v[0] <= now]:
                _PATH_CACHE.pop(stale, None)
    return frozen


def allows_resource(username: str, owner: str, kind: str, resource: str) -> bool:
    """True when `username` may read `owner`'s resource."""
    if not username or not owner:
        return False
    if username == owner:
        return True
    grant = next((g for g in _incoming_grants(username)
                  if _clean_name(g.get("owner")) == owner
                  and _clean_name(g.get("kind")) == kind
                  and _clean_name(g.get("resource")) == resource), None)
    if grant is None:
        return False
    return _owner_ok(grant)


def allows_read(username: str, real_path: str) -> bool:
    """Read-only check used by ``path_scope.sanitize_read``.

    `real_path` must already be resolved (realpath). Returns True when any
    incoming grant covers it. Never grants write access.
    """
    if not username or not real_path:
        return False
    if not sharing_resources.is_shareable_path(real_path):
        return False
    for grant in _incoming_grants(username):
        if not _owner_ok(grant):
            continue
        owner = _clean_name(grant.get("owner"))
        kind = _clean_name(grant.get("kind"))
        resource = _clean_name(grant.get("resource"))
        if kind == "path":
            if _under(real_path, resource) and _owner_allows_path(owner, resource):
                return True
        elif real_path in _resource_paths(owner, kind, resource):
            return True
    return False


def notify_share(owner: str, grant: dict, added: list[str]) -> None:
    """Publish a targeted “Shared with you” event.

    A broadcast grant (`all`) intentionally notifies everyone; the event still
    carries ``users=[]`` which the Notification Center treats as public.
    """
    kind = _clean_name(grant.get("kind"))
    label = _clean_name(grant.get("label")) or _clean_name(grant.get("resource"))
    action = {"label": "Open Sharing", "appId": "sharing"}
    if grant.get("all"):
        emit_event(
            "info", "Shared with everyone",
            f'{owner} shared "{label}" with all users',
            source="sharing", action=action,
            dedupe_key=f"sharing:{grant.get('id')}",
        )
    elif added:
        emit_event(
            "info", "Shared with you",
            f'{owner} shared "{label}" ({kind})',
            source="sharing", action=action,
            users=added, dedupe_key=f"sharing:{grant.get('id')}",
        )


def purge_user(username: str) -> list[str]:
    """Remove grants owned by `username` and recipient entries pointing at it."""
    safe = user_data.safe_username(username)
    removed: list[str] = []

    def _mutate(store: dict) -> None:
        grants = store.get("grants") or []
        kept: list[dict] = []
        for grant in grants:
            owner = _clean_name(grant.get("owner"))
            if owner in {safe, username}:
                removed.append(f"sharing:{grant.get('id')}")
                continue
            recipients = [r for r in (grant.get("recipients") or [])
                          if r not in {safe, username}]
            if recipients != (grant.get("recipients") or []):
                grant["recipients"] = recipients
                grant["updated_at"] = _now_iso()
            kept.append(grant)
        store["grants"] = kept

    try:
        _STORE.update(_mutate)
        if removed:
            _bump()
    except Exception:  # noqa: BLE001 — deletion must not fail on a broken store
        logger.error("Could not purge sharing grants for %s", username, exc_info=True)
    return removed
