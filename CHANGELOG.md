# Changelog

All notable, user-visible changes to EthOS NAS are documented here.

- Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
- Versioning: [SemVer](https://semver.org/) — one version is one `APP_VERSION` in
  `backend/config.py`, one git tag `v<APP_VERSION>` and one entry in the signed
  `releases/index.json`.
- The release ritual (bump → changelog → publish → tag) is mandatory for every push
  of runtime changes: skill `release-tag-changelog`.

## [Unreleased]

### Added

- Code Editor: **CHAT** tab that runs the `pi` coding agent in the open project
  (allowlisted agent CLIs, admin-only PTY target, own `HOME`/session/cache dirs).
- Code Editor: **Go to Definition** via `Ctrl/Cmd+Click` or `F12` — project-wide
  declaration search (`POST /api/editor/definition`), no language server needed.
- Code Editor: **Maximize Panel** — `⤢` button, double-click on the tab strip or
  View → Maximize Panel fills the whole workbench with the panel.
- Code Editor: **PROBLEMS** tab now runs a real syntax check of the active file
  (`ast.parse`, `json.loads`/JSONC, `bash -n`, `node --check`) with status-bar
  counters, line highlights and click-to-jump rows.

### Changed

- Code Editor: the bottom panel starts collapsed and the terminal PTY is only
  spawned when you open it — no shell on the welcome screen.

### Removed

- Code Editor: the OUTPUT panel tab (it only re-rendered the Logs app's App Events).

### Fixed

- Terminal: an out-of-range resize control frame is dropped instead of being typed
  into the shell/agent prompt as JSON text.

## [2.3.13] - 2026-11-18

Earlier history was not maintained as a changelog; use `git log` for releases
before this file existed.
