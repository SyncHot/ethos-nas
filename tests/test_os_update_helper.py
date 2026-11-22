#!/usr/bin/env python3
"""Static contract for the privileged OS package update helper.

`deploy/ethos-os-update` runs as root through two argument-exact sudoers rules,
so its safety properties must never regress. This test reads files only (no
execution, no root):

    .venv/bin/python tests/test_os_update_helper.py
"""

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "deploy" / "ethos-os-update"
INSTALLER = ROOT / "setup_scripts" / "setup_service.sh"
UPDATE_HELPER = ROOT / "deploy" / "ethos-update"
CONFIG = ROOT / "backend" / "config.py"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def _embedded_python(script: str) -> str:
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", script, re.DOTALL)
    return match.group(1) if match else ""


def main() -> int:
    helper = HELPER.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    updater = UPDATE_HELPER.read_text(encoding="utf-8")
    config = CONFIG.read_text(encoding="utf-8")

    print("[helper]")
    check("helper exists and is a bash script", helper.startswith("#!/bin/bash"))
    check("helper is executable", os.access(HELPER, os.X_OK))
    check("strict shell mode", "set -euo pipefail" in helper)
    check("refuses non-root", 'id -u' in helper and "must run as root" in helper)
    check("exactly one argument", '[ "$#" -ne 1 ]' in helper)
    check("action allowlist check|apply", "check|apply)" in helper and "unknown action" in helper)
    check("no eval", not re.search(r"\beval\b", helper))
    check("root-owned config file", 'CONF="/etc/ethos-nas/os-update.conf"' in helper)
    check("single instance via flock", "flock -n 9" in helper)
    check("apply survives a service restart",
          "systemd-run --scope" in helper and "ETHOS_OS_UPDATE_SCOPED" in helper)
    check("never installs arbitrary packages",
          '"apt-get", "install"' not in helper and "shell=True" not in helper)
    check("actions are dispatched from a fixed case block",
          'case "${ACTION}" in' in helper and 'exec python3 - "${ACTION}"' in helper)
    check("no dist-upgrade", "dist-upgrade" not in helper)
    check("no autoremove", "autoremove" not in helper)
    check("no purge", re.search(r"\bpurge\b", helper) is None)
    check("noninteractive apt", "DEBIAN_FRONTEND=noninteractive" in helper)
    check("kernel updates via --with-new-pkgs", "--with-new-pkgs" in helper)
    check("config prompts never block",
          "--force-confdef" in helper and "--force-confold" in helper)
    check("waits for the dpkg/apt lock", "DPkg::Lock::Timeout=600" in helper)
    check("disk-space guard", "MIN_FREE_BYTES" in helper)
    check("bounds the package list", "MAX_PACKAGES = 500" in helper)
    check("writes check/job/result JSON",
          '"check.json"' in helper and '"job.json"' in helper and '"result.json"' in helper)
    check("atomic JSON writes", "os.replace(" in helper and "os.fsync(" in helper)
    check("state files owned by the service user", "os.chown(" in helper and "SERVICE_USER" in helper)
    check("state directory handed to the service user",
          'chown "${OWN_UID}:${OWN_GID}" "${STATE_DIR}"' in helper)
    check("detects a required reboot", "/run/reboot-required" in helper)
    check("records a pid for stale-job detection", '"pid": os.getpid()' in helper)

    print("\n[embedded python]")
    embedded = _embedded_python(helper)
    check("helper embeds one python program", bool(embedded))
    if embedded:
        try:
            compile(embedded, "ethos-os-update", "exec")
            check("embedded python parses", True)
        except SyntaxError as exc:
            check("embedded python parses", False, str(exc))
        check("parses apt simulation output", "Inst (\\S+)" in embedded)
        check("marks security-pocket upgrades", "-security" in embedded)

    print("\n[installer]")
    check("installs the helper",
          'install -m 0755 -o root -g root "${OS_UPDATE_SRC}" "${OS_UPDATE_HELPER_BIN}"' in installer)
    check("ships the check rule",
          'RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${OS_UPDATE_HELPER_BIN} check")' in installer)
    check("ships the apply rule",
          'RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${OS_UPDATE_HELPER_BIN} apply")' in installer)
    check("no wildcard rule", "${OS_UPDATE_HELPER_BIN} *" not in installer
          and "${OS_UPDATE_HELPER_BIN}*" not in installer)
    check("writes the root-owned conf",
          'install -m 0644 -o root -g root "${OS_CONF_TMP}" "${OS_UPDATE_CONF}"' in installer)
    check("conf carries DATA_DIR and SERVICE_USER",
          "DATA_DIR='${UPDATE_DATA_DIR}'" in installer and "SERVICE_USER='${TARGET_USER}'" in installer)
    check("removes helper/conf on uninstall",
          'rm -f "${OS_UPDATE_HELPER_BIN}" "${OS_UPDATE_CONF}"' in installer)

    print("\n[core update helper]")
    check("refreshes the OS helper when it is installed",
          '[ -x /usr/local/bin/ethos-os-update ]' in updater
          and 'ethos-os-update.new' in updater and 'deploy/ethos-os-update' in updater)

    print("\n[key consistency]")
    path_match = re.search(r'^OS_UPDATE_HELPER_BIN="([^"]+)"', installer, re.MULTILINE)
    config_match = re.search(r'^    OS_UPDATE_HELPER: str = os\.getenv\("ETHOS_OS_UPDATE_HELPER", "([^"]+)"\)',
                             config, re.MULTILINE)
    check("installer defines the helper path", path_match is not None)
    check("config defines the helper path", config_match is not None)
    if path_match and config_match:
        check("installer and config agree", path_match.group(1) == config_match.group(1),
              f"{path_match.group(1)} != {config_match.group(1)}")

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
