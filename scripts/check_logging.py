#!/usr/bin/env python3
"""Enforce the logging rule from AGENTS.md (TODO.md P1-01).

Every failure path must leave a trace. A handler that does nothing is allowed
only when it is *explicitly* declared intentional:

    except OSError:
        pass  # best-effort cleanup — nothing to report

    except OSError:  # intentional: probe failure means "not present"
        pass

Anything else — `except Exception: pass`, a bare `pass` under a silent handler —
fails the build. Use `logger.warning(...)`/`logger.error(...)` instead, or add a
comment explaining why the failure is irrelevant.

    .venv/bin/python scripts/check_logging.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

EXCEPT = re.compile(r"^(\s*)except\b")
PASS = re.compile(r"^\s*pass\b")
PRINT = re.compile(r"^\s*print\(")


def _backend_files():
    """Backend sources only — `backend/data/` holds user/test artifacts, not code."""
    for path in sorted(BACKEND.rglob("*.py")):
        if "data" in path.relative_to(BACKEND).parts[:1]:
            continue
        yield path


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    lines = path.read_text(encoding="utf-8").split("\n")
    for index, line in enumerate(lines):
        if PRINT.match(line):
            problems.append(f"{path.relative_to(ROOT)}:{index + 1}: `print()` — use the module logger")
            continue
        if not PASS.match(line):
            continue
        # Walk back to the handler this `pass` belongs to.
        j = index - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j < 0:
            continue
        handler = lines[j]
        if not EXCEPT.match(handler):
            continue  # a plain `pass` in some other block — not our business
        if "#" in handler or "#" in line:
            continue
        rel = path.relative_to(ROOT)
        problems.append(f"{rel}:{index + 1}: silent `pass` under `{handler.strip()}` — "
                        f"log it or annotate why it is harmless")
    return problems


def main() -> int:
    problems: list[str] = []
    for path in _backend_files():
        problems.extend(check_file(path))
        if path.name == "log_service.py":
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if ".jsonl" in line and ("open(" in line or "write_text" in line):
                problems.append(
                    f"{path.relative_to(ROOT)}: second log store — write through services/log_service.py"
                )
                break
    if problems:
        print(f"FAIL: {len(problems)} unexplained silent failure path(s)\n")
        for problem in problems:
            print(" ", problem)
        print("\nRule: AGENTS.md → Logging (every failed request is logged).")
        return 1
    print("Logging OK — every silent handler is annotated as intentional.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
