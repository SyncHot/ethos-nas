---
name: add-test
description: "Write or fix a test in tests/. Use for regression, API, security, or Playwright coverage; covers ETHOS_TEST_PASSWORD, isolated instances, cleanup, CI registration."
argument-hint: "What behavior should the test cover?"
---

# Add a test

## Repository test rules
- Tests are **standalone scripts** in `tests/test_*.py`, each with `main() -> int` and
  `sys.exit(main())`. They are not pytest classes.
- `run_tests.py` starts **one** shared isolated instance and requires `ETHOS_TEST_PASSWORD`.
- `python a.py b.py` executes only `a.py` — run one file per process (CI does this).
- Tests may **never** default to `backend/data/`. Use a temp `ETHOS_DATA_DIR`, or hit a
  throwaway instance via `ETHOS_TEST_BASE`.
- Credentials are never committed: resolve them through `_require_test_password()`.

## Choose the style

| Style | Use for | Copy from |
|---|---|---|
| Pure unit (no server) | stores, services, parsers, migrations | `tests/test_stores.py` |
| API/security with own instance | auth, scoping, close codes, invariants | `tests/test_security_invariants.py`, `tests/test_router_smoke.py` |
| Playwright UI | window/app flows, focus, rendering | `tests/test_backup_ui.py`, `tests/test_desktop_trash_ui.py` |

## Procedure

### 1. Skeleton (API/security style)
```python
import os, sys

def _require_test_password() -> str:
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw

def main() -> int:
    ...  # print [PASS]/[FAIL] per check, return 1 on failure
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

### 2. Boot isolation
- Prefer booting the app in-process or as a subprocess on a free port with
  `tempfile.mkdtemp()` as `ETHOS_DATA_DIR` and a test-only `ETHOS_JWT_SECRET`
  (copy the helper from `test_security_invariants.py`).
- For UI tests, `BASE = os.getenv("ETHOS_TEST_BASE", "http://localhost:9000")`.
- Guard destructive tests: skip or refuse when `ETHOS_TEST_BASE` points at production.

### 3. Make it self-cleaning and deterministic
- Delete everything the test created (notes, tasks, containers metadata, files) in a `finally`.
- Use unique names (`test_<uuid>`) so reruns do not collide.
- Assert on API responses, not on timing sleeps, wherever possible.

### 4. Run it
```bash
.venv/bin/python tests/test_<area>.py
ETHOS_TEST_PASSWORD=... .venv/bin/python run_tests.py --filter <area>
```

### 5. Register in CI
Add the file to the fast-suite list in `.github/workflows/ci.yml` **only if** it boots its own
instance or needs no authenticated live server. Tests that need the login fixture stay in
`run_tests.py`.

## Anti-patterns
- Hardcoded password or username-specific fixtures.
- Writing to `backend/data/` or `backend/routers/users.json`.
- Depending on test execution order.
- Leaving Playwright notes/tasks/files behind.
- Asserting only on HTTP 200 while ignoring `detail` (the useful failure text).

## Done when
The test fails on the bug (verify by reverting locally or reasoning about the assertion), passes
on the fix, cleans up, and runs both standalone and through `run_tests.py --filter`.
