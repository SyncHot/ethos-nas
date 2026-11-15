# Code Editor — Pro Git (VS Code Source Control parity)

Extend the editor's Source Control panel (Phases 1–10 of `docs/code-editor.md`)
with the full VS Code git toolkit: **clone, fetch, branches, remotes, stash,
discard, merge**. Commit / stage / pull / push already exist.

## Status
- [x] Feature doc + plan
- [x] Backend: 19 new endpoints in `backend/routers/editor.py` (clone, fetch,
      branches list/create/checkout/delete/rename, remotes list/add/remove/
      set-url, upstream, stash list/push/apply/pop/drop/clear, discard, merge,
      merge/abort) + `merging` flag in `GET /git/status`
- [x] Frontend: collapsible BRANCHES / REMOTES / STASH sections in the SCM
      panel, per-file **Discard**, Fetch button + `⋯` menu (fetch / merge /
      stash), merge-in-progress banner with Abort, welcome-screen
      **Clone Repository…** dialog, status-bar branch chip opens a branch
      picker dropdown (checkout / create)
- [x] CSS in `frontend/index.html` (`.ed-scm-sec-*`, `.ed-scm-merge-warn`,
      branch dropdown)
- [x] Tests: `tests/test_editor.py` Part C (API: branches, remotes, fetch,
      upstream, stash, discard, merge, clone) + Part B UI assertions
      (sections render, branch menu, clone button)
- [x] E2E verified on isolated instance
- [ ] Human: restart the `ethos-nas` service to deploy on :9000

## Design

### Backend — new endpoints in `backend/routers/editor.py`

Same rules as the existing git endpoints: subprocess arg lists only (never
`shell=True`), `GIT_TRUST_ARGS` + `GIT_CONFIG_GLOBAL` env, cwd=repo root,
paths realpath-checked + passed after `--`, git failures → 400 (validation) /
502 (git error) or `{ok:false, detail}` for network ops (pull/push/fetch/
merge — a conflict or auth failure is *data*, not an HTTP error).

| Method & path | Purpose |
|---|---|
| `POST /git/clone` `{url, path, branch?}` | `git clone [-b branch] url path`; path must not exist; runs in a worker thread (`asyncio.to_thread`) with 300 s timeout so a big clone can't freeze the event loop; on success upserts recents → `{ok, path, detail}` |
| `POST /git/fetch` | `git fetch --all` (60 s, worker thread) → `{ok, detail}` |
| `GET /git/branches?repo=` | `{current, branches:[{name,upstream,hash,subject}], remote_branches:[…]}` via `git branch --format` + `git branch -r` |
| `POST /git/branch` `{repo, name, check_out?}` | create from HEAD; name validated with `git check-ref-format --branch`; optional `git switch` |
| `POST /git/checkout` `{repo, ref}` | `git switch`; a remote-tracking ref (`origin/x`) creates/uses the local tracking branch `x`; other refs (branch/tag/commit) switch as-is (detached allowed) |
| `POST /git/branch/delete` `{repo, name}` | `git branch -d` (refuses unmerged); deleting the current branch → 400 |
| `POST /git/branch/rename` `{repo, name, new_name}` | `git branch -m` |
| `GET /git/remotes?repo=` | `{remotes:[{name, fetch_url, push_url}]}` from `git remote -v` |
| `POST /git/remote/add` `{repo, name, url}` | name validated by regex (`[A-Za-z0-9][A-Za-z0-9._-]*`, no `.lock` suffix, no `..`) |
| `POST /git/remote/remove` `{repo, name}` | `git remote remove` |
| `POST /git/remote/set-url` `{repo, name, url}` | `git remote set-url` |
| `POST /git/upstream` `{repo, remote, branch?}` | `git branch --set-upstream-to=remote/branch [branch]` (needs the remote-tracking ref to exist, i.e. after a fetch/push) |
| `GET /git/stash?repo=` | `{stashes:[{index, ref, date, message}]}` via `git stash list --format` |
| `POST /git/stash` `{repo, message?, include_untracked?}` | `git stash push [-m msg] [--include-untracked]` |
| `POST /git/stash/apply` / `…/pop` / `…/drop` `{repo, index}` | `git stash <op> stash@{index}` (index 0-based, passed as one argv element) |
| `POST /git/stash/clear` | `git stash clear` |
| `POST /git/discard` `{repo, paths[]}` | restore paths to HEAD state: `git checkout HEAD --` tracked, `git rm -f --` staged additions, `git clean -f --` untracked; unmerged (`U`) paths → 400 "resolve conflicts first" |
| `POST /git/merge` `{repo, branch}` | `git merge <branch>` (120 s, worker thread) → `{ok, detail}`; a conflicted merge returns `ok:false` and leaves the repo in merge state |
| `POST /git/merge/abort` | `git merge --abort` → `{ok, detail}` |

`GET /git/status` gains `merging: bool` (MERGE_HEAD present) so the panel can
show the merge banner. Network/long ops (clone, fetch, merge) run via
`asyncio.to_thread` — the existing 15 s `_git()` helper is untouched.

### Frontend — `frontend/static/apps/editor.js`

- **SCM header**: `⊞` stage-all, `↓` pull, `↑` push, `⟳` refresh, new `⋯`
  menu → *Fetch / Merge branch… / Abort merge (only while merging) / Save to
  stash / Clear stash*.
- **Collapsible sections** between CHANGES and HISTORY (caret toggles, all
  collapsed by default):
  - **BRANCHES (n)** — local rows (`✓` current) with hover actions
    checkout `⎇` / rename `✎` / delete `🗑`; *REMOTE* sub-group of
    remote-tracking branches with checkout; header `＋` = create+switch
    (prompt).
  - **REMOTES (n)** — rows `name  url` with hover actions set-url `✎` /
    remove `🗑`; header `＋` = add remote (name+url form modal); a
    *Track upstream…* row appears when the current branch has no upstream.
  - **STASH (n)** — rows `#i  message` with hover actions Apply / Pop / Drop;
    header `Save` = stash push (message prompt + untracked confirm);
    *Clear all* row.
- **Per-file rows** (STAGED/CHANGES) gain a `🗑` Discard button (confirm
  dialog → `POST /git/discard`).
- **Merge banner** — when `status.merging`, a warning strip at the top of the
  SCM body: *Merge in progress — resolve conflicts, then Commit* + **Abort**
  button.
- **Status-bar branch chip** is now clickable → dropdown (VS Code branch
  picker): local branches (✓ current), remote branches, *＋ Create new
  branch…* — reuses the tree context menu with an `up` positioning flag.
- **Welcome screen** gains **⧉ Clone Repository…** — form modal (URL, target
  parent folder + Browse, optional branch); project name derived from the
  URL; on success the project opens directly.
- Sections fetch their data lazily — only when expanded, so the 30 s badge
  poll / save-triggered refreshes stay cheap (status only).

### Security notes
- No new trust surface: same subprocess model, `--` path guards, name
  validation via `git check-ref-format` / regex. Clone URL is a single argv
  element (no shell); clone target must not exist and is realpath-checked.
- `discard` is the one destructive op: untracked files are `git clean -f`'d —
  the frontend always asks for confirmation first, and only repo-relative,
  `_safe_paths`-validated paths are ever passed.

## Test plan (`tests/test_editor.py`)

- **Part C (API)** against the same temp repo + bare remote: branches
  list/create/checkout/rename/delete (+ delete-current 400), remotes
  list/add/set-url + invalid-name 400, fetch, upstream tracking, stash
  push/list/pop/drop/clear (+ untracked), discard (tracked modify → clean,
  untracked → removed), merge (fast-forward ok) + merge/abort-when-idle,
  checkout of a remote-tracking ref recreates the local branch, clone of the
  bare remote into a fresh dir (is_repo + recents entry).
- **Part B (UI)** additions: BRANCHES/REMOTES/STASH section headers render,
  expanding BRANCHES lists `main` with ✓, status-bar branch chip opens the
  picker menu, welcome screen shows the Clone button.

## Verification Log
- 2026-07-09 — E2E on isolated instance `:9006` (`ETHOS_DATA_DIR=/tmp/ethos-pro-test-data`):
  `tests/test_editor.py` → **PASS, 96/96** (Part A 27, Part C 47, Part B 22).
  Bugs caught by Part C and fixed in the same pass:
  1. `GET /git/remotes` parsed `git remote -v` as three tab-separated
     columns; the real format is `<name>\t<url> (fetch|push)` — one tab, kind
     space-suffixed. Fixed the parser (fetch_url/push_url now correct).
  2. `_check_remote_name` allowed `..` (regex permits dots), so
     `git remote add bad..name` surfaced as a 502 from git instead of a 400.
     Now rejected up front.
  Test-side fixes (test bugs, not app bugs): branch-delete ordering (must
  leave the branch before deleting it), stash apply→pop on an
  `--include-untracked` stash (pop after apply fails on the already-restored
  untracked files — split into apply→drop and a fresh push→pop scenario).
