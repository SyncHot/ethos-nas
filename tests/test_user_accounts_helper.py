#!/usr/bin/env python3
"""Unit tests for the root-owned Linux account helper; all commands are mocked."""

import importlib.machinery
import importlib.util
import pwd
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER_PATH = ROOT / "deploy" / "ethos-user-accounts"
SETUP_PATH = ROOT / "setup_scripts" / "setup_service.sh"
loader = importlib.machinery.SourceFileLoader("ethos_user_accounts", str(HELPER_PATH))
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
    accounts = {
        "alice": pwd.struct_passwd(("alice", "x", 1001, 1001, "", "/home/alice", "/bin/bash")),
        "bob": pwd.struct_passwd(("bob", "x", 1002, 1002, "", "/home/bob", "/bin/bash")),
        "root": pwd.struct_passwd(("root", "x", 0, 0, "", "/root", "/bin/bash")),
        "ethos-nas": pwd.struct_passwd(("ethos-nas", "x", 998, 998, "", "/var/lib/ethos-nas", "/usr/sbin/nologin")),
    }

    def fake_getpwnam(username):
        try:
            return accounts[username]
        except KeyError:
            raise KeyError(username) from None

    def fake_run(args, *, input=None, input_text=None, **_kwargs):
        calls.append((list(args), input if input is not None else input_text))
        if args[0] == "useradd":
            name = args[-1]
            accounts[name] = pwd.struct_passwd((name, "x", 1500, 1500, "", "/home/" + name, "/bin/bash"))
        elif args[0] == "userdel":
            name = args[-1]
            accounts.pop(name, None)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    helper.pwd.getpwnam = fake_getpwnam
    helper.subprocess.run = fake_run

    # ── create ──────────────────────────────────────────────────────────
    calls.clear()
    response = helper.handle_request({
        "action": "create", "username": "newuser", "password": "correct horse 42",
    })
    commands = [args for args, _ in calls]
    check("creates a Linux account with a private group and bash shell",
          ["useradd", "-m", "-U", "-s", "/bin/bash", "newuser"] in commands)
    check("sets the password through chpasswd on stdin",
          (["chpasswd"], "newuser:correct horse 42\n") in calls)
    check("does not expire a freshly chosen password",
          any(args[:2] == ["chage", "-d"] and args[-1] == "newuser" for args, _ in calls))
    check("grants the service user ACL access to the new home",
          any(args[:3] == ["setfacl", "-m", "u:ethos-nas:rwx"] for args, _ in calls)
          and any(args[:2] == ["setfacl", "-d"] for args, _ in calls))
    check("returns uid, home and ACL state",
          response.get("ok") and response.get("uid") == 1500
          and response.get("home") == "/home/newuser" and response.get("acl") is True)

    calls.clear()
    try:
        helper.handle_request({"action": "create", "username": "alice", "password": "correct horse 42"})
        rejected = False
    except helper.HelperError:
        rejected = True
    check("refuses an existing Linux account", rejected and not calls)

    for request, label in (
        ({"action": "create", "username": "root", "password": "correct horse 42"}, "reserved name"),
        ({"action": "create", "username": "Alice", "password": "correct horse 42"}, "uppercase name"),
        ({"action": "create", "username": "bad-", "password": "correct horse 42"}, "trailing dash"),
        ({"action": "create", "username": "../root", "password": "correct horse 42"}, "path traversal"),
        ({"action": "create", "username": "okname", "password": "short"}, "short password"),
        ({"action": "create", "username": "okname", "password": "onlyletters"}, "single character class"),
        ({"action": "create", "username": "okname", "password": "has:colon 123"}, "colon in password"),
    ):
        calls.clear()
        try:
            helper.handle_request(request)
            rejected = False
        except helper.HelperError:
            rejected = True
        check(f"rejects {label}", rejected and not calls)

    # ── password / lock / unlock ────────────────────────────────────────
    calls.clear()
    helper.handle_request({"action": "set_password", "username": "bob", "password": "another secret 7"})
    check("resets a password with chpasswd on stdin",
          (["chpasswd"], "bob:another secret 7\n") in calls)
    check("password never appears in an argument vector",
          all("another secret 7" not in " ".join(args) for args, _ in calls))

    calls.clear()
    helper.handle_request({"action": "lock", "username": "bob"})
    check("locks with usermod -L", calls[0][0] == ["usermod", "-L", "bob"])
    calls.clear()
    helper.handle_request({"action": "unlock", "username": "bob"})
    check("unlocks with usermod -U", calls[0][0] == ["usermod", "-U", "bob"])

    calls.clear()
    try:
        helper.handle_request({"action": "set_password", "username": "root", "password": "correct horse 42"})
        rejected = False
    except helper.HelperError:
        rejected = True
    check("refuses the root account", rejected and not calls)

    # ── delete ──────────────────────────────────────────────────────────
    calls.clear()
    helper.handle_request({"action": "delete", "username": "alice", "remove_home": False})
    check("deletes the account but keeps the home by default", calls[0][0] == ["userdel", "alice"])
    accounts["alice"] = pwd.struct_passwd(("alice", "x", 1001, 1001, "", "/home/alice", "/bin/bash"))

    calls.clear()
    helper.handle_request({"action": "delete", "username": "alice", "remove_home": True})
    check("removes the home only when asked", calls[0][0] == ["userdel", "-r", "alice"])

    calls.clear()
    try:
        helper.handle_request({"action": "delete", "username": "bob", "remove_home": "yes"})
        rejected = False
    except helper.HelperError:
        rejected = True
    check("refuses a non-boolean remove_home", rejected and not calls)

    # ── request shape ───────────────────────────────────────────────────
    calls.clear()
    for request in (
        {"action": "create", "username": "okname", "password": "correct horse 42", "role": "admin"},
        {"action": "set_password", "username": "bob"},
        {"action": "delete", "username": "bob"},
        {"action": "run", "args": ["id"]},
        [],
    ):
        try:
            helper.handle_request(request)
            rejected = False
        except helper.HelperError:
            rejected = True
        check(f"rejects malformed request {request if not isinstance(request, list) else '[list]'}",
              rejected)
    check("malformed requests execute no commands", not calls)

    # ── installer contract ──────────────────────────────────────────────
    setup = SETUP_PATH.read_text(encoding="utf-8")
    helper_text = HELPER_PATH.read_text(encoding="utf-8")
    check("helper requires root", "os.geteuid() != 0" in helper_text)
    check("helper rejects command-line arguments", "len(sys.argv) != 1" in helper_text)
    check("setup installs the helper root-owned",
          'install -m 0755 -o root -g root "${USER_ACCOUNTS_SRC}" "${USER_ACCOUNTS_HELPER_BIN}"' in setup)
    check("setup removes the helper on uninstall",
          'rm -f "${USER_ACCOUNTS_HELPER_BIN}"' in setup)
    check("sudoers grants only the argument-free helper",
          'NOPASSWD: ${USER_ACCOUNTS_HELPER_BIN}"' in setup)

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
