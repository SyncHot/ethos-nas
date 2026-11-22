# Sticky Notes — Spec & Status

Per-user sticky notes on colored cards (yellow / pink / blue / green / orange),
reorderable up/down, one-click remove. **This is now a Package Center package**, not a
core desktop widget: the UI ships as a native package window and the notes live in the
package's private Python service.

Package repo: `~/apps/compose/ethos-packages/ethos-sticky-notes` ·
Package id: `ethos-sticky-notes` · Version: 2.0.0 · `min_ethos_version: 2.14.1`

See [`docs/notes-pwa.md`](notes-pwa.md) for the installable mobile capture app.

## File map

| What | Where |
|---|---|
| Package manifest, icon, README | `ethos-packages/ethos-sticky-notes/` |
| Package service (all API logic) | `ethos-packages/ethos-sticky-notes/server/main.py` — stdlib HTTP server |
| Native UI module | `ethos-packages/ethos-sticky-notes/frontend/app.js` — shared package shell |
| Runtime | `runtime: "venv"`, loopback `127.0.0.1:8099`, `user_access: true` |
| Persistence (auto-created) | `<DATA_DIR>/packages/data/ethos-sticky-notes/notes.json` |
| Legacy store (read once) | `<DATA_DIR>/sticky_notes.json` — imported on first service start |

Removed from core: `backend/routers/sticky_notes.py`, its `main.py` mount, the
`frontend/static/apps/sticky-notes.js` ambient widget, the `app.js` bootstrap call and
`tests/test_sticky_notes.py`. The old widget was ambient (a right-edge tab); the package
is a normal desktop app window with a desktop icon and taskbar entry.

## Data model

Package store, keyed per account:

```jsonc
{
  "version": 1,
  "users": {
    "marcin": [
      {
        "id": "sn_a1b2c3d4e5f6",   // prefix + 12 hex chars (uuid4)
        "text": "buy milk",        // max 2000 chars, may be empty
        "color": "yellow",         // yellow | pink | blue | green | orange
        "created_at": "2026-07-11T10:00:00+00:00",
        "updated_at": "2026-07-11T10:05:00+00:00"
      }
    ]
  }
}
```

List order is display order (top → bottom). Atomic writes via tmp file + `os.replace`,
guarded by a module-level `threading.RLock`. Corrupt/missing store → empty board. Caps:
200 notes per account, 2000 characters per note. The legacy `sticky_notes.json`
(`{username: [note, ...]}`) is normalized and imported when `notes.json` does not exist.

## API (through the authenticated package proxy)

The core exposes `POST/GET /api/packages/ethos-sticky-notes/proxy/<path>` and forwards the
verified caller as `X-Ethos-User`; the service never trusts a client-supplied identity.

| Method | Path (after `/proxy`) | Body | Result |
|---|---|---|---|
| GET    | `/api/sticky-notes`            | —                          | `{"notes": [...]}` |
| POST   | `/api/sticky-notes`            | `{text?, color?}`          | full list + new note (color auto-cycled when omitted) |
| PUT    | `/api/sticky-notes/{id}`       | `{text?, color?}`          | full list + updated note |
| DELETE | `/api/sticky-notes/{id}`       | —                          | full list (note removed) |
| POST   | `/api/sticky-notes/{id}/move`  | `{"direction": "up"\|"down"}` | full list (swapped; no-op at edges) |
| GET    | `/health`                      | —                          | liveness + version |

Every mutating endpoint returns the caller's complete note list so the client re-renders
from server truth. Errors: 400 bad color/text/direction, 404 unknown note id, 409 note cap,
401 when the proxy identity is missing.

## Frontend behaviour

- Shared package shell: 218 px rail with the text-only nav (Notes, About), status pill
  and account note in the foot; main area with a summary strip and one panel.
- Note card: colored background, borderless auto-growing `<textarea>` (always editable),
  five color swatches, ↑ ↓ × controls. Light and dark themes follow the shell tokens.
- Editing is debounced ~500 ms → `PUT`; move/color/delete/add return the full list.
  Text saves never rebuild the DOM (focus-safe); structural re-renders capture and
  restore focus + caret.
- Errors surface through the global Notification Center (`ctx.notifications.toast`) and
  `ctx.log`; the package ships no toast, panel, badge or log viewer of its own.

## Status

- [x] Package manifest, icon, README
- [x] Private venv service with per-account store + legacy import
- [x] Native UI on the shared package shell
- [x] Core router, widget, bootstrap call and Playwright test removed
- [x] `tests/test_frontend_modules.py` marks `sticky-notes` as moved to a package
- [x] `tests/test_config_export.py` no longer depends on the core endpoint
- [x] Repository rebuilt, signed and verified
