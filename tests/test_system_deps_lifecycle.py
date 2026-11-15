#!/usr/bin/env python3
"""Host dependency lifecycle: installed with the package, removed when unused.

In-process, no server, no apt: the privileged helper, the ownership store, the
refcount across installed packages and the release() safety rules are stubbed or
exercised directly.

Run: .venv/bin/python tests/test_system_deps_lifecycle.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-system-deps-life-"))
os.environ.setdefault("ETHOS_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("ETHOS_JWT_SECRET", "system-deps-life-secret")
sys.path.insert(0, str(ROOT))

from backend.services import (  # noqa: E402
    notifications_service, package_jobs, packages_store, system_deps,
)
from backend.services.package_schema import PackageError  # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ── Stubs ───────────────────────────────────────────────────────────────────
_orig_helper = system_deps.helper_available
_orig_run = system_deps._run_helper
_orig_installed = packages_store.load_installed
_orig_get_package = packages_store.get_package
_orig_emit = notifications_service.emit_event

helper_calls = []
events = []
system_deps._run_helper = lambda action, req, job_id: helper_calls.append((action, req))
notifications_service.emit_event = lambda *a, **kw: events.append((a, kw))
system_deps.helper_available = lambda: True

# The binaries are absent on a CI box; pretend the probe result explicitly.
_probe_state = {"samba": False}
_orig_probe_one = system_deps.probe_one
def _probe_one(requirement):
    item = _orig_probe_one(requirement)
    if requirement == "samba":
        installed = _probe_state["samba"]
        item["ok"] = installed
        item["missing_tools"] = [] if installed else ["smbd", "testparm", "smbpasswd"]
    return item
system_deps.probe_one = _probe_one


def _installed(*records):
    packages_store.load_installed = lambda: list(records)


def _record(pkg_id, requires):
    record = {"id": pkg_id, "system_requires": list(requires)}
    packages_store.get_package = lambda pid, _r=record: dict(_r) if pid == _r["id"] else None
    return record


def _reset_store():
    system_deps.OWNERS_STORE.save({"owned": {}})


try:
    print("Install (ensure)")
    _reset_store()
    helper_calls.clear()
    record = _record("ethos-shares", ["samba"])
    _installed(record)

    # helper absent -> 503 with the setup hint, nothing installed
    system_deps.helper_available = lambda: False
    try:
        system_deps.ensure(["samba"], None, "ethos-shares")
        check("missing helper aborts the install", False, "no exception")
    except PackageError as exc:
        check("missing helper aborts with 503", exc.status == 503, f"status={exc.status}")
        check("503 points at setup_service.sh", "setup_service.sh" in exc.detail, exc.detail[:120])
    check("nothing recorded without the helper", system_deps.owned_requirements() == {})

    # helper present -> installs and records ownership
    system_deps.helper_available = lambda: True
    _probe_state["samba"] = False

    def _fake_install(action, req, job_id):
        helper_calls.append((action, req))
        _probe_state["samba"] = True  # apt would have installed it
    system_deps._run_helper = _fake_install

    system_deps.ensure(["samba"], None, "ethos-shares")
    check("helper install called once", helper_calls == [("install", "samba")], str(helper_calls))
    owned = system_deps.owned_requirements()
    check("ownership recorded", "samba" in owned, str(owned))
    check("ownership names the package", owned.get("samba", {}).get("package_id") == "ethos-shares",
          str(owned.get("samba")))
    check("ownership stores the apt package", owned.get("samba", {}).get("apt") == "samba")
    check("probe reports the requirement as EthOS-owned",
          system_deps.probe_one("samba")["owned"] is True)

    # already satisfied -> no helper run
    helper_calls.clear()
    system_deps.ensure(["samba"], None, "ethos-shares")
    check("satisfied requirement never calls the helper", helper_calls == [], str(helper_calls))

    print("Release (uninstall)")
    # another installed package still declares samba -> keep it
    other = {"id": "ethos-photos", "system_requires": ["samba"]}
    other_record = _record("ethos-shares", ["samba"])
    packages_store.get_package = lambda pid: (
        {"id": "ethos-shares", "system_requires": ["samba"]} if pid == "ethos-shares" else None
    )
    _installed(other, other_record)
    helper_calls.clear()
    removed = system_deps.release("ethos-shares")
    check("kept while another package needs it", removed == [], str(removed))
    check("no helper call while still needed", helper_calls == [], str(helper_calls))
    check("ownership survives while still needed", "samba" in system_deps.owned_requirements())

    # last consumer -> removed
    _installed(other_record)
    helper_calls.clear()
    removed = system_deps.release("ethos-shares")
    check("removed with the last package", removed == ["samba"], str(removed))
    check("helper remove called", helper_calls == [("remove", "samba")], str(helper_calls))
    check("ownership cleared", "samba" not in system_deps.owned_requirements())
    check("operator is notified", any("Removed" in str(a[0]) for a in events), str(events))

    # not EthOS-owned -> never removed
    _reset_store()
    helper_calls.clear()
    _installed(other_record)
    removed = system_deps.release("ethos-shares")
    check("software installed by hand is never removed", removed == [], str(removed))
    check("no helper call for unowned software", helper_calls == [], str(helper_calls))

    print("Docker is shared infrastructure")
    _reset_store()
    system_deps.OWNERS_STORE.save({"owned": {"docker": {"apt": "docker.io", "package_id": "x"}}})
    docker_record = _record("ethos-vmm", ["docker"])
    _installed(docker_record)
    helper_calls.clear()
    removed = system_deps.release("ethos-vmm")
    check("docker is never auto-removed", removed == [], str(removed))
    check("no helper call for docker", helper_calls == [], str(helper_calls))
    check("docker ownership is untouched", "docker" in system_deps.owned_requirements())

    print("Refcount helper")
    _installed({"id": "a", "system_requires": ["samba"]}, {"id": "b", "system_requires": ["docker"]})
    check("required_by_others finds the other owner",
          system_deps.required_by_others("samba", exclude_package="z") == ["a"])
    check("required_by_others skips the caller",
          system_deps.required_by_others("samba", exclude_package="a") == [])
    check("required_by_others ignores unrelated packages",
          system_deps.required_by_others("docker", exclude_package="z") == ["b"])
    _installed({"id": "legacy", "manifest": {"system_requires": ["samba"]}})
    check("manifest-nested requirements count",
          system_deps.required_by_others("samba", exclude_package="z") == ["legacy"])

    print("Job plan")
    plan = package_jobs.STEP_PLANS["uninstall"]
    check("uninstall plan has a sysdeps step", "sysdeps" in plan, str(plan))
    check("sysdeps runs before cleanup", plan.index("sysdeps") < plan.index("cleanup"), str(plan))
finally:
    system_deps.helper_available = _orig_helper
    system_deps._run_helper = _orig_run
    system_deps.probe_one = _orig_probe_one
    packages_store.load_installed = _orig_installed
    packages_store.get_package = _orig_get_package
    notifications_service.emit_event = _orig_emit

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    for name in failed:
        print(f"  FAILED: {name}")
    sys.exit(1)
print("OK — host dependencies follow the package lifecycle")
sys.exit(0)
