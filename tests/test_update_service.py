#!/usr/bin/env python3
"""Core update check — index parsing, signature verification and comparison.

Runs in-process (no server, no network): the HTTP layer is monkeypatched, and a
throwaway Ed25519 keypair signs the payloads. Run:
    .venv/bin/python tests/test_update_service.py
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

DATA_DIR = Path(tempfile.mkdtemp(prefix="ethos-update-test-"))
os.environ["ETHOS_DATA_DIR"] = str(DATA_DIR)
os.environ.setdefault("ETHOS_JWT_SECRET", "update-service-test-secret")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from backend.config import APP_VERSION, settings  # noqa: E402
from backend.services import package_repo, update_service  # noqa: E402

_results: list[tuple[str, bool, str]] = []
_key = Ed25519PrivateKey.generate()
PUB_B64 = base64.b64encode(
    _key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def _payload(latest: dict) -> bytes:
    return json.dumps({"schema": 1, "generated_at": "2026-11-15T00:00:00Z",
                       "latest": latest, "releases": [latest]}).encode()


def _fake_fetch(payload: bytes, signature: str | None = None):
    sig = signature if signature is not None else base64.b64encode(
        _key.sign(payload)).decode()

    def _get_bytes(url: str, max_bytes: int, **kwargs):
        return sig.encode() if url.endswith(".sig") else payload

    return _get_bytes


def main() -> int:
    settings.UPDATE_PUBKEY = PUB_B64
    settings.UPDATE_ENABLED = True
    settings.UPDATE_TTL_SECONDS = 3600

    current = {"version": "2.1.0", "git_sha": "abc1234", "build": 100}

    print("[is_newer]")
    check("same build is not newer",
          not update_service.is_newer({"version": "2.1.0", "git_sha": "zzz9999", "build": 100}, current))
    check("higher build is newer",
          update_service.is_newer({"version": "2.2.0", "git_sha": "def5678", "build": 101}, current))
    check("lower build is not newer",
          not update_service.is_newer({"version": "2.0.0", "git_sha": "old0000", "build": 99}, current))
    check("sha fallback when no build",
          update_service.is_newer({"version": "2.2.0", "git_sha": "def5678", "build": 0},
                                  {"version": "2.1.0", "git_sha": "abc1234", "build": 0}))
    check("same sha is not newer",
          not update_service.is_newer({"version": "2.1.0", "git_sha": "abc1234", "build": 0},
                                      {"version": "2.1.0", "git_sha": "abc1234", "build": 0}))
    check("version fallback with no git data",
          update_service.is_newer({"version": "2.2.0"}, {"version": "2.1.0"}))

    print("\n[parse_index]")
    parsed = update_service.parse_index(_payload({"version": "2.2.0", "git_sha": "def5678",
                                                  "build": 101, "notes": "notes"}))
    check("keeps version/sha/build", parsed["version"] == "2.2.0" and parsed["git_sha"] == "def5678"
          and parsed["build"] == 101)
    check("builds a commit url", parsed["commit_url"].endswith("/def5678"))
    check("builds the release archive url",
          parsed["download_url"]
          == "https://github.com/SyncHot/ethos-nas/archive/refs/tags/v2.2.0.zip",
          parsed["download_url"])
    check("running release has a download url",
          update_service.current_release()["download_url"].endswith(
              f"/archive/refs/tags/v{APP_VERSION}.zip"))
    check("release_download_url rejects unusable versions",
          update_service.release_download_url("") == ""
          and update_service.release_download_url("../evil") == ""
          and update_service.release_download_url("a b") == "")

    print("\n[status]")
    snapshot = update_service.status()
    check("status exposes the running release download url",
          snapshot["current"]["download_url"].endswith(
              f"/archive/refs/tags/v{APP_VERSION}.zip"),
          snapshot["current"].get("download_url"))
    check("status exposes the browser-extension fallback url",
          snapshot["extension_download_url"].endswith(
              "/releases/latest/download/ethos-nas-downloader.zip"),
          snapshot.get("extension_download_url"))
    for bad in (b"not json", json.dumps({"schema": 9, "latest": {}}).encode(),
                json.dumps({"schema": 1, "latest": {}}).encode()):
        try:
            update_service.parse_index(bad)
            check(f"rejects bad index {bad[:12]!r}", False, "no exception")
        except update_service.UpdateError:
            check(f"rejects bad index {bad[:12]!r}", True)

    print("\n[fetch_latest]")
    good = _payload({"version": "2.2.0", "git_sha": "def5678", "build": 101})
    original = package_repo.get_bytes
    try:
        package_repo.get_bytes = _fake_fetch(good)
        check("accepts a correctly signed index",
              update_service.fetch_latest()["git_sha"] == "def5678")

        package_repo.get_bytes = _fake_fetch(good, signature=base64.b64encode(b"x" * 64).decode())
        try:
            update_service.fetch_latest()
            check("rejects a bad signature", False, "no exception")
        except update_service.UpdateError:
            check("rejects a bad signature", True)

        def _offline(url, max_bytes, **kwargs):
            raise package_repo.PackageError("Failed to fetch", 502)

        package_repo.get_bytes = _offline
        try:
            update_service.fetch_latest()
            check("surfaces network failures as UpdateError", False, "no exception")
        except update_service.UpdateError:
            check("surfaces network failures as UpdateError", True)
    finally:
        package_repo.get_bytes = original

    print("\n[check + cache]")
    calls = {"n": 0}

    def _counting(url, max_bytes, **kwargs):
        calls["n"] += 1
        return _fake_fetch(good)(url, max_bytes, **kwargs)

    try:
        package_repo.get_bytes = _counting
        first = asyncio.run(update_service.check(force=True))
        check("force check reports the published build",
              first["latest"]["build"] == 101 and first["error"] == "", str(first.get("error")))
        cached = asyncio.run(update_service.check())
        check("second call is served from the cache", calls["n"] == 2, f"fetches={calls['n']}")
        check("cached result keeps the payload", cached["latest"]["git_sha"] == "def5678")
        check("status() never fetches", calls["n"] == 2 and update_service.status()["latest"] is not None)
    finally:
        package_repo.get_bytes = original

    print("\n[request_apply guards]")
    import backend.services.config_archive as config_archive

    original_spawn = update_service._spawn_helper
    original_snapshot = config_archive.take_snapshot
    spawned = {"n": 0}

    def _fake_spawn():
        spawned["n"] += 1

    try:
        config_archive.take_snapshot = lambda *a, **kw: "snapshot.zip"
        update_service._spawn_helper = _fake_spawn

        settings.UPDATE_HELPER = str(DATA_DIR / "missing-helper")
        try:
            asyncio.run(update_service.request_apply())
            check("refuses when the helper is missing", False, "no exception")
        except update_service.UpdateError as exc:
            check("refuses when the helper is missing", exc.status == 409)

        helper = DATA_DIR / "ethos-update"
        helper.write_text("#!/bin/sh\n", encoding="utf-8")
        helper.chmod(0o755)
        settings.UPDATE_HELPER = str(helper)

        update_service._save({"checked_at": 1, "update_available": False, "latest": None})
        try:
            asyncio.run(update_service.request_apply())
            check("refuses when already up to date", False, "no exception")
        except update_service.UpdateError as exc:
            check("refuses when already up to date", exc.status == 409)

        update_service._save({"checked_at": 1, "update_available": True,
                              "latest": {"version": "2.2.0", "git_sha": "def5678", "build": 101}})
        started = asyncio.run(update_service.request_apply())
        request_file = DATA_DIR / "update" / "request.json"
        check("writes an apply request",
              request_file.is_file() and json.loads(request_file.read_text())["action"] == "apply")
        check("request file is owner-only", (request_file.stat().st_mode & 0o077) == 0)
        check("starts the helper once", spawned["n"] == 1, str(spawned))
        check("reports updating", started.get("status") == "updating")

        (DATA_DIR / "update" / "job.json").write_text(json.dumps({
            "status": "running", "step": "apply", "at": update_service._iso(time.time()),
        }), encoding="utf-8")
        try:
            asyncio.run(update_service.request_apply())
            check("refuses while a job is running", False, "no exception")
        except update_service.UpdateError as exc:
            check("refuses while a job is running", exc.status == 409)
    finally:
        update_service._spawn_helper = original_spawn
        config_archive.take_snapshot = original_snapshot

    print("\n[stale job]")
    job_file = DATA_DIR / "update" / "job.json"
    original_start = update_service._process_started_at
    try:
        update_service._process_started_at = lambda: time.time()
        job_file.write_text(json.dumps({
            "status": "running", "step": "restart",
            "at": update_service._iso(time.time() - 3600),
        }), encoding="utf-8")
        status = update_service.apply_status()
        check("stale restart job is not running", status["running"] is False, str(status))
        check("stale flag is exposed", status.get("stale") is True, str(status))
        job_file.write_text(json.dumps({
            "status": "running", "step": "restart",
            "at": update_service._iso(time.time() + 30),
        }), encoding="utf-8")
        status = update_service.apply_status()
        check("a restart job from the current process is still running",
              status["running"] is True, str(status))
    finally:
        update_service._process_started_at = original_start

    print("\n[dead helper]")
    # The helper records its pid, so a `running` job whose pid is gone can only
    # mean it exited without reporting a result. Leaving that state visible made
    # the Settings card poll "fetch" forever with no progress.
    request_file = DATA_DIR / "update" / "request.json"
    request_file.write_text(json.dumps({"action": "apply"}), encoding="utf-8")
    result_file = DATA_DIR / "update" / "result.json"
    result_file.unlink(missing_ok=True)
    job_file.write_text(json.dumps({
        "status": "running", "step": "fetch", "detail": "release index",
        "pid": 999_999_999, "at": update_service._iso(time.time()),
    }), encoding="utf-8")
    status = update_service.apply_status()
    check("dead helper job is not running", status["running"] is False, str(status))
    check("dead helper job is reported stale", status.get("stale") is True, str(status))
    check("dead helper produces a failure result",
          (status.get("result") or {}).get("status") == "failed", str(status))
    check("dead helper failure names the step it died in",
          "fetch" in str((status.get("result") or {}).get("detail") or ""), str(status))
    check("dead helper clears the stale request file", not request_file.exists())
    check("dead helper result is persisted for later polls",
          json.loads(result_file.read_text(encoding="utf-8")).get("status") == "failed")

    print("\n[reconcile]")
    emitted: list[tuple] = []
    original_emit = update_service.emit_event
    update_service.emit_event = lambda *a, **kw: emitted.append((a, kw))
    try:
        (DATA_DIR / "update" / "result.json").write_text(
            json.dumps({"status": "rolled_back", "at": "2026-11-15T10:00:00+00:00",
                        "detail": "boom"}), encoding="utf-8")
        update_service._save({"checked_at": 1})
        update_service.reconcile_result()
        check("reports a rolled-back result once",
              len(emitted) == 1 and emitted[0][0][0] == "error", str(emitted))
        update_service.reconcile_result()
        check("does not repeat the same result", len(emitted) == 1, str(len(emitted)))

        (DATA_DIR / "update" / "result.json").write_text(
            json.dumps({"status": "ok", "at": "2026-11-15T11:00:00+00:00",
                        "version": "2.2.0"}), encoding="utf-8")
        update_service.reconcile_result()
        check("reports a successful update",
              len(emitted) == 2 and emitted[1][0][0] == "success", str(emitted))

        print("\n[reconcile stale job]")
        (DATA_DIR / "update" / "result.json").unlink(missing_ok=True)
        (DATA_DIR / "update" / "job.json").write_text(json.dumps({
            "status": "running", "step": "restart",
            "at": update_service._iso(time.time() - 3600),
            "to": "abc1234", "version": "2.2.0",
        }), encoding="utf-8")
        update_service._save({"checked_at": 1})
        original_current = update_service.current_release
        original_start = update_service._process_started_at
        update_service.current_release = lambda: {"version": "2.2.0", "git_sha": "abc1234", "build": 1}
        update_service._process_started_at = lambda: time.time()
        try:
            emitted.clear()
            update_service.reconcile_result()
            result = json.loads((DATA_DIR / "update" / "result.json").read_text())
            job = json.loads((DATA_DIR / "update" / "job.json").read_text())
            check("stale job is finalized from the checkout",
                  result.get("status") == "ok" and job.get("status") == "ok", str(result))
            check("finalizing emits one success notification",
                  len(emitted) == 1 and emitted[0][0][0] == "success", str(emitted))
        finally:
            update_service.current_release = original_current
            update_service._process_started_at = original_start
    finally:
        update_service.emit_event = original_emit

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())