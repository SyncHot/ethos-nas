# Notification Center — one global system for EthOS NAS and its packages

> Notifications are **events for the user** (bell, toast, sheet). Diagnostics are **logs** and live in
> a separate store and viewer — see `docs/logs.md`. Never write diagnostic output into
> `notifications.json`, and never turn a log row into a toast.

The web UI has **one** notification system. Core apps, native package modules and
backend subsystems all feed the same store, the same bell badge and the same
right-edge sheet. Nothing else may render a toast, a notification panel or an
event history — `tests/test_notification_ui.py` fails the build if anything tries.

- [x] Backend store v3 (`json_store`, dedupe, action buttons, package sources, targeted audiences)
- [x] `POST /api/notifications` with a source policy
- [x] `/ws/notifications` push (no more 30 s badge polling)
- [x] Top-bar bell + right-edge sheet, day grouping, filters, search, per-row actions
- [x] Toasts: dedupe `×N`, max 3 + `+N more`, hover pause, action button, working `onDismiss`
- [x] Sound + desktop notifications (opt-in, per browser)
- [x] `ctx.notifications` for native packages
- [x] Static guard + WS contract test + docs + rules

## Architecture

```
backend subsystems                app / package code
 emit_event(kind,title,msg,        notify({kind,title,message,action})
   source=…, action=…, dedupe_key=…)          │
        │                                      ▼
        │                            core/notification-center.js
        │                              ├── toasts      (#toastContainer, top-right)
        ├── notifications.json ───────┤── sheet       (#notificationDrawer, right edge)
        │   (version 2, ≤500 events)  ├── badge       (#notificationCount, one writer)
        │                             └── prefs       (sound / desktop, localStorage)
        └── /ws/notifications ─────────► live frames: event | read | read-all | delete | cleared | sync
              (REST /api/notifications is the fallback + history source)
```

Two different things share one visual language:

| Surface | Meaning | Lifetime |
| --- | --- | --- |
| **Toast** | “you just did something” — client-side feedback for an action in the current tab | until dismissed / timeout (success 4 s, info 5 s, warning 7 s, error sticky) |
| **Sheet** | “the system did something” — persisted history, survives reloads and tabs | until the user marks read, deletes or clears |

Backend events never spawn toasts: a toast is the echo of a local action. A new
backend event updates the badge, the sheet, and (if enabled) the sound cue or
desktop notification. This removes the duplicate-toast problem that would
otherwise appear when an action both toasts locally and emits server-side.

## Files

| Path | Role |
| --- | --- |
| `frontend/static/core/notification-center.js` | the only implementation: toasts, sheet, badge, WS client, prefs |
| `frontend/index.html` | bell button (top bar), sheet markup, `--notif-*` tokens + classes |
| `frontend/static/app.js` | bootstraps the stream, exposes `ctx.notifications` to native packages |
| `backend/services/notifications_service.py` | store v2, `emit_event`, listeners |
| `backend/routers/notifications.py` | REST API (list/create/read/delete) |
| `backend/routers/notifications_ws.py` | `/ws/notifications` + service→WS bridge |
| `tests/test_notification_ui.py` | static guard (single implementation, a11y, docs) |
| `tests/test_notifications_ws.py` | end-to-end: migration, REST policy, WS frames |
| `tests/test_notifications.py` | API + Playwright (bell, sheet, badge) |

## Data model

`<DATA_DIR>/notifications.json` — `{"version": 3, "events": [...]}` via
`services/json_store.py` (atomic writes, fsync, migration backups, corruption
quarantine). Capped at `MAX_EVENTS = 500`; oldest rows are trimmed first.

```json
{
  "id": "evt_1a2b3c4d5e6f",
  "kind": "success",
  "title": "Download completed",
  "message": "ubuntu-24.04.iso",
  "source": "downloads",
  "created_at": "2026-11-14T12:34:56+00:00",
  "users": [],
  "read": false,
  "read_by": [],
  "action": { "label": "Open Downloads", "appId": "downloads" },
  "dedupe_key": null,
  "count": 1
}
```

- `kind` — `success` | `error` | `warning` | `info` (anything else becomes `info`).
- `users` — audience; empty means a broadcast. Targeted events are only listed,
  counted and pushed to the named accounts.
- `read` — legacy global flag used by broadcasts; `read_by` holds per-user read state.
  A targeted event is never marked globally read.
- `action` — optional button: `appId` (opens a desktop app) and/or same-origin
  `href`. Labels are capped at 40 chars, `appId` must match `^[a-z0-9][a-z0-9_.-]{0,59}$`,
  `href` must start with `/`.
- `dedupe_key` — repeats from the same source within `DEDUPE_WINDOW_SECONDS = 300`
  bump `count` and `created_at` instead of adding a row (hot disk, flapping service).
- `source` — `system`, `files`, `storage`, `downloads`, `docker`, `packages`,
  `backup`, `shares`, `share-links`, `sharing`, `firewall`, `health`, `surveillance`, or
  `pkg:<package-id>` for package-originated events.

## REST API

All routes require a JWT (the global middleware); nothing here is public.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/notifications?limit=&kind=` | `{events, unread}`; `limit` 1–500, `kind` filters one severity |
| POST | `/api/notifications` | create; see policy below |
| GET | `/api/notifications/unread-count` | `{unread}` — fallback when the socket is down |
| POST | `/api/notifications/read-all` | `{ok, marked}` |
| POST | `/api/notifications/{id}/read` | `{ok}` / 404 |
| DELETE | `/api/notifications/{id}` | `{ok}` / 404 |
| DELETE | `/api/notifications` | `{ok, cleared}` |

`POST` body: `{kind, title, message, source?, package_id?, action?, dedupe_key?}`.

**Source policy** — a package cannot impersonate another subsystem:

- with `package_id`: the package must be installed; `source` is forced to
  `pkg:<package_id>` (a supplied `source` is ignored); available to any user.
- without `package_id`: admin only, and `source` defaults to `system`.

## WebSocket

`/ws/notifications?token=<jwt>` — every authenticated user may subscribe. The hub tracks
the username from the token and drops `event` frames whose `users` audience does not include
that account; `unread` in every frame is computed per connection. Missing/invalid token closes
with **4001**; the client stops reconnecting on that code (handled by `core/ws.js`).

Frames (`{"type": ...}` envelope):

```json
{"type": "sync",    "unread": 3}
{"type": "event",   "event": {...}, "unread": 4}
{"type": "read",    "event": {"id": "evt_…"}, "unread": 3}
{"type": "read-all","unread": 0}
{"type": "delete",  "event": {"id": "evt_…"}, "unread": 2}
{"type": "cleared", "unread": 0}
```

`event` is the full v3 record, so a client can render it without a refetch.
`unread` is authoritative and per-user — the badge is always set to it, never incremented.

The socket is global: it is opened by `startNotificationPolling()` and has no
`appId`, so closing a window never kills it. A 60 s REST poll runs as a safety
net and is skipped while the socket is open.

## Frontend API

```js
import { notify, addNotification } from '../core/notification-center.js';

// Canonical
notify({ kind: 'success', message: 'UFW enabled' });
notify({ kind: 'error', title: 'Upload failed', message: 'rules.pdf — permission denied',
         action: { label: 'Retry', appId: 'files' } });

// Legacy (still supported — 350 call sites, both argument orders)
addNotification('UFW enabled', 'success');
addNotification('success', 'Container started');   // reversed order tolerated
```

Options: `kind`/`type`, `title`, `message`, `action`, `autoDismissMs`,
`noAutoDismiss` (alias `persistent`), `onDismiss`.

Other exports: `dismissNotification(id)`, `openNotifications({filter})`,
`closeNotifications()`, `toggleNotificationDrawer()`, `markAllRead()`,
`clearAll()`, `getUnreadCount()`, `requestNotificationPermission()` (the only
permission-prompt helper apps and PWAs may call), `pollUnreadCount()`,
`startNotificationPolling()`, `connectNotificationStream()`.
`showToast` / `showUnifiedToast` are deprecated aliases of the same pipeline.

### Native packages

`app.js` builds `ctx` for `ui.mode: native` modules:

```js
render(container, ctx) {
  ctx.notifications.toast('Saved', 'success');           // transient
  ctx.notifications.emit({                              // persisted, appears in the sheet
    kind: 'info', title: 'Scan finished', message: '0 threats found',
    action: { label: 'Open', appId: 'pkg-antivirus' },
  });
  ctx.notifications.open('unread');                     // focus the sheet
  // ctx.notify(message, kind) is the legacy toast-only alias
}
```

`emit()` posts `{...note, package_id: pkg.id}`; the server derives
`source="pkg:<id>"`, so the sheet shows the package name as the source chip.

Port-based (`ui.mode: window`) packages run in an iframe on their own origin and
cannot reach this endpoint — their notifications must be emitted by their
backend (if it runs in EthOS) or by the shell on their behalf. Do **not** add a
second notification UI inside the iframe.

## Preferences

The ⚙ button in the sheet header toggles two per-browser options stored in
`localStorage` under `ethos.notif.settings`:

- **Sound** — a short two-tone WebAudio cue for `warning`/`error` backend events
  (default off; enabling it plays a sample).
- **Desktop** — native `Notification` while the tab is in the background
  (default off; requests permission on enable through
  `requestNotificationPermission()`). Clicking it focuses the tab and opens the sheet. No
  service worker required, so it only fires while a tab is open.

## Web Push (package reminders)

The Notification Center is the in-app history. Web Push is a separate delivery channel used by
the Sticky Notes package for due reminders — it does **not** write to `notifications.json` by
itself:

- The service worker (`frontend/sw.js`) handles `push` (shows the reminder) and
  `notificationclick` (focuses an open `/notes` window or opens one at `/notes?note=<id>`).
- Permission is requested only through `requestNotificationPermission()` from this module. No
  app, package or PWA calls `Notification.requestPermission()` directly (guard:
  `tests/test_notification_ui.py`).
- Push subscriptions are owned by the package service (`push.json`, VAPID), not by the Notification
  Center. A device without a subscription falls back to the package's `/reminders/due` endpoint
  while the page is visible.
- If a reminder should also appear in the sheet, the page emits it through the package API
  (`package_id`) — that is a normal persisted event, not a second toast/history UI.
- Specs: `docs/sticky-notes.md` (package) and `docs/notes-pwa.md` (mobile surface).

## Styling contract

Tokens are defined for both themes in `frontend/index.html`:

```
--notif-success / -error / -warning / -info          accent per kind
--notif-{kind}-tint                                  row/card background tint
--notif-sheet-w: clamp(320px, 92vw, 400px)
--notif-radius, --notif-shadow
```

Rules:

- Use the tokens; never hardcode a colour or white text on a tint.
- The sheet spans `top: var(--topbar-h)` → `bottom: var(--taskbar-h)`; the
  progress panel owns the bottom-right corner (`#taskProgressPanel`, z 9989),
  the sheet owns the right edge (z 9990), toasts float above both (z 9999).
- Keep `.notification`, `.notification-<kind>`, `.notification-icon`,
  `.notification-msg` and the ids `#notificationCount`, `#toastContainer`,
  `#notificationDrawer`, `#notificationDrawerList`, `data-testid="notification-trigger"`
  stable — tests select them.

## Rules (enforced by `tests/test_notification_ui.py`)

1. New UI feedback → `notify(...)`; never a local `div` toast.
2. Persistent events → backend `emit_event(...)`; **no second history UI**.
3. Never write `#notificationCount` — `_setBadge()` is the only writer.
4. Never call the desktop `Notification` API outside the core module; apps/PWAs that need Web
   Push permission call `requestNotificationPermission()`.
5. Escape every server string (`escapeHtml` / `escapeAttr`) before `innerHTML`.
6. Long-running work reports through `core/progress.js` (see `docs/progress-ui.md`)
   and emits **one summary** notification, not one per item.
7. Camera/surveillance “Events” tabs are domain data (snapshots, per-camera
   filters), not a notification history — that distinction is deliberate.

## Migrating old code

| Old | New |
| --- | --- |
| `window.showToast(msg, type)` | `notify({kind, message})` (alias still works) |
| `addNotification(type, msg)` | `addNotification(msg, type)` — wrapper tolerates the old order |
| taskbar bell in `renderTaskbar()` | static top-bar bell in `index.html` |
| client-side “persistent” toast counter | server `unread` only |
| per-app event list | the sheet (except domain data, see rule 7) |

## Verify

```bash
.venv/bin/python tests/test_notification_ui.py        # static guard
.venv/bin/python tests/test_notifications_ws.py       # boots its own instance
ETHOS_TEST_PASSWORD=… .venv/bin/python run_tests.py --filter notifications
```
