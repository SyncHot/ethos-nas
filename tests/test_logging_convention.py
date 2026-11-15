#!/usr/bin/env python3
"""Logging convention guard (TODO.md P1-02).

Every module under backend/routers and backend/services must define a child
logger whose name is the dotted module path under the `ethos-nas` root:

    backend/routers/health.py          -> ethos-nas.routers.health
    backend/services/camera_service.py -> ethos-nas.services.camera_service

That is what routes records into the App Events store and makes filtering by
module reliable. No server needed.

Run:
    .venv/bin/python tests/test_logging_convention.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGGER_RE = re.compile(r"getLogger\(\s*[\"'](ethos-nas[^\"']*)[\"']")

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def expected_name(rel: Path) -> str:
    return "ethos-nas." + ".".join(rel.with_suffix("").parts)


def main() -> int:
    missing: list[str] = []
    mismatched: list[str] = []

    for sub in ("routers", "services"):
        for path in sorted((ROOT / "backend" / sub).glob("*.py")):
            if path.name == "__init__.py":
                continue
            rel = path.relative_to(ROOT / "backend")
            expected = expected_name(rel)
            text = path.read_text(encoding="utf-8", errors="replace")
            names = LOGGER_RE.findall(text)
            if not names:
                missing.append(str(rel))
            elif expected not in names:
                mismatched.append(f"{rel}: expected {expected}, found {names}")

    check("every router/service defines an ethos logger", not missing, ", ".join(missing))
    check("logger names match the module path", not mismatched, "; ".join(mismatched))

    # The root logger must exist in main.py (the AppLogHandler attaches to it).
    main_src = (ROOT / "backend/main.py").read_text(encoding="utf-8")
    check("main.py defines the ethos-nas root logger", "getLogger(\"ethos-nas\")" in main_src)

    # The unified store handler must be attached from the same module.
    check("main.py attaches the unified log handler", "_applog_attach()" in main_src)
    check("main.py imports the unified store", "backend.services.log_service" in main_src)
    check("main.py keeps the store under DATA_DIR/logs", 'settings.DATA_DIR / "logs"' in main_src)

    # A second log store is a regression: only log_service.py touches the file.
    offenders = []
    for path in sorted((ROOT / "backend").rglob("*.py")):
        if path.name == "log_service.py" or "data" in path.relative_to(ROOT / "backend").parts[:1]:
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if ".jsonl" in line and ("open(" in line or "write_text" in line):
                offenders.append(str(path.relative_to(ROOT)))
                break
    check("only services/log_service.py writes the unified store", not offenders, ", ".join(offenders))

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  FAILED: {name} — {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
