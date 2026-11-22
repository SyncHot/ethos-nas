# Feature: Public Share Links (Files "Share")

> **Not the same as user sharing.** Share links are anonymous public URLs. To let another
> EthOS account read a file, album, playlist or folder without a token URL, use **Sharing**
> (`docs/sharing.md`, `/api/sharing`). Recipients authenticate normally and a grant is revoked
> with one click; the public-link system is separate and unaffected.

Tokenized, expiring share URLs for files and folders — the Files app's
"Share link" feature. Lets the owner hand out a URL that downloads content
without giving the recipient an EthOS account or SMB access.

## Rationale (from codebase scan)
- EthOS has Samba shares (`shares.py`) but **no per-file/folder sharing**: to share one file
  today you must either give someone credentials or copy it out of the UI.
- The Notification Center + JSON-store pattern (`services/notifications_service.py`,
  thread-safe, atomic writes, `settings.DATA_DIR`) is the established home for new state —
  share links follow it exactly.
- Auth middleware whitelists path prefixes in `public_paths` (`main.py:198`) — public link
  endpoints slot into that mechanism with no middleware changes beyond one prefix.
- Security model follows `files.py`: every path is created through
  `path_scope.sanitize()` for the **caller's** identity (admins full access; other accounts limited to
  their own home plus `/mnt` and `/media`). The link stores its creator (`owner`, `owner_role`) and the
  public route re-validates every stored path against that scope on each request. Legacy links without
  an owner stay limited to the old shared roots (`/home`, `/mnt`, `/media`). The token is unguessable
  (`secrets.token_urlsafe(16)` ≈ 128 bits).

## Design

### Data model — `<DATA_DIR>/share_links.json`
```json
{"version": 1, "links": [
  {"id": "<uuid4 hex>", "path": "/abs/path", "name": "label or basename",
   "token": "<urlsafe 24 chars>", "created_at": "ISO Z",
   "expires_at": "ISO Z | null", "max_downloads": int|null,
   "downloads": 0}]}
```

### Backend
- `backend/services/share_links_service.py` — thread-safe JSON store (lock + atomic write),
  mirrors notifications service. Functions: `create_link(path, name, expires_in_days,
  max_downloads)`, `list_links()`, `get_by_token(token)`, `delete_link(id)`,
  `record_download(link_id)`. Path validation: expanduser+abspath, must exist; missing → ValueError.
- `backend/routers/share_links.py` — mounted at `/api/share-links` (deliberately distinct from the
  Samba router already on `/api/shares`, whose endpoints all require JWT):
  - Authenticated CRUD:
    - `GET  /api/share-links` → `{links:[...]}` (newest first)
    - `POST /api/share-links` body `{path, name?, expires_in_days?, max_downloads?}` → created link incl. `url`
    - `DELETE /api/share-links/{id}` → `{ok:true}` (404 if unknown)
  - Public (no JWT — prefix `/api/share-links/public` added to `public_paths`):
    - `GET /api/share-links/public/{token}` → file: stream download; dir: JSON listing `{name, path, files:[{name,size,is_dir}]}`
    - `GET /api/share-links/public/{token}/download?file=<rel>` → single file inside a shared directory (traversal-safe: resolved path must stay under the shared root)
  - Expiry / max-downloads enforced at serve time → `410 Gone` with reason.
  - Emits Notification Center events on create/revoke (`source="share-links"`).

### Frontend
- New app **Shared Links** (`frontend/static/apps/share-links.js`, registry id `shared-links`, 🔗):
  - Table: name, path, created, expires, downloads (x/max), status chip (active/expired/limit reached), actions: copy link, delete.
  - Create form: absolute path, optional label, expiry select (never / 1d / 7d / 30d), optional max downloads. On success → toast + URL with copy button.
- Files context menu gains **"Share link…"** (`fm-ctx-share-link`) for files and folders —
  sets `window.__fmShareCtx = {path}` then opens the app (same pattern as Gallery's `__fmGalleryCtx`);
  the renderer pre-fills the path field once.

### Phase 6 — Photos integration (gallery)
Same Shared Links mechanism, surfaced from Photos (`frontend/static/apps/gallery.js`):
- **Lightbox** gains a 🔗 button (`ps-lb-share`) + `s` keyboard shortcut → `_lbShareCurrent()` sets
  `window.__fmShareCtx = {path, name}` and opens the app (identical flow to Files' context action).
  Private/encrypted photos are blocked with a warning toast (a public link would serve ciphertext).
- **Sidebar library folders** gain a 🔗 button per row (`ps-share-folder-<i>`) → `_psShareFolder(i)`
  shares the whole folder as one link (path pre-filled, label = folder name). Click stops propagation
  so it doesn't navigate into the folder.
- **Multi-select Share** (action bar) now opens a small options modal (`_psShareOptionsModal`) —
  expiry (never/1d/7d/30d, default 7d) + optional max downloads per link — then creates one link per
  selected photo with those options and opens the app. Previously the multi branch hard-coded 7 days.
- `share-links.js` create form also pre-fills the **Label** field when `__fmShareCtx.name` is set.

### Tests — `tests/test_share_links.py` (standalone, isolated instance on :9001)
- Part A (API E2E): login → create link for a temp file → public GET without auth returns exact bytes →
  downloads counter increments → dir link: listing + per-file download + traversal attempt (`../`) rejected →
  max_downloads=1 second fetch → 410 → delete → public URL 404.
- Part B (UI via Playwright, auto-skip if chromium unavailable): app opens from desktop icon, create form works, row appears with copy button.

## Acceptance criteria
1. `node --check` passes on touched JS; `.venv/bin/python -c "import backend.main"` OK.
2. Server up on :9000 (health curl 401 unauthenticated).
3. Public endpoints work **without** a JWT and reject unknown/expired/limit-reached tokens with 404/410.
4. Directory sharing cannot escape the shared root (`..` traversal → 400/404).
5. Files context menu offers "Share link…" for files and folders; created URL is copyable from the app.

## Status
- [x] Phase 1 — Audit & Discovery
- [x] Phase 2 — Proposal & Scoping (this file)
- [x] Phase 3 — Implementation
- [x] Phase 4 — Validation
- [x] Phase 5 — Handoff & Commit
- [x] Phase 6 — Photos integration: lightbox 🔗 + `s` shortcut, sidebar folder 🔗,
      multi-select options modal, label pre-fill in Shared Links form

### Verification Log
- `node --input-type=module --check`: share-links.js, registry.js, app.js, files.js — all OK.
- `.venv/bin/python -c "import backend.main"` + py_compile on new modules — OK.
- Live E2E (`tests/test_share_links.py`, isolated instance :9005, `ETHOS_DATA_DIR=/tmp/ethos-test-data`):
  **Part A — all 24 checks PASS**: file link public GET without JWT returns exact bytes; download
  counter increments; dir listing JSON + per-file download; traversal `../` and absolute paths → 400;
  max_downloads=1 second fetch → 410; expires_in_days=30 metadata sane; missing path → 400;
  revoke → public URL 404; unknown-id delete → 404.
  **Part B — all 4 checks PASS**: seeded row renders with Active chip, form create adds a row,
  no page JS errors.
- Production restarted on :9000 (health curl → 401 unauthenticated = up).
