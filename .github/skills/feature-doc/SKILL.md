---
name: feature-doc
description: "Create or update docs/<feature>.md. Use when starting, resuming, or handing off a feature; produces file map, data model, API, and status checkboxes."
argument-hint: "Feature name or docs/<feature>.md path"
---

# Feature document

AGENTS.md requires a `docs/<feature>.md` **when a feature starts**. It is the resume point for
future sessions — write it for an agent that has not read the codebase.

## Procedure

### 1. Create `docs/<feature>.md`
Use the structure from `docs/kanban.md` / `docs/surveillance-station.md`:

```markdown
# <Feature> — Spec & AI Handoff

One paragraph: what it is, who uses it, and the current state.
Everything below verified against the code on YYYY-MM-DD.

## File map
| What | Where |
|---|---|
| Backend router | `backend/routers/<name>.py` |
| Router mount | `backend/main.py` — `app.include_router(...)` |
| Service(s) | `backend/services/<name>_service.py` |
| Frontend app | `frontend/static/apps/<name>.js` |
| Registration | `frontend/static/app.js` RENDERERS, `core/registry.js`, `core/desktop-layout.js` |
| Store(s) | `backend/data/<name>.json` |
| Tests | `tests/test_<name>.py` |

## Data model
JSON shape / SQLite schema / file layout, with units and ID formats.

## API
| Method | Path | Auth | Notes |
|---|---|---|---|

## Frontend wiring
Entry points, which core modules are used, cross-app links.

## Decisions & constraints
Why the design is this way; known gaps and their `TODO.md` IDs.

## Status
- [x] Backend endpoints
- [ ] Frontend app
- [ ] Playwright test
- [ ] Android parity (`ethos-<app>`)

## Known gaps / next steps
```

### 2. Rules for the content
- Anchor every claim in a real path or command; avoid line numbers that will drift (name the symbol).
- Link `TODO.md` IDs instead of maintaining a second backlog.
- Record shared-state contracts (`/api/...`) so web and Android do not duplicate state.
- Note the auth level (user/admin/path-scoped) for each endpoint.

### 3. Keep it current
- Update the status boxes and the "verified on" date as work lands.
- When a decision changes, rewrite the decision and why — do not silently delete history that
  explains a constraint.
- When the feature ships, the doc stays as the architecture/handoff reference.

## Verification
All file-map paths exist, every endpoint is mounted in `backend/main.py`, and no status box is ticked without `verify-changes`.

## Done when
A new agent can implement or resume the feature from the doc alone (paths, data model, API, wiring,
status, gaps), and the file map is verified against the current tree.
