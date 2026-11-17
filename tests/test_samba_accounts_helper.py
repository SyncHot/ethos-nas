#!/usr/bin/env python3
"""Unit tests for the root-owned Samba passdb helper; all commands are mocked."""

import importlib.machinery
import importlib.util
import pwd
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER_PATH = ROOT / "deploy" / "ethos-samba-accounts"
SETUP_PATH = ROOT / "setup_scripts" / "setup_service.sh"
loader = importlib.machinery.SourceFileLoader("ethos_samba_accounts", str(HELPER_PATH))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = helper
loader.exec_module(helper)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    calls = []
    helper.pwd.getpwnam = lambda username: pwd.struct_passwd(
        (username, "x", 1001, 1001, "", "/home/" + username, "/bin/bash")
    )

    def fake_run(args, *, input=None, input_text=None, **_kwargs):
        calls.append((args, input if input is not None else input_text))
        output = "alice:1001:Alice\n" if args == ["pdbedit", "-L"] else ""
        return type("Result", (), {"returncode": 0, "stdout": output})()

    helper.subprocess.run = fake_run

    check("lists Samba users from passdb", helper.handle_request({"action": "list"}) == {"users": ["alice"]})
    response = helper.handle_request({"action": "set_password", "username": "alice", "password": "correct horse"})
    check("sets a password for an existing Samba account", response["ok"] and calls[-1][0] == ["smbpasswd", "-s", "alice"])
    check("password is supplied only over stdin", calls[-1][1] == "correct horse\ncorrect horse\n")

    calls.clear()
    helper.handle_request({"action": "set_password", "username": "newuser", "password": "correct horse"})
    check("adds an existing Unix account to passdb", calls[-1][0] == ["smbpasswd", "-s", "-a", "newuser"])

    calls.clear()
    helper.handle_request({"action": "delete", "username": "alice"})
    check("removes only the Samba account", calls[-1][0] == ["smbpasswd", "-x", "alice"])

    before = len(calls)
    for request in (
        {"action": "set_password", "username": "../../root", "password": "correct horse"},
        {"action": "set_password", "username": "alice", "password": "short"},
        {"action": "set_password", "username": "alice", "password": "password\n-a root"},
        {"action": "delete", "username": "root"},
        {"action": "run", "args": ["id"]},
    ):
        try:
            helper.handle_request(request)
            rejected = False
        except helper.HelperError:
            rejected = True
        check(f"rejects unsafe request {request['action']}", rejected)
    check("invalid requests execute no commands", len(calls) == before)

    setup = SETUP_PATH.read_text(encoding="utf-8")
    helper_text = HELPER_PATH.read_text(encoding="utf-8")
    check("helper requires root", "os.geteuid() != 0" in helper_text)
    check("helper rejects command-line arguments", "len(sys.argv) != 1" in helper_text)
    check("setup installs the helper root-owned", "install -m 0755 -o root -g root \"${SAMBA_ACCOUNTS_SRC}\" \"${SAMBA_ACCOUNTS_HELPER_BIN}\"" in setup)
    check("setup removes the helper on uninstall", 'rm -f "${SAMBA_ACCOUNTS_HELPER_BIN}"' in setup)
    check("sudoers grants only the argument-free helper", 'NOPASSWD: ${SAMBA_ACCOUNTS_HELPER_BIN}"' in setup)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())