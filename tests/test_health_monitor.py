"""Tests — System Health Monitor (disk capacity + failed systemd units).

Part A1 (in-process service logic, always runs, no server needed):
  monkeypatched clock/disks/units/emit → alert on breach, dedup within cooldown,
  re-alert after cooldown expiry, hysteresis band silence, recovery event + state
  clear, unit failure/recovery paths, pseudo-fs filtering, st_dev bind-mount dedup.

Part A2 (live API E2E — needs a fresh, ISOLATED instance with NO env overrides,
so runtime config changes are what drive the alerts):
    fuser -k 9001/tcp 2>/dev/null; \
    ETHOS_PORT=9001 ETHOS_DATA_DIR=/tmp/ethos-health-test-data \
      nohup .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 9001 \
      > /tmp/ethos-nas-health-test.log 2>&1 &
    then:  ETHOS_TEST_BASE=http://127.0.0.1:9001 .venv/bin/python tests/test_health_monitor.py
    cleanup after stopping the instance: rm -rf /tmp/ethos-health-test-data
  Verifies status shape, that the startup check emitted one warning per breached disk,
  and that an immediate manual re-check emits ZERO new events (dedup proven live).

Part B (UI via Playwright, auto-skips when chromium is unavailable): Settings → System
shows the Health Monitor card; "Run check now" responds with a toast.

Self-cleaning: deletes only its own source=health notification events at the end of A2.
"""
import json
import os

def _require_test_password() -> str:
    """Test credentials are never committed — export ETHOS_TEST_PASSWORD."""
    pw = os.environ.get("ETHOS_TEST_PASSWORD", "")
    if not pw:
        raise SystemExit("ETHOS_TEST_PASSWORD is not set (tests carry no default credentials)")
    return pw
import shutil
import sys
import tempfile
import types
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.environ.get("ETHOS_TEST_BASE", "http://localhost:9000")
USER = os.environ.get("ETHOS_TEST_USER") or os.environ.get("USER") or "user"
PASS = _require_test_password()


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
    return bool(ok)


def api(method, path, token=None, body=None):
    """Minimal JSON API client. Returns (status_code, parsed_json_or_None)."""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def login():
    code, data = api("POST", "/api/auth/login", body={"username": USER, "password": PASS})
    if code != 200 or not isinstance(data, dict):
        raise RuntimeError(f"login failed: HTTP {code}")
    return data["token"]


# ── Part A1: in-process service logic (deterministic) ───────────────────────

def part_a1() -> int:
    print("\nPart A1 — service logic (in-process)")
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from backend.config import settings
    from backend.services import health_monitor_service as hms

    failures = 0
    tmp_datadir = Path(tempfile.mkdtemp(prefix="ethos-health-test-"))
    tmp_state = tmp_datadir / "health_state.json"

    emitted: list[dict] = []
    clock = {"now": datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)}
    disks = [{"mount": "/mnt/data", "device": "/dev/sdb1", "fstype": "xfs",
              "total": 1_000, "used": 950, "free": 50, "percent": 95.0}]
    units: list[dict] = []
    memory = {"percent": 45.0, "used": 45 * 10**9, "total": 10**11}
    temps = [{"label": "coretemp/Pkg", "current": 50.0}]
    smart: list[dict] = []

    saved = {
        "data_dir": settings.DATA_DIR,
        "emit_event": hms.emit_event,
        "_now": hms._now,
        "collect_disks": hms._collect_disks,
        "collect_failed_units": hms._collect_failed_units,
        "collect_memory": hms._collect_memory,
        "collect_temps": hms._collect_temps,
        "collect_smart": hms._collect_smart,
        "warn": settings.DISK_WARN_PERCENT,
        "recover": settings.DISK_RECOVER_PERCENT,
        "cooldown": settings.HEALTH_ALERT_COOLDOWN,
    }

    def fake_emit(kind, title, message="", source="system"):
        emitted.append({"kind": kind, "title": title, "message": message, "source": source})
        return {"id": f"fake-{len(emitted)}"}

    try:
        settings.DATA_DIR = tmp_datadir
        hms.emit_event = fake_emit
        hms._now = lambda: clock["now"]
        hms._collect_disks = lambda: [dict(d) for d in disks]
        hms._collect_failed_units = lambda: [dict(u) for u in units]
        hms._collect_memory = lambda: dict(memory)
        hms._collect_temps = lambda: [dict(t) for t in temps]
        hms._collect_smart = lambda: [dict(s) for s in smart]
        settings.DISK_WARN_PERCENT = 90.0
        settings.DISK_RECOVER_PERCENT = 85.0
        settings.HEALTH_ALERT_COOLDOWN = 21600

        # 1. Breach → exactly one warning, state persisted.
        r = hms.run_check()
        if not check("breach emits one warning event",
                     len(emitted) == 1 and emitted[0]["kind"] == "warning"
                     and emitted[0]["title"] == "Disk /mnt/data at 95%"
                     and emitted[0]["source"] == "health",
                     json.dumps(emitted)[:200]):
            failures += 1
        if not check("run_check returns snapshot + emitted list",
                     r["emitted"] == ["Disk /mnt/data at 95%"]
                     and r["disks"][0]["mount"] == "/mnt/data"
                     and isinstance(r["failed_units"], list)):
            failures += 1
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("alert state persisted to file",
                     "disk:/mnt/data" in (state.get("alerts") or {}),
                     json.dumps(state)[:200]):
            failures += 1

        # 2. Same condition one minute later → deduped, no new event.
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("no re-alert within cooldown", len(emitted) == 1 and r["emitted"] == []):
            failures += 1

        # 3. Hysteresis band (85 ≤ pct < 90) while alerting → silent, state kept.
        disks[0]["percent"], disks[0]["used"], disks[0]["free"] = 87.0, 870, 130
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("hysteresis band stays silent", len(emitted) == 1 and r["emitted"] == []):
            failures += 1
        state = json.loads(tmp_state.read_text())
        if not check("state kept during hysteresis band", "disk:/mnt/data" in (state.get("alerts") or {})):
            failures += 1

        # 4. Recovery below recover threshold → info event + state cleared.
        disks[0]["percent"], disks[0]["used"], disks[0]["free"] = 80.0, 800, 200
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("recovery emits info event",
                     len(emitted) == 2 and emitted[1]["kind"] == "info"
                     and emitted[1]["title"] == "Disk /mnt/data back to normal",
                     json.dumps(emitted[1:])[:200]):
            failures += 1
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("recovery clears alert state", "disk:/mnt/data" not in (state.get("alerts") or {})):
            failures += 1

        # 5. Re-breach after recovery → alerts again even inside the old cooldown window.
        disks[0]["percent"], disks[0]["used"], disks[0]["free"] = 96.0, 960, 40
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("re-breach after recovery alerts again",
                     len(emitted) == 3 and emitted[2]["kind"] == "warning"
                     and emitted[2]["title"] == "Disk /mnt/data at 96%"):
            failures += 1

        # 6. Cooldown expiry → re-alert for a persisting condition.
        clock["now"] += timedelta(seconds=settings.HEALTH_ALERT_COOLDOWN + 1)
        r = hms.run_check()
        if not check("re-alert after cooldown expiry", len(emitted) == 4 and emitted[3]["kind"] == "warning"):
            failures += 1

        # 7. Unit failure → error event; deduped on next check; recovery clears it.
        units.append({"name": "sshd.service", "description": "OpenSSH server daemon"})
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("failed unit emits error event",
                     len(emitted) == 5 and emitted[4]["kind"] == "error"
                     and emitted[4]["title"] == "Service sshd.service failed"
                     and "OpenSSH server daemon" in emitted[4]["message"],
                     json.dumps(emitted[4:])[:200]):
            failures += 1
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("failed unit deduped within cooldown", len(emitted) == 5 and r["emitted"] == []):
            failures += 1
        units.clear()
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("unit recovery emits info + clears state",
                     len(emitted) == 6 and emitted[5]["kind"] == "info"
                     and emitted[5]["title"] == "Service sshd.service recovered"
                     and "unit:sshd.service" not in (state.get("alerts") or {})):
            failures += 1

        # 7b. Memory pressure: breach → warning; deduped; recovery clears state.
        memory["percent"] = 95.0
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("memory breach emits one warning",
                     len(emitted) == 7 and emitted[6]["kind"] == "warning"
                     and emitted[6]["title"] == "Memory at 95%"
                     and emitted[6]["source"] == "health",
                     json.dumps(emitted[6:])[:200]):
            failures += 1
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("memory alert deduped within cooldown", len(emitted) == 7 and r["emitted"] == []):
            failures += 1
        memory["percent"] = 40.0
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("memory recovery emits info + clears state",
                     len(emitted) == 8 and emitted[7]["kind"] == "info"
                     and emitted[7]["title"] == "Memory back to normal"
                     and "memory" not in (state.get("alerts") or {})):
            failures += 1

        # 7c. Temperature: alert at ≥warn(85), deduped, recover below warn−5.
        temps[0]["current"] = 92.0
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("temp breach emits one warning",
                     len(emitted) == 9 and emitted[8]["kind"] == "warning"
                     and emitted[8]["title"] == "Sensor coretemp/Pkg at 92°C",
                     json.dumps(emitted[8:])[:200]):
            failures += 1
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("temp alert deduped within cooldown", len(emitted) == 9 and r["emitted"] == []):
            failures += 1
        temps[0]["current"] = 84.0   # hysteresis band [80, 85): stays quiet + state kept
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("temp hysteresis band silent, state kept",
                     len(emitted) == 9 and "temp:coretemp/Pkg" in (state.get("alerts") or {})):
            failures += 1
        temps[0]["current"] = 79.0   # below warn−5 → recovery
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("temp recovery emits info + clears state",
                     len(emitted) == 10 and emitted[9]["kind"] == "info"
                     and emitted[9]["title"] == "Sensor coretemp/Pkg back to normal"
                     and "temp:coretemp/Pkg" not in (state.get("alerts") or {})):
            failures += 1
        # Stale sensor label dropped silently (re-alert first, then the sensor
        # disappears from the snapshot).
        temps[0]["current"] = 92.0
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("temp re-breach alerts again after recovery",
                     len(emitted) == 11 and emitted[10]["title"] == "Sensor coretemp/Pkg at 92°C"):
            failures += 1
        temps.clear()
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("stale temp key dropped silently",
                     len(emitted) == 11 and "temp:coretemp/Pkg" not in (state.get("alerts") or {})):
            failures += 1
        temps.append({"label": "coretemp/Pkg", "current": 50.0})

        # 7d. SMART: whole-disk mapping, failure cycle, unreadable silence.
        if not check("_whole_disk: partitions deduped, nvme passthrough, unknown skipped",
                     hms._whole_disk("/dev/sda1") == "/dev/sda"
                     and hms._whole_disk("/dev/vdb2") == "/dev/vdb"
                     and hms._whole_disk("/dev/nvme0n1") == "/dev/nvme0n1"
                     and hms._whole_disk("efivarfs") is None
                     and hms._whole_disk("/dev/loop0") is None):
            failures += 1
        smart.append({"device": "/dev/sdb", "ok": True, "passed": False,
                      "model": "TestDisk", "attributes": {"Reallocated_Sector_Ct": 42}})
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("SMART failure emits error event with attributes",
                     len(emitted) == 12 and emitted[11]["kind"] == "error"
                     and emitted[11]["title"] == "Disk /dev/sdb failing (SMART)"
                     and "Reallocated_Sector_Ct=42" in emitted[11]["message"],
                     json.dumps(emitted[11:])[:200]):
            failures += 1
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("SMART alert deduped within cooldown", len(emitted) == 12 and r["emitted"] == []):
            failures += 1
        smart[0]["passed"] = True
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("SMART recovery emits info + clears state",
                     len(emitted) == 13 and emitted[12]["kind"] == "info"
                     and emitted[12]["title"] == "Disk /dev/sdb SMART back to normal"
                     and "smart:/dev/sdb" not in (state.get("alerts") or {})):
            failures += 1
        # Unreadable device (e.g. non-root) → silent, no state.
        smart[0] = {"device": "/dev/sdc", "ok": False, "error": "Permission denied"}
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("SMART unreadable device stays silent",
                     len(emitted) == 13 and r["emitted"] == []):
            failures += 1
        # Stale SMART key dropped when the disk disappears.
        smart[0] = {"device": "/dev/sdb", "ok": True, "passed": False,
                    "model": "TestDisk", "attributes": {}}
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("SMART re-breach alerts again",
                     len(emitted) == 14 and emitted[13]["title"] == "Disk /dev/sdb failing (SMART)"):
            failures += 1
        smart.clear()
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("stale SMART key dropped silently",
                     len(emitted) == 14 and "smart:/dev/sdb" not in (state.get("alerts") or {})):
            failures += 1

        # 10b. Config store: defaults without file; overrides merge; invalid entries
        #      are ignored defensively; cross-field clamp; update/reset semantics.
        cfg_file = tmp_datadir / "health_config.json"
        eff = hms._effective_config()
        if not check("config: no file → env defaults",
                     eff["disk_warn_percent"] == 90.0 and eff["disk_recover_percent"] == 85.0
                     and eff["alert_cooldown_seconds"] == 21600,
                     json.dumps(eff)[:200]):
            failures += 1
        # Lowering warn below the default recover (85) → auto-clamped to warn-1.
        eff = hms.update_config({"disk_warn_percent": 70, "check_interval_seconds": 60})
        if not check("config: valid patch persisted; implicit violation auto-clamped",
                     eff["disk_warn_percent"] == 70 and eff["check_interval_seconds"] == 60
                     and eff["disk_recover_percent"] == 69 and cfg_file.exists(),
                     json.dumps(eff)[:200]):
            failures += 1
        # Partial update keeps the other key.
        eff = hms.update_config({"alert_cooldown_seconds": 3600})
        if not check("config: partial patch preserves other overrides",
                     eff["disk_warn_percent"] == 70 and eff["check_interval_seconds"] == 60
                     and eff["alert_cooldown_seconds"] == 3600):
            failures += 1
        # Hand-edited file with junk → invalid entries dropped, valid kept.
        cfg_file.write_text(json.dumps({"version": 1, "overrides": {
            "disk_warn_percent": 50, "bogus_key": 1,
            "alert_cooldown_seconds": -5, "check_interval_seconds": "fast",
        }}))
        eff = hms._effective_config()
        if not check("config: hand-edited junk ignored defensively",
                     eff["disk_warn_percent"] == 50 and eff["check_interval_seconds"] == 300
                     and eff["alert_cooldown_seconds"] == 21600,
                     json.dumps(eff)[:200]):
            failures += 1
        # Cross-field clamp: recover >= warn → clamped to warn-1.
        cfg_file.write_text(json.dumps({"version": 1, "overrides": {
            "disk_warn_percent": 50, "disk_recover_percent": 90}}))
        eff = hms._effective_config()
        if not check("config: recover>=warn clamped to warn-1",
                     eff["disk_recover_percent"] == 49.0,
                     json.dumps(eff)[:200]):
            failures += 1
        # update_config strict validation → ValueError, nothing persisted.
        before = cfg_file.read_text()
        for bad_patch in [{"nope": 1}, {"disk_warn_percent": "high"},
                          {"check_interval_seconds": 5},
                          {"disk_recover_percent": 95}]:  # recover >= warn(50)
            try:
                hms.update_config(bad_patch)
                if not check(f"config: update rejects {bad_patch}", False, "no ValueError"):
                    failures += 1
            except ValueError:
                pass
        if not check("config: rejected patches persist nothing",
                     cfg_file.read_text() == before):
            failures += 1
        # Memory pair validation (file still holds the clamp-file state).
        try:
            hms.update_config({"memory_recover_percent": 95})  # ≥ warn(50), explicit
            if not check("config: memory recover≥warn rejected", False, "no ValueError"):
                failures += 1
        except ValueError:
            pass
        eff = hms.reset_config()
        if not check("config: reset clears overrides → defaults",
                     eff["disk_warn_percent"] == 90.0 and not cfg_file.exists()):
            failures += 1
        # Implicit clamp on the memory pair (fresh defaults after reset).
        eff = hms.update_config({"memory_warn_percent": 70})
        if not check("config: lowering mem warn auto-clamps mem recover",
                     eff["memory_warn_percent"] == 70 and eff["memory_recover_percent"] == 69,
                     json.dumps(eff)[:200]):
            failures += 1
        hms.reset_config()
        # Temperature threshold: range validation + persistence.
        try:
            hms.update_config({"temp_warn_celsius": 30})   # below min 40
            if not check("config: temp warn out of range rejected", False, "no ValueError"):
                failures += 1
        except ValueError:
            pass
        eff = hms.update_config({"temp_warn_celsius": 75})
        if not check("config: temp warn persisted",
                     eff["temp_warn_celsius"] == 75 and cfg_file.exists()):
            failures += 1
        hms.reset_config()

        # 8. Stale disk key for an unmounted volume is dropped silently.
        disks.clear()   # /mnt/data (alerting, 96%) vanishes from the mount list
        emitted.clear()
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        state = json.loads(tmp_state.read_text()) if tmp_state.exists() else {}
        if not check("unmounted volume's stale key dropped silently",
                     r["emitted"] == [] and "disk:/mnt/data" not in (state.get("alerts") or {}),
                     f"emitted={r['emitted']} state={json.dumps(state)[:200]}"):
            failures += 1

        # 9. Pseudo-fs filtering + st_dev bind-mount dedup (fake psutil/os).
        class _P:  # fake psutil partition entry
            def __init__(self, mountpoint, device, fstype):
                self.mountpoint, self.device, self.fstype = mountpoint, device, fstype

        fakes = [
            _P("/", "/dev/sda1", "ext4"),
            _P("/mnt/data", "/dev/sdb1", "xfs"),
            _P("/media/x/data", "/dev/sdb1", "xfs"),   # bind mount of /mnt/data (same st_dev)
            _P("/snap/foo/1", "/dev/loop0", "squashfs"),  # filtered: read-only image
            _P("/proc", "proc", "proc"),                    # filtered: pseudo-fs
        ]
        dev_map = {"/": 1, "/mnt/data": 2, "/media/x/data": 2}

        class _FakeStat:
            def __init__(self, st_dev): self.st_dev = st_dev

        fake_os = types.SimpleNamespace(stat=lambda p: _FakeStat(dev_map[p]))
        fake_psutil = types.SimpleNamespace(
            disk_partitions=lambda all=True: list(fakes),
            disk_usage=lambda p: types.SimpleNamespace(total=10**9, used=9 * 10**8, free=10**8, percent=90.0),
        )
        # Swap back the REAL collector (still shadowed by the test-1 lambda) and
        # point its psutil/os at the fakes.
        real_collect = saved["collect_disks"]
        hms._collect_disks = real_collect
        saved_os, saved_psutil = hms.os, hms.psutil
        hms.os, hms.psutil = fake_os, fake_psutil
        try:
            got = hms._collect_disks()
            mounts = sorted(d["mount"] for d in got)
            if not check("pseudo-fs filtered + bind mount deduped by st_dev",
                         mounts == ["/", "/mnt/data"], f"got {mounts}"):
                failures += 1
        finally:
            hms.os, hms.psutil = saved_os, saved_psutil

        # 9b. SMART attribute trends — early degradation detection.
        smart.clear()
        emitted.clear()
        hist_file = Path(tmp_datadir) / "smart_history.json"
        base_attrs = {"Reallocated_Sector_Ct": 0, "Current_Pending_Sector": 2,
                      "Offline_Uncorrectable": 0, "Percentage_Used": 12}
        smart.append({"device": "/dev/sda", "ok": True, "passed": True,
                      "model": "TestDisk", "attributes": dict(base_attrs)})

        # First sighting → baseline only, no alert.
        clock["now"] += timedelta(minutes=1)
        r = hms.run_check()
        if not check("smart trend: first sighting records baseline silently",
                     r["emitted"] == [] and hist_file.exists(),
                     f"emitted={r['emitted']}"):
            failures += 1
        rec0 = json.loads(hist_file.read_text())["devices"]["/dev/sda"] if hist_file.exists() else {}
        if not check("smart trend: baseline stored with prev_attrs null",
                     rec0.get("prev_attrs") is None
                     and (rec0.get("attrs") or {}).get("Reallocated_Sector_Ct") == 0,
                     json.dumps(rec0)[:200]):
            failures += 1

        # Realloc grows 0→3 → exactly one error event naming attr + both values.
        clock["now"] += timedelta(minutes=5)
        smart[0]["attributes"]["Reallocated_Sector_Ct"] = 3
        r = hms.run_check()
        trend_events = [e for e in emitted if "SMART degradation" in e["title"]]
        if not check("smart trend: growth emits one error event",
                     len(trend_events) == 1 and trend_events[0]["kind"] == "error"
                     and trend_events[0]["title"] == "Disk /dev/sda SMART degradation"
                     and "Reallocated_Sector_Ct" in trend_events[0]["message"]
                     and "0 → 3" in trend_events[0]["message"],
                     json.dumps(emitted)[:300]):
            failures += 1

        # Same value again → silent (change-based dedup, no cooldown state).
        clock["now"] += timedelta(minutes=5)
        r = hms.run_check()
        if not check("smart trend: unchanged value stays silent",
                     len(emitted) == 1 and r["emitted"] == []):
            failures += 1

        # Grows again (3→5) → re-alerts with the new delta.
        clock["now"] += timedelta(minutes=5)
        smart[0]["attributes"]["Reallocated_Sector_Ct"] = 5
        r = hms.run_check()
        if not check("smart trend: further growth re-alerts",
                     len(r["emitted"]) == 1 and "3 → 5" in emitted[-1]["message"],
                     f"emitted={r['emitted']}"):
            failures += 1

        # Pending decreases (2→0) + wear counter grows (12→13) → no events.
        clock["now"] += timedelta(minutes=5)
        smart[0]["attributes"]["Current_Pending_Sector"] = 0
        smart[0]["attributes"]["Percentage_Used"] = 13
        r = hms.run_check()
        if not check("smart trend: decrease + wear growth stay silent",
                     len(emitted) == 2 and r["emitted"] == []):
            failures += 1

        # Unreadable probe → last recorded sample kept untouched, stays silent.
        clock["now"] += timedelta(minutes=5)
        smart[0] = {"device": "/dev/sda", "ok": False, "error": "permission denied"}
        r = hms.run_check()
        rec1 = json.loads(hist_file.read_text())["devices"]["/dev/sda"]
        if not check("smart trend: unreadable probe keeps last sample",
                     len(emitted) == 2 and r["emitted"] == []
                     and (rec1.get("attrs") or {}).get("Reallocated_Sector_Ct") == 5,
                     json.dumps(rec1)[:200]):
            failures += 1

        # Stale record (unseen >14 days, device absent) → pruned; recent kept.
        hist = json.loads(hist_file.read_text())
        hist["devices"]["/dev/sdb"] = {
            "last_seen": (clock["now"] - timedelta(days=20)).isoformat(timespec="seconds"),
            "attrs": {}, "prev_attrs": None}
        hist_file.write_text(json.dumps(hist))
        clock["now"] += timedelta(minutes=5)
        r = hms.run_check()
        recs = json.loads(hist_file.read_text())["devices"]
        if not check("smart trend: stale record pruned, recent kept",
                     "/dev/sdb" not in recs and "/dev/sda" in recs,
                     f"keys={sorted(recs)}"):
            failures += 1

        smart.clear()   # leave the fake empty for the real-environment sanity test

    finally:
        for k, v in saved.items():
            if k == "data_dir":
                settings.DATA_DIR = v
            elif k == "emit_event":
                hms.emit_event = v
            elif k == "_now":
                hms._now = v
            elif k == "collect_disks":
                hms._collect_disks = v
            elif k == "collect_failed_units":
                hms._collect_failed_units = v
            elif k == "collect_memory":
                hms._collect_memory = v
            elif k == "collect_temps":
                hms._collect_temps = v
            elif k == "collect_smart":
                hms._collect_smart = v
            else:
                setattr(settings, {"warn": "DISK_WARN_PERCENT",
                                   "recover": "DISK_RECOVER_PERCENT",
                                   "cooldown": "HEALTH_ALERT_COOLDOWN"}[k], v)
        shutil.rmtree(tmp_datadir, ignore_errors=True)

    # 10. Real-environment sanity: _collect_disks() on this host returns sane data.
    real = hms._collect_disks()
    if not check("real _collect_disks(): ≥1 mount, valid fields, no pseudo-fs",
                 len(real) >= 1 and all(
                     d["mount"] and d["fstype"] not in hms._PSEUDO_FSTYPES
                     and 0 <= d["percent"] <= 100 for d in real),
                 f"got {len(real)} mounts"):
        failures += 1

    return failures


# ── Part A2: live API E2E (isolated instance, NO env overrides) ─────────────

def part_a2(token) -> int:
    print("\nPart A2 — API E2E")
    failures = 0

    # 1. Status shape + env defaults visible.
    code, data = api("GET", "/api/health/status", token)
    if not check("GET /api/health/status → 200 with disks/failed_units/monitor",
                 code == 200 and isinstance(data, dict)
                 and isinstance(data.get("disks"), list)
                 and isinstance(data.get("failed_units"), list)
                 and isinstance(data.get("monitor"), dict), f"HTTP {code}"):
        return failures + 1
    mon = data["monitor"]
    if not check("monitor shows env defaults (disk+mem warn=90, temp 85°C)",
                 float(mon.get("disk_warn_percent", -1)) == 90.0
                 and float(mon.get("memory_warn_percent", -1)) == 90.0
                 and float(mon.get("temp_warn_celsius", -1)) == 85.0
                 and isinstance(mon.get("defaults"), dict)
                 and mon.get("interval_seconds"), json.dumps(mon)[:200]):
        failures += 1
    mem = data.get("memory") or {}
    if not check("status includes memory block (percent/used/total)",
                 isinstance(mem.get("percent"), (int, float)) and mem.get("total", 0) > 0,
                 json.dumps(mem)[:200]):
        failures += 1
    temps_list = data.get("temps") or []
    if not check("status includes non-empty temps list ({label, current})",
                 len(temps_list) >= 1 and all(
                     set(("label", "current")) <= set(t)
                     and isinstance(t["current"], (int, float)) for t in temps_list),
                 json.dumps(temps_list)[:200]):
        failures += 1
    trends0 = data.get("smart_trends", "missing")
    if not check("status.smart_trends present (null or {devices:[...]})",
                 trends0 is None or (isinstance(trends0, dict)
                                     and isinstance(trends0.get("devices"), list)),
                 json.dumps(trends0)[:200] if trends0 is not None else "null"):
        failures += 1
    disks = data["disks"]
    if not check("disks non-empty with full fields",
                 len(disks) >= 1 and all(
                     set(("mount", "device", "fstype", "total", "used", "free", "percent")) <= set(d)
                     for d in disks), f"got {len(disks)}"):
        failures += 1
    current_mounts = {d["mount"] for d in disks}

    # Preflight: clear any pre-existing alert entries left in the persistent
    # health state (by the production monitor or a previous test run) so their
    # cooldowns can't suppress the warnings this test asserts on. Raising every
    # warn+recover threshold above the current readings makes the next check
    # recover them (recover is only auto-clamped when warn is lowered, so it
    # must be raised explicitly here).
    code, r = api("PUT", "/api/health/config", token,
                  body={"disk_warn_percent": 100, "disk_recover_percent": 99,
                        "memory_warn_percent": 100, "memory_recover_percent": 99,
                        "temp_warn_celsius": 125})
    if not check("preflight: raise all thresholds → 200",
                 code == 200 and isinstance(r, dict), f"HTTP {code}"):
        failures += 1
    api("POST", "/api/health/check-now", token)  # recovers any existing entries

    # 2. Runtime overrides: PUT disk warn=1 + mem warn=1 → take effect on the
    #    very next check.
    code, r = api("PUT", "/api/health/config", token, body={"disk_warn_percent": 1})
    if not check("PUT /api/health/config {warn:1} → 200 + effective=1",
                 code == 200 and isinstance(r, dict)
                 and float((r.get("effective") or {}).get("disk_warn_percent", -1)) == 1.0,
                 f"HTTP {code}"):
        failures += 1
    code, r = api("PUT", "/api/health/config", token, body={"memory_warn_percent": 1})
    if not check("PUT {mem warn:1} → 200 + effective=1",
                 code == 200 and isinstance(r, dict)
                 and float((r.get("effective") or {}).get("memory_warn_percent", -1)) == 1.0,
                 f"HTTP {code}"):
        failures += 1
    code, r = api("PUT", "/api/health/config", token, body={"temp_warn_celsius": 40})
    if not check("PUT {temp warn:40} → 200 + effective=40",
                 code == 200 and isinstance(r, dict)
                 and float((r.get("effective") or {}).get("temp_warn_celsius", -1)) == 40.0,
                 f"HTTP {code}"):
        failures += 1
    code, r = api("POST", "/api/health/check-now", token)
    if not check("check-now after overrides → 200 with snapshot + emitted",
                 code == 200 and isinstance(r, dict) and "emitted" in r,
                 f"HTTP {code}"):
        failures += 1
    emitted_titles = (r or {}).get("emitted") or []
    if not check("override live: disk warnings emitted on next check",
                 any(t.startswith("Disk ") for t in emitted_titles),
                 f"emitted={emitted_titles}"):
        failures += 1
    mem_emitted = [t for t in emitted_titles if t.startswith("Memory at")]
    if not check("override live: exactly one memory warning (host RAM ≥1%)",
                 len(mem_emitted) == 1, f"emitted={emitted_titles}"):
        failures += 1
    # Expected warnings = sensors at/above the (just-applied) 40°C threshold in
    # the very snapshot this check evaluated — cold sensors (e.g. acpitz ~28°C)
    # correctly stay quiet.
    check_temps = r.get("temps") or []
    expected_temp_alerts = sum(1 for t in check_temps if t["current"] >= 40)
    temp_emitted = [t for t in emitted_titles if t.startswith("Sensor ")]
    if not check(f"override live: one warning per hot sensor ({expected_temp_alerts} of {len(check_temps)} ≥40°C)",
                 len(temp_emitted) == expected_temp_alerts and expected_temp_alerts >= 1,
                 f"emitted={temp_emitted}"):
        failures += 1

    # 3. Coverage of breached disks (warn=1 → every mount ≥2% must be warned).
    code, ndata = api("GET", "/api/notifications?limit=200", token)
    events = ndata.get("events") if isinstance(ndata, dict) else []
    health_warns = [e for e in events
                    if e.get("source") == "health" and e.get("kind") == "warning"]
    warned_mounts = set()
    for e in health_warns:
        t = e.get("title") or ""
        if t.startswith("Disk ") and " at " in t:
            warned_mounts.add(t.split(" at ")[0][len("Disk "):])
    must_have = {d["mount"] for d in disks if d["percent"] >= 2.0}
    missing = must_have - warned_mounts
    if not check("every stable-breached disk has a warning event",
                 len(health_warns) >= 1 and not missing, f"missing={sorted(missing)}"):
        failures += 1
    phantoms = warned_mounts - current_mounts
    if not check("no warnings for unknown mounts", not phantoms, f"phantoms={sorted(phantoms)}"):
        failures += 1

    # 4. Immediate re-check → ZERO new events (dedup/cooldown proven live).
    code, r = api("POST", "/api/health/check-now", token)
    if not check("manual re-check emits no duplicate alerts (dedup live)",
                 isinstance(r, dict) and r.get("emitted") == [],
                 f"emitted={r.get('emitted') if isinstance(r, dict) else '?'}"):
        failures += 1
    fu = r.get("failed_units") if isinstance(r, dict) else None
    if not check("check-now failed_units well-formed",
                 isinstance(fu, list) and all(set(("name", "description")) <= set(u) for u in (fu or [])),
                 f"got {fu}"):
        failures += 1

    # SMART: populated after the startup check; on this (non-root) test host every
    # probe degrades to ok:false with an error — and crucially NO false "failing"
    # alerts. On a root instance with healthy disks results would be ok:true.
    code, data = api("GET", "/api/health/status", token)
    smart = (data or {}).get("smart")
    if not check("status.smart populated after startup check",
                 isinstance(smart, dict) and smart.get("checked_at")
                 and isinstance(smart.get("results"), list) and len(smart["results"]) >= 1,
                 json.dumps(smart)[:200]):
        failures += 1
    else:
        ok_shape = all(set(("device", "ok")) <= set(s)
                       and (s["ok"] or bool(s.get("error"))) for s in smart["results"])
        if not check("smart results well-formed ({device, ok[, error]})",
                     ok_shape, json.dumps(smart["results"])[:200]):
            failures += 1
    code, ndata = api("GET", "/api/notifications?limit=200", token)
    smart_events = [e for e in (ndata or {}).get("events", [])
                    if "SMART" in str(e.get("title", ""))]
    if not check("no spurious SMART events on healthy/unreadable disks",
                 len(smart_events) == 0, json.dumps(smart_events)[:200]):
        failures += 1
    # Trend store: well-formed whatever the host's privilege level — null when
    # every probe is unreadable (non-root), populated with per-device samples
    # otherwise. Degradation events on a healthy drive are impossible by design.
    trends = data.get("smart_trends", "missing") if isinstance(data, dict) else None
    if not check("status.smart_trends well-formed after checks",
                 trends is None or (isinstance(trends, dict) and all(
                     set(("device", "last_seen", "attrs", "prev_attrs", "changes")) <= set(d)
                     for d in trends.get("devices", []))),
                 json.dumps(trends)[:200] if trends is not None else "null"):
        failures += 1

    # 5. Validation: cross-field + range violations → 422.
    code, r = api("PUT", "/api/health/config", token,
                  body={"disk_recover_percent": 95})  # recover ≥ warn(1)
    if not check("PUT disk recover≥warn → 422 with detail",
                 code == 422 and isinstance(r, dict) and r.get("detail"), f"HTTP {code}"):
        failures += 1
    code, r = api("PUT", "/api/health/config", token,
                  body={"memory_recover_percent": 95})  # ≥ mem warn(1)
    if not check("PUT memory recover≥warn → 422 with detail",
                 code == 422 and isinstance(r, dict) and r.get("detail"), f"HTTP {code}"):
        failures += 1
    code, r = api("PUT", "/api/health/config", token,
                  body={"check_interval_seconds": 5})  # below min 30
    if not check("PUT interval out of range → 422",
                 code == 422 and isinstance(r, dict) and r.get("detail"), f"HTTP {code}"):
        failures += 1
    code, r = api("PUT", "/api/health/config", token,
                  body={"temp_warn_celsius": 30})  # below min 40
    if not check("PUT temp warn out of range → 422 with detail",
                 code == 422 and isinstance(r, dict) and r.get("detail"), f"HTTP {code}"):
        failures += 1

    # 6. Reset → back to env defaults.
    code, r = api("DELETE", "/api/health/config", token)
    if not check("DELETE /config resets to env defaults (disk+mem warn=90, temp 85°C)",
                 code == 200 and isinstance(r, dict)
                 and float((r.get("effective") or {}).get("disk_warn_percent", -1)) == 90.0
                 and float((r.get("effective") or {}).get("memory_warn_percent", -1)) == 90.0
                 and float((r.get("effective") or {}).get("temp_warn_celsius", -1)) == 85.0,
                 f"HTTP {code}"):
        failures += 1
    code, data = api("GET", "/api/health/status", token)
    mon2 = (data or {}).get("monitor") or {}
    if not check("status reflects reset (disk+mem warn=90, temp 85°C)",
                 float(mon2.get("disk_warn_percent", -1)) == 90.0
                 and float(mon2.get("memory_warn_percent", -1)) == 90.0
                 and float(mon2.get("temp_warn_celsius", -1)) == 85.0):
        failures += 1

    # Cleanup: delete only the health-source events this test's instance created.
    deleted = 0
    for e in events:
        if e.get("source") == "health":
            c, _ = api("DELETE", f"/api/notifications/{e['id']}", token)
            deleted += (c == 200)
    print(f"         cleanup: removed {deleted} health-source event(s)")
    return failures


# ── Part B: UI (Playwright; auto-skips without chromium) ─────────────────────

def part_b(token) -> int:
    print("\nPart B — UI (Playwright)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [SKIP] playwright not installed")
        return 0

    failures = 0
    # The whole body must run INSIDE the sync_playwright() context — the
    # driver's event loop is closed when the with-block exits, and any
    # browser call after that raises "Event loop is closed" (T-0007).
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:
            print(f"  [SKIP] chromium unavailable ({str(e).splitlines()[0][:120]}…)")
            return 0

        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        js_errors: list[str] = []
        page.on("pageerror", lambda e: js_errors.append(str(e)))

        try:
            # Fresh page → login overlay.
            page.goto(BASE, wait_until="networkidle")
            page.evaluate("localStorage.removeItem('token')")
            page.reload(wait_until="networkidle")
            page.fill('[data-testid="login-username"]', USER)
            page.fill('[data-testid="login-password"]', PASS)
            # The login handler hides the overlay and then does
            # window.location.reload() — mark the document so we can wait for
            # the fresh post-login page before interacting (T-0007).
            page.evaluate("window.__preLoginDoc = true")
            page.click('[data-testid="login-submit"]')
            page.wait_for_function(
                "() => { const o = document.getElementById('loginOverlay'); return !o || o.style.display === 'none'; }",
                timeout=20_000,
            )
            page.wait_for_function(
                "() => !window.__preLoginDoc && document.readyState === 'complete'",
                timeout=20_000,
            )

            # Open Settings → System tab (default) and wait for the health card.
            page.evaluate("window.openApp('settings')")
            try:
                page.wait_for_function(
                    "() => { const c = document.getElementById('healthMonitorCard');"
                    " return c && !/Loading/.test(c.textContent); }", timeout=15_000)
            except Exception:
                pass
            card = page.locator("#healthMonitorCard")
            if not check("Health Monitor card rendered in Settings → System",
                         card.count() == 1 and "Health Monitor" in (card.inner_text() or "")):
                browser.close()
                return failures + 1

            # Run check now → toast appears.
            page.click("#healthMonitorCard button:has-text('Run check now')")
            try:
                # Unified toast renders .notification elements (notification-center.js)
                page.wait_for_selector("#toastContainer .notification", timeout=8_000)
                if not check("'Run check now' produced a toast", True):
                    failures += 1
            except Exception:
                if not check("'Run check now' produced a toast", False, "no toast within 8 s"):
                    failures += 1

            if js_errors:
                print(f"         page errors: {js_errors[:3]}")
        finally:
            browser.close()
    return failures


def main():
    total = part_a1()

    try:
        token = login()
    except Exception as e:
        print(f"\n[SKIP] Part A2/B — server not reachable at {BASE} ({e})")
        print("       Start a fresh instance first:")
        print("         fuser -k 9001/tcp 2>/dev/null; ETHOS_PORT=9001 ETHOS_DISK_WARN_PERCENT=1 \\")
        print("           nohup .venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 9001 \\")
        print("           > /tmp/ethos-nas-health-test.log 2>&1 &")
        print(f"       then re-run with ETHOS_TEST_BASE=http://127.0.0.1:9001")
        sys.exit(1 if total else 0)

    total += part_a2(token)
    total += part_b(token)

    print(f"\n{'=' * 50}\nRESULT: {'ALL PASS' if total == 0 else f'{total} FAILURE(S)'}")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
