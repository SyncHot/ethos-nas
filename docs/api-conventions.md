# API conventions

The contract every endpoint follows. Status: 2026-11-11 (TODO.md P6-07). Enforced incrementally — P1-09
(response models) and P1-10 (error contract) are the code-side items.

## Base

- All JSON under `/api/<domain>`, one router per domain, mounted in `backend/main.py`.
- `Content-Type: application/json`. File uploads use `multipart/form-data`; chunked uploads use
  `upload_id` + `chunk_index` + `data`.
- Timestamps are ISO-8601 UTC strings (`...Z`) in stores; epoch seconds in metrics payloads.

## Authentication

```
Authorization: Bearer <jwt>
```

Accepted alternatives (same token): cookie, `?token=` query (for `<img>`, `<video>`, downloads and
WebSockets that cannot set headers). Never put a token in a URL that is logged where avoidable.

| Level | Rule |
|---|---|
| Public | exact paths only: `/api/auth/login`, `/api/app/version`, `/api/packages/desktop-icons`, `/docs`, `/openapi.json`, `/favicon.ico` |
| User | valid JWT; file paths further limited to `/home`, `/mnt`, `/media` |
| Admin | `require_admin` → 403 `{"detail": "Admin access required"}` |

## Success responses

| Kind | Shape |
|---|---|
| Read | the resource, or `{items: [...], total: N}` |
| Mutation | the updated resource, or `{ok: true, ...}` with identifiers |
| Long-running | `{op_id}` / `{upload_id}` — poll the matching `GET` |
| Delete | `{ok: true}` |

HTTP status: `200` read/update, `201` created, `202` accepted for queued work, `204` when there is
nothing to return.

## Error responses

Always non-2xx with `{"detail": "human readable reason"}`:

| Status | When |
|---|---|
| 400 | malformed request / invalid path |
| 401 | missing or expired token (client clears it and re-authenticates) |
| 403 | authenticated but not allowed (403 for path scope includes the allowed roots) |
| 404 | unknown id/path |
| 409 | conflict (running operation, container transitional state, existing file) |
| 422 | request body failed validation (FastAPI default) |
| 500 | unexpected failure — logged with `exc_info=True` |
| 502/504 | upstream (TMDB, debrid, docker) failed or timed out |

Never return `200` with `{"ok": false}` or `{"error": ...}` to report a *failed operation* — that was the
legacy pattern P1-10 removed. `detail` is a string the client can show as-is, except on `422` where it is FastAPI's list of field
errors (`[{loc, msg, type}]` — `readErrorDetail()` in `core/api.js` stringifies it). Structured payloads (a
merge-conflict state, a hunk list) ride along as extra keys, never instead of `detail`.

### Negative results are not errors

Some endpoints *run a probe*; "the probe completed and the answer is no" is a successful API call and must
stay `200`, otherwise the client cannot tell a broken NAS from a broken camera:

| Endpoint | Body |
|---|---|
| `POST /api/surveillance/cameras/{id}/test` | `{ok: true/false, detail: "Cannot reach camera: …"}` |
| `GET /api/health/status` | per-disk `smart.ok: false` + `error` (the snapshot itself is 200) |
| `GET /api/videos/tmdb/test` | `200 {ok: true, …}` / non-2xx when the API key is rejected |

Rule of thumb: if the *request* could not be processed → non-2xx + `detail`. If the request was processed
and the answer is negative → `200` with the endpoint's own boolean.

### Documented exceptions

| Router | Behaviour | Why |
|---|---|---|
| `editor.py` | `200 {ok: false, detail, …}` for git operations | the SPA needs the merge-conflict state / hunk list that came back with the failure; converting it is tracked with the editor UI work (P3-04) |
| `surveillance` camera test, `health` SMART | see above | probe semantics |

## Pagination

List endpoints that can grow accept `limit` and `offset` and always report `total` plus the echoed
`limit`/`offset`.

**`limit=0` means "no limit"** (the default) so the pre-existing single-shot callers keep receiving the full
list; the UI pages once a listing needs it. Paginated today: `GET /api/files` (directory listing),
`/api/files/trash`, `/api/downloads/tasks`, `/api/systemd`, `/api/containers`, `/api/music/library`
(`gallery/items`, `processes`, `editor/git/log`, `notifications` predate the convention and follow it).

The `files` listing is two-phase: `scandir` + sort, then `stat()` only for the requested slice — 100 000
entries answer a 50-row page in ~130 ms.

Deliberately not paged: `GET /api/notifications` (bounded ring) and
`GET /api/moments/timeline` (bounded by `per_month` samples per month).

## Filtering

- Single-value filters are exact matches (`?camera=Canon`, `?status=error`).
- Search text always uses `q`.
- Never merge semantic filters into `q` (the photo facets depend on `camera` staying separate).

## WebSockets

```
ws(s)://host/ws/<name>?token=<jwt>
```

- Reject with close code **4001** when the token is missing/invalid, **4003** when the role is not allowed,
  **1013** on capacity, **4002** for an invalid target.
- Messages are JSON objects (`{"type": ..., ...}`); the terminal is the only binary stream.
- Clients must reconnect with backoff and stop on 4001/4003 (see `core/ws.js`).
- Logs use one socket (`/ws/logs?source=app|container:…|file:…|journal`) with `entry` / `line` / `end` /
  `truncated` / `error` frames; the legacy `/ws/logs/system` and `/ws/logs/{container}` paths stay for
  one release (`docs/logs.md`).

## Logs ingest

`POST /api/logs/ingest` is the only write path for browser and package diagnostics and is allowed for
every authenticated user (the store is read only by admins). A `package_id` is verified server-side and
stored as `app="pkg:<id>"`; the client cannot choose `app`. Batches are capped at 50 and rate-limited
per user (`ETHOS_LOG_INGEST_PER_MIN`). Response: `{"ok": true, "accepted": N, "dropped": N}`.

## Idempotency and long operations

- Copy/move/delete return an `op_id` immediately; progress is polled at `/api/files/ops/{op_id}`.
- Backups and restores are single-flight: a second request returns 409 while one is running.
- Background work must go through `services/task_registry.spawn` so shutdown can cancel it.

## Compatibility notes

- The Android apps (Moments/Music/Video) use the same endpoints; breaking changes need a version bump in
  the app repos (AGENTS.md parity rule).
- `/api/downloads/qb` mimics the qBittorrent Web API for Sonarr/Radarr and is exempt from the JWT layer;
  each handler authenticates with its own SID cookie or API key.
