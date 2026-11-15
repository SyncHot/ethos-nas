# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use GitHub's
[private vulnerability reporting](https://github.com/SyncHot/ethos-nas/security/advisories/new)
(Security → Report a vulnerability). Include the version (`GET /api/system/version`),
the deployment (bare metal / VM), reproduction steps and the impact you expect.

You can expect an acknowledgement within a few days. This is a volunteer project,
so please allow reasonable time before disclosing publicly.

## Supported versions

Only the latest release on `master` receives fixes. The version is reported by
`GET /api/system/version` (`version`, `git_sha`).

## Threat model in one paragraph

EthOS NAS runs as an unprivileged system user (`ethos-nas`) behind systemd and
serves the UI and API on port 9000. Authentication is PAM (Linux accounts) →
JWT; authorised users are *not* administrators — privileged routers check the
admin role server-side. The service user is granted a small set of
`NOPASSWD` sudo rules (Samba config reload, firewall rules, USB partition
helper, power commands, Docker provisioning helper) and is a member of the
`docker` group. Full inventory: `docs/service-user.md`, the generator lives in
`setup_scripts/setup_service.sh`.

Consequences worth knowing:

- **A compromise of the service account is close to root** (docker group,
  narrow sudo). That is why the project keeps sudo rules argument-exact and
  moves all validation inside root-owned helpers — and why every `/api/*`
  mutation is admin-gated.
- **The unit deliberately does not set `NoNewPrivileges`/`ProtectSystem=strict`**
  because the sudo paths above would stop working. Do not copy the unit file
  into a hardened environment without testing Samba/firewall/USB flows.
- **Filesystem access is scoped**: non-admin users are limited to `/home`,
  `/mnt` and `/media`; admin paths are never taken from the request body
  without `path_scope.sanitize()`.

## Defaults that are safe by design

- No default credentials. Accounts come from PAM; tests refuse to run without
  `ETHOS_TEST_PASSWORD`.
- `ETHOS_JWT_SECRET` has no usable default: when none is configured the app generates a random
  secret once and persists it in `<DATA_DIR>/jwt_secret` (0600). It never signs tokens with the
  public placeholder; that only happens with an explicit `ETHOS_ALLOW_INSECURE=1` opt-in.
  The installer also generates one into `/etc/ethos-nas/ethos.env`.
- Public endpoints are an exact-path allowlist (`backend/main.py`): login,
  APK version info, desktop icon metadata, share-link tokens, the OpenAPI docs
  and PWA plumbing. Everything else needs a JWT.
- Package installation only accepts an **Ed25519-signed** catalog
  (`docs/packages.md`); the core venv is never modified by a package.

## What is out of scope

- Findings that require shell/root access to the NAS itself.
- Self-XSS, clickjacking on pages that require an authenticated session, and
  missing security headers without a demonstrated impact.
- The `ETHOS_ALLOW_INSECURE` / `ETHOS_PACKAGES_ALLOW_INSECURE` flags when
  they were enabled on purpose.
