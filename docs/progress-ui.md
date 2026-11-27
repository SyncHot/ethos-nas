# Progress UI — one component for every long-running action

`frontend/static/core/progress.js` is the single implementation of progress
feedback in the web UI. Uploads, copy/move, delete/trash/purge, Packages
jobs and Docker engine installs all render through it — same markup, same CSS,
same accessibility attributes, same behaviour.

## API

| Call | Purpose |
|---|---|
| `startTask({ appId, title, total, detail, host, testIdPrefix, retry, onDismiss })` | Create a row; returns a handle |
| `task.update({ percent, detail, log, index, total, bytesDone, bytesTotal, status })` | Push progress |
| `task.finish('done' \| 'failed' \| 'cancelled', { percent, detail, log })` | Terminal state |
| `task.dismiss()` | Remove the row |
| `clearFinishedTasks(appId?, { globalOnly })` | Clear settled rows (panel ✓ / app cleanup) |
| `showTaskPanel()` | Re-open the floating panel |
| `getTaskStatus()` / `getAppTaskStatus(appId)` | Aggregate counters for status bars/chips |

Hosts:

- **Floating panel** (`#taskProgressPanel`, bottom-right) — default. Every app's
  running tasks stack here; the header summarises `↑ 2 · ⚠ 1 · ✓ 3`.
- **Inline host** (`host: <element>`) — same row rendered inside an app window
  (Packages banner, Docker install box).

Extra attributes on rows: `<prefix>-progress` (role=`progressbar`),
`-percent`, `-title`, `-step`, `-log`, `-error`, `-retry`, `-item`.

Events: `ethos:tasks-changed` (throttled 150 ms, immediate on status changes)
with `{running, done, failed, cancelled, total, percent, byApp}`.

## Footprint — the panel never covers an app

The floating panel is `position: fixed` above every window (z-index 9989), so it
**publishes the space it occupies** instead of lowering itself behind windows.
On `document.documentElement`:

| Signal | Values | Meaning |
|---|---|---|
| `--task-panel-h` | `0px` … `px` | Measured panel height (`0px` while the panel is hidden) |
| `--task-panel-w` | `0px` … `px` | Measured panel width |
| `data-task-panel-state` | `hidden` \| `idle` \| `active` | Not shown · finished rows only · at least one task running |

Recomputed in a `requestAnimationFrame` (throttled) when the panel shows/hides,
collapses/expands, gains/loses a row, and on `resize`.

**Idle panel collapses to a chip.** When no task is `running` and the user has
not expanded it by hand, the panel keeps only its header (`🗂 Tasks · ✓ 3`) and
hides the body — so the reserved strip shrinks back automatically. Clicking the
header toggles it (`aria-expanded`), and the override resets the next time the
panel goes idle. While a task runs the panel is always expanded: live progress
is never hidden. The `✓` (clear finished) and `×` (hide + clear) buttons keep
their semantics.

**Every app surface reserves the footprint**: `frontend/index.html` gives
`.window-body` a `padding-bottom: calc(min(var(--task-panel-h, 0px), 40vh) + 12px)`
and the phone desktop grid the same strip. That covers shell apps (`.ui-main`
scrolls its last row above the panel), Files' status bar, and Terminal / native /
iframe package windows.

The panel is anchored `8px` above the **workspace bottom** — `bottom:
calc(var(--workspace-bottom, var(--taskbar-h, 52px)) + 8px)` in
`css/progress.css` — where `--workspace-bottom` is the taskbar height in the
desktop shell (52 px) and `0px` on phones (no taskbar, full-screen windows).
That keeps the measured height exactly the clearance a window needs, and the
12 px slack in the reservation covers the 8 px gap.

Rules for new code:

- Never place interactive content underneath the panel, and never add a
  per-app/hardcoded offset for it — use `--task-panel-h`.
- Never change the panel's z-index to "fix" an overlap; publish/reserve instead.
- The 40vh clamp is deliberate (phones, short windows); don't remove it.

## Rules (enforced by `tests/test_progress_ui.py`)

1. No app builds its own progress card, floating panel or `role="progressbar"`
   markup — only `core/progress.js` does.
2. `percent: null` while running = indeterminate bar (animated).
3. Finished rows persist until the user clears/closes the panel. No timers that
   erase failures or completed work.
4. Failures show the server `detail`; repeatable actions pass `retry`.
5. One summary toast per action, not one per item.
6. Stable `testIdPrefix` per app for UI tests.

## Migration map

| Action | App id | Test id prefix | Host |
|---|---|---|---|
| Uploads | `uploads` | `upload` | floating |
| Copy / move / hard delete | `files` | `fm-op` | floating |
| Move to trash / purge | `files` | `fm-op` | floating |
| Package install/update/uninstall | `packages` | `pkg-job` | inline (`#pkgJobBanner`) |
| Docker engine install | `docker` | `docker-engine` | inline (`#engineJobBox`) |

The File Manager status-bar chip (`fm-statusbar-upload`) stays upload-specific:
it aggregates upload jobs from `upload-engine.js` (`ethos:uploads-changed`) and
opens the shared panel on click. Clearing upload rows dismisses the task, whose
`onDismiss` callback drops the job so the chip stays accurate.

## Adding a new long-running action

```js
import { startTask } from '../core/progress.js';

const task = startTask({ appId: 'myapp', title: 'Doing the thing…', total: 3, testIdPrefix: 'myapp-job' });
task.update({ index: 1, detail: 'step 2 of 3' });
task.finish('done', { percent: 100, detail: 'Finished' });
```

Polling stays in the owning app (or its service); only rendering and lifecycle
live in `core/progress.js`.
