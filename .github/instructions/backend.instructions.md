---
description: "Backend rules for EthOS NAS Python: ethos logging, admin authz, path scope, json_store, subprocess safety."
applyTo: "backend/**/*.py"
---

# Backend rules

## Logging
- Child logger in every module: `logger = logging.getLogger("ethos-nas.routers.<name>")` or
  `"ethos-nas.services.<name>"`.
- `error` = request failed; `warning` = degraded but handled; `info` = state transition.
- Exceptions: `logger.error("Failed to <op> (id=%s)", item_id, exc_info=True)`.
- Never `except Exception: pass` without a comment explaining why it is harmless
  (`scripts/check_logging.py` enforces this).
- Never log passwords, tokens, API keys, or request bodies containing credentials.
- One store only: INFO+ goes to `<DATA_DIR>/logs/events.jsonl` through
  `services/log_service.py` (rotated, 5 × 1 MiB). Never write your own log file or JSONL; the
  legacy `app_events.jsonl` is imported once and archived. Full contract: `docs/logs.md`; rule:
  `.github/instructions/logging.instructions.md`; guard: `tests/test_logging_ui.py`.

## Auth and paths
- Privileged routers: `APIRouter(dependencies=[Depends(require_admin)])`; role subsets:
  `Depends(require_role(...))` from `backend.routers.auth`.
- Filesystem paths: mount `Depends(scope_request)` and call `path_scope.sanitize(path)` on every path
  (sources and destinations). `scope_request` must remain `async def`.
- Read-only endpoints may use `path_scope.sanitize_read(path)` to honour received user grants.
  Never use it on a write: `sanitize()` must stay share-unaware. Grant creation uses
  `sanitize_share()` (`/home`, `/mnt`, `/media` only, never DATA_DIR). Spec: `docs/sharing.md`.
- Never add a public prefix; public paths are the exact-path set in `backend/main.py`.

## State
- `_STORE = json_store.get(settings.DATA_DIR / "<name>.json", dict, version=1)`
- `.load()` / `.save()` / `.update(mutator)`; `update()` for read-modify-write.
- Version + `migrate=` when the shape changes; cap growing lists.

## Notifications
- Every state-changing subsystem that finishes or fails publishes one event:
  `emit_event("success"|"error"|"warning"|"info", title, message, source="<domain>",
  action={...}, dedupe_key="...")` from `services/notifications_service.py`.
- Use `dedupe_key` for repeating conditions (hot disk, flapping unit) — it collapses into one row.
- `source` is the domain (`files`, `downloads`, `health`, …) or `pkg:<id>`; never invent a new
  notification store, history endpoint or WebSocket. Contract: `docs/notifications.md`.

## Process safety
- `subprocess` with argument lists only — never `shell=True`.
- docker-py is synchronous: wrap calls in `asyncio.to_thread()`.
- Background work goes through `services.task_registry.spawn`.

## Errors
- `HTTPException(status_code=..., detail="human readable")`; never `200 {"ok": false}` for a failure.
- 400 input · 401 token · 403 forbidden · 404 unknown · 409 conflict · 422 validation · 500 unexpected.
