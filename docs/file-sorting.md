# File Manager — Sort Options

Sort-by control for the Files app, working in **both** grid and list views.

## Plan

- [x] Shared `_fmSortFiles(files, state)` comparator (name / size / kind / date-modified, asc/desc)
- [x] Apply sorting in `fmRenderGridView` (grid previously rendered backend name-order only)
- [x] Reuse same comparator in `fmRenderListView` (replaces inline sort; behavior preserved)
- [x] Toolbar "Sort by" popup menu (`data-testid="fm-sort-btn"`, macOS Finder style): Name, Size, Kind, Date Modified + Ascending/Descending with checkmarks; opens via `window.fmToggleSortMenu(event)`, closes on outside click / Escape
- [x] `_fmSyncSortUI(root)` keeps the sort button title in sync when sorting via list-view column headers (existing `fmSortBy`)
- [x] CSS for `.fm-sort-menu` in `index.html` (reuses `.fm-context-menu` styling)

## Notes

- Sort state lives in per-window `fmWindowStates` (`sortCol`, `sortAsc`) — persists across navigation within a window, same as before.
- Choosing a new column from the sort menu keeps the current direction (Finder behavior); clicking the active column header in list view toggles it.
- Search results keep their own Name/Location/Size table and are unaffected.
- Frontend-only change: no backend changes, no ethos-logger impact, no server restart needed — browser refresh picks up new static files.

## Status

Done 2026-07-15; toolbar restyled macOS-style 2026-07-21 (sort select + direction button replaced by the sort popup menu; grid/list became a segmented control). Changed files: `frontend/static/apps/files.js`, `frontend/static/apps/fm-controls.js`, `frontend/index.html`.
