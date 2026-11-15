#!/usr/bin/env python3
"""Architecture boundary checks (TODO.md P2-12).

Fails when:
  1. `backend/services/*` imports from `backend.routers.*` — the dependency
     direction is routers → services. `qb_compat` is allow-listed: it is a
     transport shim that deliberately reuses the download engine internals.
  2. A router serialises JSON itself (`json.dump(`) instead of using
     `services/json_store.py` / `users_store.py`. (Binary/atomic file writes —
     file content, encrypted blobs, images — are legitimate and not flagged.)

Warnings (non-fatal) list routers above the documented size budget so growth is
visible during review; the split work is tracked in TODO.md P2-01…P2-08.

Run: .venv/bin/python scripts/check_boundaries.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ROOT / "backend" / "services"
ROUTERS = ROOT / "backend" / "routers"

LAYER_ALLOWLIST = {"qb_compat.py"}
SIZE_BUDGET = 900

errors: list[str] = []
warnings: list[str] = []


def main() -> int:
    for path in sorted(SERVICES.glob("*.py")):
        if path.name in LAYER_ALLOWLIST:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"from backend\.routers|import backend\.routers", line):
                errors.append(f"{path.relative_to(ROOT)}:{lineno}: service imports a router — {line.strip()}")

    for path in sorted(ROUTERS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "json.dump(" in line:
                errors.append(
                    f"{path.relative_to(ROOT)}:{lineno}: router writes state directly — "
                    "use services/json_store.py"
                )
        lines = text.count("\n") + 1
        if lines > SIZE_BUDGET:
            warnings.append(f"{path.relative_to(ROOT)}: {lines} lines (budget {SIZE_BUDGET}) — see TODO.md P2")

    for warning in warnings:
        print(f"WARN  {warning}")
    for error in errors:
        print(f"FAIL  {error}")

    if errors:
        print(f"\n{len(errors)} boundary violation(s).")
        return 1
    print(f"\nBoundaries OK ({len(warnings)} size warning(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
