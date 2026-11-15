# ADR 0002 — JSON files plus SQLite instead of a database server

**Status:** accepted; hardened by `services/json_store.py` (2026-11-11).

## Context

EthOS runs on a single host with one writer process. The data volume is small (tens of stores, one photo
index). Running Postgres/MySQL would add a daemon, credentials, migrations and memory pressure to a box
whose job is to serve files.

## Decision

- Small, user-facing state → one JSON file per domain under `ETHOS_DATA_DIR`.
- The photo index → SQLite (`gallery_index.db`) because it is queried by facets and grows to many rows.
- All JSON access goes through `services/json_store.py`: per-file lock, tempfile + fsync + `os.replace`,
  `0600`, corrupt-file quarantine, optional `version` with a downgrade guard.

## Consequences

- Backup is a directory copy; restore is the reverse (see `docs/ops.md` §5).
- Every store must declare a version once its shape changes; migrations run on read.
- No cross-store transactions. Where atomicity matters (single-flight backups), the router serialises.
- A store that is not on `json_store` yet carries the old risks — tracked in TODO.md P0-14.
