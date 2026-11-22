---
name: verify-changes
description: "Run the pre-commit gate before commit/push or when CI fails. Covers compile, import smoke, ruff, boundaries, logging rule, JS syntax, and the relevant tests."
argument-hint: "What changed? (backend area, frontend app, or 'everything')"
---

# Verify changes

Run top to bottom, stop at the first failure. This mirrors `.github/workflows/ci.yml`.

## 1. Static checks
```bash
.venv/bin/python -m compileall -q backend
.venv/bin/python -c "import backend.main; print('import OK')"
ruff check backend tests
```

## 2. Project-specific guards
```bash
.venv/bin/python scripts/check_boundaries.py   # services must not import routers; no json.dump in routers
.venv/bin/python scripts/check_logging.py      # no unexplained `except ...: pass`
```

## 3. Frontend syntax
```bash
fail=0
while IFS= read -r f; do
  node --check "$f" || { echo "syntax error: $f"; fail=1; }
done < <(find frontend/static -name '*.js' -not -path '*/vendor/*')
exit $fail
```

## 4. Tests
Needs `ETHOS_TEST_PASSWORD` (never hardcoded). Prefer the targeted subset, then the fast suite.

| Changed area | Run |
|---|---|
| Stores / services | `tests/test_stores.py`, `tests/test_store_migrations.py`, `tests/test_services.py` |
| Auth / security / paths | `tests/test_security_invariants.py`, `tests/test_logging_convention.py` |
| Any router | `tests/test_router_smoke.py` |
| Frontend escaping/UI | `tests/test_frontend_escaping.py`, `tests/test_a11y.py`, `tests/test_ui_states.py` |
| Packages / catalog | `scripts/ethos-qa static --repo ~/apps/compose/ethos-packages --live`, `tests/test_packages_qa_ui.py`, `scripts/ethos-pack lint\|verify` |
| A feature | `tests/test_<feature>.py` (Playwright) |
| Everything | `ETHOS_TEST_PASSWORD=... .venv/bin/python run_tests.py` |

```bash
ETHOS_TEST_PASSWORD=... .venv/bin/python run_tests.py --filter <area>
```

## 5. Runtime checks
`curl -sf http://localhost:9000/api/system/status` → 401 = up. If a backend change needs a restart, ask the user to use Settings → Restart; never restart the service yourself.

## 6. Pre-commit hygiene
`git status --short` / `git diff --stat`: no `.test_token`, credentials, tokens, or `backend/data/` files staged; no unrelated churn.

## Done when
Every check above is green — or the failures are explicitly reported with the exact command and
output. Never report a change as done while suppressing a failing guard.
