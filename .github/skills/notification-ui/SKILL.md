---
name: notification-ui
description: "Add or change a notification/toast: use the global Notification Center (core/notification-center.js + emit_event). Covers the API, dedupe, actions, WS push, package ctx contract, styling tokens and the guard test."
argument-hint: "What should be reported, to whom, and does it need to persist?"
---

# Notification UI (global system)

One system for the whole product. Spec: `docs/notifications.md`.
Rules: `.github/instructions/notifications.instructions.md`.
Guard: `tests/test_notification_ui.py`.

## Pick the right surface

| Need | Use | Persists? |
| --- | --- | --- |
| Feedback for an action the user just took | `notify({ kind, message })` | no (toast) |
| Something the system did (job finished, disk hot, package installed) | backend `emit_event(...)` | yes (sheet + badge) |
| Long-running work | `core/progress.js` row + **one** summary notification at the end | progress row persists |

Never render a toast, panel, badge or event history yourself — one implementation only.

## Frontend

```js
import { notify, dismissNotification } from '../core/notification-center.js';

notify({ kind: 'success', message: 'UFW enabled' });
notify({ kind: 'error', title: 'Upload failed', message: err.detail || err.message,
         action: { label: 'Retry', appId: 'files' },
         onDismiss: () => revokePreview() });
```

- `kind`: `success` | `error` | `warning` | `info`.
- Errors are sticky; success/info auto-dismiss; hover or focus pauses the timer.
- Identical toasts inside 2.5 s collapse into one card with `×N`; at most 3 cards show, the rest queue
  behind `+N more`.
- Legacy `addNotification(msg, type)` and `showToast(...)` still work — do not add new call sites.

## Backend

```python
from backend.services.notifications_service import emit_event

emit_event("warning", "Disk running hot", f"{dev} is at {temp} °C",
           source="health", dedupe_key=f"disk-hot:{dev}",
           action={"label": "Open Storage", "appId": "storage"})
```

- `source` = the domain (`files`, `downloads`, `health`, `backup`, …) or `pkg:<id>`.
- `dedupe_key` collapses repeats within 300 s into one row with `count`.
- `action` is validated server-side (`appId` pattern, same-origin `href`).
- Never raise from the call site — `emit_event` already swallows failures.

## Packages

`ctx.notifications.toast(msg, kind)` / `.emit({kind, title, message, action})` / `.open(filter)`.
`emit()` is stored with `source="pkg:<id>"` (derived server-side from `package_id`).
`ctx.notify` stays as a toast-only legacy alias.

## Styling

Use `--notif-success/-error/-warning/-info`, `--notif-*-tint`, `--notif-sheet-w`,
`--notif-radius`, `--notif-shadow` from `frontend/index.html`. Both themes must stay readable —
no hardcoded colours, no white text on a tint, no emoji icons (`uiIcon()` instead).

Stable anchors (tests select them): `#notificationCount`, `#toastContainer`,
`#notificationDrawer`, `#notificationDrawerList`, `data-testid="notification-trigger"`,
`notification-event-<id>`, `notification-mark-all-read`, `notification-clear-all`,
`notification-drawer-close`.

## WebSocket

`/ws/notifications?token=` → `{"type":"event"|"read"|"read-all"|"delete"|"cleared"|"sync", ...}`.
The client is `connectNotificationStream()` in the core module; do not open a second socket and do
not poll `/unread-count` from an app.

## Verify

```bash
.venv/bin/python tests/test_notification_ui.py
.venv/bin/python tests/test_notifications_ws.py
node --check frontend/static/core/notification-center.js
```

## Done when

The surface matches the table, no local notification UI exists, strings are escaped, both themes
render, the guard test passes, and any new backend source is documented in `docs/notifications.md`.
