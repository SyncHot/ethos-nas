---
description: "Frontend rules for EthOS NAS ES modules: escaping, core helpers, app registration, no new globals."
applyTo: "frontend/static/**/*.js"
---

# Frontend rules

## Escaping (XSS)
- API/user strings in HTML: `escapeHtml`; attributes: `escapeAttr`; inside inline JS: `jsAttr` /
  `safeJsStr` (all from `core/utils.js`). Prefer `textContent` for plain text.
- Never interpolate API data into `onclick="..."` without `jsAttr`.
- `tests/test_frontend_escaping.py` guards regressions.

## Core modules (do not re-implement)
- API: `apiFetch` / `apiJsonFetch` from `core/api.js` (Bearer + 401 handling).
- Errors: show `err.detail || err.message` (e.g. the 403 path-scope explanation).
- WebSocket: `connectWs` from `core/ws.js`; never raw `new WebSocket`. Pass `appId` so it closes with
  the window and `stopOnCodes` for permanent failures.
- Windows: `openApp`, `onAppClose` from `core/window-manager.js`.
- Notifications: `notify({kind, title, message, action})` from `core/notification-center.js`
  (legacy `addNotification(msg, type)` still routes there). Persistent events come from the backend
  `emit_event` and render in the same module (**no second history UI, no own toast/panel/badge**).
  Full contract: `docs/notifications.md`; guard: `tests/test_notification_ui.py`.

## Logging
- `core/logger.js` is the only reporter: `logInfo/logWarn/logError(message, detail)` batches into
  `POST /api/logs/ingest`. Uncaught errors, rejections and `console.warn/error` are captured; do not
  add another buffer, endpoint or history list.
- Reading/streaming logs: `fetchLogs()` / `streamLogs()` from `core/log-stream.js`. No app may build
  `/api/logs` URLs or open `/ws/logs` directly.
- To show logs for an app or package, call `openLogs({ app: 'pkg:<id>' })` from `apps/logs.js` — do
  not render a local log panel. One viewer, one source tree, one filter bar.
- Full contract: `docs/logs.md`; rule: `.github/instructions/logging.instructions.md`;
  guard: `tests/test_logging_ui.py`.

## Registration (new app)
1. `static/apps/<app>.js` exports the renderer.
2. `static/app.js` — import + add to `RENDERERS`.
3. `static/core/registry.js` — `APP_REGISTRY.<id>`.
4. `static/core/desktop-layout.js` — `DEFAULT_ORDER` (must match `APP_REGISTRY_KEYS` in
   `backend/routers/packages.py`).

## Window layout (mandatory)
Every app window is built with `mountShell()` from `core/ui-shell.js` and the
`.ui-*` classes in `frontend/static/css/ethos-ui.css` — the same shell as the
native package windows. No `<style>` injection, no hardcoded colours, no
`.glass-card`/`.app-tab` in apps. Spec `docs/app-ui.md`; rule
`.github/instructions/app-ui.instructions.md`; skill `app-ui`; guard
`tests/test_app_ui.py`.

## Conventions
- No build step. Plain ES modules only.
- No new `window.*` globals; export/import instead.
- Use CSS custom properties (`--surface-1`, `--text-1`, `--border`) for theming.
- Add `data-testid` to interactive elements used by Playwright tests.

## Progress UI (long-running actions)
- **One component**: every long-running action — uploads, copy/move, delete,
  trash/purge, package installs, engine installs, backups — reports through
  `core/progress.js`:
  `startTask({ appId, title, … })` → `task.update({ percent, detail, log })` →
  `task.finish('done' | 'failed' | 'cancelled', { detail })`.
- Default host is the shared floating panel (`#taskProgressPanel`). Pass
  `host: <element>` **only** when the progress must stay inside the app window
  (Packages banner, Docker box) — markup, CSS and a11y are identical.
- Never hand-roll a progress card/panel, a `position:fixed` progress box, or
  `role="progressbar"` markup in an app. `tests/test_progress_ui.py` fails the
  build on regressions.
- Behaviour requirements:
  - `percent: null` while running renders an indeterminate bar.
  - Finished rows persist until the user clears (✓) or closes (×) the panel —
    never auto-dismiss failed or completed work.
  - Failures carry the server `detail` (`readErrorDetail`), and repeatable
    actions pass `retry` so the row offers a Retry button.
  - One summary toast per action (`addNotification`), not one per item.
- Give every task a stable `testIdPrefix` (`fm-op`, `upload`, `pkg-job`,
  `docker-engine`, …) — UI tests select `[data-testid="<prefix>-progress"]`,
  `-percent`, `-title`, `-step`, `-error`, `-retry`.
