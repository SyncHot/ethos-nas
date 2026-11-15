---
description: "Mandatory core app window shell: every desktop app uses core/ui-shell.js + ethos-ui.css and the package-app layout/metrics. No local styles, no hardcoded colours."
applyTo: "frontend/static/apps/**/*.js, frontend/static/core/ui-shell.js"
---

# App window UI rule (mandatory)

EthOS has **one** window language: the native package shell. Core apps must look
and behave like `ethos-antivirus`, `domains` and `backup-manager`. The full
contract, metrics and skeleton live in `docs/app-ui.md`; the procedure is the
`app-ui` skill.

## Build the window

- Use `mountShell(container, opts)` from `core/ui-shell.js`. Multi-section windows
  get the 218 px rail; single-section windows pass `rail: false`.
- Fill `shell.view` with `.ui-panel`, `.ui-summary`, `.ui-table`, `.ui-toolbar`,
  `.ui-tag`, `.ui-empty`, `.ui-callout`, `.ui-bar` from
  `frontend/static/css/ethos-ui.css`.
- Use `shell.setActive/setStatus/setPill/setHeader` for section state and the one
  status line. Never set the window title (the window manager owns it).
- Loading/empty/error states come from `core/states.js`; long work from
  `core/progress.js`; confirms from `core/modal.js`; feedback from
  `core/notification-center.js`.

## Never

- Never inject `<style>`/`createElement('style')` or add a second CSS file per app.
- Never hardcode colours (`#hex`, `white`, `black`) in app templates — use
  `var(--ui-*)`, `var(--surface-*)`, `var(--text-*)`, `var(--border)`.
- Never use `.glass-card` or `.app-tab` in app windows (legacy, being removed).
- Never add a rail to a single-pane app, and never copy the exceptions listed in
  `docs/app-ui.md` (Files, Terminal, Login).
- Never introduce a new `window.*` global for handlers; use module listeners.
- Never ship dark-only or light-only styling — both themes must pass.

## Verify

`tests/test_app_ui.py` (static, in CI), `tests/test_a11y.py`, `node --check`,
then walk the window in light **and** dark and at ≤ 780 px width.
