# Core app UI shell

Status: 2026-11-15 · migration complete — every core app uses the shell; Files,
Terminal and Login keep their documented layouts. Tracked in `TODO.md` P3-15.

One visual language for every desktop window. Core apps use the **same layout and
metrics as the native package windows** (`ethos-antivirus`, `domains`,
`backup-manager`), expressed with EthOS theme tokens instead of the package-local
palettes.

| Part | File |
|---|---|
| Stylesheet | `frontend/static/css/ethos-ui.css` (linked from `index.html`) |
| Shell helper | `frontend/static/core/ui-shell.js` — `mountShell()` |
| Rule | `.github/instructions/app-ui.instructions.md` |
| Skill | `.github/skills/app-ui/SKILL.md` |
| Guard | `tests/test_app_ui.py` (in CI) |
| Package counterpart | `ethos-packages/AGENTS.md` → “Native package UI shell (mandatory)” |

## When to use which shell

- **Multi-section window (≥ 2 views)** — rail + main: Settings, Packages, Storage,
  Security, Logs, Kanban-style tools. `mountShell(..., { sections })`.
- **Single-section window** — header + panels only, no rail: Overview, Network, Processes,
  Services, SMART, Cron, Search, Shared Links.
  `mountShell(..., { sections: [{ id: 'main', label: 'Overview' }], rail: false })`.
- **Dense tools keep their own panes** — File Manager (explorer) and Terminal
  (xterm) are documented layout exceptions. They still use the tokens and the
  `.ui-btn`/`.ui-panel` vocabulary for dialogs and chrome.

## Contract

| Part | Rule |
|---|---|
| Root | `.ui-shell` — `height:100%`, flex, radial-glow background on `--bg-primary` |
| Rail | `218px`, `padding:20px 14px`, right border, brand + text-only nav + pinned foot |
| Brand | 26 px product emoji, product name, `EthOS` line (window title stays with the window manager) |
| Nav | `<button class="ui-nav-item">`, 12 px/600, `9px 11px`, radius 9, `.is-active` = accent-soft |
| Foot | `.ui-pill` (ready / starting / offline) + one 10 px note; `margin-top:auto` |
| Main | `flex:1; min-width:0; overflow-y:auto; padding:24px 28px 32px` |
| Header | `<h1>` `clamp(22px, 3.4vw, 30px)`; 12 px muted subtitle; one `role="status"` line |
| Views | `.ui-view` — flex column, `gap:14px`, `margin-top:16px` |
| Panels | `.ui-panel` — `17px 18px`, radius 15 |
| Summary | `.ui-summary` — four tiles, 9 px uppercase labels, 20 px tabular values |
| Tables | `.ui-table` — 11 px, 9 px uppercase headers, `.num` / `.actions` / `.path` |
| Buttons | `.ui-btn` — `8px 13px`, radius 9, 11 px/600; `.primary`, `.danger`, `.small` (4×9, 10 px) |
| Inputs | `.ui-input` — `9px 11px`, radius 9, 12 px (`.mono` for paths/keys) |
| States | `.ui-tag` (ok/bad/warn/info), `.ui-callout`, `.ui-empty`, `.ui-bar` |
| Responsive | ≤ 780 px: rail becomes a horizontal strip, foot note hides, grids collapse |
| Themes | Both are mandatory; only `var(--ui-*)` / `var(--surface-*)` / `var(--text-*)` tokens |
| Icons | `uiIcon(name, size)` in buttons/headers; emoji only for the brand + product identity |
| Long work | `core/progress.js`; dialogs via `core/modal.js`; never a local progress bar or modal |
| Feedback | `notify(...)` from `core/notification-center.js`; never a local toast |
| Tests | `data-testid` on every interactive element; guard in `tests/test_app_ui.py` |

## Skeleton

```js
import { mountShell } from '../core/ui-shell.js';
import { skeletonRows, errorState } from '../core/states.js';

const SECTIONS = [
    { id: 'overview', label: 'Overview', title: 'Overview', subtitle: 'At a glance.' },
    { id: 'settings', label: 'Settings', title: 'Settings', subtitle: 'Tune the service.' },
];

export function renderThingPage(body) {
    const shell = mountShell(body, {
        idPrefix: 'thing',
        title: 'Thing',
        subtitle: 'Short description of the window.',
        icon: '🧩',
        sections: SECTIONS,
        note: 'One short sentence about the data.',
    });

    shell.onSection((id) => renderSection(id));
    shell.setActive('overview');

    async function renderSection(id) {
        shell.view.innerHTML = skeletonRows(5);
        shell.setStatus('');
        try {
            // fetch + render into shell.view using .ui-panel/.ui-table
            shell.setStatus(`Updated ${new Date().toLocaleTimeString()}`);
        } catch (err) {
            shell.view.innerHTML = errorState({ detail: err.detail || err.message });
            shell.setStatus('Could not load this view', 'error');
        }
    }
}
```

## Cross-app links (`core/app-links.js`)

Core windows never park data on `window.*` to talk to each other. Use the one-shot context channel:

```js
setContext('shared-links', { path });     // before opening
if (typeof window.openApp === 'function') window.openApp('shared-links');
```

The target app calls `consumeContext('<id>')` once while rendering (`files.js`, `share-links.js`,
`settings.js` consume a section). `openAppAt(id, section)` opens an app at a rail section, and
`openPathInFiles(path)` navigates a live Files window or hands the path to the next one.

Package apps (Gallery, Editor, Photos) still read their documented `window.__fm*Ctx` contract; those
globals belong to the package API and must not be used by core apps.

## Command palette (`core/command-palette.js`)

`Ctrl+K` / `Cmd+K` opens one search over every registry app plus the Settings sections. It reads
`core/registry.js`, so a new app appears automatically; add Settings sections to `SETTINGS_SECTIONS`
in the palette when the rail grows. Styling is `.cmdk-*` in `ethos-ui.css` (tokens only), and the
terminal is excluded so `Ctrl+K` keeps working in readline.

## Theming

`ethos-ui.css` defines the shell tokens on `:root`, `:root.dark` and `:root.light`.
Light is the default (`<html class="light">`); the topbar toggle swaps the class
at runtime, and the shell follows with no app code. Never hardcode `#hex`,
`white` or `black` in an app template — use `--ui-accent`, `--ui-ok`,
`--ui-bad`, `--ui-warn`, `--ui-border`, `--text-*`, `--surface-*`.

## Verification

```bash
.venv/bin/python tests/test_app_ui.py     # static contract, runs in CI
.venv/bin/python tests/test_a11y.py
node --check frontend/static/apps/<app>.js
```

Walk the window in **light and dark**, resize to 780 px, and confirm the rail
becomes a horizontal strip. The same checklist is in the `app-ui` skill.

## Exceptions

| App | Why |
|---|---|
| `files.js` | Explorer: sidebar places + list/grid panes, not a section rail |
| `terminal.js` | xterm surface owns the viewport |
| `login.js` | full-screen overlay before the desktop exists |
| `app-drawer.js`, `fm-controls.js` | chrome/helpers, not standalone windows |

Package exception: `ethos-video` uses the documented cinema/media layout (full-bleed nav, hero,
poster shelves, bottom tab bar on phones, immersive player) — see `ethos-packages/AGENTS.md`.
Settings and popovers still use the shell vocabulary and both themes.

New apps and redesigns must not copy an exception.
