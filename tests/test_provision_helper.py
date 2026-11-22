#!/usr/bin/env python3
"""Static + planner tests for the privileged provisioning helper.

`deploy/ethos-provision` runs as root from the core updater, so its manifest
validation must never regress. No root, no writes to system paths:

    .venv/bin/python tests/test_provision_helper.py
"""

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "deploy" / "ethos-provision"
MANIFEST = ROOT / "deploy" / "provision.json"
INSTALLER = ROOT / "setup_scripts" / "setup_service.sh"
UPDATER = ROOT / "deploy" / "ethos-update"

_results: list[tuple[str, bool, str]] = []

EXPECTED_RULES = [
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-update",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-os-update check",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-os-update apply",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-os-image apply",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-set-password",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-user-accounts",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-samba-accounts",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-system-deps install samba",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-system-deps remove samba",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-system-deps install archive-tools",
    "ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-system-deps remove archive-tools",
]

EXPECTED_HELPERS = {
    "ethos-provision", "ethos-update", "ethos-os-update", "ethos-os-image",
    "ethos-set-password", "ethos-user-accounts", "ethos-samba-accounts",
    "ethos-system-deps",
}


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def _embedded_python(script: str) -> str:
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", script, re.DOTALL)
    return match.group(1) if match else ""


def _namespace() -> dict:
    """Exec the embedded planner with the real checkout as INSTALL_DIR."""
    code = _embedded_python(HELPER.read_text(encoding="utf-8"))
    prelude = code.split("\nif __name__")[0]
    ns = {"__name__": "provision_test"}
    sys.argv = ["ethos-provision", str(ROOT), "ethos-nas", tempfile.mkdtemp(prefix="prov-state-"), "1"]
    exec(compile(prelude, "ethos-provision", "exec"), ns)
    # The checkout on the test machine may be group-writable by a real
    # `ethos-nas` account; the ownership guard has dedicated tests below.
    ns["SERVICE_UID"] = -1
    ns["SERVICE_GIDS"] = set()
    return ns


def _raises(ns: dict, function: str, args, needle: str = "") -> bool:
    try:
        ns[function](*args)
    except ns["ManifestError"] as exc:
        return needle in str(exc)
    return False


def main() -> int:
    helper = HELPER.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    updater = UPDATER.read_text(encoding="utf-8")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    print("[helper]")
    check("helper exists and is a bash script", helper.startswith("#!/bin/bash"))
    check("helper is executable", os.access(HELPER, os.X_OK))
    check("strict shell mode", "set -euo pipefail" in helper)
    check("refuses non-root (except dry run)",
          '"$(id -u)" -ne 0' in helper and 'DRY}' in helper and "must run as root" in helper)
    check("takes no arguments", '[ "$#" -ne 0 ]' in helper and "takes no arguments" in helper)
    check("single instance via flock", "flock -n 9" in helper)
    check("reads the root-owned update.conf", 'CONF="/etc/ethos-nas/update.conf"' in helper)
    check("no eval", not re.search(r"\beval\b", helper))
    check("separate sudoers file", 'SUDOERS_PATH = "/etc/sudoers.d/ethos-nas-provided"' in helper)
    check("never touches the consolidated sudoers file",
          "/etc/sudoers.d/ethos-nas\"" not in helper)
    check("validates sudoers with visudo", '["visudo", "-cf", tmp]' in helper)
    check("backs up and restores", "_backup(" in helper and "_restore(" in helper)
    check("atomic writes", "os.replace(" in helper and "os.fsync(" in helper)
    check("dry run writes nothing", "if DRY:" in helper and "ETHOS_PROVISION_DRY_RUN" in helper)
    check("records a result for the doctor", "result.json" in helper and "write_result(" in helper)
    check("only deploys files from deploy/",
          'if not real.startswith(DEPLOY_DIR + os.sep)' in helper)
    check("no shell execution of manifest values", "shell=True" not in helper)

    print("\n[manifest]")
    check("schema 1", manifest.get("schema") == 1)
    helpers = manifest.get("helpers") or []
    names = {item.get("name") for item in helpers}
    check("lists every deploy helper", names == EXPECTED_HELPERS, str(sorted(names)))
    check("every source is deploy/<name>",
          all(item.get("source") == f"deploy/{item.get('name')}" for item in helpers))
    check("every source exists",
          all((ROOT / item["source"]).is_file() for item in helpers))
    check("unit source", (manifest.get("unit") or {}).get("source") == "deploy/ethos-nas.service")
    check("exact sudoers rules", manifest.get("sudoers") == EXPECTED_RULES)
    check("no wildcards in rules", not any(ch in rule for rule in manifest["sudoers"]
                                           for ch in "*?#"))
    confs = manifest.get("confs") or []
    check("one OS update conf", len(confs) == 1
          and confs[0].get("destination") == "/etc/ethos-nas/os-update.conf")
    check("conf uses placeholders",
          "DATA_DIR='{data_dir}'" in confs[0]["lines"]
          and "SERVICE_USER='{service_user}'" in confs[0]["lines"])

    print("\n[planner]")
    ns = _namespace()
    plan = ns["build_plan"]()
    destinations = [item["dest"] for item in plan["files"]]
    check("plans every helper", all(f"/usr/local/bin/{name}" in destinations
                                    for name in EXPECTED_HELPERS))
    check("plans the unit", "/etc/systemd/system/ethos-nas.service" in destinations)
    check("plans the conf", "/etc/ethos-nas/os-update.conf" in destinations)
    check("sudoers carries every rule", all(rule in plan["sudoers"] for rule in EXPECTED_RULES))
    check("unit is templated for the service user",
          "User=ethos-nas" in next(item["data"] for item in plan["files"]
                                    if item["dest"].endswith("ethos-nas.service")).decode())
    conf_data = next(item["data"] for item in plan["files"]
                     if item["dest"].endswith("os-update.conf")).decode()
    check("conf substitutes paths", "DATA_DIR='" in conf_data and "SERVICE_USER='ethos-nas'" in conf_data)

    print("\n[rejections]")
    check("rejects a non-ethos command",
          _raises(ns, "_plan_sudoers", (["ethos-nas ALL=(root) NOPASSWD: /bin/sh"],), "must start"))
    check("rejects a wildcard",
          _raises(ns, "_plan_sudoers", (["ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-*"],), "forbidden"))
    check("rejects uppercase arguments",
          _raises(ns, "_plan_sudoers", (["ethos-nas ALL=(root) NOPASSWD: /usr/local/bin/ethos-os-update APPLY"],), "lowercase"))
    check("rejects a foreign user",
          _raises(ns, "_plan_sudoers", (["root ALL=(root) NOPASSWD: /usr/local/bin/ethos-update"],), "must start"))
    check("rejects a non-conf destination",
          _raises(ns, "_plan_confs", ([{"destination": "/etc/passwd", "lines": ["X='1'"]}],), "destination"))
    check("rejects rewriting update.conf",
          _raises(ns, "_plan_confs", ([{"destination": "/etc/ethos-nas/update.conf",
                                        "lines": ["X='1'"]}],), "setup_service"))
    check("rejects a shell conf line",
          _raises(ns, "_plan_confs", ([{"destination": "/etc/ethos-nas/x.conf",
                                        "lines": ["X='$(id)'"]}],), "unsafe"))
    check("rejects an unknown placeholder",
          _raises(ns, "_plan_confs", ([{"destination": "/etc/ethos-nas/x.conf",
                                        "lines": ["X='{secret}'"]}],), "placeholder"))
    check("rejects a missing helper file",
          _raises(ns, "_plan_helpers", ([{"name": "ethos-does-not-exist",
                                          "source": "deploy/ethos-does-not-exist"}],), "missing"))
    check("rejects a source outside deploy/",
          _raises(ns, "_plan_helpers", ([{"name": "ethos-update",
                                          "source": "setup_scripts/setup_service.sh"}],), "deploy/"))

    tmp_manifest = Path(tempfile.mkdtemp(prefix="prov-manifest-")) / "provision.json"
    tmp_manifest.write_text(json.dumps({"schema": 1, "helpers": [], "surprise": True}), encoding="utf-8")
    old_manifest = ns["MANIFEST"]
    ns["MANIFEST"] = str(tmp_manifest)
    check("rejects unknown manifest keys",
          _raises(ns, "build_plan", (), "unknown manifest keys"))
    ns["MANIFEST"] = old_manifest

    print("\n[ownership guard]")
    guard_dir = Path(tempfile.mkdtemp(prefix="prov-guard-"))
    guard_file = guard_dir / "helper"
    guard_file.write_text("#!/bin/sh\n", encoding="utf-8")
    guard_file.chmod(0o644)
    old_uid, old_gids = ns["SERVICE_UID"], set(ns["SERVICE_GIDS"])
    try:
        ns["SERVICE_UID"] = os.getuid()
        check("rejects a service-user-owned source",
              _raises(ns, "_ensure_protected", (str(guard_file),), "owned by"))
        ns["SERVICE_UID"] = -1
        guard_file.chmod(0o666)
        check("rejects a world-writable source",
              _raises(ns, "_ensure_protected", (str(guard_file),), "world-writable"))
        guard_file.chmod(0o664)
        ns["SERVICE_GIDS"] = {os.getgid()}
        check("rejects a group-writable source for the service user",
              _raises(ns, "_ensure_protected", (str(guard_file),), "group-writable"))
        ns["SERVICE_GIDS"] = set()
        check("accepts an owner-read-only source",
              ns["_ensure_protected"](str(guard_file)) is None)
    finally:
        ns["SERVICE_UID"], ns["SERVICE_GIDS"] = old_uid, old_gids

    print("\n[atomic write]")
    target_dir = Path(tempfile.mkdtemp(prefix="prov-write-"))
    target = target_dir / "helper"
    ns["_atomic_write"](str(target), b"#!/bin/sh\nexit 0\n", 0o755)
    check("writes content", target.read_bytes().startswith(b"#!/bin/sh"))
    check("sets the mode", stat.S_IMODE(target.stat().st_mode) == 0o755)

    print("\n[installer + updater]")
    check("installer installs the provision helper",
          'install -m 0755 -o root -g root "${PROVISION_SRC}" "${PROVISION_HELPER_BIN}"' in installer)
    check("installer runs provisioning",
          '"${PROVISION_HELPER_BIN}"' in installer and "deploy/provision.json" in installer)
    check("installer warns about a writable checkout",
          "can write ${REPO_DIR}/deploy" in installer)
    check("installer removes the provided sudoers + backups on uninstall",
          '"${PROVISION_SUDOERS_FILE}"' in installer and "/var/lib/ethos-provision" in installer)
    check("updater applies provisioning before restart",
          'step "provision"' in updater and updater.index('step "provision"') < updater.index('step "restart"'))
    check("updater fallback refuses a writable helper",
          "check_helper_source" in updater and "group-writable by" in updater)
    check("updater re-provisions on rollback",
          updater.index('/usr/local/bin/ethos-provision') < updater.index('systemctl restart "${SERVICE_UNIT}" || true'))

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
