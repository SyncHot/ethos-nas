# Code Editor — Projects & Git (VS Code-style)

Extend the existing single-file CodeMirror editor into a project-aware editor with
git integration and a recent-projects list, desktop-app style.

## Status
- [x] Phase 1 — Audit of current `apps/editor.js` + files API
- [x] Phase 2 — Proposal & scoping (this file)
- [x] Phase 3 — Backend: `/api/editor` router (recents store + git ops)
- [x] Phase 4 — Frontend: welcome screen, project tree, git panel
- [x] Phase 5 — E2E test (`tests/test_editor.py`) — 32/32 pass on isolated :9006
- [x] Phase 6 — Commit & push
- [x] Phase 7 — VS Code-style file/project creation: `POST /projects/new` (optional git init),
      welcome-screen *New Project…* dialog, tree toolbar (new file/folder/refresh) + row context menu
      (new here / rename / delete). E2E extended → 41/41 pass on isolated :9003
- [x] Phase 8 — VS Code-style bottom panel: the embedded terminal moved out of the top-right
      toolbar into a persistent bottom panel (resize grip + body + tab bar, tab bar always
      visible, body collapsible; `Ctrl+\`` still toggles). The tab bar is the extension point for
      future panel tools (each gets a `.ed-panel-view` + tab). The terminal tab gains a status dot
      and a **shell-target picker** (host / sudo / `user:<name>`) mirroring the Terminal app —
      backend `/ws/terminal/{target}` + `GET /api/terminal/options` already supported this; no
      backend change. Selecting a target live-restarts the PTY session.
- [x] Phase 9 — Full VS Code workbench rework (Dark+ palette): activity bar (Explorer /
      Search / Source Control / Run & Debug / Extensions), dynamic sidebar, tabbed editor with
      breadcrumbs, bottom panel with **split xterm terminals** + Output/Problems/Debug Console,
      and a blue status bar (branch + sync state, Ln/Col, UTF-8, language). Source Control
      sidebar: commit box, staged/unstaged groups with per-file stage/unstage, Pull/Push,
      recent history. Open files get **inline git diff gutter markers** backed by the new
      `GET /api/editor/git/diff` endpoint (unified diff vs HEAD; untracked files → whole file
      reads as added). Terminal sessions start inside the project root (`?cwd=` on
      `/ws/terminal/{target}`, added in Phase 8 backend) and the target picker is populated
      from `/api/terminal/options` + `/api/docker/containers` like the Terminal app.
      E2E extended for the workbench DOM → 43/43 pass on isolated :9006 (see Verification Log).
- [x] Phase 10 — **Stage All** in the Source Control panel: `⊞` button in the SCM header
      stages every file in the CHANGES group at once (one `POST /git/stage` with all
      unstaged paths, incl. untracked) and refreshes the panel. No new endpoint — reuses
      the existing multi-path stage API. Also added the missing ethos logging to the
      file-level `git_stage`/`git_unstage` endpoints (error on failure, info on success).
- [x] Phase 11 — **Pro Git** (VS Code Source Control parity): 19 new endpoints —
      clone, fetch, branches (list/create/checkout/delete/rename), remotes
      (list/add/remove/set-url), upstream, stash (list/push/apply/pop/drop/clear),
      discard, merge + merge/abort (`merging` flag added to `GET /git/status`).
      Frontend: collapsible BRANCHES / REMOTES / STASH sections in the SCM panel,
      per-file Discard, Fetch button + `⋯` menu (fetch / merge / stash), merge-in-progress
      banner with Abort, welcome-screen **Clone Repository…** dialog, status-bar branch
      chip opens a branch picker. Long ops (clone/fetch/merge) run via
      `asyncio.to_thread`. Full design in `docs/editor-git-pro.md`. E2E extended →
      96/96 pass on isolated :9006 (Part A 27, Part C 47, Part B 22).
- [x] Phase 12 — **Menu bar + resizable Explorer sidebar** (VS Code parity):
  top menu bar in both single-file and project modes — File (New File / Open File… /
  Open Folder… / New Project… / Clone Repository… / Save / Save All / Close Tab /
  Close Project), Edit (Undo/Redo/Cut/Copy/Paste/Select All, Find in Files, Toggle
  Word Wrap), View (Toggle Sidebar, Toggle Bottom Panel, Toggle Word Wrap), Terminal
  (New Split / Restart / Shell Target… / Hide-Show), Help (Keyboard Shortcuts, About).
  Menus are evaluated live on open (enabled-state reflects the current view), support
  mouse + full keyboard navigation (arrows/Enter/Escape, ←/→ walk the menus). The
  Explorer sidebar is now drag-resizable via a grip on its right edge (min 180 px,
  max 50 % of the workbench, width persisted in `localStorage`, CodeMirror instances
  refreshed during the drag; hidden while collapsed). New window-scoped shortcuts:
  `Ctrl+B` toggle sidebar, `Ctrl+W` close tab (unsaved guard), `Alt+Z` word wrap,
  `Ctrl+S` fallback when focus is outside CodeMirror. Welcome-screen *New Project…*
  and *Clone Repository…* dialogs were extracted (`_newProjectModal` / `_cloneRepoModal`)
  so the File menu reuses them. E2E extended → 101/101 pass on isolated :9006.
- [x] Phase 13 — **Full correctness pass + UX upgrade** (panel lifecycle, CRLF,
  save-conflict detection, context-menu bug, content search, Quick Open, settings,
  Markdown preview). Details below.
- [x] Phase 14 — **UX/UI polish + editor ergonomics** (file-type badges, custom
  Find/Replace bar, tab context menu, active-row highlight, language picker,
  window title with dirty state, font zoom, terminal focus fix, dead UI removed).
  Details below.
- [x] Phase 15 — **Outline, Command Palette, Compare with HEAD, terminal fixes**
  (symbol navigation, 30+ command palette, git diff from a tab, PTY job control,
  default `host` terminal target without a password prompt, cwd fix). Details below.

## Phase 15 — navigation + terminal UX (2026-11-09)

### Outline (symbol navigator)
- New **Outline** activity view (between Search and Source Control) listing the
  active file's symbols: Python `class`/`def`, JS/TS `function`/`class`/`interface`/
  `type`/arrow functions, Go, Rust, Java/Kotlin/C#/Swift/Dart/PHP, CSS selectors,
  Markdown headings (nesting by `#` level) and top-level JSON keys. Regex-based, no
  extra dependency; live refresh 400 ms after edits; click → jump to the line.
  `_outlineSymbols()` / `_refreshOutline()`.

### Command Palette (`Ctrl+Shift+P`)
- Overlay listing every enabled menu command plus Quick Open, Find/Replace and
  “Compare Active File with HEAD”; filter-as-you-type, ↑/↓/Enter/Esc. Built from
  `_menuDefs()` so it never drifts from the menu bar. ~34 commands in a project.

### Git
- Tab context menu → **Compare with HEAD** (opens the Git Diff app for the file,
  `changes` side). Disabled outside a repo.

### Terminal (editor + Terminal app)
- **PTY job control fixed** in `backend/routers/terminal.py`: the child now sets
  `TIOCSCTTY` in `preexec_fn`, so `su - <name>` sessions no longer print
  `cannot set terminal process group …` / `no job control in this shell`.
- **Default target is `host`** (`frontend/static/core/terminal-target.js`): opening
  the editor / logging in no longer auto-starts `su` and prompts for a password.
  User targets are an explicit picker choice, labelled `(password)`; the server
  account is shown as e.g. `marcin (server)` / `ethos-nas`.
- **Editor terminal cwd fixed**: `_currentCwdFor()` now accepts the shell element
  itself, so the terminal starts inside the open project (`cwd=/tmp/project` in the
  backend log) instead of the server's working directory.
- Terminal focus is never stolen on startup/reconnect (already in Phase 14);
  server-account labels update in the target button and pane header.

### Verification (isolated :9008, own DATA_DIR)
- `tests/test_editor.py` → **all pass**, including new checks: Outline lists symbols,
  Command Palette opens (>10 commands).
- Raw WebSocket check: `host` and `user:marcin` sessions show no job-control errors
  after the TIOCSCTTY fix; backend log shows `cwd=/tmp/uxdemo2` for the editor session.

## Phase 14 — UX/UI polish (2026-11-08)

### Visual / explorer
- **File-type badges** (colored monograms: `PY`, `TSX`, `MD`, `{}`, `IMG`, `ZIP`,
  `LIC`, …) in the Explorer, tabs, Quick Open and content-search results instead of a
  single generic file icon. Folder icons are amber; arbitrary folder nesting keeps
  correct indentation (depth-based padding, no longer capped at 3 levels).
- Explorer toolbar uses clean inline SVG icons (new file, new folder, refresh,
  **Collapse All**, **Reveal Active File**); the root row is a clickable section that
  collapses the tree. The active file's row is highlighted (`.ed-tree-row.selected`).
- Diff gutter toned down: only thin gutter bars (no full-line green/red background);
  untracked files show a single hint marker instead of painting every line.
- Tab strip: per-file badge, middle-click close, active-tab auto-scroll, bordered
  **Save** button; right-click menu (Save / Copy Path / Reveal in Explorer / Close /
  Close Others / Close All with unsaved-changes guards).
- Window title and taskbar label show `Code Editor — <project>`, plus `●` while any
  buffer is dirty. Breadcrumb action buttons (Preview, Wrap, Save) are now proper
  buttons.
- Removed dead UI: Run & Debug / Extensions activity views and the Debug Console
  panel tab (they had no functionality).

### Editing ergonomics
- **Custom Find/Replace bar** (replaces the CodeMirror dialog): `Ctrl+F`, `Ctrl+H`,
  `F3` / `Shift+F3`, match counter (`n of N`), wrap-around, Match Case (`Aa`), regex
  (`.*`), Replace, Replace All, `Esc` closes. Search results are selected and scrolled
  into view; the bar lives per tab.
- Status-bar **language picker** (click the language): switch syntax highlighting for
  the current file (`Plain Text`…`Python`…).
- Font zoom `Ctrl+=` / `Ctrl+-` / `Ctrl+0` (persisted); view shortcuts
  `Ctrl+Shift+E` / `F` / `G` for Explorer / Search / Source Control.
- **Terminal no longer steals focus** when a project opens or the PTY reconnects —
  typing goes to the editor; the terminal focuses on click or when explicitly opened.
- Empty editor pane shows keyboard hints (`Ctrl+P`, `Ctrl+F`, `` Ctrl+` ``).
- Status-bar Problems item opens the Problems panel.

### Verification (isolated :9008, own DATA_DIR)
- `tests/test_editor.py` → **all pass** (Part A + Part B), including new UI checks:
  file-type badges render, active row highlighted, Find bar counter, Escape closes
  Find, tab context menu has Close Others/Close All, window title names the project,
  language picker lists modes and updates the status bar.
- Visual pass via Playwright screenshots at 1000×700 and 1024×768.

## Phase 13 — correctness pass (2026-11-08)

### Bugs fixed
- **Bottom panel was destroyed on Close Project / switching projects.** The terminal
  panel is moved into `.ed-maincol` while a project is open; `content.replaceChildren()`
  then removed it with the old project DOM, so the terminal (and the whole panel) was
  lost. `_parkPanel()` now re-parents the panel back under `.ed-shell` before any
  content swap, so the terminal session survives project switches. `_teardownProject()`
  also releases tab CodeMirror state / resize observers / autosave timers instead of
  leaking them.
- **Tree context-menu actions were dead.** The `oncontextmenu` handler parameter `e`
  shadowed the loop's entry variable, making `e.path` `undefined` — New File Here /
  New Folder Here / Rename / Delete / Open threw `TypeError`. Handler params renamed to
  `ev`; covered by a Part B rename test.
- **CRLF files were silently converted to LF.** The loader normalized `\r\n`→`\n`
  before CodeMirror was told the line separator, so CRLF was always lost on save and
  the status bar always showed LF. `_decodeBuffer()` now detects the dominant EOL,
  normalizes mixed endings to it, mounts CodeMirror with the matching `lineSeparator`,
  and saves round-trip the original bytes.
- **Saves could silently clobber external edits.** `PUT /api/files/content` already
  supported conflict detection via `mtime`, but the editor never sent it. The editor
  now keeps the load-time mtime, sends it on save, and shows a **File Changed on Disk**
  dialog (Reload from Disk / Overwrite / Cancel) on HTTP 409.
- **Stale buffers after git checkout/pull/discard/merge/stash.** `_reloadCleanTabs()`
  re-stats open tabs on every git refresh and reloads clean tabs whose file changed on
  disk; dirty buffers are left untouched (saving them surfaces the conflict dialog).
- **Rename did not retarget open tabs.** Renaming a file/folder now rewrites tab paths
  (and breadcrumbs/status bar) via `_retargetTabs()`.
- **Deleted recent projects** opened a half-broken workbench. A missing/non-dir recent
  entry now shows an error, removes itself from recents, and returns to the welcome screen.
- **`showFormModal` ignored checkbox state** (unchecked boxes returned `"on"`), so the
  New Project dialog always ran `git init`. Checkboxes now return booleans; textareas
  are collected too.
- Tab strip scrolls the active tab into view; middle-click closes tabs; context menus
  and the terminal target picker close on Escape.

### New features
- **Content search** — `GET /api/editor/search?repo=&q=&regex=&case_sensitive=&max_results=`
  searches file contents in a worker thread (skips `.git`, `node_modules`, `venv`,
  `dist`, `build`, caches; text files ≤2 MB; caps at 5000 files / requested matches;
  invalid regex → 400). The Search sidebar has **Content / Names** modes plus `Aa`
  and `.*` toggles, grouped per-file results with line numbers, and clicking a match
  opens the file at that line/column.
- **Quick Open** (`Ctrl+P`): VS Code-style file picker (arrows + Enter, Esc).
- **Editor Settings** (View menu): font size, tab size, auto-close brackets, word
  wrap, auto-save after 1.2 s — persisted in `localStorage` (`ethos_editor_settings`)
  and applied to all open editors.
- **Markdown preview**: Preview/Editor toggle for `.md` files (single-file + project
  tabs), rendered with marked + DOMPurify (CDN), live-updating while open.
- **Go to line** (`Ctrl+G` / `Alt+G`) via the jump-to-line addon; the CodeMirror
  find/replace dialog is styled for the Dark+ palette. **Reload from Disk** added to
  the File menu.

### Verification (isolated :9006, own DATA_DIR)
- `tests/test_editor.py` → **all pass** (Part A now covers content search + fresh /
  stale-mtime saves; Part B covers inline new-file input and rename-from-context-menu).
- Ad-hoc Playwright checks: bottom panel survives Close Project and project reopen;
  CRLF detected + byte-exact round-trip; Markdown preview renders/closes; Quick Open
  opens files; content-search result jumps to the right line; settings persist and
  apply; the conflict dialog appears on external modification and Overwrite writes
  the buffer.

## Phase 11 design (Pro Git)

See `docs/editor-git-pro.md` for the full endpoint table, frontend design, security
notes, and verification log. Summary: clone/fetch/merge run in worker threads
(`asyncio.to_thread`, 300/60/120 s); network & merge failures return
`{ok:false, detail}` (data, not HTTP errors); validation errors (bad branch/remote
name, path escape, existing clone target, deleting the current branch, discarding
unmerged paths) return 400/409. Branch names validated with
`git check-ref-format --branch`; remote names by regex (no `.lock`, no `..`);
checkout of a remote-tracking ref (`origin/x`) creates/uses the local `x`.

## Phase 9 design (VS Code workbench)

- **Palette** (VS Code Dark+): bg `#1E1E1E`, sidebar `#252526`, activity bar `#333333`, status
  bar `#007ACC`, accent `#007ACC`. CSS lives in the `Code Editor App` block in
  `frontend/index.html` (all `.ed-*` classes preserved/extended).
- **Layout** (`.ed-workbench`): `.ed-activitybar` (48px, icons with active indicator) →
  `.ed-sidebar` (260px, resizable, active view: explorer/search/scm) → `.ed-maincol`
  (`.ed-tabs` + `.ed-breadcrumbs` + `.ed-editors` + `.ed-panel`) → `.ed-statusbar` (22px).
  Single-file mode keeps the old shell (breadcrumbs + editor + status bar, no sidebar).
- **Explorer**: lazy tree (dirs expand on first click, `GET /api/files?path=`), toolbar
  (new file / new folder / refresh), row context menu (new here / rename / delete) — same
  files-API mutations as before, new files open in a tab immediately.
- **Search**: `GET /api/files/search?q=&path=<root>` (name match, 500 cap); results click
  opens the file in a tab.
- **Source Control**: `GET /git/status` → staged (index column) / unstaged (worktree column)
  file rows with status letter (M/A/D/R/U/?) + per-row stage/unstage; commit box + Commit
  button; Pull/Push; history from `GET /git/log`. Refreshes after saves, git ops, and file
  mutations.
- **Diff gutter**: when a tab's file is dirty or listed in git status, fetch
  `GET /api/editor/git/diff?repo=&path=` and map the unified diff onto CodeMirror line
  gutter marks — green bar = added line, red bar = deleted line (untracked → all added).
  Markers re-apply on every diff refresh; `CodeMirror` line widget via `setGutterMarker`
  on a dedicated `ed-diff-gutter`.
- **Bottom panel**: tab bar always visible (Terminal / Problems / Output / Debug Console),
  body collapsible via active-tab click or `Ctrl+\``. Terminal view = split panes
  (`+`/`×` per pane, drag divider); each pane is an xterm.js session with its own target
  (host / sudo / `user:<name>` / `container:<name>`, picker from `/api/terminal/options`
  + running containers), status dot, cwd display, exponential-backoff reconnect
  (4001 auth / 4002 target / 4003 bad-cwd are terminal states). Output/Problems/Debug
  Console read the unified log store (`/api/logs?limit=&level=&search=`).
- **Status bar**: branch chip + ahead/behind + change count (left), Ln/Col (live from
  CodeMirror cursor), spaces/UTF-8/language (right).
- **Tabs**: dirty dot, middle-click / × close with unsaved-changes guard (Save / Don't Save /
  Cancel overlay), `Ctrl+S` saves the active tab, breadcrumb bar shows `root / … / file`.
- **Backend** — one new endpoint in `backend/routers/editor.py`:
  `GET /git/diff?repo=&path=` → `{path, diff, untracked}`. Untracked detection via
  `git status --porcelain=v1 -z -- <path>` (`??` prefix); tracked files get
  `git diff HEAD --unified=3 -- <path>`. Reuses `_repo_root`/`_is_repo`/`_safe_paths`/
  `_git` (realpath prefix check + `--` option-injection guard). Git failure → 502 with
  logged error. Untracked → empty diff + `untracked:true` (client renders the whole file
  as added).

## Phase 8 design (bottom panel + terminal target picker)

- **Layout** (`.ed-shell` → column): `.ed-content` (flex:1) + `.ed-panel` (flex:none).
  `.ed-panel` = `.ed-panel-resize` (5px drag strip, persists height like before) →
  `.ed-panel-body` (flex:1, `hidden` when collapsed) → `.ed-panel-tabs` (30px, always visible).
  Collapsed height ≈ 32px (grip + tab bar); expanded height = stored `TERM_HEIGHT_KEY` value.
- **Terminal tab**: status dot (`connected`/`connecting`/`reconnecting`/`error`/`disconnected`),
  status text, cwd (right-aligned, follows project root / file dir as before), target button
  (label = `host` / `sudo` / username), `⟳` restart, `⌃` collapse. Clicking the active tab
  toggles the panel (VS Code behavior); `Ctrl+\`` unchanged.
- **Target picker**: dropdown reusing `.ed-tree-menu` styles, populated from
  `GET /api/terminal/options` (eligible login users UID ≥ 1000 + sudo availability, same
  source as the Terminal app's picker). Selecting a target sets `st.target` and restarts the
  WS session against `/ws/terminal/<target>?cwd=…` (4002 "target rejected" already handled).
  Startup line is target-aware: `Starting host shell in /path…` / `Starting shell as marcin…`.
- Removed: `⌨ Terminal` file-toolbar button, `⌨` project-header button, `×` kill button
  (restart `⟳` covers it), `_termKillAndHide`, `_applyTermHeight`.

## Current state (audit)

`frontend/static/apps/editor.js` (~3400 lines): VS Code-style workbench (Phase 9) —
top menu bar (File/Edit/View/Terminal/Help, Phase 12), activity bar, drag-resizable
sidebar (Explorer tree / Search / Source Control; grip on the right edge, Phase 12),
tabbed CodeMirror editors with breadcrumbs + unsaved-changes close guard, bottom panel
(split xterm terminals with target picker, Output/Problems), status bar. Phase 13/14
added per-type file badges, a custom Find/Replace bar, tab context menus, a
status-bar language picker, Markdown preview, content search, Quick Open and editor
settings. Single-file mode (Files → Edit) keeps the slim shell via
`openEditorForPath` (menu bar + bottom panel still apply). File I/O via
`/api/files/*` (`GET /api/files?path=` listing, `raw`, `stat`, `touch`, `mkdir`, `rename`,
`DELETE /delete` + `GET /ops/{op_id}` poll, `search`); project/git state via `/api/editor/*`
(content search: `GET /api/editor/search`).
CodeMirror 5.65.16 (CDN): modes loaded per-need in `index.html` (JSX, GFM, TOML,
Dockerfile, Lua, Perl, R, Swift, Dart added with the workbench); languages without a CM5
mode (ini/conf/cfg/env, graphql, gitignore) fall back to plain text. The Dark+ theme is
plain CSS (`.cm-s-ed-vscode` in `index.html`) — CM5 has no JS theme API.

## Design

### Backend — `backend/routers/editor.py`, mounted `/api/editor` (JWT like all /api)

**Recent projects** — JSON store `<DATA_DIR>/editor.json`:
`{version: 1, recents: [{path, name, opened_at}]}` newest first, cap 12. Thread-safe
(lock + atomic tmp-rename write), same pattern as `share_links_service`.

| Method & path | Purpose |
|---|---|
| `GET /projects/recent` | `{recents:[…]}` |
| `POST /projects/recent` `{path}` | must be an existing dir; upsert to top (dedupe by path) → returns recents |
| `DELETE /projects/recent?path=` | remove one entry |
| `POST /projects/new` `{path, git_init=false}` | creates the directory (`os.makedirs`), 409 if it exists, optional `git init`; upserts into recents → `{ok, path, name, is_repo}`. A failed `git init` degrades to a plain folder (warning logged) — the project itself still succeeds |

**Git** — subprocess arg lists only, never `shell=True`, 15 s timeout, cwd=repo.
All endpoints take the repo root as a parameter; paths are validated to stay inside
the repo (realpath prefix check) and passed after `--`. Git failures return
`{ok:false, detail}` or 400/404 — never an unhandled 500.

| Method & path | Purpose |
|---|---|
| `GET /git/status?repo=` | `{is_repo, branch, ahead, behind, files:[{path,status}]}` (porcelain v1; ahead/behind vs upstream, null when no upstream) |
| `POST /git/stage` `{repo, paths[]}` | `git add -- <paths>` |
| `POST /git/unstage` `{repo, paths[]}` | `git restore --staged -- <paths>` |
| `POST /git/commit` `{repo, message}` | commits **staged** changes; 400 when nothing staged → `{hash, subject}` |
| `GET /git/log?repo=&limit=` | last N commits `{hash, author, date, subject}` (unit-separator formatted) |
| `POST /git/pull` / `POST /git/push` `{repo}` | `{ok, detail}` — network/auth failures are data, not errors |

### Frontend — `apps/editor.js` rework

- **Welcome screen** (app opened without context): *Open Folder…* (`pickFolder` →
  POST recents → project view) + *New Project…* dialog (project name, parent folder with
  Browse via `pickFolder`, “initialize git repository” checkbox → `POST /projects/new`
  → opens the new project), recent projects list (name, path, relative time, ×
  to remove), and the existing single-file path prompt below.
- **Project view**: header bar (project name + git branch chip when repo is detected)
  · left pane: lazy file tree (`GET /api/files?path=`, dirs expand on click, files open
  in the main pane). The tree sits under a VS Code-style toolbar — project title,
  *new file* / *new folder* (act on the root) and *refresh*. Right-clicking any row opens
  a context menu: directories get *New File Here* / *New Folder Here*, every entry gets
  *Rename…* and *Delete* (confirm dialog). Mutations reuse the files API (`touch`,
  `mkdir`, `rename`, DELETE) — no new filesystem logic in the editor router. After each
  mutation the affected listing re-renders, a collapsed target dir auto-expands so the
  new item is visible, new files open immediately in the pane (VS Code parity), and the
  git panel refreshes. Deleting something that’s open resets its pane to the empty state.
  · main pane: the existing CodeMirror single-file UI (toolbar with
  Save/dirty dot, status bar) rendered into an inner container.
- **Git panel** (right side when `is_repo`): branch + ahead/behind, changed-files list
  with per-row stage/unstage, commit message box + Commit button, Pull/Push buttons,
  recent commits log. Refreshes after every git op and file save; manual refresh button.
- Single-file mode (Files → Edit) keeps working unchanged via `openEditorForPath`.
- Files context menu: *Open as Project…* on folders (`window.__fmProjectCtx`).

### Security notes

- Git runs with the server user's credentials — same trust model as the rest of the app
  (OS-level access control, no allowlist by design). No shell anywhere.
- `repo` param must be an existing directory; every path argument is realpath-checked
  against the repo root and passed after `--` to block option injection (`-foo`).

## Test plan (`tests/test_editor.py`, isolated instance)

Part A — API: recents upsert/dedupe/cap/remove; git endpoints against a **temp repo the
test creates** (git init → commit → modify → status shows M → stage → commit → log grows
→ unstage flow → pull/push on local-path remote); non-repo dir → `is_repo:false`;
path-escape attempt rejected.

Part B — UI (Playwright): open editor app → welcome screen with recents renders →
open a temp folder as project → tree lists files → click file → editor loads content →
git panel shows branch + changed file after a modification → commit via panel works →
toolbar *new file* prompt creates the file (row appears, opens in pane) → right-click
context menu offers Rename/Delete.

Part A also covers `POST /projects/new`: dir created + recents upsert, `git_init` flag
produces a real repo (`is_repo:true`), existing path → 409.

## Verification Log
- 2026-11-04 — Phase 12 (menu bar + sidebar resize) on isolated instance (:9006, own
  DATA_DIR): `tests/test_editor.py` → **101/101** (Part A 27, Part C 47, Part B 27 —
  new UI checks: menubar renders 5 menus, File menu contents, Edit menu contents,
  sidebar drag widens the sidebar by 80 px). Frontend-only change — no backend edits,
  no restart needed for the isolated run; production picked it up on the next page
  load (static files are served from disk).
- 2026-07-09 — Phase 11 (Pro Git) on isolated instance (:9006, own DATA_DIR):
  `tests/test_editor.py` → **96/96** (Part A 27, Part C 47 new, Part B 22 incl. 6 new
  UI checks: clone button, BRANCHES/REMOTES/STASH sections, branch picker). Part C
  caught two backend bugs, fixed in the same pass: `GET /git/remotes` mis-parsed
  `git remote -v` (format is `<name>\t<url> (fetch|push)`, not three tab columns) and
  `_check_remote_name` accepted `..` (git rejects → 502 instead of 400). See
  `docs/editor-git-pro.md` → Verification Log.
- 2026-07-11 — Phase 10 (Stage All) on isolated instance (:9006, own DATA_DIR):
  `tests/test_editor.py` Part A 24/24 · Part B 16/16 (all green). Also fixed a stale
  Part B locator broken by the git-diff feature (fbefafe): the SCM row gained a second
  button (⧉ open-diff), so `git_row.locator("button")` violated strict mode → now
  targets `button[title="Stage"]`. Test harness hardening: `ETHOS_TEST_USER`/
  `ETHOS_TEST_PASS` env overrides + http(s)-only scheme guard on `api()`.
- 2026-10-19 — Phase 8/9 verification + fixes on isolated instance (:9006, own DATA_DIR):
  `tests/test_editor.py` Part A 27/27 · Part B 16/16 (welcome + recent row, project view,
  tree, file open in pane, branch chip, SCM stage/unstage, commit → clean tree + history,
  new-file prompt → row + pane, context menu, no JS errors). Regressions found & fixed:
  tree listed via nonexistent `GET /api/files/list` (404) → now `GET /api/files`; new file
  used `PUT /content` (existing files only) → `POST /touch`; rename used `POST /move`
  `{from,to}` → `PUT /rename {path,new_name}`; delete used `POST /delete {path}` →
  `DELETE /delete {paths:[…]}` + `GET /ops/{op_id}` poll; `CodeMirror.defineTheme` (not a
  CM5 5.65 API — threw on every open) removed in favour of `.cm-s-ed-vscode` CSS token
  colors; 9 missing mode scripts added to `index.html` (jsx, gfm, toml, dockerfile, lua,
  perl, r, swift, dart) and unmapped modes (ini, graphql, gitignore) → `mode: null`.
  Test: Phase 9 selectors (side views, SCM rows, branch chip, history), history wait for
  the separate `/git/log` fetch, and the new-file `prompt()` answered from a
  `page.on("dialog")` listener — `expect_event("dialog")` + click deadlocks in sync
  Playwright because `prompt()` blocks the renderer the click's post-dispatch check waits on.
- 2026-10-15 — Phase 7 on isolated instance (:9003, own DATA_DIR): `tests/test_editor.py`
  Part A 27/27 (incl. new-project endpoint: create + recents-on-top, git_init → real repo,
  existing path → 409) · Part B 14/14 (incl. toolbar new-file prompt → row appears + opens
  in pane, right-click context menu with Rename/Delete). Note: :9001 is unusable on this
  host — a Docker container maps it; use another free port for isolated runs.
- 2026-09-26 — `tests/test_editor.py` on isolated instance (:9006, own DATA_DIR):
  Part A 21/21 (recents add/dedupe/remove/400; git status clean→dirty, stage/unstage
  round-trip, commit + empty-commit 400, log growth, path-escape & option-injection
  rejected, non-repo detection, push/pull vs local bare remote) · Part B 11/11
  (welcome screen, seeded recent row, project view, tree → file open in pane,
  branch chip, modified-file row, Stage→Unstage flip, commit via panel → clean tree
  + log entry, no page JS errors). Production restarted on :9000.
- Note: `git push` auto-setups upstream when exactly one remote exists (VS Code parity);
  plain-push failure with "upstream" in stderr triggers the retry.
