# Packages QA — cross-app audit & loop

How the 25 community packages are checked as **one product**: catalog hygiene, the native window
contract, port ownership, theme and copy consistency, and what each packaged app actually renders.
`ethos-pack lint` answers "can this catalog be published?"; this document and `scripts/ethos-qa`
answer "do the apps agree with each other?".

Verified 2026-11-22 against `ethos-packages` @ `2186e5b` (25 packages) and core `2.28.3`.

## File map

| What | Where |
|---|---|
| Static + API sweep | `scripts/ethos-qa` (`static`, `api`) |
| UI sweep (Playwright, isolated instance) | `tests/test_packages_qa_ui.py` |
| Proxy 503 contract | `tests/test_package_proxy_error.py` |
| Publish gate (unchanged) | `scripts/ethos-pack build\|lint\|sign\|verify` |
| Window contract | `ethos-packages/.github/skills/package-ui/SKILL.md` |

## Run it

```bash
cd ~/apps/compose/ethos-nas
# 1. offline: manifest/catalog/JS contract for every package
.venv/bin/python scripts/ethos-qa static --repo ~/apps/compose/ethos-packages --live

# 2. every native window on a throwaway instance (no Docker, no services)
ETHOS_QA_SHOTS=/tmp/qa-shots .venv/bin/python tests/test_packages_qa_ui.py

# 3. a live NAS, GETs only (needs a session)
.venv/bin/python scripts/ethos-qa api --base http://127.0.0.1:9000 \
  --password-env ETHOS_TEST_PASSWORD
```

`--live` adds read-only host checks (`ss`, `docker ps`); `ETHOS_QA_THEME=dark` re-runs the UI sweep
in the dark palette, and `ETHOS_QA_CLICK_NAV=0` keeps it to the first screen (by default it clicks
every rail/tab view of every window). Neither tool installs, starts, stops or uninstalls anything.

Findings are `error` (contract broken), `warn` (defect or drift) or `info` (needs a human eye — for
example a listening port whose owner cannot be read without root).

Rules: `S01` identity/icon · `S02` icon artwork · `S03` declared files on disk · `S04` min core
version · `S05` UI/healthcheck contract · `S06` port ownership · `S07–S11` forbidden JS patterns ·
`S12` container healthchecks · `S13` module self-containment · `S15` test hooks · `S16` palette
scheme · `S17` catalog copy · `S18` language · `A01–A06` live API/health/logs.

## Matrix (2026-11-22)

| Package | Version | UI | Port | Theme scheme | data-testid | onclick= |
|---|---|---|---|---|---|---|
| `backup-manager` | 1.0.3 | native | — | own tokens (26) + dark | 0 | 0 |
| `domains` | 1.2.2 | native | 8093 | own tokens (28) + dark | 15 | 0 |
| `download-manager` | 1.1.1 | native | — | own tokens (26) + dark | 49 | 0 |
| `ethos-antivirus` | 2.1.6 | native | 8092 | own tokens (28) + dark | 0 | 0 |
| `ethos-books` | 1.2.1 | native | 8089 | own tokens (21) + dark | 67 | 0 |
| `ethos-cleaner` | 1.0.1 | native | — | host tokens (46) | 13 | 0 |
| `ethos-docker` | 1.0.5 | native | — | own tokens (21) + dark | 52 | 0 |
| `ethos-editor` | 1.0.1 | native | — | literal palette (80 colours) | 226 | 0 |
| `ethos-git` | 1.0.1 | window | 3005 | — | 0 | 0 |
| `ethos-git-diff` | 1.0.1 | native | — | host tokens (18) | 34 | 0 |
| `ethos-kanban` | 1.0.3 | native | 8098 | host tokens (34) | 36 | 0 |
| `ethos-media-enhance` | 1.0.2 | engine | 8097 | — | 0 | 0 |
| `ethos-monitor` | 1.0.1 | window | 3091 | — | 0 | 0 |
| `ethos-music` | 1.0.3 | native | — | own tokens (17) + dark | 7 | 0 |
| `ethos-music-engine` | 1.0.1 | engine | 8096 | — | 0 | 0 |
| `ethos-photos` | 1.0.3 | native | — | host tokens (152) | 136 | 61 |
| `ethos-rss` | 1.0.1 | window | 8085 | — | 0 | 0 |
| `ethos-shares` | 1.3.0 | native | — | own tokens (23) + dark | 37 | 0 |
| `ethos-sticky-notes` | 2.0.0 | native | 8099 | own tokens (42) + dark | 38 | 0 |
| `ethos-surveillance` | 2.0.1 | native | — | own tokens (24) + dark | 28 | 0 |
| `ethos-sync` | 1.0.2 | window | 8384 | — | 0 | 0 |
| `ethos-video` | 2.3.1 | native | — | own tokens (16) + dark | 83 | 0 |
| `ethos-welcome` | 1.0.4 | window | 8090 | — | 0 | 0 |
| `tic-tac-toe` | 2.1.1 | native | — | own tokens (15) + dark | 0 | 0 |
| `vmm` | 1.2.1 | native | 8095 | own tokens (16) + dark | 0 | 0 |

UI sweep: **18/18 native windows render**, no uncaught page error, no unexpected console error,
every window painted in light **and** dark, no iframe for native modules, and — after `ethos-music`
1.0.3 — **every window exposes a stable hook** on its interactive elements. The sweep then walks
**every rail/tab view** (66 switches across the 18 windows): no view throws, no view renders blank.
A `503` is reported as `info` — on a throwaway instance the package services and Samba are absent.

## P1 — visible defect, fix next

### ☑ P1-01 · A dead package service leaks Python internals into the window
- **Evidence.** With the AntiVirus service stopped the window shows in red:
  `Could not load settings: <urlopen error [Errno 111] Connection refused>`.
- **Cause.** `package_ui_proxy` raised `PackageError(f"Package service is unavailable: {exc}", 503)`
  and the native UIs print `detail` verbatim.
- **Fix.** `backend/routers/packages.py` now logs the real exception and answers
  `"The <id> service is not responding. Start it in Package Center and try again."`
  Guard: `tests/test_package_proxy_error.py`.

### ☑ P1-02 · No package collides with a host service any more
`ethos-qa static --live` compares every published host port with `docker ps`. It found three, all
now gone:

| Package | Wanted | Was taken by | Now |
|---|---|---|---|
| `ethos-git` | 3000 | a compose project | 3005 (`2186e5b`) |
| `ethos-monitor` | 3001 | `wawalocal-user-1` | **3091** (`3e66992`) |
| `ethos-rss` | 8080 | `wawalocal-api-1` | **8085** (`3e66992`) |

`vmm` was a **false positive in the tool**: its console uses `"8094"`, a container-only mapping
where Docker assigns the host port, so it can never collide. `_ports()` now skips single-value and
range mappings. Because 2.28.5 remaps a busy compose port anyway, the remaining risk was that the
manifest/README/changelog promised a port the user did not get — moving the two packages fixes that
at the source. Venv/native services cannot be remapped, so they must never declare a host port that
is commonly used (they are the `info` rows in the sweep).

### ☐ P1-03 · The Music and Books surfaces are Polish; everything else is English
The gap is wider than one package: `ethos-music` (185 non-English characters in the module) *and*
the core PWAs `/music` (`frontend/static/music/music.js`, 94 lines) and `/books`
(`frontend/static/books/books.js` 52, `frontend/books.html` 18, `offline.js` 14) are written in
Polish, while `ethos-books`, the shell and the other 23 packages are English. A session shows
"Dobry wieczór" next to the core's "Tasks / Analyzing /home".
- **Fix.** One decision for the media surfaces: translate to English, or add a language switch driven
  by the core locale. Do it per surface (Music package, `/music`, `/books`) so each stays reviewable;
  `S18` in `ethos-qa` keeps the packages honest meanwhile.

### ☑ P1-04 · Stable test hooks on the native windows
The rule in `package-ui/SKILL.md` accepts a `data-testid` **or** a stable action attribute — the
first version of this sweep only counted `data-testid` and over-reported. Measuring properly:
`backup-manager` 8/8, `ethos-antivirus` 19/22 rendered (all 19 source buttons carry a `data-*` —
several are valueless attributes such as `data-scan-custom`, which the checker initially missed),
`vmm` 13/13 and `domains`/`download-manager`/… all hooked, while **`ethos-music` had 0/6** (plain
`class` only).
- **Fix.** `ethos-music` 1.0.3 adds `em-nav-N`, `em-mini-*` and `em-fp-*` hooks; the sweep and
  `ethos-qa` now share the same "hook" definition and both report zero findings. AntiVirus's three
  unhooked buttons are covered by its `data-scan`/`data-tab` attributes.

### ☐ P1-05 · `ethos-editor` is a package that needs the core's stylesheet
The 200 KB module ships **no CSS**: all 406 `.ed-*` rules live in the core
`frontend/index.html` (200 KB) and its palette is 80 literal colours with no variables. That breaks
the "native module must be self-contained" rule, keeps the core's design system from being extracted
(`TODO` P3-10) and makes the editor invisible to any other host.
- **Fix.** Move the `.ed-*` block into the module (tokenised, light + dark), then delete it from
  `index.html`. Related: P3-04 (split the module).

## P2 — inconsistency / polish

- **Catalog copy.** Fixed for the five offenders (`vmm`, `ethos-books`, `download-manager`,
  `ethos-surveillance`, `ethos-sync`, `b1bc084`): the newest note is one sentence now, because that
  is what Package Center shows on an update. The first measurement of this was wrong — the checker
  parsed "the rest of the changelog" as the newest entry, so the totals (2.7–3.5 KB) looked like one
  entry; the real entries were 240–430 chars. Long **descriptions** are informational: the catalog
  table shows 150 chars, the dialog shows all of it.
- **Theme scheme drift.** `ethos-cleaner`, `ethos-git-diff`, `ethos-kanban`, `ethos-photos` style
  themselves with the core's `--text-1/--surface-*` variables; `ethos-editor` uses literals. All of
  them follow light/dark correctly, but a redesign has to touch a different vocabulary in each app.
- **Inline handlers.** `ethos-kanban` 1.0.3 replaced its 12 `onclick`/`onchange`/`ondblclick`
  attributes with one delegated listener (`data-kanban-act` / `data-kanban-arg`, ids escaped as HTML
  attributes instead of JavaScript inside an attribute). `ethos-photos` still has 61 (`TODO` P3-03);
  the same pattern applies, and its handlers can be verified the same way — stub the
  `window._kanban*`-style targets and assert the dispatcher calls them with the right id.
- **localStorage in the shared origin.** 7 packages store under `ethos_*`/`em_*` keys; verify every
  key carries the package prefix so two apps can never collide.
- **Opening Code Editor spawns a host shell.** Rendering the window immediately starts a host
  terminal session (`Starting host terminal…` → `marcin@nasos:~$`), before the user asks for one.
  The QA sweep therefore opens a real shell on the host; keep that in mind when running it.
- **No screenshots.** Every manifest ships `screenshots: []`, so Package Center cards and the detail
  dialog have no previews. The UI sweep already produces them — publish a few per package.
- **Legacy exceptions** stay as documented: `ethos-welcome` (static page), `tic-tac-toe` (single
  card), `ethos-video` (cinema). They are audited for correctness only, never restyled.

## Verifying a refactor without the service

A package service is not running on a throwaway instance, so a "create a board" flow cannot be
exercised. Instead, stub the action targets and click the real markup:
`window._kanbanNewBoard = (...a) => log.push(...)` for every action, then click each button and
assert the call and its argument. This proves the dispatcher, the escaping and the event type
(`click` vs `dblclick` vs `change`) without any backend.

## Waivers

- Engine packages (`ethos-media-enhance`, `ethos-music-engine`) have no window; the JS contract rules
  do not apply. Their `python.entry` is a service, not a UI.
- `tic-tac-toe` and `ethos-welcome` may skip the shell checks.
- A `503` from `ctx.proxy` in the UI sweep is expected: the isolated instance runs no package
  services. Only uncaught errors and non-proxy console errors fail the sweep.

## Loop

1. Run `ethos-qa static` and `tests/test_packages_qa_ui.py`; both write JSON to `/tmp`.
2. Fix the P1 findings in `ethos-packages` (patch bump + one-line changelog + `build/sign/verify/readme`)
   or in the core (test + version bump + signed index + tag).
3. Re-run both sweeps; a finding only closes when the tool stops reporting it and the manual check in
   the matrix above still holds.
4. New P2 rows go to `TODO.md`, not into this list, once the P1 set is empty.
