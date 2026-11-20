#!/usr/bin/env python3
"""First-run setup ("claim this appliance") — routers/setup.py.

Runs the real app in-process with a temp data dir, a temp setup state file and
a recording stand-in for the root helper, so the privileged boundary is checked
without needing root:

  * /api/setup/status is public and never leaks the code,
  * a wrong code is refused and never reaches the helper,
  * a weak password is refused before any privileged process is spawned,
  * the right code sets the password once, returns a JWT and marks the state,
  * a second claim is refused (the image can be provisioned exactly once),
  * repeated guesses are rate-limited.

PAM is stubbed: the test host has no `ethosadmin` account, and what matters
here is the flow around the helper, not /etc/shadow.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(tempfile.mkdtemp(prefix="ethos-setup-claim-"))
STATE = DATA / "setup.json"
RECORD = DATA / "helper-request.json"
HELPER = DATA / "fake-set-password"
# Stand-in for deploy/ethos-set-password: records the request and marks the
# state claimed, exactly like the real helper (see test_set_password_helper.py
# for the helper's own checks).
HELPER.write_text(
    "#!/usr/bin/env python3\n"
    "import datetime, json, sys\n"
    f"request = json.load(sys.stdin)\n"
    f"open({str(RECORD)!r}, 'w').write(json.dumps(request))\n"
    f"state = json.load(open({str(STATE)!r}))\n"
    "state['claimed_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()\n"
    f"json.dump(state, open({str(STATE)!r}, 'w'))\n"
    "print('{\"ok\": true}')\n",
    encoding="utf-8",
)
HELPER.chmod(0o755)

os.environ.update({
    "ETHOS_DATA_DIR": str(DATA),
    "ETHOS_JWT_SECRET": "setup-claim-test-secret",
    "ETHOS_SETUP_STATE": str(STATE),
    "ETHOS_SET_PASSWORD_HELPER": str(HELPER),   # no ETHOS_SET_PASSWORD... → no sudo
    "ETHOS_PACKAGES_DEFAULT_SOURCE": "0",
})
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
import backend.routers.auth as auth_module  # noqa: E402
import backend.routers.setup as setup_module  # noqa: E402
from backend.main import app  # noqa: E402

CODE = "ABCD-EFGH"
PASSWORD = "Passw0rdPass"
GOOD_USER = "ethosadmin"

# The helper would chpasswd for real; PAM is stubbed to accept the password the
# flow just set.
auth_module.pam_authenticate = lambda username, password: password == PASSWORD

client = TestClient(app)
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    suffix = f" — {detail}" if detail and not ok else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}")


def reset(code=CODE, claimed=False):
    state = {"user": GOOD_USER, "code": code}
    if claimed:
        state["claimed_at"] = "2026-01-01T00:00:00+00:00"
    STATE.write_text(json.dumps(state), encoding="utf-8")
    RECORD.unlink(missing_ok=True)
    setup_module._ATTEMPTS.clear()


def helper_request():
    if not RECORD.exists():
        return None
    return json.loads(RECORD.read_text(encoding="utf-8"))


print("status endpoint")
reset()
resp = client.get("/api/setup/status")
body = resp.json()
check("public (no token needed)", resp.status_code == 200, str(resp.status_code))
check("reports the pending account", body.get("user") == GOOD_USER, str(body))
check("code is never returned", "code" not in body and CODE not in resp.text, resp.text[:120])

reset(claimed=True)
check("claimed appliance needs no setup", client.get("/api/setup/status").json().get("required") is False)
STATE.unlink()
check("no setup state means nothing to claim", client.get("/api/setup/status").json().get("required") is False)

print("claim endpoint")
reset()
resp = client.post("/api/setup/claim", json={"code": "WRON-CODE", "password": PASSWORD})
check("wrong code refused", resp.status_code == 403, str(resp.status_code))
check("wrong code never reaches the helper", helper_request() is None)

resp = client.post("/api/setup/claim", json={"code": CODE, "password": "short"})
check("weak password refused", resp.status_code == 400, str(resp.status_code))
check("weak password never reaches the helper", helper_request() is None)

resp = client.post("/api/setup/claim", json={"code": CODE, "password": PASSWORD, "extra": 1})
check("unknown field rejected", resp.status_code == 422, str(resp.status_code))

reset()
resp = client.post("/api/setup/claim", json={"code": "abcd efgh", "password": PASSWORD})
body = resp.json()
check("code accepted case- and dash-insensitively", resp.status_code == 200, str(body)[:160])
check("response carries a JWT", bool(body.get("token")))
check("response identifies the admin", (body.get("user") or {}).get("username") == GOOD_USER, str(body.get("user")))

request = helper_request() or {}
check("helper received the account", request.get("username") == GOOD_USER, str(request))
check("helper received the password", request.get("password") == PASSWORD)
check("helper asked for the setup action", request.get("action") == "setup-password", str(request))

marked = json.loads(STATE.read_text(encoding="utf-8"))
check("state is marked claimed", bool(marked.get("claimed_at")), str(marked))
check("state keeps no password", "password" not in marked and PASSWORD not in STATE.read_text())

print("single use")
RECORD.unlink(missing_ok=True)
resp = client.post("/api/setup/claim", json={"code": CODE, "password": PASSWORD})
check("second claim refused", resp.status_code == 409, str(resp.status_code))
check("second claim never reaches the helper", helper_request() is None)
check("status reports setup is done", client.get("/api/setup/status").json().get("required") is False)

print("rate limit")
reset()
statuses = [
    client.post("/api/setup/claim", json={"code": f"BAD{i}CODE", "password": PASSWORD}).status_code
    for i in range(setup_module.MAX_ATTEMPTS + 1)
]
check("first attempts are rejected but allowed",
      all(code == 403 for code in statuses[:setup_module.MAX_ATTEMPTS]), str(statuses))
check("next attempt is rate-limited", statuses[-1] == 429, str(statuses))
check("rate limit did not reach the helper", helper_request() is None)

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
