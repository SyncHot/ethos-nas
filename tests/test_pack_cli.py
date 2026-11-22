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

        # ── lint / readme ───────────────────────────────────────────────────
        (repo / "README.md").write_text(
            "# Demo\n\n<!-- catalog:start -->\n<!-- catalog:end -->\n", encoding="utf-8")
        readme = run("readme", "--repo", str(repo))
        table = (repo / "README.md").read_text(encoding="utf-8")
        check("readme writes the catalog table",
              readme.returncode == 0 and "`demo-pkg`" in table and "1.0.0" in table, readme.stderr)

        dirty = run("lint", "--repo", str(repo))
        check("lint rejects a missing min_ethos_version and restart policy",
              dirty.returncode != 0 and "min_ethos_version" in dirty.stderr, dirty.stderr)

        (pkg / "manifest.json").write_text(json.dumps({
            "id": "demo-pkg", "name": "Demo", "version": "1.0.0",
            "description": "Demo package", "icon": "📦", "category": "tools",
            "author": "Test", "min_ethos_version": "2.0.0",
            "compose": "docker-compose.yml",
        }), encoding="utf-8")
        (pkg / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: nginx:1.27\n    restart: unless-stopped\n"
            "    healthcheck:\n      test: [\"CMD\", \"true\"]\n", encoding="utf-8")
        run("build", "--repo", str(repo), "--base-url", "https://example.test/repo")
        clean = run("lint", "--repo", str(repo))
        check("lint passes a pinned, restarted service", clean.returncode == 0, clean.stderr)

        (pkg / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: nginx:latest\n    restart: unless-stopped\n", encoding="utf-8")
        run("build", "--repo", str(repo), "--base-url", "https://example.test/repo")
        floating = run("lint", "--repo", str(repo))
        check("lint rejects a floating image tag",
              floating.returncode != 0 and "not pinned" in floating.stderr, floating.stderr)

        # Restore the tamper fixture: build recomputes compose hashes, so write
        # an untouched compose file and re-sign before the tamper check below.
        (pkg / "docker-compose.yml").write_text(
            "services:\n  app:\n    image: nginx:1.27\n    restart: unless-stopped\n", encoding="utf-8")
        run("build", "--repo", str(repo), "--base-url", "https://example.test/repo")
        run("sign", "--repo", str(repo), "--key", str(TMP / "keys/repo.key"))

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
