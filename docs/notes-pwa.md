# Sticky Notes PWA (`/notes`)

An installable, phone-first capture surface for the `ethos-sticky-notes` package. It is a
standalone HTML page — not the desktop shell — so it loads quickly, works as a home-screen app
and can receive Web Push reminders.

- URL: `/notes` (and `/notes/`)
- Manifest: `/manifest-notes.webmanifest` (`id`/`scope`/`start_url` = `/notes`, standalone)
- Service worker: `/sw.js` (scope `/`)
- API: the package proxy, `/api/packages/ethos-sticky-notes/proxy/api/sticky-notes/…`
- Store/spec: [`docs/sticky-notes.md`](sticky-notes.md)

## Why a separate page

The desktop shell is a full window manager; on a phone it is the wrong tool for “add a note in
two seconds”. `/notes` has one composer, a searchable list and the same account store. The
desktop right-edge ribbon is hidden below 900 px, so phones use this page instead. Both surfaces
edit the same package data and share the per-account ribbon settings.

## Routes and auth

`backend/main.py` serves three **exact public paths** before the SPA catch-all:

| Path | File | Why public |
|---|---|---|
| `/notes`, `/notes/` | `frontend/notes.html` | the browser loads it before a token exists |
| `/manifest-notes.webmanifest` | `frontend/manifest-notes.webmanifest` | fetched by the browser while signing in |

Static assets (`/static/css/tokens.css`, `/static/css/notes.css`, `/static/notes/notes.js`, the
icons) are already public under `/static/`.

Authentication is the normal EthOS login:

- The page reads `localStorage.token` (same origin as the desktop, so an existing desktop session is
  reused).
- Without a valid, unexpired token it shows its own sign-in card and posts `/api/auth/login`; on
  success it stores the token/user and reloads.
- Every package call carries the JWT; the core proxy stamps `X-Ethos-User`, so the service only
  ever sees the caller's own notes.
- Sign out removes the token and the device's push subscription.

## Features

- **Quick capture:** a single composer with **Note** / **Task** modes. Enter adds; tasks are
  checklists with one item created atomically.
- **Search and filters:** all / notes / tasks / archive, plus pinned-first ordering.
- **Note actions:** pin, archive/restore, delete; color, due date and reminder are set in the
  package window (the PWA shows them as badges, including an overdue state).
- **Checklists:** toggle, edit, add and remove items inline.
- **Launch params:** manifest shortcuts `?new=note` / `?new=checklist` switch the composer mode;
  `?note=<id>` scrolls to and highlights a note (used by push notification clicks).
- **Share target (GET):** another app can share `title`, `text` and `url` to `/notes`; the page
  combines them into the composer.
- **Install:** `beforeinstallprompt` is captured for an **Install app** menu item; iOS falls back to
  “Add to Home screen”.
- **Theme:** follows `ethos-theme` and toggles light/dark; the standalone page loads the shared
  `tokens.css` so colors match the desktop.
- **Feedback:** toasts go through the global Notification Center; logs go through `core/logger.js`.
  The page has no local notification panel or log viewer.

## Offline queue

Writes are optimistic and survive an unreachable NAS:

| Key | Content |
|---|---|
| `ethos-notes-cache:<user>` | last notes + settings + `savedAt` |
| `ethos-notes-outbox:<user>` | at most **200** pending operations |

- Client-generated ids (`sn_` + 12 hex, `ci_` + 12 hex) make `POST` retries idempotent.
- Operations: `create`, `update`, `add-item`, `item`, `delete`, `delete-item`.
- A network error (or 5xx) queues the operation and shows the offline/queued pill. A 4xx is
  dropped (the note was deleted, the text is invalid) and logged as a warning.
- The queue replays on `online`, on tab visibility, and after a successful reload; after it drains,
  the page reloads from the server.
- The service worker (`frontend/sw.js`, cache `ethos-shell-v5`) is **network-first**. It precaches
  `/notes`, its manifest, CSS, module and icons, and falls back to the cached `/notes` shell only
  when the network is unavailable. It never caches `/api/*`, `/ws*`, `Range` responses or URLs with
  a `?token=`.

## Web Push

Push requires a **secure context** (HTTPS or localhost) and a browser with `ServiceWorker` +
`PushManager` + `Notification`.

1. The menu’s **Enable notifications** asks for permission through
   `core/notification-center.js#requestNotificationPermission()` — the only module allowed to touch
   the `Notification` API.
2. It fetches the installation’s VAPID public key from `/push/key`, subscribes with
   `pushManager.subscribe({userVisibleOnly: true, applicationServerKey})`, then posts the
   subscription to `/push/subscribe`.
3. The package service validates the endpoint (HTTPS, public IP, no credentials) and stores it
   (max 5 per account).
4. When a reminder is due, the service sends `{title, body, tag, url, note_id, …}`. `sw.js` shows a
   notification tagged with the reminder; `notificationclick` focuses an open `/notes` window (or
   opens one) at `/notes?note=<id>`.
5. If push is unavailable or the device has no subscription, the page polls `/reminders/due` while
   visible and emits a toast + a persisted Notification Center event (`source` is the package).
6. **Disable notifications** unsubscribes the browser and removes the endpoint server-side; sign-out
   does the same.

The PWA menu also offers a test notification through the package API.

## Files

| File | Role |
|---|---|
| `frontend/notes.html` | standalone shell, login card, composer, list, menu |
| `frontend/static/notes/notes.js` | auth, API, cache/outbox, render, push, launch params |
| `frontend/static/css/notes.css` | mobile layout, note cards, themes |
| `frontend/static/css/tokens.css` | shared light/dark tokens |
| `frontend/manifest-notes.webmanifest` | installability, shortcuts, share target |
| `frontend/sw.js` | shell precache, offline `/notes`, push/notificationclick |
| `frontend/static/icons/notes-*.png` | 192/512/maskable/apple icons |
| `scripts/make_icons.py` | generates the desktop and notes icon sets |

## Tests

```bash
.venv/bin/python tests/test_notes_pwa.py        # live: login gate, quick add, search, offline queue
.venv/bin/python tests/test_pwa_assets.py       # manifest/icons/CSS/JS/sw guards (52 checks)
.venv/bin/python tests/test_frontend_modules.py # module graph, including sw.js references
.venv/bin/python tests/test_notification_ui.py  # the Notification API stays in the core module
```

## Status

- [x] Public `/notes` page + manifest routes (exact public paths)
- [x] Login gate reusing the desktop token, sign-out
- [x] Note/task quick capture, search, filters, pin/archive/delete, checklist editing
- [x] Offline cache + bounded outbox with idempotent client ids and replay
- [x] Service worker network-first shell, offline `/notes` fallback
- [x] Web Push subscribe/unsubscribe/test, service worker push + notification click
- [x] In-app reminder fallback through `/reminders/due`
- [x] Install prompt, theme toggle, shortcuts, GET share target
- [x] Icons generated by `scripts/make_icons.py`
- [x] Tests green (12 PWA checks, 52 PWA asset checks)
