# Sticky Notes — Spec & Status

Per-account notes and checklist tasks on colored cards (yellow / pink / blue / green /
orange). It is a **Package Center package** with three surfaces that share one store:

- a native EthOS window (full management);
- a collapsible **right-edge ribbon** on the desktop (quick capture, checklist toggles);
- an installable mobile PWA at `/notes` (quick capture, offline queue, Web Push).

Package repo: `~/apps/compose/ethos-packages/ethos-sticky-notes` ·
Package id: `ethos-sticky-notes` · Version: **2.0.0** · `min_ethos_version: 2.14.1`

## File map

| What | Where |
|---|---|
| Package manifest, icon, README, requirements | `ethos-packages/ethos-sticky-notes/` |
| Package service (all API logic) | `ethos-packages/ethos-sticky-notes/server/main.py` — stdlib HTTP server |
| Web Push (VAPID, subscriptions, delivery) | `ethos-packages/ethos-sticky-notes/server/push.py` |
| Native UI module | `ethos-packages/ethos-sticky-notes/frontend/app.js` — shared package shell |
| Right-edge ribbon module | `ethos-packages/ethos-sticky-notes/frontend/ribbon.js` — `mount(host, ctx)` |
| Package service tests | `ethos-packages/ethos-sticky-notes/tests/test_service.py` |
| Wheelhouse | `ethos-packages/ethos-sticky-notes/wheels/` (`http_ece`, `py_vapid`; the rest are PyPI URLs) |
| Core ribbon schema | `backend/services/package_schema.py` — `ui.ribbon` |
| Core ribbon host | `frontend/static/core/package-ribbon.js` + `frontend/static/css/ribbon-host.css` |
| Mobile PWA | `frontend/notes.html`, `frontend/static/notes/notes.js`, `frontend/static/css/notes.css` |
| PWA manifest / icons | `frontend/manifest-notes.webmanifest`, `frontend/static/icons/notes-*.png` |
| Core routes | `backend/main.py` — `/notes`, `/notes/`, `/manifest-notes.webmanifest` (exact public paths) |
| Service worker | `frontend/sw.js` — notes shell precache + `push` / `notificationclick` |
| PWA spec | `docs/notes-pwa.md` |

Runtime: `runtime: "venv"`, loopback `127.0.0.1:8099`, `user_access: true`, native UI plus
`ui.ribbon`. The core proxies `/api/packages/ethos-sticky-notes/proxy/...` and stamps the
verified caller as `X-Ethos-User`.

## Persistence

Package data lives under `<DATA_DIR>/packages/data/ethos-sticky-notes/`:

| File | Mode | Content |
|---|---|---|
| `notes.json` | 0600 | `{version: 2, users: {name: [note, ...]}}` |
| `settings.json` | 0600 | `{version: 1, users: {name: {ribbon_enabled, ribbon_show_completed}}}` |
| `push.json` | 0600 | `{version: 1, vapid: {private_pem, public_key}, users: {name: [subscription]}}` |
| `vapid_private.pem` | 0600 | P-256 private key for Web Push (generated on first use) |

All writes are atomic (tmp + fsync + `os.replace`). Corrupt/missing stores start empty (logged to
stderr → the unified Logs app as `pkg:ethos-sticky-notes`). The first start imports the legacy
built-in widget store `<DATA_DIR>/sticky_notes.json` (`{username: [note, ...]}`) when `notes.json`
does not exist. A v1 package store is migrated in place to v2.

### Note model

```jsonc
{
  "id": "sn_a1b2c3d4e5f6",       // prefix + 12 hex chars; client ids are accepted for idempotency
  "kind": "note",                 // note | checklist (items[] forces checklist)
  "text": "buy milk",             // ≤ 2000 chars; may be empty
  "items": [                      // ≤ 100 items
    {"id": "ci_0123456789ab", "text": "2 l", "done": false}  // item text ≤ 500 chars
  ],
  "color": "yellow",              // yellow | pink | blue | green | orange
  "pinned": false,
  "archived": false,
  "due_at": "2026-11-20",         // YYYY-MM-DD or null
  "reminder_at": "2026-11-20T09:00:00+00:00", // ISO instant in UTC or null
  "reminder_fired_at": null,      // set when the reminder was claimed/sent
  "created_at": "2026-11-19T10:00:00+00:00",
  "updated_at": "2026-11-19T10:05:00+00:00"
}
```

List order is display order (top → bottom). Caps: **200 notes per account**, **2000 characters per
note**, **100 items per checklist**, **500 characters per item**, request body 256 KiB. `due_at` and
`reminder_at` apply to both notes and checklists; a finished checklist never fires a reminder.

## API (through the authenticated package proxy)

The table paths are the service paths after `/api/packages/ethos-sticky-notes/proxy` (for example
`GET /api/packages/ethos-sticky-notes/proxy/api/sticky-notes`). The service never trusts a
client-supplied identity.

| Method | Path (after `/proxy`) | Body | Result |
|---|---|---|---|
| GET | `/api/sticky-notes` | — | `{"notes": [...]}` |
| POST | `/api/sticky-notes` | `{client_id?, kind?, text?, color?, items?}` | 201 `{notes, note}` |
| PUT | `/api/sticky-notes/{note}` | `{text?, color?, kind?, pinned?, archived?, due_at?, reminder_at?}` | `{notes, note}` |
| DELETE | `/api/sticky-notes/{note}` | — | `{notes}` |
| POST | `/api/sticky-notes/{note}/move` | `{"direction": "up"\|"down"}` | `{notes}` |
| POST | `/api/sticky-notes/{note}/items` | `{text, client_id?}` | 201 `{notes, note}` |
| PUT | `/api/sticky-notes/{note}/items/{item}` | `{text?, done?}` | `{notes, note}` |
| DELETE | `/api/sticky-notes/{note}/items/{item}` | — | `{notes, note}` |
| GET | `/api/sticky-notes/settings` | — | `{settings}` |
| PUT | `/api/sticky-notes/settings` | `{ribbon_enabled?, ribbon_show_completed?}` | `{settings}` |
| GET | `/api/sticky-notes/reminders/due` | — | `{reminders: [...]}` (claims them) |
| GET | `/api/sticky-notes/push/key` | — | `{public_key}` (503 when push is unavailable) |
| POST | `/api/sticky-notes/push/subscribe` | `{subscription}` | `{ok, subscriptions}` |
| POST | `/api/sticky-notes/push/unsubscribe` | `{endpoint}` | `{ok, removed}` |
| POST | `/api/sticky-notes/push/test` | — | `{ok, sent, failed, ...}` |
| GET | `/health` | — | `{ok, version, user, push}` |

Errors: 400 bad color/kind/text/date/direction/subscription, 401 missing proxy identity, 404
unknown note/item, 409 note cap, 413 oversized body, 503 push unavailable. Every mutating note/items
endpoint returns the caller's complete note list so clients re-render from server truth.
`client_id` (`sn_` or `ci_` prefix) makes retries idempotent — the PWA and the offline outbox rely on
it.

## Frontend behaviour (package window)

- Shared package shell: 218 px text-only rail (Notes / Archive / Settings / About), status pill,
  account note in the foot; main area with summary strip and panels.
- Views: active notes, archived notes, settings, about. Search filters text and checklist items;
  filters all / notes / tasks / archive; pinned notes sort first.
- Composer supports **Note** and **Task** (checklist); checklist rows toggle, edit and delete.
- Note card: colored background, borderless auto-growing textarea, five color swatches,
  pin / archive / delete, due date and reminder inputs; JSON bodies use `ctx.proxy`.
- Editing is debounced ~500 ms → `PUT`; text saves never rebuild the DOM (focus-safe); structural
  re-renders restore focus and caret.
- Errors surface through the global Notification Center (`ctx.notifications.toast`) and `ctx.log`;
  the package ships no toast, panel, badge or log viewer of its own.

## Right-edge ribbon

A pinned native package may declare:

```json
"ui": {
  "mode": "native",
  "entry": "frontend/app.js",
  "ribbon": {"entry": "frontend/ribbon.js", "title": "Sticky Notes"}
}
```

- `ui.ribbon.entry` must be listed in `files[]` and export `mount(host, ctx)` (or a default
  function); it may return a cleanup function.
- The core mounts **one ribbon** in `#ethos-ribbon-host` (`package-ribbon.js`). The host sits above
  app windows and the taskbar (z 950), below the App Drawer (960), progress panel (9989), notification
  sheet (9990) and toasts (9999), and is hidden below 900 px (phones use `/notes`).
- `ctx` is the native package context plus `openApp(appId)` and `setDesktopGutter(px)`. The ribbon
  calls `setDesktopGutter(44)`; the core sets `--desktop-right-gutter` and `registry._gridGeometry`
  shifts the rightmost desktop icon column instead of covering it.
- The package owns every visual. The ribbon is a tab + slide-over panel: quick add note/task,
  checklist toggles, pin, open the full app, and a Settings shortcut. It closes on Escape, outside
  click or when the Notification Center opens.
- Per-account settings live in the package service: `ribbon_enabled` (default false) and
  `ribbon_show_completed` (default true). Disabling the ribbon unmounts it and resets the gutter.
  The package window and the PWA share the same settings.
- If several installed packages declare a ribbon, the first pinned candidate wins and the core logs a
  warning. Only pinned, signed native packages are eligible.

## Reminders and Web Push

- The service runs a 30 s scheduler. A due reminder is sent to every subscription of the account
  when `reminder_at <= now` and `now - reminder_at <= 24 h`; archived and finished checklists are
  skipped. The 24 h grace stops a NAS that was off for days from firing stale alerts.
- `GET /reminders/due` is the in-app fallback: it claims due reminders once (sets
  `reminder_fired_at`) and returns `{note_id, kind, title, body, due_at, reminder_at, open_items, tag,
  url}`. The package window, ribbon and PWA poll it while visible and without a push subscription.
- Push is per-installation VAPID (P-256). `push.py` generates the key pair on first use, keeps the
  private PEM at 0600, and stores at most **5 subscriptions per account**.
- Subscription endpoints are attacker-controlled: only HTTPS, public-IP hosts without userinfo are
  accepted, max 2048 chars, and the endpoint is validated again before every send (DNS can change).
  This keeps the service from becoming an SSRF proxy.
- `sw.js` owns the `push` handler (shows a notification with the reminder tag) and
  `notificationclick` (focuses an open `/notes` window or opens one). The permission prompt goes
  through `core/notification-center.js#requestNotificationPermission()`; no app touches the
  `Notification` API directly.
- Push requires a secure context (HTTPS or localhost). Settings → Notifications in the package and
  the PWA menu both enable/disable the current device and offer a test notification.

## Mobile PWA

`/notes` is a standalone, installable quick-capture app for phones: note/task composer, search,
filters, checklist toggles, pin/archive/delete, due/reminder badges, offline outbox and Web Push.
It shares the account store through the package proxy. Full spec: `docs/notes-pwa.md`.

## Status

- [x] Package service v2: notes + checklists, search, pin, archive, due dates, reminders
- [x] Per-account settings and legacy/v1 store migration
- [x] Native package window on the shared package shell
- [x] Right-edge ribbon (`ui.ribbon`) with quick capture, checklist toggles and gutter
- [x] Core `ui.ribbon` schema validation + host + desktop gutter
- [x] Web Push (VAPID, subscriptions, scheduler, test notification) and in-app fallback
- [x] Mobile PWA at `/notes` (offline queue, share target, shortcuts, push)
- [x] Package tests (`ethos-sticky-notes/tests`), core ribbon/PWA/package-UI tests
- [x] Package 2.0.0 built, signed and verified (`min_ethos_version: 2.14.1`)

## Verify

```bash
# package service + repo CI
.venv/bin/python -m unittest discover -s \
  ~/apps/compose/ethos-packages/ethos-sticky-notes/tests -v
.venv/bin/python ~/apps/compose/ethos-packages/.github/scripts/validate_repo.py

# core guards
.venv/bin/python tests/test_package_ribbon.py
.venv/bin/python tests/test_notes_pwa.py
.venv/bin/python tests/test_pwa_assets.py
.venv/bin/python tests/test_sticky_notes_package_ui.py
```

