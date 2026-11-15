---
name: state-store
description: "Create or migrate JSON state stores. Use for new persistence, corrupt/lost data, schema changes, growth caps, or json.dump in a router; enforces services/json_store.py."
argument-hint: "What state are you persisting or migrating?"
---

# Persistent JSON state

## The rule
All JSON state goes through `services/json_store.py`; hand-rolled `write_text`/`os.replace` and `json.dump(` in routers caused truncated stores and are rejected by `check_boundaries.py`.

## Procedure

### 1. Get a store instance
```python
from backend.config import settings
from backend.services import json_store

_STORE = json_store.get(
    settings.DATA_DIR / "<name>.json",
    dict,                 # default_factory for a missing/corrupt file
    version=1,            # bump when the shape changes
)
```
`get()` returns one locked `Store` per path per process, so every writer serialises on the same lock.

### 2. Use the API
```python
data = _STORE.load()          # deep copy; never raises; quarantines corruption
_STORE.save(data)             # temp file + fsync + os.replace, mode 0600
result = _STORE.update(lambda d: d.setdefault("items", []).append(item))
```
Use `update()` for read-modify-write; `load()` + `save()` is only for pure reads or full rewrites.

### 3. Version and migrate
- Change the shape → increment `version` and pass `migrate=...`:
  `migrate(from_version, to_version, data) -> data` (raise `ValueError` to abandon).
- `load()` runs the migrator, logs `store X migrated vN→vN+1`, and writes it back.
- A file written by a newer build is never overwritten (downgrade guard): `load()` returns the
  default and logs an ERROR. Do not work around this.

### 4. Bound growth
- Cap lists in the writer (`MAX_EVENTS`, newest-first trimming).
- Large or time-based data → register with `services/retention_service.py` instead of growing forever.
- Keep files small enough to rewrite atomically (they are read whole).

### 5. Path and isolation
- Always resolve the path under `settings.DATA_DIR`; never a path under `backend/routers/`.
- Tests must pass a temp dir (`ETHOS_DATA_DIR`); a test that writes `backend/data/` is a bug.

### 6. Migrating a hand-rolled writer
1. Keep the existing file name and shape.
2. Replace read/write code with `json_store.get(...)`; add `version`/`migrate` if the shape changes.
3. Delete the old lock/tempfile/write code.
4. Add a test that loads a legacy file and asserts the upgraded result.
5. Run the app once against a **copy** of the real file, not production in place.

## Verification
Run `scripts/check_boundaries.py`, `tests/test_stores.py`, `tests/test_store_migrations.py`. The store tests cover round-trip, 0600, corruption quarantine, concurrency, and the version guard — extend them when you add behavior.

## Anti-patterns
- Module-level mutable dict as the source of truth (loses data on restart).
- Writing from a worker thread without the store lock.
- Unbounded lists or caches.
- `except Exception: pass` around persistence (data loss is silent).
- Storing secrets in plaintext without documenting it.

## Done when
State survives restart, concurrent writers do not truncate it, corruption is quarantined and logged,
the schema is versioned, growth is bounded, and store tests pass.
