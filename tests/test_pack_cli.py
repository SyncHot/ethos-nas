#!/usr/bin/env python3
"""ethos-pack CLI: build, sign, verify and tamper detection."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "scripts" / "ethos-pack"
TMP = Path(tempfile.mkdtemp(prefix="ethos-pack-test-"))
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def run(*args):
    return subprocess.run([sys.executable, str(PACK), *args], capture_output=True, text=True)


def main() -> int:
    try:
        repo = TMP / "repo"
        pkg = repo / "demo-pkg"
        pkg.mkdir(parents=True)
        (pkg / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx:1.27\n", encoding="utf-8")
        (pkg / "manifest.json").write_text(json.dumps({
            "id": "demo-pkg", "name": "Demo", "version": "1.0.0",
            "description": "Demo package", "icon": "📦", "category": "tools",
            "author": "Test", "compose": "docker-compose.yml",
        }), encoding="utf-8")

        keygen = run("keygen", "--out-dir", str(TMP / "keys"))
        check("keygen", keygen.returncode == 0 and (TMP / "keys/repo.key").exists(), keygen.stderr)

        build = run("build", "--repo", str(repo), "--base-url", "https://example.test/repo")
        check("build", build.returncode == 0, build.stderr)
        index = json.loads((repo / "index.json").read_text())
        entry = index["packages"][0]
        check("index has manifest hash", len(entry.get("manifest_sha256", "")) == 64)
        manifest = json.loads((repo / "demo-pkg/manifest.json").read_text())
        check("manifest has compose hash", len(manifest.get("compose", {}).get("sha256", "")) == 64)

        sign = run("sign", "--repo", str(repo), "--key", str(TMP / "keys/repo.key"))
        check("sign", sign.returncode == 0 and (repo / "index.json.sig").exists(), sign.stderr)

        verify = run("verify", "--repo", str(repo), "--pub", str(TMP / "keys/repo.pub"))
        check("verify", verify.returncode == 0, verify.stderr)

        (repo / "demo-pkg/docker-compose.yml").write_text("services:\n  app:\n    image: evil:1\n", encoding="utf-8")
        tampered = run("verify", "--repo", str(repo), "--pub", str(TMP / "keys/repo.pub"))
        check("tampered compose rejected", tampered.returncode != 0)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
