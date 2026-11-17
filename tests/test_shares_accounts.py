#!/usr/bin/env python3
"""Samba account service tests; subprocesses and host account data are mocked."""

import grp
import io
import logging
import os
import pwd
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["ETHOS_DATA_DIR"] = tempfile.mkdtemp(prefix="ethos-shares-accounts-")
os.environ["ETHOS_JWT_SECRET"] = "shares-accounts-test-secret"
sys.path.insert(0, str(ROOT))

from backend.services import shares_service as ss  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    original_require = ss.require_samba
    original_helper = ss._run_samba_accounts
    original_users = pwd.getpwall
    original_get_user = pwd.getpwnam
    original_groups = grp.getgrall
    try:
        ss.require_samba = lambda: {"installed": True}
        ss._run_samba_accounts = lambda request: {"users": ["alice"]}
        pwd.getpwall = lambda: [
            pwd.struct_passwd(("alice", "x", 1000, 1000, "", "/home/alice", "/bin/bash")),
            pwd.struct_passwd(("nobody", "x", 65534, 65534, "", "/nonexistent", "/usr/sbin/nologin")),
            pwd.struct_passwd(("ethos-nas", "x", 1001, 1001, "", "/var/lib/ethos", "/usr/sbin/nologin")),
            pwd.struct_passwd(("system", "x", 999, 999, "", "/nonexistent", "/usr/sbin/nologin")),
        ]
        pwd.getpwnam = lambda name: pwd.struct_passwd(
            (name, "x", 1000, 1000, "", "/home/" + name, "/bin/bash")
        )
        grp.getgrall = lambda: [
            grp.struct_group(("storage", "x", 1000, ["alice"])),
            grp.struct_group(("nogroup", "x", 65534, [])),
            grp.struct_group(("system", "x", 999, [])),
        ]

        accounts = ss.get_samba_accounts()
        check("account list includes only eligible host users", accounts["users"] == [
            {"username": "alice", "samba_enabled": True},
        ], str(accounts["users"]))
        check("account list includes only eligible groups", accounts["groups"] == [{"name": "storage"}],
              str(accounts["groups"]))

        calls = []
        ss._run_samba_accounts = lambda request: calls.append(request) or {"username": "alice"}
        result = ss.set_samba_password("alice", "a password for tests")
        check("password update requests only the intended action", calls == [{
            "action": "set_password", "username": "alice", "password": "a password for tests",
        }])
        check("password update response excludes the password", "password" not in result, str(result))

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        ss.logger.addHandler(handler)
        try:
            ss._run_samba_accounts = original_helper
            original_run = ss.subprocess.run
            seen = []

            def failing_run(args, **kwargs):
                seen.append((args, kwargs))
                return type("Result", (), {"returncode": 1, "stdout": "secret echoed", "stderr": "secret echoed"})()

            ss.subprocess.run = failing_run
            secret = "unique-secret-not-for-logs"
            try:
                original_helper({"action": "set_password", "username": "alice", "password": secret})
                failed = False
            except ss.SambaAccountError as exc:
                failed = True
                check("helper failure response excludes credential and stderr", secret not in str(exc) and "secret echoed" not in str(exc))
            check("subprocess command contains no password", failed and secret not in repr(seen[0][0]))
            check("password is piped to helper stdin", secret in seen[0][1]["input"])
            check("credential is absent from service logs", secret not in stream.getvalue() and "secret echoed" not in stream.getvalue())
            ss.subprocess.run = original_run
        finally:
            ss.logger.removeHandler(handler)
    finally:
        ss.require_samba = original_require
        ss._run_samba_accounts = original_helper
        pwd.getpwall = original_users
        pwd.getpwnam = original_get_user
        grp.getgrall = original_groups

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())