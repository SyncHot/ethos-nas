#!/usr/bin/env python3
"""Static + offline tests for the OS image helper (`deploy/ethos-os-image`).

The helper writes a block device as root, so its safety properties are pinned:
signed index verified by the helper, image hash, standby slot only, no reboot.

    .venv/bin/python tests/test_os_image_helper.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "deploy" / "ethos-os-image"
MANIFEST = ROOT / "deploy" / "provision.json"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def _embedded(script: str) -> str:
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", script, re.DOTALL)
    return match.group(1) if match else ""


def main() -> int:
    helper = HELPER.read_text(encoding="utf-8")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    print("[helper]")
    check("helper exists and is a bash script", helper.startswith("#!/bin/bash"))
    check("helper is executable", os.access(HELPER, os.X_OK))
    check("strict shell mode", "set -euo pipefail" in helper)
    check("refuses non-root", '"$(id -u)" -eq 0' in helper and "must run as root" in helper)
    check("only the apply action is accepted",
          '[ "$#" -eq 1 ] && [ "$1" = "apply" ]' in helper)
    check("requires the slot tool (A/B only)", "ethos-slot" in helper)
    check("single instance via flock", "flock -n 9" in helper)
    check("runs in a systemd scope", "systemd-run --scope" in helper and "ETHOS_OS_IMAGE_SCOPED" in helper)
    check("verifies the signed index itself",
          '"openssl", "pkeyutl", "-verify"' in helper and 'INDEX_URL + ".sig"' in helper)
    check("requires schema 2 + layout ab1",
          'data.get("schema") != 2' in helper and 'entry.get("layout") != "ab1"' in helper)
    check("verifies the image sha256", "sha256_file(source) != rootfs[" in helper)
    check("never writes the active slot",
          'active_slot = active.stdout.strip()' in helper
          and 'slot = "b" if active_slot == "a" else "a"' in helper)
    check("resolves the target by label only",
          '/dev/disk/by-label/' in helper and "ETHOS-ROOT-" in helper)
    check("refuses non-block devices", "stat.S_ISBLK" in helper)
    check("verifies the written slot read-only",
          '["mount", "-o", "ro"' in helper and "build-info.json" in helper)
    check("never reboots", "systemctl reboot" not in helper)
    check("no shell execution of request values", "shell=True" not in helper)
    check("request path stays inside the image dir",
          'os.path.dirname(source) != os.path.realpath(IMAGE_DIR)' in helper)
    check("caps the image size", "MAX_IMAGE" in helper)
    check("requires the A/B layout marker", 'layout != "ab1"' in helper)

    print("\n[embedded python]")
    code = _embedded(helper)
    check("helper embeds one python program", bool(code))
    prelude = code.split("\nraise SystemExit(main())")[0]
    ns = {"__name__": "os_image_test"}
    sys.argv = ["ethos-os-image", str(ROOT), "ethos-nas", tempfile.mkdtemp(prefix="osimg-"),
                "https://example.invalid/index.json", "/tmp/key.pem", "/usr/local/sbin/ethos-slot"]
    try:
        exec(compile(prelude, "ethos-os-image", "exec"), ns)
        check("embedded python parses", True)
    except SyntaxError as exc:
        check("embedded python parses", False, str(exc))
        ns = {}

    print("\n[offline helpers]")
    if ns:
        payload = b"ethos os image test payload\n"
        target = Path(tempfile.mkdtemp(prefix="osimg-sha-")) / "image"
        target.write_bytes(payload)
        import hashlib
        check("sha256_file matches hashlib",
              ns["sha256_file"](str(target)) == hashlib.sha256(payload).hexdigest())
        if shutil.which("xz"):
            archive = target.with_suffix(".xz")
            subprocess.run(["xz", "-k", "-f", str(target)], check=True)
            check("xz_uncompressed reads the real size",
                  ns["xz_uncompressed"](str(archive)) == len(payload),
                  str(ns["xz_uncompressed"](str(archive))))
        else:
            print("  skip xz_uncompressed (xz not installed)")
        identity = ns["os_identity"]()
        check("identity reports an arch", identity["arch"] in ("amd64", "arm64"), str(identity))

    print("\n[provisioning]")
    names = {item.get("name") for item in manifest.get("helpers") or []}
    check("manifest installs the image helper", "ethos-os-image" in names)
    check("manifest grants exactly one image rule",
          sum(1 for rule in manifest.get("sudoers") or []
              if "/usr/local/bin/ethos-os-image" in rule) == 1)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
