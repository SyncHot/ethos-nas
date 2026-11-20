#!/usr/bin/env python3
"""deploy/ethos-set-password — the root side of the first-run setup.

This helper is the security boundary: the API runs unprivileged and can only
ask it to set *one* password, for the account the image recorded, and only
while the appliance is unclaimed. The checks below run without root by loading
the script as a module and stubbing the two commands it shells out to, so the
refusals are tested against the real code rather than a copy.
"""
import json
import re
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="ethos-set-password-"))
STATE = DATA / "setup.json"

helper = SourceFileLoader("ethos_set_password", str(ROOT / "deploy" / "ethos-set-password")).load_module()
helper.STATE_FILE = STATE

results = []
CALLS = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    suffix = f" — {detail}" if detail and not ok else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}")


def fake_run(args, *, stdin=None):
    CALLS.append({"args": list(args), "stdin": stdin})


helper._run = fake_run
# The real helper looks up the `ethos-nas` group; a dev box may not have it.
helper.os.chown = lambda *a, **k: None


EXISTING = {"ethosadmin", "someone"}


class _Account:
    pw_uid = 1000
    pw_dir = "/home/ethosadmin"
    pw_gid = 1000


def _getpwnam(name):
    if name in EXISTING:
        return _Account()
    raise KeyError(name)


class _Group:
    gr_name = "ethosadmin"
    gr_gid = 1000


# ...and the admin account only exists inside an EthOS image.
helper.pwd = type("_Pwd", (), {"getpwnam": staticmethod(_getpwnam)})
helper.grp = type("_Grp", (), {
    "getgrgid": staticmethod(lambda gid: _Group()),
    "getgrnam": staticmethod(lambda name: _Group()),
})


def reset(claimed=False, user="ethosadmin"):
    state = {"user": user, "code": "ABCD-EFGH"}
    if claimed:
        state["claimed_at"] = "2026-01-01T00:00:00+00:00"
    STATE.write_text(json.dumps(state), encoding="utf-8")
    CALLS.clear()


def attempt(**request):
    try:
        return helper.handle_request(request), None
    except helper.HelperError as exc:
        return None, str(exc)


print("refusals")
reset()
_, error = attempt(action="setup-password", username="root", password="Passw0rdPass")
check("other accounts are refused", error is not None, str(error))
check("nothing is executed for a foreign account", not CALLS)

_, error = attempt(action="setup-password", username="ethosadmin", password="short")
check("short password refused", error is not None and "10" in str(error), str(error))
check("nothing is executed for a weak password", not CALLS)

_, error = attempt(action="setup-password", username="ethosadmin", password="abcdefghij")
check("single-character-class password refused", error is not None, str(error))

_, error = attempt(action="delete-user", username="ethosadmin", password="Passw0rdPass")
check("unknown action refused", error is not None, str(error))

_, error = attempt(action="setup-password", username="ethosadmin", password="Passw0rdPass", extra=1)
check("unknown field refused", error is not None, str(error))

reset(claimed=True)
_, error = attempt(action="setup-password", username="ethosadmin", password="Passw0rdPass")
check("claimed appliance refuses further changes", error is not None, str(error))
check("nothing is executed after claim", not CALLS)

STATE.unlink()
_, error = attempt(action="setup-password", username="ethosadmin", password="Passw0rdPass")
check("missing state file refuses (stock install)", error is not None, str(error))

print("success path")
reset()
response, error = attempt(action="setup-password", username="ethosadmin", password="Passw0rdPass")
check("valid claim accepted", response == {"ok": True, "username": "ethosadmin"}, str(error or response))
commands = [call["args"][0] for call in CALLS]
check("password written with chpasswd", commands == ["chpasswd", "chage"], str(commands))
check("password travels on stdin, never argv",
      CALLS[0]["stdin"] == "ethosadmin:Passw0rdPass\n" and "Passw0rdPass" not in " ".join(CALLS[0]["args"]),
      str(CALLS[0]))
check("expiry is cleared for the chosen password",
      CALLS[1]["args"][:2] == ["chage", "-d"] and CALLS[1]["args"][-1] == "ethosadmin", str(CALLS[1]))

state = json.loads(STATE.read_text(encoding="utf-8"))
check("state marked claimed", bool(state.get("claimed_at")), str(state))
check("state stores no password", "Passw0rdPass" not in STATE.read_text(encoding="utf-8"))
check("state stays readable by the service group", re.fullmatch(r"0?640", oct(STATE.stat().st_mode)[-3:]) is not None,
      oct(STATE.stat().st_mode))

CALLS.clear()
_, error = attempt(action="setup-password", username="ethosadmin", password="Passw0rdPass")
check("second use refused", error is not None, str(error))
check("no commands on second use", not CALLS)

print("choosing a username")
reset()
response, error = attempt(action="setup-password", username="ethosadmin",
                           password="Passw0rdPass", new_username="marcin")
check("rename accepted", response == {"ok": True, "username": "marcin"}, str(error or response))
commands = [call["args"] for call in CALLS]
check("account renamed with usermod",
      any(args[:3] == ["usermod", "-l", "marcin"] for args in commands), str(commands))
check("private group renamed too",
      any(args[:3] == ["groupmod", "-n", "marcin"] for args in commands), str(commands))
check("password set on the new name",
      any(args[0] == "chpasswd" and call["stdin"] == "marcin:Passw0rdPass\n"
          for args, call in zip(commands, CALLS)), str(commands))
state = json.loads(STATE.read_text(encoding="utf-8"))
check("state records the new username", state.get("user") == "marcin", str(state))

for bad, why in (("Root", "uppercase"), ("-bad", "leading dash"), ("a" * 33, "too long"),
                 ("bad name", "space"), ("root", "reserved"), ("someone", "taken")):
    reset()
    _, error = attempt(action="setup-password", username="ethosadmin",
                       password="Passw0rdPass", new_username=bad)
    check(f"invalid username refused ({why})", error is not None, str(error))
    check(f"nothing executed for {why}", not CALLS)

reset()
response, error = attempt(action="setup-password", username="ethosadmin",
                          password="Passw0rdPass", new_username="ethosadmin")
check("keeping the image name works", response == {"ok": True, "username": "ethosadmin"}, str(error))
check("no rename when the name is unchanged",
      not any(call["args"][0] in ("usermod", "groupmod") for call in CALLS), str(CALLS))

print("input handling")
reset()
for payload in ("", "not json", "[]"):
    try:
        helper.handle_request(json.loads(payload) if payload else None)
        ok = False
    except (helper.HelperError, ValueError):
        ok = True
    check(f"garbage request refused ({payload or 'null'})", ok)

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
