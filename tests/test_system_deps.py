#!/usr/bin/env python3
"""Host dependency preflight (`system_requires`).

In-process, no server, no Docker: the registry mapping, the binary probe, the
503 with an actionable apt hint, the Docker delegation, and the Samba service
guard that keeps smb.conf untouched when Samba is absent.

Run: .venv/bin/python tests/test_system_deps.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-system-deps-"))
os.environ.setdefault("ETHOS_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("ETHOS_JWT_SECRET", "system-deps-secret")
sys.path.insert(0, str(ROOT))

from backend.services import docker_host, shares_service, system_deps  # noqa: E402
from backend.services.package_schema import (  # noqa: E402
    PACKAGE_SYSTEM_REQUIRES, PackageError,
)

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


print("Registry")
check("allowlist is derived from the registry",
      PACKAGE_SYSTEM_REQUIRES == frozenset(system_deps.SYSTEM_REQUIREMENTS))
check("samba is a known requirement", "samba" in system_deps.SYSTEM_REQUIREMENTS)
check("samba needs smbd + testparm",
      set(system_deps.SYSTEM_REQUIREMENTS["samba"]["bins"]) == {"smbd", "testparm", "smbpasswd"})
check("only docker is provisionable",
      {k for k, v in system_deps.SYSTEM_REQUIREMENTS.items() if v.get("provisionable")} == {"docker"})
check("install hint is the apt command",
      system_deps.install_hint("samba") == "sudo apt install samba")
check("unknown requirement has no hint", system_deps.install_hint("nginx") == "")

print("Probe")
probe = system_deps.probe(["samba", "docker", "samba"])
check("probe deduplicates ids", [item["id"] for item in probe] == ["samba", "docker"])
check("probe reports label and tools",
      probe[0]["label"] == "Samba" and isinstance(probe[0]["missing_tools"], list))
check("probe tolerates None", system_deps.probe(None) == [])
unknown = system_deps.probe_one("nvidia")
check("unknown id reports not ok", unknown["ok"] is False and unknown["error"])

# Force both branches of the probe without touching the host.
_orig_has = system_deps.has_binary
system_deps.has_binary = lambda name: False
try:
    missing = system_deps.missing(["samba"])
    check("missing() surfaces the unsatisfied requirement",
          len(missing) == 1 and missing[0]["id"] == "samba")
    check("missing_tools lists every binary when none exist",
          missing[0]["missing_tools"] == ["smbd", "testparm", "smbpasswd"])

    print("ensure()")
    try:
        system_deps.ensure(["samba"])
        check("ensure raises for a missing host requirement", False, "no exception")
    except PackageError as exc:
        check("ensure raises 503", exc.status == 503, f"status={exc.status}")
        check("detail names the missing binaries", "smbd" in exc.detail, exc.detail[:120])
        check("detail carries the install command", "sudo apt install samba" in exc.detail,
              exc.detail[:160])

    calls = []
    _orig_ensure = docker_host.ensure_system_deps
    docker_host.ensure_system_deps = lambda requires, job_id=None: calls.append(requires)
    try:
        system_deps.ensure(["docker"])
        check("docker is delegated to the privileged helper", calls == [["docker"]], str(calls))
        calls.clear()
        system_deps.ensure([])
        system_deps.ensure(None)
        check("no requirements means no probing", calls == [], str(calls))
    finally:
        docker_host.ensure_system_deps = _orig_ensure

    print("Samba service guard")
    status = shares_service.get_status()
    check("status reports samba_installed", status["samba_installed"] is False, str(status))
    check("status lists missing tools", status["missing_tools"] == ["smbd", "testparm", "smbpasswd"],
          str(status["missing_tools"]))
    check("status carries the install hint", status["install_hint"] == "sudo apt install samba")

    try:
        shares_service.add_share(name="PreflightShare", path=str(TMP))
        check("add_share refuses without Samba", False, "no exception")
    except shares_service.SambaUnavailable as exc:
        check("add_share raises SambaUnavailable", True)
        check("SambaUnavailable explains the fix", "sudo apt install samba" in exc.detail)
    except Exception as exc:  # noqa: BLE001
        check("add_share raises SambaUnavailable", False, f"got {type(exc).__name__}: {exc}")

    smb_conf = Path(shares_service.SMB_CONF)
    check("smb.conf was never written", not Path(str(smb_conf) + ".tmp").exists())
finally:
    system_deps.has_binary = _orig_has

print("Satisfied host")
if system_deps.has_binary("smbd"):
    check("this host has Samba", system_deps.is_satisfied("samba") is True)
    try:
        system_deps.ensure(["samba"])
        check("ensure does not raise on a satisfied requirement", True)
    except PackageError as exc:
        check("ensure does not raise on a satisfied requirement", False, exc.detail)
else:
    print("  [SKIP] Samba is not installed on this host")

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    for name in failed:
        print(f"  FAILED: {name}")
    sys.exit(1)
print("OK — system dependency preflight works")
sys.exit(0)
