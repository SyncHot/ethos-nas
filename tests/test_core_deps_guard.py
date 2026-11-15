#!/usr/bin/env python3
"""Guard test: the core venv must not grow optional, package-owned dependencies.

Runs ``scripts/check_core_deps.py`` and fails on any *new* core import or
requirements entry listed in FORBIDDEN. Known extraction sites are reported as
PENDING and are removed with the package that replaces them.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_core_deps.py"


def main() -> int:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    print(output.strip())
    if proc.returncode != 0:
        print("FAIL: slim-core guard reported a violation")
        return 1
    if "OK:" not in output:
        print("FAIL: slim-core guard produced no success line")
        return 1
    print("PASS: core has no unexpected optional dependencies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
