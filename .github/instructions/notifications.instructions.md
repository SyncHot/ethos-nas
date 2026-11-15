---
description: "Mandatory global Notification Center rules: any new app or package must use core/notification-center.js and emit_event — never a local toast, panel or event history."
applyTo: "frontend/static/**/*.js"
---

# Notifications — one global system, no exceptions

Full spec: `docs/notifications.md`. Guard: `tests/test_notification_ui.py` (the build fails on
violations).

## What you must call

```js
import { notify, addNotification } from '../core/notification-center.js';

notify({ kind: 'success', message: 'UFW enabled' });                 // new code
notify({ kind: 'error', title: 'Upload failed', message: detail,
         action: { label: 'Retry', appId: 'files' } });
addNotification(message, kind);                                      // legacy alias, still valid
```

- `kind` is one of `success` | `error` | `warning` | `info`.
- `action` opens a desktop app (`appId`) or a same-origin path (`href`).
- Notifications that must survive a reload are **backend** events: call
  `emit_event(kind, title, message, source="<domain>", action=…, dedupe_key=…)` on the server.
  The bell badge and the sheet update over `/ws/notifications`.
- Pass `dedupe_key` for repeating conditions so the sheet shows one row with `×N`.

## What you must never do

- Never create a toast/`div.notification`, a fixed notification panel, or a second
  event-history list in an app or package. There is exactly one implementation:
  `core/notification-center.js` + the markup in `frontend/index.html`.
- Never write `#notificationCount` — `_setBadge()` is the single writer; the value always
  comes from the server's `unread`.
- Never call `new Notification(...)` / `Notification.requestPermission()` outside the core module.
- Never use the desktop notification API or the bell for progress — long-running work belongs in
  `core/progress.js` (`docs/progress-ui.md`) with **one** summary notification on completion.
- Never add emoji icons to notification UI; use `uiIcon()` from `core/icons.js`.

## Packages (native modules)

`ctx.notifications` is provided by `app.js`:

```js
ctx.notifications.toast(message, kind);        // transient
ctx.notifications.emit({ kind, title, message, action });   // persisted (source = pkg:<id>)
ctx.notifications.open('unread');              // focus the sheet
ctx.notify(message, kind);                     // legacy: toast only
```

A package never ships its own notification component, history list or badge. `source` is derived
server-side from `package_id`; do not try to impersonate another subsystem.

## Escaping

Server strings reaching `innerHTML` go through `escapeHtml`; attributes through `escapeAttr`
(from `core/utils.js`). `tests/test_frontend_escaping.py` guards this too.
