---
name: app-ui
description: "Build or restyle a core EthOS desktop app so it matches the package windows (AntiVirus / Domains / Backup Manager). Use for new app windows, layout redesigns, or when an app looks inconsistent."
argument-hint: "Which core app window needs the shell?"
---

# Core app window shell

EthOS desktop apps share one product language with the native package windows.
The metrics are copied from `ethos-packages/AGENTS.md` → “Native package UI shell
(mandatory)” and `.github/skills/package-ui/SKILL.md`; the values are part of the
contract, not suggestions.

Read `docs/app-ui.md` for the full table. Stylesheet:
`frontend/static/css/ethos-ui.css`. Helper: `frontend/static/core/ui-shell.js`.

## Procedure

1. **Decide the shape.** Two or more sections → rail. One section → header plus
   panels only (`rail: false`). Files/Terminal/Login keep their layouts; do not
   extend the exception.
2. **Mount the shell** inside the renderer’s `container` (a `.window-body`):

   ```js
   const shell = mountShell(container, {
       idPrefix: 'storage',
       title: 'Storage',
       subtitle: 'Disks, partitions and removable media.',
       icon: '💾',
       sections: [
           { id: 'disks', label: 'Disks', title: 'Physical Disks' },
           { id: 'partitions', label: 'Partitions', title: 'Mounted Partitions' },
       ],
       note: 'SMART status lives in the SMART Health app.',
   });
   shell.onSection(renderSection);
   shell.setActive('disks');
   ```

3. **Render into `shell.view`** with the shared building blocks: `.ui-panel`,
   `.ui-summary`, `.ui-table`, `.ui-toolbar`, `.ui-row`, `.ui-kv`, `.ui-tag`,
   `.ui-btn`, `.ui-input`, `.ui-fields`, `.ui-callout`, `.ui-empty`, `.ui-bar`.
   Reuse `core/states.js` for loading/empty/error.
4. **Wire state**: `shell.setStatus(text, 'error' | 'warn' | 'ok')` for the one
   status line; `shell.setPill('ready' | 'starting' | 'offline', label)` in the
   rail foot; `shell.setHeader()` only when a section needs its own heading.
5. **Behaviour**: `apiRequest`/`readErrorDetail` from `core/api.js`, `logError`
   from `core/logger.js` in every catch, `startTask` for long work, `showConfirm`
   from `core/modal.js`, `notify` from `core/notification-center.js`.
6. **Accessibility**: nav buttons get `aria-current` from the shell; every input
   needs a `<label>`; keep `data-testid` on actions and views.

## Definition of done

- [ ] Rail is 218 px with a text-only nav, active item, status pill + note in the foot.
- [ ] Header uses `.ui-head h1`, a 12 px subtitle and exactly one status line.
- [ ] Content uses `.ui-panel`/`.ui-summary`/`.ui-table`/`.ui-tag`; no `glass-card`.
- [ ] No `<style>` injection, no `#hex`/`white`/`black` colours, no `app-tab`.
- [ ] Long work uses `core/progress.js`; confirms use `core/modal.js`.
- [ ] Light **and** dark screenshots match AntiVirus/Backup Manager.
- [ ] ≤ 780 px: rail is a horizontal strip, grids collapse to one column.
- [ ] `tests/test_app_ui.py`, `tests/test_a11y.py`, `node --check` pass.

## Do not

- Do not invent a second visual language, a different rail width or new radii.
- Do not copy another app’s content — copy the layout and the tokens.
- Do not add `window.*` handlers; use module listeners.
- Do not extend the Files/Terminal/Login exceptions to new apps.
