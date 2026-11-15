# Kanban Board — Spec & AI Handoff

**Moved to Packages (`ethos-kanban`), `docs/app-store.md` phase 3.** The core no longer
ships a Kanban router or app: the board is a signed package with a native UI and its own
stdlib-only Python service. Boards from the built-in version are imported on first start
(`<DATA_DIR>/kanban_boards.json` → `<DATA_DIR>/packages/data/ethos-kanban/boards.json`).

A self-hosted Kanban board (boards → columns → cards) for EthOS NAS. This document is
written so that **a new AI agent can pick up implementation work without reading the
whole codebase first**.

## File map

| What | Where |
|---|---|
| Backend service (all API logic) | `ethos-packages/ethos-kanban/server/main.py` (stdlib HTTP server) |
| Native UI module | `ethos-packages/ethos-kanban/frontend/app.js` |
| Manifest (`user_access: true`, port 8098) | `ethos-packages/ethos-kanban/manifest.json` |
| Proxy + identity headers | `backend/routers/packages.py` (`package_ui_proxy`, `_proxy_identity`) |
| Renderer registration | none — the package registers its own desktop icon |

Everything else below still describes the data model and the API contract, which the
package implements unchanged under `/api/packages/ethos-kanban/proxy/api/kanban/...`.

## Legacy overview
| Frontend app (UI + drag & drop) | `frontend/static/apps/kanban.js` (~567 lines, ES module) |
| App registration (renderer map) | `frontend/static/app.js` — `import { renderKanbanPage }` + `RENDERERS.kanban` |
| Desktop icon / drawer entry | `frontend/static/core/registry.js` — `APP_REGISTRY.kanban` + gradient |
| Drawer ordering | `frontend/static/core/desktop-layout.js` — `"kanban"` in `DEFAULT_ORDER` |
| Persistence file (auto-created) | `backend/data/kanban_boards.json` |
| Playwright test | `tests/test_kanban.py` |

**There is no separate service layer for Kanban on purpose** — the router owns its own
JSON store, mirroring how `cron.py` works. If you add real complexity (multi-user,
WebSocket sync), move logic into `backend/services/kanban_service.py`.

## Data model

Single JSON file at `backend/data/kanban_boards.json`:

```jsonc
{
  "version": 1,
  "boards": [
    {
      "id": "board_a1b2c3d4e5f6",          // prefix + 12 hex chars (uuid4)
      "name": "Getting Started",
      "created_at": "2026-07-10T12:00:00+00:00",
      "columns": [
        {
          "id": "col_...",
          "title": "To Do",
          "cards": [
            {
              "id": "card_...",
              "title": "Explore Files",   // required, ≤200 chars
              "description": "",                  // free text
              "labels": ["tour"],                 // short strings, comma-separated in UI
              "due_date": null,                   // "YYYY-MM-DD" or null
              "created_at": "..."
            }
          ]
        }
      ]
    }
  ]
}
```

- **Ordering is positional**: array order = display order. `position` in requests is an
  insert index, clamped to `[0, len]`. There are no explicit sort keys — keep it that way
  unless you need concurrent editing.
- A board created via the API is seeded with columns `["To Do", "In Progress", "Done"]`.
- First run seeds a demo board **"Getting Started"** so the UI never starts empty.
  (Delete `backend/data/kanban_boards.json` to reset.)
- Writes are atomic (`tempfile` + `os.replace`) under a module-level `threading.Lock`;
  reads re-load from disk every request, so external edits are picked up live.

## API reference (all under `/api/kanban`, JWT required like every other endpoint)

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| GET | `` | — | `{boards: [...]}` | full content of all boards |
| POST | `` | `{name}` | board (201) | seeds default columns |
| GET | `/{board_id}` | — | board | 404 if missing |
| PUT | `/{board_id}` | `{name?}` | board | rename only |
| DELETE | `/{board_id}` | — | `{ok, deleted}` | cascades to columns/cards |
| POST | `/{board_id}/columns` | `{title, position?}` | column (201) | append at end by default |
| PUT | `/{board_id}/columns/{column_id}` | `{title?, position?}` | column | rename and/or reorder |
| DELETE | `/{board_id}/columns/{column_id}` | — | `{ok, deleted}` | cascades to cards |
| POST | `/{board_id}/columns/{column_id}/cards` | `{title, description?, labels?, due_date?}` | card (201) | appends at column end |
| PUT | `/{board_id}/cards/{card_id}` | `{title?, description?, labels?, due_date?}` | card | searched across all columns; `due_date: null` clears it |
| DELETE | `/{board_id}/cards/{card_id}` | — | `{ok, deleted}` | |
| POST | `/{board_id}/move-card` | `{card_id, to_column_id, position?}` | **updated board** | the only endpoint that returns a whole board (so clients re-render from one response) |

All request models use `model_config = {"extra": "forbid"}` — unknown fields → 422.
Errors are standard FastAPI: `{"detail": "..."}` with 400/404/422.

### curl examples (token from a logged-in browser session)

```bash
T=$(cat .test_token)   # or grab it from localStorage in the UI

# list boards
curl -s http://localhost:9000/api/kanban -H "Authorization: Bearer $T" | python3 -m json.tool

# create a board (prints its id + default columns)
curl -s -X POST http://localhost:9000/api/kanban \
  -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"name":"NAS Projects"}'

# add a card to the first column of board <BID> (column id from GET)
curl -s -X POST http://localhost:9000/api/kanban/<BID>/columns/<CID>/cards \
  -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"title":"Upgrade firmware","labels":["ops"],"due_date":"2026-08-01"}'

# move a card to the end of another column (returns the whole updated board)
curl -s -X POST http://localhost:9000/api/kanban/<BID>/move-card \
  -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"card_id":"card_...","to_column_id":"col_..."}'
```

## Frontend architecture (`frontend/static/apps/kanban.js`)

Follows the house pattern (see `cron.js`): one exported async renderer that takes the
window body element, sets `innerHTML`, and exposes every inline handler as
`window._kanban*`. No framework, no build step.

```
renderKanbanPage(body)          ← called by window-manager via RENDERERS.kanban
  └─ _state = { boards: [], boardId }   (module-level; survives re-renders)
     ├─ toolbar: board <select>, + New Board, 🗑 delete board, meta, 🔄 refresh
     ├─ #kanbanBoardArea → #kanbanColumns (horizontal flex, overflow-x auto)
     │    └─ per column (.kanban-col): header (dot/title/count/✏️/🗑),
     │         .kanban-col-cards (drop zone, cards rendered inline by _columnHtml),
     │         "+ Add a card" composer
     └─ card editor modal: reuses the `.ethos-modal-*` CSS classes from index.html,
          but with an inline `z-index: 9500` so unified confirms (9999) stack on top

window._kanbanLoad()            ← GET /api/kanban → _state.boards → window._kanbanRender()
window._kanbanRender()          ← full innerHTML rebuild (simple + reliable at this scale)
```

**Interaction model:** every mutation calls the API, then `await window._kanbanLoad()`
to re-render from server truth. There is no optimistic local state — if you add it,
keep the load-after-mutate as the fallback path.

### Drag & drop (HTML5 DnD)

- Cards are `draggable="true"`; `dragstart` stores the module-level `_dragCardId`.
- Each `.kanban-col-cards` is a drop zone: `dragover` computes an insert index from
  `e.clientY` vs. card midpoints (`_insertIndexAt`) and shows a blue 3px insertion line
  (`_showDropIndicator`).
- `drop` → `window._kanbanMoveCard(cardId, toColumnId, position)` →
  `POST /move-card {card_id, to_column_id, position}` → re-render.
- The hovered column gets a blue border via inline style (cleaned up in `_clearDropVisuals`).
- **Drag-vs-click guard:** `dragend` sets `window.__kanbanJustDragged = true` for 200 ms;
  card clicks route through `window._kanbanCardClick(cardId)` which ignores clicks inside
  that window (synthetic test drags can otherwise fire a click right after the drop and
  pop the editor modal).

### Test-ID catalog (used by Playwright — keep names stable)

| Element | testid |
|---|---|
| App root / board area / columns container | `kanban-app`, `kanban-board-area`, `kanban-columns` |
| Board select / new / delete / refresh | `kanban-board-select`, `kanban-new-board-btn`, `kanban-delete-board-btn`, `kanban-refresh-btn` |
| Empty-state CTA | `kanban-empty-new-board` |
| Column (per id) | `kanban-col-{colId}`, `kanban-col-title-{colId}`, `kanban-col-count-{colId}`, `kanban-col-rename-{colId}`, `kanban-col-delete-{colId}` |
| Card list drop zone (per column) | `kanban-col-cards-{colId}` |
| Add-card button / composer (per column) | `kanban-add-card-btn-{colId}`, `kanban-composer-{colId}`, `kanban-composer-input-{colId}`, `kanban-composer-save-{colId}`, `kanban-composer-cancel-{colId}` |
| Card (per id) | `kanban-card-{cardId}`, `kanban-card-title-{cardId}`, `kanban-card-label-{cardId}`, `kanban-card-due` |
| Add column | `kanban-add-column-btn` |
| Card editor modal | `kanban-modal`, `kanban-modal-title`, `kanban-modal-desc`, `kanban-modal-labels`, `kanban-modal-due`, `kanban-modal-save`, `kanban-modal-delete`, `kanban-modal-cancel`, `kanban-modal-close` |

**Dialogs — two different systems, do not mix them up:**

| Action | API | Source | Testids |
|---|---|---|---|
| New board / add column / rename column (single input) | `window.showPrompt(title, message, defaultValue, callback)` — positional args | `core/modal.js` | `ethos-modal-input`, `ethos-modal-ok`, `ethos-modal-cancel` |
| Delete board / column / card (confirm) | `window.showConfirm({ title, message, onConfirm })` — **object arg** | `core/unified-confirm.js` | `unified-confirm-ok`, `unified-confirm-cancel`, `unified-confirm-close` |

⚠️ `app.js` exposes the **unified-confirm** version as `window.showConfirm` (it shadows
the positional-arg one from `modal.js`, which is available as `window.showModalConfirm`
if you ever need it). Calling `showConfirm('title', 'msg', cb)` silently renders a dialog
titled "Confirm" with the message "undefined" and never invokes your callback.

## Conventions to keep following

- Theme: only CSS variables (`var(--surface-0/1/2)`, `var(--text-1/2/3)`, `var(--border)`,
  `var(--bg-primary)`). Never hardcode colors except for accent chips (label palette is
  deterministic — same label → same color, see `labelColor()`).
- All user-provided strings go through `escapeHtml` / `escapeAttr` from `core/utils.js`.
- API calls via `apiFetch` from `core/api.js` (injects the Bearer token).
- Toasts: `notify({ kind, message })` from `core/notification-center.js`;
  `window.showToast(message, type)` is a deprecated alias that routes to the same pipeline.
- Inline handlers must be reachable as `window._kanban*` — ES-module scope is not visible
  to inline `onclick` attributes.

## Testing

```bash
# server must be running on :9000 (see AGENTS.md)
.venv/bin/python tests/test_kanban.py
```

The test is **self-cleaning**: it logs in through the real PAM login overlay (same
credentials as `tests/test_file_station_login.py`), opens Kanban via
`window.openApp('kanban')`, then works on a timestamped board (`PW Test <ts>`) created
through the UI and deletes that board at the end — the seeded "Getting Started" demo
board is only read. Steps: seeded-board checks → create board (prompt) → add column
(prompt) → add card via inline composer → edit card in modal (title/description/labels/
due date, chip + badge DOM asserts) → **drag & drop** to another column (`locator.drag_to`,
Chromium fires native HTML5 DnD) → rename column (prompt) → delete card (modal →
unified confirm `unified-confirm-ok`) → delete board (unified confirm). Every step
asserts against the API (`GET /api/kanban` from page context with the stored JWT)
as well as the DOM, so it catches both frontend and backend regressions. Exit code =
number of failed checks.

## Roadmap (suggested next features, easiest first)

1. **WIP limits per column** — ✅ shipped (`wip_limit: int | null` on the column model); enforce in
   `_move_card`/`add_card` (409 when full); show a red count badge in the UI when at limit.
2. **Card search/filter** — client-side only: an input in the toolbar that dims non-matching
   cards (title + labels). No API change needed.
3. **Column collapse / board zoom** — pure CSS/JS state, persist per-board in localStorage
   under key `ethos-kanban-view-{boardId}`.
4. **Multi-user live sync** — add a `/ws/kanban/{board_id}` WebSocket that broadcasts the
   updated board after each mutation (pattern: `file_events_ws.py`); clients re-render on
   messages from other users. Requires moving mutations through a shared service so both
   REST and WS paths broadcast.
5. **Card subtasks / checklists** — add `subtasks: [{id, text, done}]` to the card model +
   render in the editor modal; API stays the same (PUT card).
6. **Board export/import** — `GET /{board_id}/export` (JSON download) and
   `POST /import`; trivial with the current store shape.

When implementing any of these: update this file's data model + API table + testid catalog,
and extend `tests/test_kanban.py`.
