# Domains — reverse proxy package (Caddy + ACME)

Status: 2026-11-13 · package `domains` v1.0.0 (in `~/apps/compose/ethos-packages`)

A Packages add-on that owns ports 80/443, forwards domains to any host:port and keeps Let's
Encrypt certificates renewed. It is a **separate repository**: EthOS only ships the package plumbing
(`/api/packages`, proxy, native UI loader).

## File map

| What | Where |
|---|---|
| Manifest (native UI, port 8093) | `ethos-packages/domains/manifest.json` |
| Compose (Caddy + control plane) | `ethos-packages/domains/docker-compose.yml` |
| Static Caddyfile (admin + imports) | `ethos-packages/domains/caddy/Caddyfile` |
| Control plane API | `ethos-packages/domains/server/app.py` |
| Validation + Caddyfile rendering | `ethos-packages/domains/server/config_gen.py` |
| Route/settings state | `ethos-packages/domains/server/store.py` |
| Certificate inventory | `ethos-packages/domains/server/certs.py` |
| Native UI | `ethos-packages/domains/frontend/app.js` |
| Tests (no Docker) | `ethos-packages/domains/tests/test_config_gen.py` |
| EthOS uninstall options | `backend/services/package_cleanup.py`, `backend/routers/packages.py` |

## Data model

`domains-data` volume, `/data/domains.json`:

```json
{
  "version": 1,
  "settings": {"acme_email": "you@example.com", "staging": false},
  "routes": [
    {"id": "r_1a2b3c4d", "domains": ["app.example.com"], "upstream": "192.168.1.10:8080",
     "preserve_host": true, "stream": false, "enabled": true, "note": "Home Assistant",
     "created_at": "…", "updated_at": "…"}
  ]
}
```

Volumes: `caddy-data` (certs, ACME account), `caddy-config`, `caddy-generated` (rendered
`options.caddy` + `routes.caddy`), `caddy-logs`, `domains-data`.

## API (control plane, loopback 8093 — reached via `/api/packages/domains/proxy/`)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | `{status, caddy, version, managed_routes}` |
| GET | `/api/state` | routes, settings, caddy reachability, certs, last apply |
| GET | `/api/certs` | issuer, names, expiry, days left |
| GET | `/api/logs?limit=` | tail of `/var/log/caddy/caddy.log` |
| GET | `/api/dns?domain=` | resolved addresses + hint (public domain needed) |
| POST | `/api/routes` | create/update; validated, adapted and loaded before saving |
| POST | `/api/routes/delete` | `{id}` |
| POST | `/api/settings` | `{acme_email, staging}` |
| POST | `/api/apply` | re-push the rendered config |

Only GET/POST: the EthOS package proxy forwards nothing else.

## How a change is applied

`validate → new state → POST /adapt → POST /load → persist`. `/adapt` parses without touching the
running config, so a bad route is rejected with Caddy's message and nothing changes; `/load` is
zero-downtime and rolls back on error. Files are written only after Caddy accepted them, and Caddy
imports them on boot, so a reboot restores routing before the control plane starts.

## Migration from Nginx Proxy Manager (done 2026-11-13)

NPM was the previous ingress on this NAS and held 80/443. `ethos-packages/domains/tests/migrate_npm.py`
reads NPM's SQLite **read-only** and emits the `POST api/routes/import` payload.

| Result | Value |
|---|---|
| Imported routes | 17 (from 18 NPM hosts) |
| Skipped | `domains.myserver.pl` (pointed at NPM's own UI, port 81) |
| Duplicate handled | `home.myserver.pl` — NPM #1 was already `is_deleted`, only #8 imported |
| Preserved | response headers (HSTS/CSP/X-Frame-Options/…) and the `www.wawalocal.pl → wawalocal.pl` 301 |
| Not migrated | access lists, caching, block-exploits, arbitrary `if`/`location` blocks (none were in use) |
| Certificates | 17 new Let's Encrypt certs issued by Caddy, ~89 days, renewed automatically |

Upstreams are kept verbatim from NPM: `172.17.0.1` is the host's docker0 address and is reachable from
any compose network — `host.docker.internal` would *not* reach services published on host loopback only.

Hosts returning 502 are upstreams that were already down before the migration (jellyfin 8096, rdt 6500,
sonarr 8989, alergolog 8091, sasidu 89; mqtt 9001 is a non-HTTP listener).

Rollback: stop the package, then `docker compose -f ~/apps/compose/nginxproxymanager/docker-compose.yml up -d`
(NPM's `data/` and `letsencrypt/` bind mounts were never touched).

## Window layout (1.2.0)

The native UI follows the EthOS AntiVirus package: a 218 px left rail with the sections, a scrolling main
pane, and the same panels, pills, stat tiles and tables. It collapses to a horizontal chip bar below 780 px.

| Section | Content |
|---|---|
| Overview | Ready card, tiles (routes · certificates · expiring ≤ 30 d · disabled), action cards (add a domain, check DNS, reload proxy), callouts for a stopped proxy / rejected config / staging certificates |
| Routes | Table with option chips (redirect, host→target, streaming, header count), certificate state, inline enable toggle, and an add/edit form whose headers, Host and streaming fields sit under **Advanced** |
| Certificates | Tiles (total · valid > 30 d · expiring · expired) and names, issuer, expiry per certificate |
| Settings | ACME email + staging switch, package summary and a shortcut to Diagnostics |
| Diagnostics | DNS preflight, proxy log with JSON lines pretty-printed (120/300/500) and the last applied configuration with its error/warnings |

Interaction rules: the 15 s refresh re-renders only the active section, so an open form and the ACME email
field are never overwritten; errors stay in the header callout until the next successful action; deleting a
route asks for confirmation. `domains/preview/` (mock `ctx`, light/dark, empty/offline/error scenarios) is a
developer harness and is not shipped.

## Status

- [x] Package manifest, icon, README, compose (policy-clean)
- [x] Control plane: routes, settings, certs, DNS preflight, log tail, bulk import
- [x] Strict validators (domain, target, email, headers, redirects) — injection-safe
- [x] Tabbed native UI (1.2.0) with light/dark palettes, preview harness, 63 package unit checks
- [x] `ethos-pack build|sign|verify`; published 1.0.0 → 1.1.1 → 1.2.0
- [x] Installed through Packages and migrated the NPM proxy hosts (17 routes, 17 certs)
- [ ] Restart `ethos-nas` to pick up the uninstall-options backend (needs the UI restart button)
- [ ] DNS-01 (wildcards) — needs a custom Caddy image with `caddy-dns/*`

## Deferred

- Wildcard certificates and DNS-01 providers (custom image, digest-pinned).
- Access lists, redirect rules, per-route headers, HTTPS upstreams.
- Container auto-discovery (requires the Docker socket, rejected by compose policy).
