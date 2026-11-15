# Photos — audit & fixes

Audit of the Photos app (frontend `frontend/static/apps/gallery.js`, backend
`backend/routers/gallery.py` + `backend/routers/moments.py`) after the UI was
reported as effectively unusable. Findings, fixes and how to verify them.

## Symptoms found

- The grid showed **"Loading photos…" forever**, even though tiles were already
  rendered underneath it (the placeholder was appended to, never removed).
- Clicking a sub-folder chip inside a folder listed the same folder again, so
  nested folders could never be opened.
- Camera filter + text search always returned **0 results** (both values were
  glued into one `q` substring).
- "All Photos" did nothing while in Calendar / Favorites / Album / Camera /
  Moments (early-return compared only `_currentFolder`, which those views reset).
- Calendar opened on the current month — often empty — with no way to see that
  the library simply had no photos that month.
- Private section: no folder navigation, Private count hidden until the PIN was
  entered, and "folder" was never sent with `view=private` requests.
- Triggering a rescan (Add folder / Rescan now / restored from trash) never
  refreshed the grid — the app looked stuck on the old/empty library.
- Trashing a private item stored the plain filename in the trash metadata, so
  restoring it wrote the ciphertext under `photo.jpg` and orphaned the file.
- Calendar mini-thumbnails for videos requested `size=48`, which
  `/api/files/video-thumbnail` rejects (422).
- Moments ignored the library's "Include videos" setting and its tiles/lightbox
  lost the favourite star and video duration.
- Timeline / Calendar / Cameras counts ignored the active search, camera,
  favourite and album filters.

## Fixes

Frontend (`frontend/static/apps/gallery.js`)

- Paginated fetches are serialized through a promise chain; results for stale
  views are still dropped by `_gen`. The loading placeholder is cleared before
  tiles are appended.
- Shared `_viewParams()` builds the query for items/months/calendar/cameras
  (folder + q + camera + day + fav + album + private session), so every view
  uses the same scope. Camera is now its own parameter.
- Facets are folder-relative ("In this folder" = media directly in the current
  folder) and the parent chip stops at the library root.
- Calendar jumps to the newest month that contains media.
- Private section supports folder chips and shows its count without unlocking
  the PIN (`/rescan-status` now returns `private_items`).
- Leaving any special view via "All Photos" works again.
- Rescan completion is detected by watching `scanned_at` (a fast incremental
  scan could finish between 5 s status polls) and refreshes grid + sidebar.
- Lightbox `next` pulls the next page when it reaches the end of the loaded
  page, so browsing is no longer capped at 60 items.
- Thumb URLs are clamped to ≥ 64 px (video-thumbnail API limit); private-thumb
  failures retry like queued video thumbs.

Backend (`backend/routers/gallery.py`, `backend/routers/moments.py`, `backend/routers/files.py`)

- `_top_folders(items, roots, base)`: facets are children of the current folder
  (or of the library roots at the top level); implemented without per-item
  `abspath`/`relpath` (≈11 ms for 10k items instead of ≈87 ms).
- `camera` query parameter on `/items`, `/months`, `/calendar`; shared
  `_apply_view_filters()` applies q + camera + album + favourites consistently.
- `/rescan-status` returns `private_items` and `favorites` counts.
- Private items trashed as `<name>.ethos.enc` now store that encrypted path in
  the trash metadata, so restore is lossless; `files.py` triggers an
  incremental gallery rescan after restoring media.
- Moments honours `include_videos` and exposes `favorite`, `duration`, `camera`
  in its public item payload.

## Verification

Isolated instance (never shares production data):

```bash
ETHOS_PORT=9010 ETHOS_DATA_DIR=/tmp/ethos-photos-test \
  nohup .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 9010 &
```

Checked end-to-end with a fixture library (`AlbumA/Sub1`, `AlbumA/Sub2`,
`AlbumB`, EXIF camera tags, a 1 s video, 130 extra photos for paging):

- Grid: no lingering "Loading photos…", 60 tiles → lightbox `next` at 60 loads
  the next page (`61 / 120`), rescan adds new files automatically (6 → 7).
- Facets: root → `AlbumA` → `Sub1` → back; private section mirrors this.
- `camera=TestCam TC100&q=p` returns the 3 matching photos (was 0).
- Calendar opens on a month with media; video thumbnails return 200 (size 64).
- Moments hides videos when `include_videos=false`, shows favourite stars.
- Private item: make private → trash → restore `.ethos.enc` → auto rescan →
  visible again; make public → file restored.

## UI cleanup (second pass)

Reported after the first fixes: the sidebar was cluttered and folders seemed not
to load completely.

- Removed the **CAMERAS** list from the sidebar (it was a wall of device names;
  free-text search still matches camera make/model via the API).
- Added a **FOLDERS** section header next to ALBUMS.
- Toolbar rebuilt for narrow windows: search takes a row, then one compact row
  with sort, grid/timeline toggle, Select, ⚙ and the status; no more orphaned
  gear/status rows.
- Status now shows `loaded / total` (`90 / 9607 items`), and a **Load more
  (N of M)** button sits under the grid, so paging is explicit even when the
  IntersectionObserver sentinel is missed. Page size is 90.
- Folder facets are smaller, prefixed with 📁, and the redundant
  “In this folder” chip is gone.
- Fixed light-theme contrast: sidebar active/inactive colors, tile name overlay,
  calendar day borders, folder-picker cards and breadcrumb now use theme tokens.

## Status

- [x] Frontend fixes
- [x] Backend fixes
- [x] Isolated end-to-end verification
- [ ] Production restart (`Settings → Restart`) required to load the backend
      changes; the frontend fixes are active after a browser reload.
