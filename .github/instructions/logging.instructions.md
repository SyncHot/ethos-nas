---
description: "Mandatory unified logging rules: one store, one API, one WebSocket, one viewer — apps and packages use core/logger.js, core/log-stream.js and ctx.log, never their own log panel or store."
applyTo: "backend/**/*.py, frontend/static/**/*.js"
---

# Logging — one global system, no exceptions

Spec: `docs/logs.md`. Guard: `tests/test_logging_ui.py` (the build fails on violations).

## Backend

```python
logger = logging.getLogger("ethos-nas.routers.files")

logger.info("Moved %d files", n)
logger.error("Copy failed: %s", exc, exc_info=True)
```

- Use the module logger (`ethos-nas.routers.<name>` / `ethos-nas.services.<name>`); the handler
  forwards INFO+ into `<DATA_DIR>/logs/events.jsonl` and journald.
- Failure paths: `exc_info=True`. No `print(`. No bare `except: pass` without a comment.
- Persist through `backend/services/log_service.py` only. Never open your own log file, ring, or
  JSONL store, and never write `app_events.jsonl` (legacy file, imported once).
- Never log tokens, passwords, API keys or credential-bearing request bodies — the store redacts
  the common shapes, but do not rely on it.

## Frontend apps

```js
import { logInfo, logWarn, logError } from '../core/logger.js';
import { fetchLogs, streamLogs } from '../core/log-stream.js';

logWarn('Preview failed', err.stack);
```

- Reading history: `fetchLogs()`; streaming: `streamLogs({ source, onEntry, onLine })`. These two
  helpers are the only place that may contain `'/api/logs'` or `'/ws/logs'`.
- Never build a second log viewer/panel/history. To show logs from an app or package,
  `openLogs({ app: 'pkg:<id>' })` (exported by `apps/logs.js`) or reuse the editor panel.
- `console.log` is not captured; use `logInfo()` when you need an INFO row. Uncaught errors,
  rejections and `console.warn/error` are captured automatically.
- `setLogContext({ app })` is called by the window manager; do not set it per app unless the window
  is chrome-less (e.g. the login overlay).

## Packages (native modules)

`ctx` provides the shared logger:

```js
ctx.log('info', 'Scan started', path);
ctx.logger.warn('Cache empty');
```

- The server verifies `package_id` and stores the row as `app="pkg:<id>"`; never send a fake id.
- Package backend logs stay on stdout/stderr (Docker). EthOS shows them in the Logs app grouped by
  the package; a package must not ship its own log viewer, history list or log file.
- Domain datasets (scan results, job history) are not logs and may stay package-local.
- Port/iframe package UIs run on another origin and are not captured — use `ctx.log` or stdout.

## Levels

`debug` (memory only) · `info` (state transitions) · `warning` (degraded, handled) · `error`
(request/job failed). Identical events within 30 s collapse into one row with `×N`.

## Escaping

Server strings rendered by the Logs app use `textContent`; any HTML path uses `escapeHtml` /
`escapeAttr` from `core/utils.js` (`tests/test_frontend_escaping.py` guards it).
