#!/usr/bin/env python3
"""The privileged host-dependency helper must stay in sync with the registry.

`deploy/ethos-system-deps` runs as root through sudo, so its allowlist is
duplicated on purpose. This test keeps the two lists identical, checks the
script keeps its safety properties (no arbitrary package names, no `purge`, no
`eval`, argument-count guard), and verifies `setup_service.sh` installs it with
one sudoers rule per (action, id) pair.

Static only — nothing is executed as root.

Run: .venv/bin/python tests/test_system_deps_helper.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ETHOS_DATA_DIR", str(Path(tempfile.mkdtemp(prefix="ethos-sd-helper-")) / "data"))
os.environ.setdefault("ETHOS_JWT_SECRET", "sd-helper-secret")
sys.path.insert(0, str(ROOT))

from backend.services import system_deps  # noqa: E402

HELPER = ROOT / "deploy" / "ethos-system-deps"
SETUP = ROOT / "setup_scripts" / "setup_service.sh"

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


helper = HELPER.read_text(encoding="utf-8")
setup = SETUP.read_text(encoding="utf-8")

print("Allowlist sync")
# ids handled by the helper's apt_packages() case block
helper_ids = set(re.findall(r"^\s{8}([a-z0-9-]+)\)\s*echo\s+", helper, re.MULTILINE))
serviceable = {k for k, v in system_deps.SYSTEM_REQUIREMENTS.items() if v.get("serviceable")}
check("every serviceable requirement has a helper entry", serviceable <= helper_ids,
      f"missing: {sorted(serviceable - helper_ids)}")
check("the helper invents no extra requirement", helper_ids <= serviceable,
      f"extra: {sorted(helper_ids - serviceable)}")
check("docker is not in the helper (own helper)", "docker" not in helper_ids)
for req, meta in system_deps.SYSTEM_REQUIREMENTS.items():
    if meta.get("serviceable"):
        check(f"{req} maps to the same apt package",
              re.search(rf"^\s{{8}}{re.escape(req)}\)\s*echo\s+\"([^\"]+)\"", helper, re.MULTILINE)
              .group(1) == meta["apt"])

print("Helper safety")
check("fails on any error", "set -euo pipefail" in helper)
check("refuses to run as non-root", 'id -u' in helper and "must run as root" in helper)
check("requires exactly two arguments", '"$#" -ne 2' in helper)
check("rejects unknown actions", "unknown action" in helper)
check("rejects unknown ids", "unknown requirement" in helper)
check("installs without recommends", "--no-install-recommends" in helper)
check("removes without purging config",
      "apt-get remove -y" in helper
      and not re.search(r"apt-get\s+purge|--purge|autoremove\s+--purge", helper))
check("no eval or shell interpolation", "eval " not in helper and "${ACTION} " not in helper)
check("wraps apt calls in an argument list only",
      "apt-get install -y --no-install-recommends ${PKGS}" in helper)
check("reports a machine-readable result", helper.count("RESULT=ok") == 2)
check("is executable", os.access(HELPER, os.X_OK))

print("Installer wiring")
check("setup_service.sh uses the same path",
      '/usr/local/bin/ethos-system-deps"' in setup)
check("setup_service.sh installs the helper",
      "deploy/ethos-system-deps" in setup and "SYSTEM_DEPS_HELPER_BIN}" in setup)
check("setup_service.sh removes it on uninstall", 'rm -f "${SYSTEM_DEPS_HELPER_BIN}"' in setup)
ids_decl = re.search(r'^SYSTEM_DEPS_IDS="([^"]*)"', setup, re.MULTILINE)
check("setup_service.sh declares the id list", ids_decl is not None)
if ids_decl:
    declared = set(ids_decl.group(1).split())
    check("setup_service.sh ids match the registry", declared == serviceable,
          f"declared={sorted(declared)} registry={sorted(serviceable)}")
check("one sudoers rule per action and id",
      "NOPASSWD: ${SYSTEM_DEPS_HELPER_BIN} install ${_req}" in setup
      and "NOPASSWD: ${SYSTEM_DEPS_HELPER_BIN} remove ${_req}" in setup)
check("rules are generated per id (no wildcard)",
      'for _req in ${SYSTEM_DEPS_IDS}' in setup and "NOPASSWD: ${SYSTEM_DEPS_HELPER_BIN} *" not in setup)

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    for name in failed:
        print(f"  FAILED: {name}")
    sys.exit(1)
print("OK — root helper and Python registry agree")
sys.exit(0)
