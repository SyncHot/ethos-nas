#!/usr/bin/env python3
"""Static contract for the privileged core-update helper.

`deploy/ethos-update` runs as root through one argument-free sudoers rule, so
its safety properties must never regress. This test reads the files (no
execution, no root):

    .venv/bin/python tests/test_update_helper.py
"""

import base64
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "deploy" / "ethos-update"
INSTALLER = ROOT / "setup_scripts" / "setup_service.sh"
CONFIG = ROOT / "backend" / "config.py"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def main() -> int:
    helper = HELPER.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    config = CONFIG.read_text(encoding="utf-8")

    print("[helper]")
    check("helper exists and is a bash script", helper.startswith("#!/bin/bash"))
    check("strict shell mode", "set -euo pipefail" in helper)
    check("refuses non-root", 'id -u' in helper and "must run as root" in helper)
    check("refuses any argument", '[ "$#" -ne 0 ]' in helper and "takes no arguments" in helper)
    check("single instance via flock", "flock -n 9" in helper)
    check("root-owned config file", 'CONF="/etc/ethos-nas/update.conf"' in helper)
    check("fetch is https-only with size caps",
          "--proto '=https'" in helper and "--max-filesize" in helper)
    check("verifies the index signature", "openssl pkeyutl -verify" in helper)
    check("fails closed on a bad signature", "release index signature is invalid" in helper)
    check("request file can only ask for apply",
          'data.get("action") == "apply"' in helper)
    check("shallow image clones can reach an older release commit",
          "--depth=1 origin" in helper and "--unshallow" in helper
          and "deepen the shallow clone" in helper)
    check("fetch tracks branches and tags, not just the pinned tag",
          "+refs/heads/*:refs/remotes/origin/*" in helper
          and "+refs/tags/*:refs/tags/*" in helper)
    check("every remote operation is bounded by a timeout",
          helper.count('timeout "${FETCH_TIMEOUT}"') >= 4, str(helper.count('timeout "${FETCH_TIMEOUT}"')))
    check("git can never block on a credential prompt",
          "export GIT_TERMINAL_PROMPT=0" in helper)
    check("a stale REPO_OWNER falls back to the install dir owner",
          'id -u "${REPO_OWNER}"' in helper
          and 'stat -c %U "${INSTALL_DIR}"' in helper)
    check("every exit path writes a terminal result",
          "trap on_exit EXIT" in helper and "RESULT_WRITTEN" in helper
          and "update helper exited" in helper)
    check("job.json carries the helper pid for liveness checks",
          '"pid": os.getppid()' in helper)
    check("no eval", not re.search(r"\beval\b", helper))
    # `set -u` kills the script on the first undefined variable, and everything
    # before `trap on_exit EXIT` dies *without* writing a result — the UI then
    # shows "nothing happened" and the request file stays behind. 2.29.4 lost
    # STATE_DIR/JOB that way.
    conf_vars = {"INSTALL_DIR", "REPO_OWNER", "SERVICE_USER", "SERVICE_UNIT",
                 "DATA_DIR", "INDEX_URL", "PUBKEY_PEM"}
    assigned = set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)=", helper, re.M))
    used = {name for name, rest in re.findall(r"\$\{([A-Z_][A-Z0-9_]*)([^}]*)\}", helper)
            if ":-" not in rest}
    undefined = sorted(used - assigned - conf_vars)
    check("every variable is defined before use", not undefined, str(undefined))
    check("git/pip run as the repo owner, not root",
          'runuser -u "${REPO_OWNER}"' in helper and "runuser -u \"${REPO_OWNER}\" -- \"${INSTALL_DIR}/.venv/bin/pip\"" in helper)
    check("restarts the unit", 'systemctl restart "${SERVICE_UNIT}"' in helper)
    check("runs itself in a systemd scope so the restart cannot kill it",
          "systemd-run --scope" in helper and "ETHOS_UPDATE_SCOPED" in helper)
    check("refreshes its installed copy atomically",
          "ethos-update.new" in helper and "mv -f" in helper)
    check("refreshes the installed OS package helper too",
          "[ -x /usr/local/bin/ethos-os-update ]" in helper
          and "ethos-os-update.new" in helper)
    check("health probe with timeout",
          "api/system/status" in helper and "HEALTH_TIMEOUT=90" in helper)
    check("auto-rollback restores the previous commit",
          'reset --hard "${FROM_SHA}"' in helper and "rolled_back" in helper)
    check("index URL comes from the root-owned conf only",
          'curl -fsSL --proto' in helper and '"${INDEX_URL}"' in helper
          and "INDEX_URL=" not in helper.split("# ── Signed index")[1])

    print("\n[installer]")
    check("installs the helper", 'install -m 0755 -o root -g root "${UPDATE_SRC}" "${UPDATE_HELPER_BIN}"' in installer)
    check("ships an argument-free sudoers rule",
          'RULES+=("${TARGET_USER} ALL=(root) NOPASSWD: ${UPDATE_HELPER_BIN}")' in installer)
    check("writes the root-owned conf", 'install -m 0644 -o root -g root "${CONF_TMP}" "${UPDATE_CONF}"' in installer)
    check("writes the public key", 'UPDATE_PUBKEY_PEM' in installer and "openssl pkey -pubin -inform DER" in installer)
    check("removes helper/conf/key on uninstall",
          'rm -f "${UPDATE_HELPER_BIN}" "${UPDATE_CONF}" "${UPDATE_PUBKEY_PEM}"' in installer)

    print("\n[key consistency]")
    match = re.search(r'^RELEASE_PUBKEY_B64 = "([^"]+)"', config, re.MULTILINE)
    check("config pins a release key", match is not None)
    der_match = re.search(r'^UPDATE_PUBKEY_DER_B64="([^"]+)"', installer, re.MULTILINE)
    check("installer pins a DER public key", der_match is not None)
    if match and der_match:
        raw = base64.b64decode(match.group(1))
        expected = base64.b64encode(bytes.fromhex("302a300506032b6570032100") + raw).decode()
        check("installer key matches config key", expected == der_match.group(1),
              f"expected {expected}")

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
