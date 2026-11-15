# Appearance / Per-User Wallpaper — Spec & Status

Per-user desktop wallpaper for the EthOS NAS UI: each account picks a built-in
fluid-gradient background or uploads its own image; the choice is stored on the
server and follows the user across devices. The default background is an
**original** CSS "liquid" gradient (soft mesh style in the spirit of recent macOS
wallpapers — no Apple artwork, all gradients are generated code, nothing to license).

Status: **implemented 2026-07-14** (see checkboxes below).

## File map

| What | Where |
|---|---|
| Backend router (all API logic) | `backend/routers/appearance.py` (~230 lines, self-contained) |
| Router mount (`/api/appearance`) | `backend/main.py` — import + `app.include_router(appearance_router, ...)` |
| Wallpaper CSS (5 built-ins × dark/light) | `frontend/index.html` — `.wallpaper-*` classes in the "Wallpaper Background" block |
| Core module (apply/cache/sync/upload) | `frontend/static/core/wallpaper.js` |
| Settings → Appearance tab | `frontend/static/apps/settings.js` — `loadAppearanceTab()` / `_renderAppearanceTab()` |
| Bootstrap hook | `frontend/static/app.js` — `loadRemoteWallpaper()` next to `loadRemoteLayout()` |
| Persistence file (auto-created) | `backend/data/wallpapers.json` → `{username: {wallpaper, custom_file}}` |
| Custom images (per user) | `backend/data/wallpapers/<username>.{jpg,png,webp}` |
| API test | `tests/test_appearance.py` (isolated instance on :9002) |

## Data model

`backend/data/wallpapers.json`:

```jsonc
{
  "marcin": {
    "wallpaper": "dusk",            // one of the built-in ids, or "custom"
    "custom_file": "marcin.jpg"     // only when a custom image was uploaded
  }
}
```

Built-in ids (must stay in sync with `BUILTIN_WALLPAPERS` in `appearance.py`,
`WALLPAPER_IDS` in `wallpaper.js`, and the `.wallpaper-*` CSS classes):

| id | Look (dark) |
|---|---|
| `aurora` *(default)* | deep indigo with teal / violet / blue glows |
| `dusk` | plum base, coral / fuchsia / amber glows |
| `meadow` | forest base, emerald / teal / lime glows |
| `tide` | deep ocean, cyan / azure / indigo glows |
| `graphite` | near-black neutral with a faint steel glow (minimal) |

Each wallpaper has a light-theme variant (`:root.light .wallpaper-*`). Custom
uploads replace the CSS class with an inline `background-image` pointing at
`/api/appearance/wallpaper/image?token=…`.

## API

All endpoints require JWT (header, cookie or `?token=` — the image endpoint is
used from `<img>`-style contexts via query token).

| Method | Path | Result |
|---|---|---|
| GET | `/api/appearance/wallpaper` | `{username, wallpaper, has_custom, custom_url?}` · **404** when the user has no record yet (client seeds it) |
| POST | `/api/appearance/wallpaper` | body `{"wallpaper": "<id>"}` — saves a built-in id or `"custom"` (requires an uploaded file); 400 on unknown id |
| DELETE | `/api/appearance/wallpaper` | resets to default, deletes the record **and** the custom image file |
| POST | `/api/appearance/wallpaper/upload` | multipart `file` — JPEG/PNG/WebP ≤ 8 MB; stored as `<username><ext>`, switches selection to `custom` |
| GET | `/api/appearance/wallpaper/image` | serves the caller's own custom image (404 when none) |

## Frontend behaviour

- **Bootstrap** (`app.js`): after login, `loadRemoteWallpaper()` fetches the
  server record — 200 → apply it; 404 → seed the record with the local choice
  (localStorage key `ethos-wallpaper`, default `aurora`). Same pattern as the
  desktop icon layout (`core/desktop-layout.js`).
- **Theme toggle** needs no changes: every wallpaper ships dark + light variants.
- **Settings → Appearance**: grid of live CSS previews (the same `.wallpaper-*`
  classes scaled down), click to apply; custom-image card with upload / remove.

## Security notes

- Custom images are stored under `ETHOS_DATA_DIR/wallpapers/`, never served from
  an arbitrary path — the image endpoint only resolves `<username><ext>` inside
  that directory (traversal-checked in `_custom_path()`).
- Uploads: content-type allowlist (jpeg/png/webp), 8 MB cap, filename derived
  from the authenticated username (user-supplied names are ignored).

## Status

- [x] Backend router + mount (`/api/appearance`)
- [x] Original fluid wallpaper CSS — 5 built-ins × dark/light in `index.html`
- [x] `core/wallpaper.js` (apply, cache, server sync, upload, reset)
- [x] Settings → Appearance tab (grid + custom upload/remove)
- [x] Bootstrap hook in `app.js`; entry script bumped to `?v=esm-v7`
- [x] API test (`tests/test_appearance.py`, 15/15 checks) against an isolated instance
- [x] UI smoke (Playwright, isolated instance): all 5 wallpapers render in light + dark,
      tile selection applies instantly with toast, Settings → Appearance tab works, no JS errors
- [ ] Manual check on the production server after restart: upload a photo, log in from two accounts
