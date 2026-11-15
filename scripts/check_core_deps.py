#!/usr/bin/env python3
"""Enforce the slim-core contract (docs/app-store.md).

Optional, heavy dependencies must live in a package's private venv and be
reached over loopback, never imported by the core process. Two rules:

1. No `backend/**` module may import a distribution from ``FORBIDDEN`` —
   not at module level, not inside a function. A lazy import still means the
   core interpreter needs the wheel installed.
2. ``requirements.txt`` may not list those distributions either: they are
   installed into ``<DATA_DIR>/packages/apps/<id>/venv`` instead.

``PENDING`` / ``PENDING_REQUIREMENTS`` are the known extraction sites from the
first phase of docs/app-store.md. They are reported but do not fail the build
until the package that replaces them is published; delete each entry with its
extraction commit and the rule becomes absolute.

    .venv/bin/python scripts/check_core_deps.py
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
REQUIREMENTS = ROOT / "requirements.txt"

# import name -> distribution name (for the requirements.txt check)
FORBIDDEN = {
    "cv2": "opencv-python-headless",
    "yt_dlp": "yt-dlp",
    "numpy": "numpy",
}

# No extraction is pending: the core must stay clean from now on.
PENDING: set[tuple[str, str]] = set()
PENDING_REQUIREMENTS: set[str] = set()


def _backend_files():
    """Backend sources only — `backend/data/` holds user/test artifacts, not code."""
    for path in sorted(BACKEND.rglob("*.py")):
        if "data" in path.relative_to(BACKEND).parts[:1]:
            continue
        yield path


def _root_module(name: str) -> str:
    return (name or "").split(".", 1)[0]


def check_imports(path: Path) -> tuple[list[str], list[str]]:
    """Return (violations, pending) for one module."""
    problems: list[str] = []
    pending: list[str] = []
    relative = path.relative_to(ROOT).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{relative}:{exc.lineno}: cannot parse: {exc.msg}"], pending
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for name in names:
            root = _root_module(name)
            if root not in FORBIDDEN:
                continue
            line = f"{relative}:{node.lineno}: imports {name!r} ({FORBIDDEN[root]})"
            if (relative, root) in PENDING:
                pending.append(line)
            else:
                problems.append(f"{line} — belongs in a package venv (docs/app-store.md)")
    return problems, pending


def check_requirements() -> tuple[list[str], list[str]]:
    if not REQUIREMENTS.exists():
        return [], []
    problems: list[str] = []
    pending: list[str] = []
    banned = {dist.lower() for dist in FORBIDDEN.values()}
    for number, line in enumerate(REQUIREMENTS.read_text(encoding="utf-8").splitlines(), 1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        name = text.split("[", 1)[0].split("=", 1)[0].split(">", 1)[0].split("<", 1)[0].strip().lower()
        if name not in banned:
            continue
        line_ref = f"requirements.txt:{number}: {name}"
        if name in PENDING_REQUIREMENTS:
            pending.append(line_ref)
        else:
            problems.append(
                f"{line_ref} — install it from the package wheelhouse, not into the core venv"
            )
    return problems, pending


def main() -> int:
    problems: list[str] = []
    pending: list[str] = []
    for path in _backend_files():
        found, waiting = check_imports(path)
        problems.extend(found)
        pending.extend(waiting)
    found, waiting = check_requirements()
    problems.extend(found)
    pending.extend(waiting)

    for line in pending:
        print(f"PENDING extraction: {line}")
    if problems:
        print(f"FAIL: {len(problems)} slim-core violation(s)\n")
        for problem in problems:
            print(f"  {problem}")
        print("\nSee docs/app-store.md — optional deps run inside a package venv.")
        return 1
    suffix = f" ({len(pending)} awaiting extraction)" if pending else ""
    print(f"OK: core imports and {REQUIREMENTS.name} are free of optional packages{suffix}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
