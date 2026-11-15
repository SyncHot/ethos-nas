#!/usr/bin/env python3
"""Package install honours `system_requires` before deploying files.

In-process: the repository fetch and the compose runner are stubbed, so the test
asserts the *ordering* (dependency failure aborts the install, staging is
cleaned up, no record is written) without network, Docker or a server.

Run: .venv/bin/python tests/test_package_deps.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="ethos-package-deps-"))
os.environ.setdefault("ETHOS_DATA_DIR", str(TMP / "data"))
os.environ.setdefault("ETHOS_JWT_SECRET", "package-deps-secret")
sys.path.insert(0, str(ROOT))

from backend.services import (  # noqa: E402
    docker_host, package_installer, package_jobs, package_repo, packages_store, system_deps,
)
from backend.services.package_schema import PackageError  # noqa: E402

results = []
def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


MANIFEST = {
    "schema": 2, "id": "deps-demo", "name": "Deps Demo", "version": "1.0.0",
    "description": "Needs Docker", "icon": "📦", "capabilities": ["compose"],
    "system_requires": ["docker"], "files": [],
}
ENTRY = {"id": "deps-demo", "source": "https://example.test/index.json"}

_orig_stage = package_installer._stage_package
_orig_deps = docker_host.ensure_system_deps
_orig_status = docker_host.engine_status
_orig_fetch = package_repo.fetch_manifest


async def _fetch_stub(_package):
    """Serve MANIFEST without a configured source (the test has none)."""
    return dict(MANIFEST)


package_repo.fetch_manifest = _fetch_stub


def _new_job():
    # `install()` is called directly here, so nothing releases the one-job-per-
    # package lock that package_jobs.start() would normally clear.
    package_jobs._ACTIVE_PACKAGES.discard("deps-demo")
    return package_jobs.create("install", "deps-demo")


def _make_staging() -> Path:
    staging = packages_store.STAGING_DIR / "deps-demo-1"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    (staging / "ethos-package.json").write_text("{}", encoding="utf-8")
    return staging


def _stage_stub(staging: Path):
    async def _stage(entry, job_id, manifest=None):
        package_jobs.set_step(job_id, "policy")
        package_jobs.log(job_id, "compose policy passed")
        return dict(MANIFEST), staging
    return _stage


def _install(manifest_patch=None):
    staging = _make_staging()
    package_installer._stage_package = _stage_stub(staging)
    job = _new_job()
    try:
        asyncio.run(package_installer.install(dict(ENTRY), job["id"]))
    finally:
        package_installer._stage_package = _orig_stage
    return staging, job


try:
    # 1. Missing engine => install aborts at the deps step and cleans staging.
    staging = _make_staging()
    package_installer._stage_package = _stage_stub(staging)

    def _fail_deps(requires, job_id=None, **kwargs):
        package_jobs.set_step(job_id, "deps")
        assert requires == ["docker"], requires
        raise PackageError("This package requires Docker Engine, which is not installed.", 503)

    docker_host.ensure_system_deps = _fail_deps
    job = _new_job()
    error = None
    try:
        asyncio.run(package_installer.install(dict(ENTRY), job["id"]))
    except PackageError as exc:
        error = exc
    finally:
        package_installer._stage_package = _orig_stage

    check("dependency failure aborts the install", error is not None and error.status == 503,
          str(error))
    check("error names Docker", "Docker" in str(error))
    check("staging directory removed", not staging.exists())
    check("no installed record written", packages_store.get_package("deps-demo") is None)
    check("job step recorded as deps", package_jobs.get(job["id"])["step"] == "deps",
          package_jobs.get(job["id"])["step"])

    # 2. Engine available => the install proceeds and records the package.
    calls = []
    docker_host.ensure_system_deps = lambda requires, job_id=None, **kw: calls.append(requires)
    docker_host.emit_event = lambda *a, **kw: None
    staging, job = _install()
    try:
        record = packages_store.get_package("deps-demo")
        check("dependencies checked before deploy", calls == [["docker"]], str(calls))
        check("package recorded after successful install", bool(record), str(record))
        check("deployed directory exists", packages_store.package_dir("deps-demo").exists())
        check("job finished at done", package_jobs.get(job["id"])["step"] == "done")
    finally:
        packages_store.remove_package("deps-demo")
        shutil.rmtree(packages_store.package_dir("deps-demo"), ignore_errors=True)

    # 3. Packages without system_requires never touch the engine probe.
    docker_host.ensure_system_deps = _orig_deps
    probed = []

    def _record(requires, job_id=None, **kwargs):
        probed.append(requires)

    docker_host.ensure_system_deps = _record
    system_deps.ensure([], None)
    system_deps.ensure(None, None)
    check("no requirement means no engine probe", probed == [], str(probed))
    docker_host.ensure_system_deps = _orig_deps
finally:
    package_installer._stage_package = _orig_stage
    docker_host.ensure_system_deps = _orig_deps
    docker_host.engine_status = _orig_status
    package_repo.fetch_manifest = _orig_fetch
    package_jobs._ACTIVE_PACKAGES.discard("deps-demo")
    for name in ("deps-demo",):
        packages_store.remove_package(name)
        shutil.rmtree(packages_store.package_dir(name), ignore_errors=True)

failed = [name for name, ok, _ in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
