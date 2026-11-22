#!/usr/bin/env python3
"""Child-process limit tests — services/child_limits.py.

Run:
    .venv/bin/python tests/test_child_limits.py
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services import child_limits  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    cmd = [sys.executable, "-c", "print('child ok')"]
    check("wrap() disables itself for mem_mb=0",
          child_limits.wrap(cmd, mem_mb=0) == cmd)

    wrapped = child_limits.wrap(cmd, mem_mb=256)
    check("wrap() prefixes the launcher", wrapped[:2] == [sys.executable, "-c"]
          and wrapped[3] == "256" and wrapped[5:] == cmd, str(wrapped[:6]))

    ok = subprocess.run(wrapped, capture_output=True, text=True, timeout=30)
    check("a capped child still runs", ok.returncode == 0 and "child ok" in ok.stdout,
          f"rc={ok.returncode} err={ok.stderr[-200:]}")

    # 600 MiB inside a 256 MiB address space → MemoryError, a normal failure the
    # caller can report instead of the kernel OOM-killing the biggest process.
    hog = [sys.executable, "-c", "b = bytearray(600 * 1024 * 1024); print(len(b))"]
    limited = subprocess.run(child_limits.wrap(hog, mem_mb=256),
                             capture_output=True, text=True, timeout=60)
    check("the address-space cap actually binds", limited.returncode != 0,
          f"rc={limited.returncode}")

    check("describe_exit names a signal",
          "signal 9" in child_limits.describe_exit(-9, "boom"))
    check("describe_exit keeps the stderr tail",
          "Cannot allocate memory" in child_limits.describe_exit(1, "Cannot allocate memory"))
    check("is_memory_exit flags signal deaths", child_limits.is_memory_exit(-9))

    failed = [name for name, ok_, _ in _results if not ok_]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
