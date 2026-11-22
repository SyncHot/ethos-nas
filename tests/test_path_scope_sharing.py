#!/usr/bin/env python3
"""Read-only sharing scope tests.

Guards the most security-sensitive decision of the feature:
``sanitize_read`` may honour a received grant, while ``sanitize`` /
``sanitize_for`` (used by every write and every public link) must not.

Run: .venv/bin/python tests/test_path_scope_sharing.py
"""

import ast
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-scope-sharing-data-"))
SHARE_ROOT = Path(tempfile.mkdtemp(prefix="ethos-scope-sharing-root-"))
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

from backend.services import path_scope, sharing_resources, sharing_service, users_store  # noqa: E402

ALICE = "ethostestalice"
BOB = "ethostestbob"
CAROL = "ethostestcarol"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _status(path: str, username: str, role: str, *, read: bool = False) -> int:
    fn = path_scope.sanitize_read if read else path_scope.sanitize
    with path_scope.identity_scope(username, role):
        try:
            fn(path)
            return 0
        except HTTPException as exc:
            return exc.status_code


def _record(username: str) -> dict:
    return {
        "id": username, "username": username, "role": "user", "status": "active",
        "created_at": "2026-01-01T00:00:00Z", "last_login": None,
        "source": "test", "api_key": "",
    }


def _static_guard() -> None:
    """Write-capable routes must not use the share-aware read helper."""
    src = (ROOT / "backend" / "routers" / "files.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    allow = {"start_transcode", "warm_thumbnails"}  # derived cache, never the source
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        methods = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                methods.append(dec.func.attr)
        if not any(m in ("post", "put", "delete", "patch") for m in methods):
            continue
        if node.name in allow:
            continue
        segment = ast.get_source_segment(src, node) or ""
        if "_sanitize_read" in segment:
            offenders.append(node.name)
    check("write file routes never use the shared read scope", not offenders, ", ".join(offenders))


def main() -> int:
    for name in (ALICE, BOB, CAROL):
        (SHARE_ROOT / name / "photos").mkdir(parents=True, exist_ok=True)
    shared = SHARE_ROOT / ALICE / "photos" / "trip"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "one.txt").write_text("one")
    secret = SHARE_ROOT / ALICE / "photos" / "secret.txt"
    secret.write_text("secret")

    path_scope.SHARE_ROOTS = (str(SHARE_ROOT),)
    sharing_resources.SHAREABLE_ROOTS = (str(SHARE_ROOT),)
    path_scope.home_dir = lambda name: str(SHARE_ROOT / name)
    users_store.save([_record(ALICE), _record(BOB), _record(CAROL)])

    try:
        sharing_service.upsert_grant(ALICE, "path", str(shared), recipients=[BOB])

        check("own scope still allows the owner",
              _status(str(shared), ALICE, "user") == 0)
        check("plain sanitize denies a shared path",
              _status(str(shared), BOB, "user") == 403)
        check("sanitize_read allows a granted path",
              _status(str(shared), BOB, "user", read=True) == 0)
        check("sanitize_read still denies a sibling",
              _status(str(secret), BOB, "user", read=True) == 403)
        check("sanitize_read denies a non-recipient",
              _status(str(shared), CAROL, "user", read=True) == 403)
        with path_scope.identity_scope(BOB, "user"):
            check("can_write is false for a received share", path_scope.can_write(str(shared)) is False)
        with path_scope.identity_scope(ALICE, "user"):
            check("can_write is true for the owner", path_scope.can_write(str(shared)) is True)

        # A public share link (sanitize_for) must not inherit a user grant.
        try:
            with path_scope.identity_scope(BOB, "user"):
                path_scope.sanitize_for(str(shared), BOB, "user")
            owner_status = 0
        except HTTPException as exc:
            owner_status = exc.status_code
        check("sanitize_for does not inherit grants", owner_status == 403, str(owner_status))

        # DATA_DIR is rejected before any grant is considered.
        data_file = DATA_DIR / "users.json"
        data_file.write_text("[]")
        sharing_service.upsert_grant(ALICE, "path", str(data_file), recipients=[BOB])
        check("sanitize_read never reaches DATA_DIR",
              _status(str(data_file), BOB, "user", read=True) == 403)

        with path_scope.identity_scope(ALICE, "user"):
            try:
                path_scope.sanitize_share(str(data_file))
                share_data_status = 0
            except HTTPException as exc:
                share_data_status = exc.status_code
        check("sanitize_share rejects DATA_DIR", share_data_status == 403, str(share_data_status))

        with path_scope.identity_scope(BOB, "user"):
            try:
                path_scope.sanitize_share(str(shared))
                other_share_status = 0
            except HTTPException as exc:
                other_share_status = exc.status_code
        check("a user cannot offer another home via sanitize_share",
              other_share_status == 403, str(other_share_status))

        with path_scope.identity_scope(ALICE, "user"):
            try:
                path_scope.sanitize_share(str(shared))
                own_share_status = 0
            except HTTPException as exc:
                own_share_status = exc.status_code
        check("an owner can offer their own path", own_share_status == 0, str(own_share_status))

        # Admins may share NAS data but never DATA_DIR / system paths.
        with path_scope.identity_scope("root", "admin"):
            try:
                path_scope.sanitize_share(str(data_file))
                admin_data_status = 0
            except HTTPException as exc:
                admin_data_status = exc.status_code
            try:
                path_scope.sanitize_share("/etc/passwd")
                admin_system_status = 0
            except HTTPException as exc:
                admin_system_status = exc.status_code
        check("admin shares cannot target DATA_DIR", admin_data_status == 403)
        check("admin shares are limited to NAS data roots", admin_system_status == 403)

        _static_guard()
    finally:
        shutil.rmtree(DATA_DIR, ignore_errors=True)
        shutil.rmtree(SHARE_ROOT, ignore_errors=True)

    print()
    failed = [name for name, ok, _ in results if not ok]
    if failed:
        print(f"{len(failed)} scope check(s) failed")
        return 1
    print(f"All {len(results)} scope checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
