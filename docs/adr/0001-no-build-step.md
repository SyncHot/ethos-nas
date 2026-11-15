# ADR 0001 — No build step, no bundler

**Status:** accepted (long-standing; written down 2026-11-11).

## Context

The frontend is a desktop-metaphor SPA with 32 app modules. A build step (Vite/webpack) would add a Node
toolchain to a NAS appliance that already ships Python only, complicate the systemd unit, and make
"edit a file, reload the page" debugging slower on the box itself.

## Decision

Ship the frontend as native ES modules served by FastAPI. No transpilation, no bundling, no framework.

## Consequences

- Deploy = `git pull`; nothing to build. Frontend edits only need a browser reload.
- Cache-busting must be handled explicitly. Today only the two entry modules carry `?v=`; deep imports do
  not (TODO.md P3-10 covers versioning every import or relying on the existing `Cache-Control: no-cache`
  middleware, which already forces revalidation).
- Third-party libraries come from CDNs and must be vendored for offline use (TODO.md §9).
- No type checking; `node --check` plus JSDoc on the core modules is the agreed ceiling.
- Deep imports mean the module graph is the dependency system — keep `core/` free of app imports.
