# Built-in apps — improvement pass (2.29.0)

Cross-cutting upgrade of the eighteen core windows (everything except the Package Center
packages). Three problems were addressed: apps were islands with no drill-down, some API
capabilities were never surfaced (SMART details/trends, per-unit Logs, cron next runs), and
two windows duplicated each other. Everything below verified against the code on 2026-11-22.

## File map

| What | Where |
|---|---|
| Cross-app context/links | `frontend/static/core/app-links.js` (new) |
| Command palette | `frontend/static/core/command-palette.js` (new) + `.cmdk-*` in `frontend/static/css/ethos-ui.css` |
| Shell wiring | `frontend/static/app.js` (alias `systemd → services`, `initCommandPalette`) |
| Apps changed | `apps/overview.js`, `storage.js`, `smart.js`, `network.js`, `processes.js`, `services.js`, `cron.js`, `search.js`, `sharing.js`, `share-links.js`, `settings.js`, `files.js`, `terminal.js` |
| Shared renderer | `core/sharing-ui.js` (`openGrantApp` → `openPathInFiles`) |
| SMART store/service | `backend/services/health_monitor_service.py` (`smart_history`, v2 samples) |
| SMART router | `backend/routers/smart.py` (`/history`, `/{device}/self-test`, `/{device}/selftest-log`) |
| Cron schedule | `backend/services/cron_schedule.py` (new), `backend/routers/cron.py` (`/preview`, write validation) |
| Processes | `backend/routers/processes.py` (`ppid`, `cmdline_full`, `?signal=TERM|KILL`) |
| Network | `backend/routers/network.py` (`errors_in/out`, `drops_in/out`) |
| Tests | `tests/test_cron_schedule.py`, `tests/test_smart_history.py`, extended `tests/test_app_ui.py` |
| Removed | `frontend/static/apps/systemd.js` (merged into Service Manager) |

## Data model

`backend/data/smart_history.json` — v2, written by the health monitor:

```json
{"version": 2, "devices": {"/dev/sda": {
  "last_seen": "2026-11-22T18:00:00+00:00",
  "attrs": {"Reallocated_Sector_Ct": 2},
  "prev_attrs": {"Reallocated_Sector_Ct": 1},
  "samples": [{"ts": "…", "temp": 34, "attrs": {"Reallocated_Sector_Ct": 2}}]
}}}
```

`samples` is a ring of `_SMART_SAMPLES_MAX = 288` entries (one per health check, ~24 h at the
default 5 min interval). v1 files load unchanged; the first write upgrades the version.
Unknown/corrupt samples are skipped, never fatal.

Palette/context state is in memory only (`core/app-links.js`); nothing is persisted.

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/smart/history?hours=24&device=/dev/sda` | user | Persisted samples, no probing; `hours` clamped 1–168 |
| POST | `/api/smart/{device}/self-test` | admin | Body `{"mode": "short"\|"long"\|"abort"}`; `smartctl -t`, argument list only |
| GET | `/api/smart/{device}/selftest-log` | user | `smartctl -l selftest`, raw text |
| GET | `/api/cron/preview?schedule=…&count=3` | admin | `{ok, description, next_runs[], detail}`; invalid → `ok:false` (200) |
| GET | `/api/cron` | admin | Each job now carries `description` and `next_run` |
| POST/PUT | `/api/cron[/{id}]` | admin | A 5-field/@macro schedule is validated; 400 with the reason |
| DELETE | `/api/processes/{pid}?signal=TERM\|KILL` | admin | Default TERM; KILL documented as uncatchable |
| GET | `/api/network/interfaces` | user | Adds `errors_in/out`, `drops_in/out` |

Route order matters: `/api/smart/history` is declared **before** `/api/smart/{device}`.

## Frontend wiring

- **Service Manager** (`apps/services.js`) owns four tabs — All, Containers, Systemd, EthOS NAS.
  `openApp('systemd')` is aliased in `app.js` to `services` with `setContext('services', {tab:'systemd'})`.
  Every row has a **Logs** button → `openLogs({container})` / `openLogs({unit})`.
- **SMART** disk cards open an inline detail: attribute table with change deltas (from
  `/api/health/status.smart_trends`), 24 h temperature canvas, self-test buttons and the drive's
  self-test log. Background refresh runs every 60 s and re-renders an open panel.
- **Overview** shows a health alert strip (failed units, near-full disks, SMART failures,
  high temperature) from `/api/health/status`, plus SWAP, and every card/tile drills into the
  app that explains the number.
- **Cron** validates as you type (`/api/cron/preview`), and the table shows the human schedule and
  the next run returned by `GET /api/cron`.
- **Processes** has a detail panel (full command line, ppid, container), SIGKILL and a
  container-log link.
- **Network** polls interface counters every 5 s and updates only the rate cells; IP/MAC copy
  buttons; Firewall link for admins.
- **Search** keeps the last eight queries, sorts results (newest/name/size) and offers
  “Open folder” through `openPathInFiles`.
- **Command palette** (`Ctrl+K` / `Cmd+K`) searches apps and Settings sections; the terminal is
  excluded so `Ctrl+K` keeps working in readline.
- **Cross-app links** go through `core/app-links.js`. Package apps (Gallery, Editor, Photos) still
  read their documented `window.__fm*Ctx` globals — those are the package contract.

## Decisions & constraints

- No new dependencies: the temperature chart is a plain canvas like the Network sparkline; no QR
  library, no chart library.
- Cron **Run now** was deliberately not shipped: it would execute an arbitrary shell command from
  the UI. If it is ever added it must be a separate, explicitly confirmed admin action.
- `smartctl` may still lack permissions when the service user is unprivileged; the UI shows the
  server's error verbatim instead of a generic failure.
- Files (`apps/files.js`) was **not** split in this pass — `TODO.md` P3-06 / P3-03 still cover it.
- Mobile shell (P3-13) and RAID/UPS (F-04/F-06) remain out of scope.

## Status

- [x] Cross-app links (`core/app-links.js`) and no core `window.__fmShareCtx`
- [x] Services + System Services merged, per-row Logs
- [x] Overview alert strip, SWAP and drill-downs
- [x] Command palette (Ctrl+K)
- [x] SMART detail, trends, history chart, self-tests
- [x] Cron validation + next-run preview
- [x] Process details / SIGKILL / container logs
- [x] Network per-interface rates, copy, firewall link
- [x] Search recents, sorting, open-in-Files
- [x] Static guards (`tests/test_app_ui.py`), unit tests (`test_cron_schedule.py`, `test_smart_history.py`)
- [ ] Split `files.js` and remove inline `onclick` (TODO.md P3-06/P3-03)
- [ ] Cron “Run now” (security decision above)
- [ ] Playwright coverage for Services/SMART flows (needs an isolated instance with a test password)

## Known gaps / next steps

- The SMART self-test needs `smartctl` write access; on a hardened host add the root helper
  described in `TODO.md` F-04 before relying on it.
- `smart_history.json` keeps ~24 h; a longer window needs a coarser sampling interval or a second
  ring (documented in the store, not implemented).
- The palette lists core apps only; package apps appear after the packages expose a command list.
