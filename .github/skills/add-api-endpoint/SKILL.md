---
name: add-api-endpoint
description: "Add/change a FastAPI endpoint in backend/routers/ — new route or /api domain, auth, admin checks, path scope, json_store, emit_event, main.py mount."
argument-hint: "Describe the endpoint (method, path, who may call it, what it touches)"
---

# Add an API endpoint

## When to use
- New route in an existing router under `backend/routers/`.
- New router file mounted at `/api/<domain>`.
- Changing auth, error shape, pagination, or persistence of an existing route.

## Read first
`docs/api-conventions.md`, plus the closest sibling router (`appearance.py`, `cron.py`, `downloads.py`).

## Procedure

### 1. Choose the access level
| Caller | Wiring |
|---|---|
| Public | Exact path in the `public_paths` **set** in `backend/main.py` (never a prefix) — ask before adding |
| Authenticated user | Default middleware; use `request.state.current_user` |
| Admin | `APIRouter(dependencies=[Depends(require_admin)])` or per-route `dependencies=[Depends(require_admin)]` from `backend.routers.auth` |
| Role subset | `Depends(require_role("admin", "..."))` |
| Filesystem paths | `APIRouter(dependencies=[Depends(scope_request)])` + `sanitize(path)` from `backend.services.path_scope` |

`scope_request` **must stay `async def`** — a sync dependency runs in a worker thread and the
contextvar never reaches the handler (this silently disabled every scope check once).

### 2. Write the router
```python
import logging
from fastapi import APIRouter, Depends, HTTPException, Request

from backend.config import settings              # never os.getenv outside config.py
from backend.routers.auth import require_admin   # only if privileged
from backend.services import json_store

logger = logging.getLogger("ethos-nas.routers.<name>")   # child of the ethos logger
router = APIRouter()                                     # or APIRouter(dependencies=[...])
```

- Pydantic models inline in the router (`models.py` only if shared across routers).
- Validate input at the boundary; return 400/422 with a human-readable `detail`.

### 3. Errors and logging
- `HTTPException(status_code=…, detail="…")`; never `200 {"ok": false}` for a failure.
- 400 input, 401 token, 403 forbidden, 404 unknown, 409 conflict, 422 validation, 500 unexpected.
- Log every failure: `logger.error("Failed to X (id=%s)", item_id, exc_info=True)`; never log credentials.

### 4. Persist state
```python
_STORE = json_store.get(settings.DATA_DIR / "<name>.json", dict, version=1)
data = _STORE.load()
_STORE.save(data)
_STORE.update(lambda d: d.update(...))   # atomic read-modify-write
```
- `json.dump(` in a router fails `scripts/check_boundaries.py`.
- Pass `version=` and a `migrate=` callable when the shape changes.
- Bound unbounded lists (cap length or register with `services/retention_service.py`).

### 5. Long-running work
- Return `{op_id}` / `{upload_id}` immediately and expose a `GET` to poll.
- Spawn background work with `backend.services.task_registry.spawn` (cancellable on shutdown).
- Single-flight operations return **409** while one is running.

### 6. Notify on meaningful transitions
```python
from backend.services.notifications_service import emit_event
emit_event("success" | "error" | "warning" | "info", title, message, source="<domain>")
```
Copy the `_notify_status_change()` pattern in `backend/routers/downloads.py` (~line 1422).
`emit_event` never raises and must never break the operation.

### 7. Mount it
In `backend/main.py`, import the module and add:
`app.include_router(<name>_router, prefix="/api/<domain>", tags=["<Domain>"])`.
Public endpoints go into the exact-path `public_paths` set only.

## Verification
Run `verify-changes`, then probe the new route on an isolated instance: no token → **401**, `user` token on an admin route → **403**, `?path=/etc/x` on a scoped route → **403**, failure visible in `GET /api/logs?limit=20` (or `<DATA_DIR>/logs/events.jsonl`).

## Done when
- Auth level is explicit and correct; no `startswith` public prefixes added.
- Failures are logged (no `except Exception: pass`), contract uses `detail`.
- State uses `json_store`; growth is bounded.
- Import/smoke checks pass; a feature or smoke test covers the new route.
