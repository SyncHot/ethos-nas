---
name: frontend-app
description: "Add or modify a desktop app in frontend/static/apps/. Use for new app windows, icon/registry wiring, API/WS calls, escaping/XSS fixes, and Android parity."
argument-hint: "Which app or screen are you adding/changing?"
---

# Frontend desktop app

## Architecture in one screen
- No framework, no build step: vanilla ES modules loaded by `frontend/static/app.js`.
- Each app exports a renderer (`render<App>Page` / `open<App>` / `init<App>`).
- `app.js` holds imports and the `RENDERERS` map (~line 82). App code does **not** go in `app.js`.
- Windows/taskbar: `core/window-manager.js`. Desktop icons: `core/registry.js`.

## Procedure

### 1. Create the module
`frontend/static/apps/<app>.js`:
```js
import { mountShell } from '../core/ui-shell.js';
import { apiRequest } from '../core/api.js';
import { notify } from '../core/notification-center.js';
import { escapeHtml } from '../core/utils.js';

export function render<App>Page(container) {
    const shell = mountShell(container, {
        idPrefix: '<app>', title: '<App>', icon: '🧩',
        sections: [{ id: 'main', label: 'Overview' }], rail: false,
    });
    // render into shell.view, fetch, wire events
}
```
Use the shared shell (`docs/app-ui.md`, skill `app-ui`) for every app window;
Files/Terminal/Login are the only layout exceptions.

### 2. Register the app (all required)
1. `frontend/static/app.js` — import the renderer and add it to `RENDERERS`:
   `<id>: render<App>Page`.
2. `frontend/static/core/registry.js` — add `APP_REGISTRY.<id> = { id, title, icon }`.
3. `frontend/static/core/desktop-layout.js` — add `<id>` to `DEFAULT_ORDER` (must match
   `APP_REGISTRY_KEYS` in `backend/routers/packages.py`).
4. `frontend/static/core/app-icons.js` — icon/gradient if it has a custom one.

### 3. Use the core modules (do not re-implement)
| Need | Use |
|---|---|
| API calls | `apiJsonFetch` / `apiFetch` from `core/api.js` (adds Bearer, handles 401 reload) |
| Errors | show `err.detail \|\| err.message` (e.g. the 403 path-scope explanation) |
| WebSocket | `connectWs(...)` from `core/ws.js` — never raw `new WebSocket` |
| Open another app | `openApp(appId)` from `core/window-manager.js` |
| Cleanup on close | `onAppClose(appId, fn)` / `appId` option on `connectWs` |
| Toasts | `notify({ kind, title, message, action })` from `core/notification-center.js` (legacy `addNotification(msg, type)`) |
| System events | backend `emit_event` → the same module's sheet + bell badge (never build a second history UI) |

### 3b. Notifications (mandatory)
All user feedback goes through the global Notification Center — see `docs/notifications.md` and
`.github/instructions/notifications.instructions.md`.

```js
import { notify } from '../core/notification-center.js';
notify({ kind: 'success', message: 'Share created' });
notify({ kind: 'error', title: 'Copy failed', message: detail, action: { label: 'Retry', appId: 'files' } });
```

- Persistent events belong to the backend: `emit_event(..., source="<domain>")`; the badge and sheet
  update live over `/ws/notifications`.
- **Never** ship a local toast, a fixed notification panel, an unread badge or a second event list.
  `tests/test_notification_ui.py` fails the build.
- Long-running work shows progress via `core/progress.js`, then emits **one** summary notification.

### 4. Escaping and XSS
- Every API/user string interpolated into HTML goes through `escapeHtml`; attributes through
  `escapeAttr`; values inside inline JS handlers through `jsAttr`/`safeJsStr` (`core/utils.js`).
- Prefer `textContent` over `innerHTML` for plain text. Never interpolate API data into
  `onclick="..."` without `jsAttr`.
- `tests/test_frontend_escaping.py` will catch regressions.

### 5. States and UX
- Loading, empty, error, and `403` states; surface `err.detail` verbatim.
- CSS custom properties (`--surface-1`, `--text-1`, `--border`); `data-testid` on interactive elements.
- No new `window.*` globals (301 already exist) — export/import instead.

### 6. Web ⇄ Android parity
If the feature exists in `ethos-music`, `ethos-video`, or `ethos-moments`, mirror the same screens,
actions, and data in this web app in the same task. Report the parity check.

## Verification
`node --check frontend/static/apps/<app>.js`, then `tests/test_frontend_escaping.py` (+ `test_a11y.py` / `test_ui_states.py` if applicable). Open it on an isolated instance: icon appears, window opens, close cleans up, console clean. Run `verify-changes` before commit.

## Done when
Renderer is wired in all four registration points, core modules are reused, all dynamic text is
escaped, states are handled, parity is checked, and syntax/tests pass.
