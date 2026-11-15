# ADR 0003 — PAM login plus a custom HMAC-SHA256 JWT

**Status:** accepted; authorization added 2026-11-11.

## Context

Users are Linux accounts on the NAS. Storing passwords would duplicate PAM, and a full identity provider
(OIDC/Keycloak) is disproportionate for a single-host appliance. The Android apps also need long-lived
credentials that do not require re-entering a password.

## Decision

- `POST /api/auth/login` validates against PAM (`pamela`, service `login`) and issues a 24-hour JWT signed
  with HMAC-SHA256 (`ETHOS_JWT_SECRET`).
- Accounts are auto-registered in `<DATA_DIR>/users.json` with role `user`; the first eligible system user
  is seeded as `admin`.
- Each account gets an `ethos_…` API key for the Android apps; keys are compared in constant time.
- Authorization is a separate `require_admin` dependency; routers opt in. Router-level dependencies that
  publish per-request state (role for path scoping) must be `async def` — FastAPI runs sync dependencies in
  a worker thread, and a contextvar written there never reaches the handler.

## Consequences

- Rotating `ETHOS_JWT_SECRET` logs everyone out and invalidates API keys' tokens (keys themselves survive).
- The default secret must be replaced in production; startup logs CRITICAL and raises a notification when
  it is still the built-in value.
- No 2FA, no session revocation list. Both are roadmap items (TODO.md F-02).
- PAM is Linux-only; there is no portable fallback (by design).
