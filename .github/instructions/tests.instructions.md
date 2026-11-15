---
description: "Test rules for EthOS NAS: isolated ETHOS_DATA_DIR, ETHOS_TEST_PASSWORD, one file per process, cleanup."
applyTo: "tests/**"
---

# Test rules

- Standalone scripts: `main() -> int`, `sys.exit(main())`; no pytest classes.
- Credentials: resolve through `_require_test_password()` / `ETHOS_TEST_PASSWORD`; never hardcode.
- **Never** use the default `backend/data/` — set a temp `ETHOS_DATA_DIR` or target an isolated
  instance with `ETHOS_TEST_BASE` (default `:9000` only for safe UI tests).
- `python a.py b.py` runs only `a.py` — one test file per process (CI and `run_tests.py` do this).
- Self-clean: delete notes/tasks/files created by the test in a `finally` block; use unique names.
- Destructive tests must refuse to run against production (`ETHOS_TEST_BASE` pointing at :9000).
- Register in the CI fast suite only if the test boots its own instance or needs no login.
