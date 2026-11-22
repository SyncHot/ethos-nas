# Download providers — Spec & AI Handoff

One extensible provider registry for the Downloads engine: torrent debrid services
(Real-Debrid / AllDebrid / TorBox) and hoster-link multihosters (AllDebrid / tb7.pl)
are described in one place, configured in two independent slots, and consumed by the
router, `/api/downloads/*` and the download-manager package UI. Verified against the
code on 2026-11-22.

## File map

| What | Where |
|---|---|
| Provider registry (specs, capabilities, credentials, dispatch) | `backend/services/download_providers.py` |
| tb7.pl client (login, unlock, account, hosters) | `backend/services/tb7.py` |
| Existing debrid clients (RD / AD / TorBox) | `backend/services/debrid.py` |
| Config schema, migration and slot helpers | `backend/services/download_config.py` |
| Router: routing, pump, worker, `/providers`, `/verify` | `backend/routers/downloads.py` |
| Transfer engine (optional provider headers) | `backend/services/download_transfer.py` |
| Task persistence (strips transient provider headers) | `backend/services/download_tasks.py` |
| Base URL override for tests | `backend/config.py` — `Settings.TB7_BASE_URL` |
| Package UI (separate repo) | `~/apps/compose/ethos-packages/download-manager/frontend/app.js` |
| Tests | `tests/test_download_providers.py`, `tests/test_download_providers_api.py`, `tests/test_download_config.py`, `tests/test_debrid_service.py` |
| Mount | `backend/main.py` — the existing `/api/downloads` router |

## Provider model

A `ProviderSpec` declares:

- `id`, `label`, `kind` (`debrid` | `multihoster`), `homepage`, `description`;
- `credentials`: ordered `CredentialField(name, label, secret, kind, placeholder)` —
  values live under `debrid.<name>` in the config and are masked in every API response;
- `capabilities`: `torrents` and/or `link_unlock`;
- `hoster_match`: `none`, `any` (AllDebrid unlocks any host) or `list` (tb7.pl routes
  only hosts from its `/jdhostingi.txt` list).

| Provider | Kind | Torrents | Link unlock | Hoster match |
|---|---|---|---|---|
| Real-Debrid | debrid | yes | no | none |
| AllDebrid | debrid | yes | yes | any |
| TorBox | debrid | yes | no | none |
| tb7.pl | multihoster | no | yes | list |

The config has **two independent slots**, so a torrent debrid and a multihoster can
be active at the same time:

- `debrid.provider` — torrent slot (magnets, `.torrent` uploads, watch folders);
- `debrid.link_provider` — hoster-link slot (premium file-host URLs).

`torrent_provider(cfg)` / `link_provider(cfg)` return `(id, credentials)` only when
the configured id supports that capability.

## Data model

`downloads_config.json` (json_store version **9**):

| Field | Meaning |
|---|---|
| `debrid.provider` | `""` \| `realdebrid` \| `alldebrid` \| `torbox` |
| `debrid.link_provider` | `""` \| `alldebrid` \| `tb7` |
| `debrid.realdebrid_api_token` | Real-Debrid API token (secret) |
| `debrid.alldebrid_api_key` | AllDebrid API key (secret) |
| `debrid.torbox_api_key` | TorBox API key (secret) |
| `debrid.tb7_username` | tb7.pl login (not secret) |
| `debrid.tb7_password` | tb7.pl password (secret) |

Migration v8 → v9: `link_provider` becomes `alldebrid` when `provider` was
`alldebrid` (preserves the old routing), otherwise empty; the tb7 fields default to
empty strings.

Transport-only task fields (never persisted; `download_tasks.write()` strips them):
`_debrid_key` (legacy primary secret) and per-file `_link_url_headers` (e.g. a tb7
session `Cookie`). The resolved `_link_url` is still persisted so a restart can
resume without re-unlocking.

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/api/downloads/providers` | JWT | Provider catalogue: labels, kinds, capabilities, eligible slots, credential fields (`set` + masked secret value), hoster list, cached account summary (admin only). Never returns a secret. |
| POST | `/api/downloads/verify` | admin | Body `{provider, key?}` (legacy single secret) or `{provider, credentials:{…}}`. Masked/blank secrets verify the stored value. Returns `{ok, provider, account?}`. 401 = rejected credentials, 429 = provider rate limit, 502 = unreachable. |
| GET | `/api/downloads/config` | JWT | `debrid` block with every secret masked. |
| POST | `/api/downloads/config` | admin for engine fields | `debrid.provider` / `debrid.link_provider` are validated against capabilities; credential fields are merged (masked/absent keeps the stored value). |
| POST | `/api/downloads/tasks` | JWT | `magnet:` → torrent slot; http(s) → hoster-link slot when `host_match` passes, else the built-in HTTP engine. |

Task kinds are unchanged: `http`, `debrid`, `debrid_link` (`debrid_link` now means
"resolved by the configured link provider", not "AllDebrid only").

## tb7.pl client

Protocol used by `backend/services/tb7.py` (clean-room; observed public flow, no
GPL code copied from pyload):

1. `GET /login` (cookie jar + hidden form fields) → `POST /login` with
   `login` + `password`; success is detected by the "Wyloguj" session marker. A
   rejected login becomes `ProviderHTTPError(401)`.
2. `POST /mojekonto/sciagaj` `step=1&content=<url>` → page with the file name
   (`div.name`), size (`Rozmiar:` span) and the confirmation form.
3. `POST /mojekonto/sciagaj` `step=2&0=on` → page with `<a href="…">Pobierz</a>`,
   the direct link.
4. Account summary from the logged-in home page: "Dostęp Premium ważny do …" and
   "Pozostały Limit Premium …" (cached 5 min).
5. Supported hosters from `GET /jdhostingi.txt` (normalised, deduplicated, cached
   12 h, 5 min retry on failure, static fallback if unreachable).

Error phrases are mapped to English task errors (inactive link, unsupported hoster,
limit exceeded, premium inactive, rejected login). The session cookie is attached
only when the generated link points back at tb7 itself — never sent to a third-party
hoster. `Referer` is always sent. The `stop_event` is checked between steps.

`ETHOS_TB7_BASE` (`Settings.TB7_BASE_URL`) lets tests point the client at a local
fixture server; production always uses `https://tb7.pl`.

## Frontend wiring

The download-manager package fetches `/api/downloads/providers` once at render and
uses it for everything provider-related:

- Settings → Providers: "Torrent provider" and "Hoster link provider" selects are
  built from `slots`; each selected provider gets its declared credential inputs,
  a Verify button and (tb7) account status + supported hosters.
- Secret inputs render blank with a "Saved — leave blank to keep it" placeholder, so
  typing a new value replaces it; leaving it blank preserves the stored secret.
- Overview/add form show a routing hint (`Torrents: Real-Debrid · Hoster links:
  tb7.pl (17 hosters)`).
- No hardcoded provider list, key map or label map remains in the package.

## Decisions & constraints

- **Registry over hardcoded lists** so a new provider is one `ProviderSpec` plus a
  client module.
- **Two slots** instead of a single active provider: switching to tb7 must not break
  magnet downloads.
- **Credentials are admin-global engine fields**, like the previous debrid keys (one
  NAS account; the quota is shared).
- **`kind="debrid_link"` retained** for task/qBittorrent compatibility.
- **tb7 uses `hoster_match="list"`**; AllDebrid keeps `any` so its previous behaviour
  is unchanged.
- **No HEAD pre-probe** of generated links (may be single-use / consume quota);
  Reset re-unlocks and can cost provider transfer.
- Clean-room implementation: EthOS is MIT, pyload is GPLv3. pyload was used only as
  a protocol reference (endpoint names and page markers); no code, regex or text was
  copied.

## Status

- [x] Provider registry, credentials, mask/merge, slot helpers
- [x] tb7.pl client (login, unlock, account, hosters, error mapping)
- [x] Config v9 + v8→v9 migration
- [x] Router routing, pump/worker, `/providers`, `/verify`
- [x] Transfer headers + transient-header persistence guard
- [x] Package UI (provider selects, credential blocks, verify, hints)
- [x] Offline unit tests + isolated-instance API/E2E tests
- [x] Package manifest 1.1.0, README, changelog
- [ ] Live verification against a real tb7.pl account (user supplies credentials + a small supported-host link)
- [ ] Phase 2 — direct-hoster plugin framework

## Known gaps / next steps

- **Phase 2 — direct-hoster plugins.** Planned `backend/services/download_plugins/`
  package with a `HosterPlugin` protocol (`match(url)`, `resolve(url) -> direct URL +
  headers`), ordered hoster plugin → link provider → plain HTTP. Initial CAPTCHA-free
  resolvers: MediaFire, Pixeldrain, Gofile, KrakenFiles; fixtures + local-server tests;
  task field `plugin` surfaced in the UI. Port behaviour from pyload plugins by
  observation only (same GPL constraint). See `TODO.md` when it lands.
- **Sister services** `xt7.pl` and `gb7.pl` share the tb7 flow (different limits);
  later a `base_url` parameter / extra `ProviderSpec` entries.
- If tb7 ever requires a CSRF token beyond hidden-field echo or blocks non-browser
  logins, the fallback is a pasted session-cookie credential (not implemented).
- Temporary live check: use an isolated instance on `:9002` with a throwaway
  `ETHOS_DATA_DIR`, save the account via `POST /api/downloads/config`, run
  `POST /api/downloads/verify` and add one small supported-host link.
