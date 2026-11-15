#!/usr/bin/env python3
"""Host Docker Engine status and provisioning helpers (no server, no Docker).

The privileged install path is stubbed: these tests assert the *decisions*
(installed / needs restart / helper missing / conflict), never run apt.

Run: .venv/bin/python tests/test_docker_host.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-docker-host-"))
os.environ.setdefault("ETHOS_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("ETHOS_JWT_SECRET", "docker-host-test-secret")
sys.path.insert(0, str(ROOT))

from backend.services import docker_host, package_jobs  # noqa: E402
from backend.services.package_schema import PackageError  # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _raises(fn, status=None):
    try:
        fn()
    except PackageError as exc:
        return status is None or exc.status == status
    return False


class _P:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _status(**overrides):
    base = {
        "ok": False, "cli": False, "daemon": False, "version": "", "compose": "",
        "socket": {"path": "/var/run/docker.sock", "exists": False},
        "group_member": False, "group_granted": False, "restart_required": False,
        "helper": False, "install_supported": False, "conflict": "",
        "os": {"id": "ubuntu", "version": "24.04"},
    }
    base.update(overrides)
    return base


print("engine_status — nothing installed")
_orig = {
    "which": docker_host.shutil.which, "run": docker_host._run,
    "helper": docker_host.HELPER_PATH, "group": docker_host._docker_group,
    "conflict": docker_host._conflict, "engine_status": docker_host.engine_status,
    "subprocess_run": docker_host.subprocess.run, "emit_event": docker_host.emit_event,
    "install_docker": docker_host.install_docker,
    "ensure_system_deps": docker_host.ensure_system_deps,
    "socket": docker_host.SOCKET_PATH,
}
try:
    docker_host.HELPER_PATH = TMP / "missing-helper"
    docker_host.shutil.which = lambda name: None
    docker_host._run = lambda cmd, timeout=8: None
    docker_host._docker_group = lambda: None
    docker_host._conflict = lambda: ""
    absent = docker_host.engine_status()
    check("no CLI reported", absent["cli"] is False and absent["daemon"] is False)
    check("helper absent reported", absent["helper"] is False)
    check("install unsupported without helper", absent["install_supported"] is False)
    check("no restart required", absent["restart_required"] is False)

    print("engine_status — daemon and Compose present")
    docker_host.shutil.which = lambda name: "/usr/bin/docker" if name == "docker" else None
    docker_host._run = lambda cmd, timeout=8: (
        _P(stdout="27.3.1\n") if cmd[1] == "version" else _P(stdout="v2.29.1\n")
    )
    healthy = docker_host.engine_status()
    check("version parsed", healthy["version"] == "27.3.1", healthy["version"])
    check("compose parsed", healthy["compose"] == "v2.29.1", healthy["compose"])
    check("daemon true", healthy["daemon"] is True and healthy["ok"] is True)

    print("group membership")
    group = SimpleNamespace(gr_gid=999, gr_mem=[])
    docker_host._docker_group = lambda: group
    real_getgroups = os.getgroups
    try:
        os.getgroups = lambda: [999]
        check("process in docker group", docker_host._process_has_docker_group() is True)
        os.getgroups = lambda: [0]
        check("process not in docker group", docker_host._process_has_docker_group() is False)
    finally:
        os.getgroups = real_getgroups
    docker_host._process_has_docker_group = lambda: False
    docker_host._user_granted_docker_group = lambda: True
    docker_host.shutil.which = lambda name: "/usr/bin/docker" if name == "docker" else None
    docker_host._run = lambda cmd, timeout=8: _P(returncode=1, stderr="permission denied")
    docker_host.SOCKET_PATH = TMP / "docker.sock"
    docker_host.SOCKET_PATH.write_text("", encoding="utf-8")
    restart = docker_host.engine_status()
    check("installed but unreadable => restart required", restart["restart_required"] is True)
    check("unreadable socket => daemon false", restart["daemon"] is False)
finally:
    docker_host.shutil.which = _orig["which"]
    docker_host._run = _orig["run"]
    docker_host.HELPER_PATH = _orig["helper"]
    docker_host._docker_group = _orig["group"]
    docker_host._conflict = _orig["conflict"]
    docker_host.subprocess.run = _orig["subprocess_run"]
    docker_host.emit_event = _orig["emit_event"]
    docker_host.SOCKET_PATH = Path("/var/run/docker.sock")

print("install_docker")
try:
    docker_host.HELPER_PATH = TMP / "missing-helper"
    docker_host.engine_status = lambda: _status()
    check("refused without helper (503)", _raises(lambda: docker_host.install_docker(None), 503))

    helper = TMP / "ethos-docker-install"
    helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    docker_host.HELPER_PATH = helper
    docker_host.engine_status = lambda: _status(helper=True, install_supported=True,
                                                conflict="podman shim present")
    check("refused on engine conflict (409)", _raises(lambda: docker_host.install_docker(None), 409))

    docker_host.engine_status = lambda: _status(helper=True, install_supported=False,
                                                os={"id": "centos", "version": "9"})
    check("refused on unsupported distribution (400)",
          _raises(lambda: docker_host.install_docker(None), 400))

    calls = []
    docker_host.engine_status = lambda: _status(helper=True, install_supported=True)
    docker_host.subprocess.run = lambda cmd, **kw: (calls.append(cmd) or _P(stdout="OK docker=27.3.1\n"))
    docker_host.emit_event = lambda *a, **kw: None
    # After installing, the engine answers.
    docker_host.engine_status = iter([
        _status(helper=True, install_supported=True),
        _status(cli=True, daemon=True, version="27.3.1", compose="v2.29.1"),
    ]).__next__
    job = package_jobs.create("docker-install", "system:docker")
    result = docker_host.install_docker(job["id"])
    check("helper invoked with sudo -n and no args",
          calls == [["sudo", "-n", str(helper)]], str(calls))
    check("daemon result returned", result["daemon"] is True and result["version"] == "27.3.1")
    check("job step reached verify", package_jobs.get(job["id"])["step"] == "verify")
    check("job log mentions install", any("installing" in line.lower()
                                          for line in package_jobs.get(job["id"])["log"]))
finally:
    docker_host.HELPER_PATH = _orig["helper"]
    docker_host.engine_status = _orig["engine_status"]
    docker_host.subprocess.run = _orig["subprocess_run"]
    docker_host.emit_event = _orig["emit_event"]

print("ensure_system_deps")
try:
    calls = []
    docker_host.engine_status = lambda: calls.append(1) or _status(daemon=True, version="27.3.1")
    docker_host.ensure_system_deps([], None)
    check("no requirement => no probe", calls == [], str(len(calls)))

    docker_host.ensure_system_deps(["docker"], None)
    check("engine present => no error", True)

    docker_host.engine_status = lambda: _status(helper=False)
    check("missing helper raises actionable 503",
          _raises(lambda: docker_host.ensure_system_deps(["docker"], None), 503))
    try:
        docker_host.ensure_system_deps(["docker"], None)
    except PackageError as exc:
        check("message points at the Docker Manager", "Docker Manager" in exc.detail, exc.detail)

    installed = []
    seq = iter([_status(helper=True, install_supported=True),
                _status(cli=True, daemon=True, version="27.3.1")])
    docker_host.engine_status = lambda: next(seq)
    docker_host.install_docker = lambda job_id=None: installed.append(job_id)
    docker_host.ensure_system_deps(["docker"], None)
    check("auto-installs when helper is available", installed == [None], str(installed))

    seq = iter([_status(helper=True, install_supported=True), _status(cli=True)])
    docker_host.engine_status = lambda: next(seq)
    installed.clear()
    check("daemon still unreachable after install => 503",
          _raises(lambda: docker_host.ensure_system_deps(["docker"], None), 503))
finally:
    docker_host.engine_status = _orig["engine_status"]
    docker_host.install_docker = _orig["install_docker"]

print("Job step plans")
check("install runs deps after policy",
      package_jobs.STEP_PLANS["install"].index("deps") ==
      package_jobs.STEP_PLANS["install"].index("policy") + 1,
      str(package_jobs.STEP_PLANS["install"]))
check("update runs deps after policy",
      package_jobs.STEP_PLANS["update"].index("deps") ==
      package_jobs.STEP_PLANS["update"].index("policy") + 1)
check("docker-install plan is ordered",
      package_jobs.STEP_PLANS["docker-install"] == ("check", "install", "verify", "done"))

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
