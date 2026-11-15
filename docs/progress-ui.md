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
