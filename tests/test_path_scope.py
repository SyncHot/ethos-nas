#!/usr/bin/env python3
"""Path scope unit + static guard tests.

Covers the fail-closed, per-user filesystem scope in
``backend/services/path_scope.py`` and guards the routers that must mount
``scope_request`` before calling ``sanitize()``.

Run:
    .venv/bin/python tests/test_path_scope.py
"""

import os
import pwd
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DATA_DIR = tempfile.mkdtemp(prefix="ethos-path-scope-")
os.environ["ETHOS_DATA_DIR"] = _DATA_DIR

from fastapi import HTTPException  # noqa: E402

from backend.config import settings  # noqa: E402
from backend.services import path_scope  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _status(path: str, username: str, role: str) -> int:
    """Sanitize as an explicit identity → HTTP status (0 = allowed)."""
    try:
        path_scope.sanitize_for(path, username, role)
        return 0
    except HTTPException as exc:
        return exc.status_code


def _static_checks() -> None:
    print("\n[static]")
    routers = ROOT / "backend" / "routers"
    offenders = []
    for file in routers.glob("*.py"):
        src = file.read_text(encoding="utf-8")
        uses_scope = "path_scope.sanitize" in src or "from backend.routers.files import _sanitize" in src
        if not uses_scope:
            continue
        if "scope_request" in src or "require_admin" in src or "require_role" in src:
            continue
        offenders.append(file.name)
    check("filesystem routers mount scope_request (or are admin-only)",
          not offenders, ", ".join(offenders))

    stray_roots = []
    svc = ROOT / "backend" / "services"
    for file in list(svc.glob("*.py")) + list(routers.glob("*.py")):
        if file.name == "path_scope.py":
            continue
        src = file.read_text(encoding="utf-8")
        if "ALLOWED_ROOTS" in src and "path_scope.ALLOWED_ROOTS" not in src:
            stray_roots.append(str(file.relative_to(ROOT)))
    check("no hardcoded ALLOWED_ROOTS outside path_scope", not stray_roots,
          ", ".join(stray_roots))


def _unit_checks() -> None:
    print("\n[unit]")
    me = pwd.getpwuid(os.getuid())
    home = os.path.realpath(me.pw_dir)
    other = "not-the-owner-7f3a"

    check("admin keeps full filesystem access",
          _status("/etc/passwd", me.pw_name, "admin") == 0)
    check("user may read their own home",
          _status(os.path.join(home, "doc.txt"), me.pw_name, "user") == 0)
    check("user may use /mnt", _status("/mnt/SharedDrive/x", me.pw_name, "user") == 0)
    check("user may use /media", _status("/media/usb/x", me.pw_name, "user") == 0)
    check("user cannot read /etc", _status("/etc/passwd", me.pw_name, "user") == 403)
    check("user cannot read /root", _status("/root/x", me.pw_name, "user") == 403)
    check("user cannot read /tmp", _status("/tmp/x", me.pw_name, "user") == 403)
    check("user cannot read another home",
          _status(f"/home/{other}/x", me.pw_name, "user") == 403)
    check("unknown role is treated as user",
          _status("/etc/passwd", me.pw_name, "guest") == 403)

    # `~` expands to the caller's home, not the service account's.
    check("~ expands to the caller's home",
          path_scope.sanitize_for("~/Music", me.pw_name, "user")
          == os.path.join(home, "Music"))

    # The EthOS state directory is never user-facing, even inside a home.
    original = settings.DATA_DIR
    try:
        settings.DATA_DIR = Path(home) / ".ethos-state-test"
        check("user cannot read DATA_DIR even under their home",
              _status(str(settings.DATA_DIR / "users.json"), me.pw_name, "user") == 403)
        check("admin can read DATA_DIR",
              _status(str(settings.DATA_DIR / "users.json"), me.pw_name, "admin") == 0)
    finally:
        settings.DATA_DIR = original

    # Background identity override.
    with path_scope.system_scope():
        username, role = path_scope.current_identity()
        check("system_scope publishes the system identity",
              (username, role) == ("system", "admin"), f"{username}/{role}")
        check("system_scope can sanitize system paths",
              _status("/etc/passwd", username, role) == 0)
    username, role = path_scope.current_identity()
    check("system_scope restores the previous identity",
          (username, role) == ("", "user"), f"{username}/{role}")

    # Fail-closed default: no dependency mounted → non-admin.
    try:
        path_scope.sanitize("/etc/passwd")
        denied = False
    except HTTPException as exc:
        denied = exc.status_code == 403
    check("default identity is fail-closed (user)", denied)


def main() -> int:
    try:
        _static_checks()
        _unit_checks()
    finally:
        shutil.rmtree(_DATA_DIR, ignore_errors=True)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
