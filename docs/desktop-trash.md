# Desktop Trash Shortcut — Spec & Status

A `Trash` icon on the desktop that opens Files' Trash view, shows the
number of trashed items as a badge, and can empty the trash from its right-click
menu. Files items can be dragged onto the icon to move them to trash.
Verified against the code on 2026-11-13.

## File map

| What | Where |
|---|---|
| Trash behaviour (open / empty / badge / drop) | `frontend/static/core/desktop-trash.js` |
| Desktop icon + badge markup, placement, context menu | `frontend/static/core/registry.js` |
| Default pin + per-user seed marker | `frontend/static/core/desktop-layout.js` (`ensureTrashPinned`) |
| Icon glyph + gradient | `frontend/static/core/app-icons.js` (`trash`) |
| Bootstrap wiring (`openApp('trash')` branch, seed after remote layout) | `frontend/static/app.js` |
| Badge CSS / drop highlight | `frontend/index.html` (`.desktop-trash-badge`, `.trash-drop-target`) |
| Id allowlist (server-side layout validation) | `backend/routers/packages.py` (`APP_REGISTRY_KEYS`) |
| Files trash UI reused | `frontend/static/apps/files.js` (`fmOpenTrash`, `fmEmptyTrash`) |
| File-event → badge trigger | `frontend/static/apps/files.js` (`handleFileEvent`, `ethos:trash-changed`) |
| Tests | `tests/test_desktop_trash.py`, `tests/test_desktop_trash_ui.py` |

No backend router was added: the icon reuses `GET/POST/DELETE /api/files/trash*`
(see `docs/file-manager.md` and `backend/routers/files.py`).

## Data model

Nothing new is persisted for the shortcut itself. The shortcut uses the existing
per-user desktop layout (`backend/data/desktop_layouts.json`) with the id `trash`
inside `visible` and, optionally, `positions.trash = {c, r}` when the user drags
it. Deleted items live in per-user recycle-bin namespaces — see
`docs/trash.md`.

- `trash` is a registered app id in the three places that must agree:
  `APP_REGISTRY` (`core/registry.js`), `DEFAULT_ORDER` (`core/desktop-layout.js`)
  and `APP_REGISTRY_KEYS` (`backend/routers/packages.py`). A missing backend entry
  makes the server drop the id on every layout save.
- The desktop is empty by design (Ubuntu-style pinning). The icon is seeded once
  per account/browser: `ensureTrashPinned()` sets
  `localStorage["ethos-desktop-trash-seeded:<username>"]` and appends `trash` to
  `visible`. Unpinning it later is permanent for that browser.
- Placement: when `trash` has no stored cell, `_computePlacement()` claims the
  free grid cell closest to the bottom-right corner before the column-major
  auto-fill. "Auto-arrange" restores it there; dragging it elsewhere stores a
  normal `{c, r}` cell.

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/files/trash?limit=1` | any authenticated user | badge count = **the caller's** `total` (sidecars/excluded) |
| POST | `/api/files/trash` | any authenticated user, path-scoped source | drop-to-trash: `{path}` per item; lands in the caller's namespace; broadcasts `delete_soft` |
| DELETE | `/api/files/trash` | any authenticated user | empties **only the caller's** trash; admins may pass `all=1`; emits a notification event |

The recycle bin itself is per-user — see `docs/trash.md` for the storage layout,
ownership rules and the admin all-users view. The badge polls every 60 s, on
`visibilitychange`, after its own actions, and when Files dispatches
`ethos:trash-changed` (from the `/ws/files/events` handler for
`delete_soft` / `restore`, and after restore/delete/empty in the Trash view).

## Frontend wiring

- `openApp('trash')` is intercepted in `app.js` and calls
  `openTrashFromDesktop()`; this also covers App Drawer clicks, because the
  drawer opens every item through `window.openApp`.
- `openTrashFromDesktop()` focuses/restores an existing Files window
  (multi-window app) or opens one, then calls `window.fmOpenTrash()`. It never
  opens a second Files window on repeat clicks.
- `emptyTrashFromDesktop()` delegates to `window.fmEmptyTrash()` so the
  confirmation dialog, DELETE request and toast stay in one place.
- Right-click menu: `Open Trash` (`icon-ctx-trash-open`), `Empty Trash`
  (`icon-ctx-trash-empty`), `Refresh count` (`icon-ctx-trash-refresh`), plus the
  normal Remove-from-Desktop / Reset entries.
- Drag & drop: Files drags carry `application/x-ethos-fm-items`
  (`[{name, path, is_dir}]`). `initDesktopTrash()` handles dragover/drop on the
  icon (capture phase), POSTs each path, toasts the summary and refreshes the
  badge. `initDesktopDropZone()` now ignores that MIME instead of trying to pin
  an app from the drag's `text/plain` file names.
- Badge: hidden at 0, capped at `99+`, `aria-label` mirrors the count.

## Decisions & constraints

- Shortcut, not a window app: no `RENDERERS` entry and no separate window type,
  so Files' `_getActiveFmRoot()` (which filters `appId === 'files'`)
  keeps working unchanged.
- Reuse over duplication: open/empty/confirm/toast come from Files.
- Badge and Empty Trash are **caller-scoped** (per-user recycle bin); File
  Station's Trash view has an admin-only "Show all users" toggle.
- Badge cost: `limit=1` keeps the response to one item; no recursive size walk
  (`/api/system/storage-usage` walks the whole trash tree and is not used).
- Mobile: the icon participates in the responsive static icon grid and keeps its
  badge; it is not a fixed overlay, so it cannot cover content.
- Known gap: emptying the trash emits a Notification Center event but no
  `delete_hard` WS broadcast, so other clients learn about it on their next
  poll/refresh.
- Known gap: the seed marker is browser-local, so a brand-new browser may pin
  the icon once again even if it was deliberately removed elsewhere.

## Status

- [x] Registered id in registry / default order / backend allowlist
- [x] Per-user recycle bin backend (isolation + admin all-users view)
- [x] Badge + bottom-right default placement + context menu
- [x] Open / empty from the shortcut (own trash only)
- [x] Drag & drop Files items onto the icon
- [x] Static guard test
- [x] Playwright test (destructive empty step gated by `ETHOS_TEST_ALLOW_TRASH_EMPTY=1`)
- [x] Static checks, boundary/logging guards and an isolated end-to-end run (badge,
      context menu, open Trash, synthetic drop, empty)
- [ ] Production check on `:9000` after the service restart (backend allowlist
      change needs a restart)

## Known gaps / next steps

- Broadcast `delete_hard`-style events after empty/item-delete so every client's
  open Trash view refreshes without a poll.
- Optional: show trash size instead of item count (needs a cheap summary
  endpoint; `GET /api/files/trash` has no sizes).
