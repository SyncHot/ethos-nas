# Git Diff Tool (IntelliJ-style side-by-side diff with selectable changes)

A dedicated **Git Diff** app (⎇, `git-diff`) — a 3-pane IntelliJ-style diff viewer:

```
┌ Toolbar: [repo path ▾ recents] [Load] [Refresh]        branch badge ┐
├──────────────┬────────────────────────────┬────────────────────────┤
│ File list    │  OLD  (index | HEAD)       │  NEW  (worktree|index)  │
│ Staged (n)   │  line numbers + content    │  line numbers + content │
│  M a.py      │  red = deleted lines       │  green = added lines    │
│ Changes (n)  │  blue = selected hunk      │  blue = selected hunk   │
│  M b.py      │                            │                          │
│  ?? c.py     │                            │                          │
└──────────────┴────────────────────────────┴────────────────────────┘
├ status bar: path · side · hunk count · [Stage/Unstage selected] [all] [clear] [stage/unstage file] ┤
```

- **Left pane** — changed files from `GET /api/editor/git/status`, grouped **Staged** (HEAD→index)
  and **Changes** (index→worktree), like the editor's SCM panel.
- **Middle/right panes** — side-by-side old|new with line numbers, scroll-synced, changed lines
  highlighted (red/green).
- **Select changes** (IntelliJ "Select Changes" mode, always on): click any changed line to toggle
  its whole hunk (blue tint). Then:
  - **Changes** group → **Stage selected** (partial staging, like `git add -p`)
  - **Staged** group → **Unstage selected** (reject selected hunks)
  - Whole-file **Stage file / Unstage file** also available.

## Semantics (which diff is shown / applied)

| Group | Panes show | Unified diff | Action | git mechanics |
|---|---|---|---|---|
| Changes | index (old) vs worktree (new) | `git diff -- path` | Stage selected | `git apply --cached` with only the selected hunks |
| Staged | HEAD (old) vs index (new) | `git diff --cached -- path` | Unstage selected | `git apply --cached -R` with only the selected hunks |

Edge cases (verified in a scratch repo, git 2.53):
- **Untracked** (`??`): no diff exists → whole file rendered as added, one implicit hunk;
  "Stage" = `git add -- path` (whole file).
- **Worktree-deleted** (` D`): diff has `+++ /dev/null` hunk; staging it removes the file from the
  index; unstaging restores it.
- **Binary**: detected via the `Binary files …` marker in the unified diff or NUL bytes in the
  first 8 KB of either side → panes show a notice, hunk selection disabled, whole-file
  stage/unstage still works.
- **Stale selection**: client sends the hunk count it saw; backend regenerates the diff and
  returns 409 if the count changed (file modified since the view was loaded).
- **Size cap**: 200 KB per side → `too_large`, panes show a notice (diff still lists the file).

## API (in `backend/routers/editor.py`, under `/api/editor/git/…`)

| Endpoint | Body/Query | Returns |
|---|---|---|
| `GET /diff-view?repo=&path=&side=changes\|staged` | — | `{path, side, old, new, unified, untracked, binary, too_large}` |
| `POST /stage-hunks` | `{repo, path, hunks:[int], total:int}` | `{ok, whole_file}` |
| `POST /unstage-hunks` | `{repo, path, hunks:[int], total:int}` | `{ok}` |

`hunks` = 0-based positions of `@@` blocks in `unified`, in order. The backend re-runs the diff
at apply time, keeps header + selected hunks, and pipes the patch to `git apply --cached`
(stdin, never a shell). All failures log via the `ethos-nas.editor` logger.

## Connection to the Code Editor (both directions)

- **Editor → Git Diff**: each file row in the editor's Source Control panel has a **⧉** button
  ("Open in Git Diff") that calls `window.openGitDiffFor(repo, path, side)` — opens (or focuses)
  the Git Diff app with that file preselected, on the matching side (`staged` for Staged rows,
  `changes` for Changes rows). Implemented in `apps/git-diff.js` (`openGitDiffFor` export;
  handles both "window already open" via a live handle and "fresh boot" via a pending-open
  variable consumed by `renderGitDiffPage`).
- **Git Diff → Editor**: the status bar's **✎ Editor** button calls
  `window.openEditorForPath(root + '/' + path)` — opens the file in a Code Editor tab (which
  then shows the inline git gutter markers for the same changes).
- `openGitDiffFor` is exposed on `window` from `app.js` (same pattern as `openEditorForPath`).

## Status

- [x] Plan + scratch-repo verification of `git apply --cached` hunk mechanics
- [x] Backend: `diff-view`, `stage-hunks`, `unstage-hunks`
- [x] Frontend: `frontend/static/apps/git-diff.js` (3-pane, hunk selection, scroll sync)
- [x] Registered: `core/registry.js`, `window-manager.js` APP_INFO, `app.js` RENDERERS
- [x] Editor integration: ⧉ button in editor SCM rows + ✎ Editor button in Git Diff status bar
- [x] Verified end-to-end on isolated instance (:9002): 26/26 endpoint checks passed
  (partial stage/unstage, MM files, untracked, binary, stale-409, empty-400)
- [ ] Production restart (user action: Settings → Restart)

- [x] Plan + scratch-repo verification of `git apply --cached` hunk mechanics
- [x] Backend: `diff-view`, `stage-hunks`, `unstage-hunks`
- [x] Frontend: `frontend/static/apps/git-diff.js` (3-pane, hunk selection, scroll sync)
- [x] Registered: `core/registry.js`, `window-manager.js` APP_INFO, `app.js` RENDERERS
- [x] Verified end-to-end on isolated instance (:9002)
- [ ] Production restart (user action: Settings → Restart)
