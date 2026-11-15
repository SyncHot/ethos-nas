---
name: logging-ui
description: Add or change a log source, the Logs viewer or package logging. Covers core/logger.js, core/log-stream.js, log_service, /api/logs, /ws/logs, ctx.log, retention/rotation and the guard test.
---

# Logging — unified system

Spec: `docs/logs.md`. Rule: `.github/instructions/logging.instructions.md`.
Guard: `tests/test_logging_ui.py`. Backend store: `backend/services/log_service.py`.

## Where things live

| Concern | File |
|---|---|
| Store (ring 5000, rotation 1 MiB × 5, dedupe, redaction) | `backend/services/log_service.py` |
| HTTP API (`/api/logs`, `/apps`, `/stats`, `/ingest`, `/files`, `/tail`, `/journal`, `/containers`) | `backend/routers/logs.py` |
| WebSocket (`/ws/logs?source=…`, legacy `/ws/logs/system`, `/ws/logs/{container}`) | `backend/routers/logs_ws.py` |
| Browser reporter + queue | `frontend/static/core/logger.js`, `core/error-reporter.js` |
| Client for history + streaming | `frontend/static/core/log-stream.js` |
| Viewer | `frontend/static/apps/logs.js` |
| Package contract | `app.js` → `ctx.log`, `ctx.logger` |

## Adding a source

1. **Backend producer** — just log: `logger.info(...)` / `logger.error(..., exc_info=True)`.
   Do not open files, rings or JSONL stores.
2. **New fetchable source** (e.g. a device log): add a read-only admin endpoint in
   `routers/logs.py`, or stream it in `logs_ws.py` as a new `source=` value with JSON frames
   (`line` / `end` / `error` / `truncated`) and a 5000-line cap.
3. **Viewer entry** — add it to the rail in `apps/logs.js` (`_loadTree`) and to `_subscribe` /
   `_loadHistory`. Keep state per `body` (WeakMap), never module-level.
4. **Package source** — native UI: `ctx.log(level, message, detail)`; backend: stdout/stderr.
   Containers are grouped by `com.docker.compose.project` (= package id), so no manifest change.

## Invariants

- One envelope `{id, ts, level, app, source, message, detail, count}`; `app` is server-derived.
- Ingest: any authenticated user; list/clear/files/journal/containers/WS: **admin**.
- Redaction runs in the store; never log secrets anyway.
- Envelope caps: message 500, detail 4000, source 120, batch 50, rate 120/min/user.
- WebSocket frames are JSON only; `4001` no token, `4003` non-admin.
- Viewer: `textContent` for server strings, `escapeHtml`/`escapeAttr` for HTML, max 2000 rows,
  `data-testid` on controls.

## Verification

```bash
.venv/bin/python scripts/check_logging.py
.venv/bin/python tests/test_logging_ui.py
.venv/bin/python tests/test_logging_convention.py
.venv/bin/python tests/test_log_service.py
.venv/bin/python tests/test_logs_api.py
.venv/bin/python tests/test_logs_ws.py
node --check frontend/static/apps/logs.js
```

Isolated instance (`ETHOS_DATA_DIR=/tmp/...`, port 9002): user ingest → 200; user `GET /api/logs`
→ 403; admin sees the row; WS `source=file:/var/log/syslog` → 4003 for a user; package id that is
not installed → 403. Manual: all rail groups, level chips, search, live/pause, export, clear, and a
`ctx.log` row tagged `pkg:<id>`.
