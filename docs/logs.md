# Logs — one system for EthOS and packages

Every diagnostic message in EthOS goes through **one store, one HTTP API, one WebSocket, one
viewer**: `backend/services/log_service.py` → `backend/routers/logs.py` + `logs_ws.py` →
`frontend/static/core/logger.js` / `log-stream.js` → `frontend/static/apps/logs.js`.

Rule file: `.github/instructions/logging.instructions.md`. Guard: `tests/test_logging_ui.py`.
Package rule: `ethos-packages/AGENTS.md` → Logging.

Verified against the code on 2026-11-14.

## Envelope

```json
{
  "id": "log_ab12cd34ef56",
  "ts": 1763123456.123,
  "level": "debug | info | warning | error",
  "app": "core | browser | pkg:<id> | container:<name> | system",
  "source": "ethos-nas.routers.files | browser:files | pkg:domains | container:ethos-domains-caddy-1 | file:/var/log/syslog",
  "message": "…",
  "detail": "traceback / stack / extra context",
  "count": 1
}
```

- `message` ≤ 500 chars, `detail` ≤ 4000, `source` ≤ 120. `count` grows while the same
  `(level, app, source, message)` repeats inside the dedupe window (30 s).
- `app` is **derived server-side**. Clients never set it: a browser event is `browser`, a package
  event is `pkg:<id>` after the package id is verified, in-process Python records are `core`.
- Credentials are redacted before storage: `token=…`, `Authorization: Bearer …`, `password=…`,
  `api_key=…`, raw `ethos_<48 hex>` API keys.

## Store

| | |
|---|---|
| In-memory ring | 5000 entries (`ETHOS_LOG_RING_SIZE`) |
| Disk | `<DATA_DIR>/logs/events.jsonl`, appended |
| Rotation | 1 MiB per file, 5 generations (`ETHOS_LOG_MAX_BYTES`, `ETHOS_LOG_FILES`) ≈ 5 MiB |
| Persisted levels | `info`, `warning`, `error` (plus `debug` in memory only) |
| Legacy | old `app_events.jsonl` is imported once, then renamed `logs/legacy-app-events.jsonl` |

`log_service.record()` never raises — logging must not break the subsystem reporting it.
Backend handlers attach to `ethos-nas` (INFO+), `uvicorn*` (INFO+) and the root logger (WARNING+,
skipping names already captured).

## Backend usage

```python
logger = logging.getLogger("ethos-nas.routers.files")

logger.info("Moved %d files", n)                    # INFO+ → store + journald
logger.error("Copy failed: %s", exc, exc_info=True)  # failure path: always exc_info
```

Module naming is enforced by `tests/test_logging_convention.py`.

## HTTP API (mounted at `/api/logs`)

| Method / path | Auth | Purpose |
|---|---|---|
| `GET /api/logs` | admin | list, filters: `limit` (≤1000), `level`, `app`, `source`, `search`, `since` |
| `GET /api/logs/apps` | admin | source tree: `[{app, count, last_ts}]` |
| `GET /api/logs/stats` | admin | level counts, ring size, disk bytes, dropped count |
| `POST /api/logs/ingest` | any user | 1 event or an array (≤50) from the browser or a native package |
| `DELETE /api/logs` | admin | clear the ring + active file (rotated history stays) |
| `GET /api/logs/files` | admin | readable files under `/var/log` |
| `GET /api/logs/tail` | admin | last `lines` of a file (plain or `.gz`) |
| `GET /api/logs/journal` | admin | `journalctl -n <lines> [-u <unit>]` |
| `GET /api/logs/containers` | admin | containers grouped by compose project (= package id) |
| `GET /api/logs/app`, `POST /api/logs/client`, `DELETE /api/logs/app` | — | legacy aliases, kept for old clients |

Ingest body: `{level, message, detail, source, package_id?}`. With `package_id` the server verifies
the package is installed and sets `app="pkg:<id>"`, `source` defaults to `pkg-ui`; without it the
event is stored as `browser`. A per-user rate limit (120 events/min) drops excess events and counts
them; the response is always `{"ok": true, "accepted": N, "dropped": N}`.

## WebSocket `/ws/logs`

One socket for every source. JSON frames only (`docs/api-conventions.md`):

```
/ws/logs?source=app
/ws/logs?source=container:<name>&tail=200
/ws/logs?source=file:/var/log/syslog&tail=200
/ws/logs?source=journal&unit=ethos-nas&tail=200
```

| Frame | Meaning |
|---|---|
| `{"type":"entry","entry":{…}}` | new app event (live) |
| `{"type":"line","line":"…","source":"…"}` | one file/container/journal line |
| `{"type":"end","detail":"…"}` | stream finished (container stopped, file rotated) |
| `{"type":"truncated","sent":N}` | per-connection cap reached (5000 lines) |
| `{"type":"error","detail":"…"}` | source-level failure |

Close codes: `4001` missing/invalid token (`core/ws.js` stops retrying), `4003` the account is not an
admin. Legacy paths `/ws/logs/system` and `/ws/logs/{container}` are served by the same handler for
one release.

## Frontend

```js
import { log, logDebug, logInfo, logWarn, logError, setLogContext } from '../core/logger.js';

logInfo('Scan queued', `paths=${n}`);
setLogContext({ app: 'files' });           // window-manager does this on focus
```

- Batching: 2 s or 10 events, `pagehide` beacon, 200-entry offline queue, never throws.
- Captured automatically: uncaught errors, unhandled rejections, `console.warn`/`console.error`.
  `console.log` is **not** captured; use `logInfo()` explicitly.
- Threshold: `localStorage.ethosLogLevel` (`debug|info|warning|error`), default `warning`.
- Reading/streaming uses `core/log-stream.js` (`fetchLogs`, `streamLogs`) — the only module that
  knows the API and socket shape. Apps render data, they never open the socket themselves.

## Packages

Native package modules get logging through `ctx` (built in `app.js`):

```js
ctx.log('info', 'Scan started', path);     // → app="pkg:<id>", source="pkg-ui"
ctx.logger.warn('Cache empty');
```

- The server verifies `package_id`; a package cannot impersonate `core` or another package.
- Package **backend** logs are container stdout/stderr. The Logs app shows them under the package
  (containers grouped by compose project = package id); the package must not ship its own log viewer
  or log store.
- Domain records (scan history, job history) are datasets, not logs — they may stay in the package.
- A port/iframe package UI runs on another origin and cannot be captured by the reporter; use
  `ctx.log` in native UIs and container logs otherwise.

## What apps and packages must not do

- No second log store, file, ring buffer or history list outside `log_service.py`.
- No bespoke log panel/modal inside an app or package — open the Logs app
  (`openLogs({ app: 'pkg:<id>' })`) or use the editor's output panel.
- No direct `fetch('/api/logs…')` or raw `WebSocket('/ws/logs…')` in app code.
- No secrets in `message`/`detail`; no `print()` in backend code.
- No `logger.error(..., exc)` without `exc_info=True` on a failure path.

## Ops

```bash
journalctl -u ethos-nas -f                 # stderr mirror
tail -f <DATA_DIR>/logs/events.jsonl       # unified store
du -sh <DATA_DIR>/logs                     # capped at ~5 MiB + legacy file
```

`GET /api/logs/stats` reports ring/disk/dropped counters for the doctor check.
