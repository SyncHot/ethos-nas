"""EthOS NAS — System Health Monitor Service

Background checker that watches the two highest-value NAS failure modes and
reports them to the Notification Center (services/notifications_service.py):

1. Disk capacity  — any real mount at/above DISK_WARN_PERCENT → `warning` event;
   usage dropping below DISK_RECOVER_PERCENT → `info` recovery event.
2. Unit failures  — a systemd unit enters the failed state → `error` event;
   it leaves that state → `info` recovery event.

Alerts are deduplicated per subject (mount point / unit) and persisted to
backend/data/health_state.json, so:
- a persisting condition re-alerts at most once every HEALTH_ALERT_COOLDOWN;
- a server restart does not re-spam already-reported conditions.

The loop is lifespan-managed via start_health_monitor()/stop_health_monitor()
(same pattern as the backup scheduler). run_check() is synchronous and may be
called directly from POST /api/health/check-now or from tests.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import psutil

from backend.config import settings
from backend.services.notifications_service import emit_event

logger = logging.getLogger("ethos-nas.services.health_monitor_service")

def _state_file() -> Path:
    """Resolved per call so ETHOS_DATA_DIR is honoured (and testable)."""
    return Path(settings.DATA_DIR) / "health_state.json"

# Filesystems that are kernel-internal or read-only images — never user data,
# never meaningfully fillable. tmpfs is deliberately NOT listed: /dev/shm and
# /run are real failure modes on a Docker host.
_PSEUDO_FSTYPES = {
    "proc", "sysfs", "devtmpfs", "devpts", "cgroup", "cgroup2", "pstore",
    "securityfs", "debugfs", "tracefs", "configfs", "fusectl", "mqueue",
    "hugetlbfs", "bpf", "autofs", "rpc_pipefs", "ramfs", "squashfs", "iso9660",
}

_LOCK = threading.Lock()
_status: dict = {"last_check_at": None, "checks_run": 0}


def _now() -> datetime:
    """UTC clock — module-level so tests can monkeypatch it."""
    return datetime.now(timezone.utc)


# ── Collection (pure reads, no side effects) ─────────────────────────────────

def _collect_disks() -> list[dict]:
    """Usage of every real mount.

    Dedups by kernel device number (os.stat().st_dev): bind mounts of the same
    volume share st_dev and would otherwise double-alert; distinct tmpfs/overlay
    instances have their own st_dev and are kept separately. For each device the
    shortest mount path is kept as canonical.
    """
    best: dict[int, Any] = {}  # st_dev -> psutil partition entry (sdiskpart)
    for p in psutil.disk_partitions(all=True):
        if p.fstype in _PSEUDO_FSTYPES:
            continue
        try:
            st_dev = os.stat(p.mountpoint).st_dev
        except OSError:
            continue  # vanished/broken mount point
        cur = best.get(st_dev)
        if cur is None or len(p.mountpoint) < len(cur.mountpoint):
            best[st_dev] = p

    disks = []
    for p in best.values():
        try:
            usage = psutil.disk_usage(p.mountpoint)
        except (OSError, PermissionError):
            continue
        disks.append({
            "mount": p.mountpoint,
            "device": p.device,
            "fstype": p.fstype,
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round(usage.percent, 1),
        })
    disks.sort(key=lambda d: (-d["percent"], d["mount"]))
    return disks


def _run_systemctl(args: list[str], timeout: int = 15) -> str | None:
    """Run systemctl with given args; stdout on success else None. Never raises."""
    try:
        result = subprocess.run(
            ["systemctl"] + args, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout if result.returncode == 0 else None
    except FileNotFoundError:
        logger.debug("systemctl not found — unit checks disabled")
        return None
    except Exception as exc:  # noqa: BLE001 — health check must never crash the loop
        logger.debug("systemctl error: %s", exc)
        return None


def _collect_failed_units() -> list[dict]:
    """Parse ``systemctl --failed --no-legend`` → [{name, description}]."""
    out = _run_systemctl(["--failed", "--no-legend"])
    if not out:
        return []
    units = []
    for line in out.strip().splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        units.append({"name": parts[0], "description": parts[4]})
    return units


def _whole_disk(dev: str) -> str | None:
    """Map a (possibly partitioned) device path to its whole disk.

    ``/dev/sda1`` → ``/dev/sda``; NVMe namespaces pass through (smartctl 7.x
    resolves them); unknown schemes (loop, dm-*, efivarfs…) are skipped.
    """
    if not dev.startswith("/dev/"):
        return None
    name = os.path.basename(dev)
    if re.fullmatch(r"(?:sd|vd|xvd|hd)[a-z]+\d+", name):
        return f"/dev/{re.sub('\\d+$', '', name)}"
    if re.fullmatch(r"nvme\d+n\d+", name):
        return dev
    return None


def _probe_smart(dev: str) -> dict:
    """Run ``smartctl -j <dev>`` and reduce it to pass/fail + key attributes.

    Never raises — every failure mode (missing binary, permission denied,
    timeout, bad JSON) yields ``{device, ok: False, error}``.
    """
    try:
        proc = subprocess.run(["smartctl", "-j", dev],
                              capture_output=True, text=True, timeout=SMART_TIMEOUT_S)
        if proc.returncode != 0:
            # smartctl reports open/read failures on stderr (e.g. permission
            # denied) — must NOT be read as "device failing".
            lines = [l for l in (proc.stderr or "").strip().splitlines() if l.strip()]
            return {"device": dev, "ok": False,
                    "error": (lines[-1] if lines else f"exit code {proc.returncode}")[:120]}
        data = json.loads(proc.stdout or "{}")
        passed = bool((data.get("smart_status") or {}).get("passed"))
        table = (data.get("ata_smart_attributes") or {}).get("table", [])
        attrs = {a["name"]: a.get("value") for a in table
                 if isinstance(a, dict) and a.get("name") in _KEY_SMART_ATTRS}
        temp_info = data.get("temperature") or {}
        return {"device": dev, "ok": True, "passed": passed,
                "temperature": temp_info.get("current") if isinstance(temp_info, dict) else None,
                "model": data.get("model_name", ""), "attributes": attrs}
    except FileNotFoundError:
        return {"device": dev, "ok": False, "error": "smartctl not installed"}
    except subprocess.TimeoutExpired:
        return {"device": dev, "ok": False,
                "error": f"timed out after {SMART_TIMEOUT_S}s"}
    except Exception as exc:  # noqa: BLE001 — health check must never crash the loop
        return {"device": dev, "ok": False, "error": str(exc)[:120]}


def _collect_smart() -> list[dict]:
    """SMART status for every whole disk backing a current mount.

    Partition devices are deduped to their whole disk; probes run in parallel
    (≤4 workers). Slow by design — only called from run_check, never on the
    read path of GET /status.
    """
    devs: list[str] = []
    seen: set[str] = set()
    for d in _collect_disks():
        wd = _whole_disk(d["device"])
        if wd and wd not in seen:
            seen.add(wd)
            devs.append(wd)
    if not devs:
        return []
    with ThreadPoolExecutor(max_workers=min(4, len(devs))) as pool:
        return list(pool.map(_probe_smart, devs))


# Last completed SMART results — served by GET /status without re-probing.
_LAST_SMART: dict = {"checked_at": None, "results": []}


def last_smart() -> dict | None:
    """Cached SMART results from the most recent run_check (None before the
    first check completes)."""
    if _LAST_SMART["checked_at"] is None:
        return None
    return {"checked_at": _LAST_SMART["checked_at"], "results": list(_LAST_SMART["results"])}


# ── SMART attribute trends (early degradation warning) ───────────────────────
# Error-type attributes where ANY increase is a real event: the drive is
# reallocating / losing sectors while smart_status may still report PASSED.
# Wear counters (Wear_Leveling_Count, Percentage_Used) grow monotonically over
# a device's lifetime — recorded and displayed, but never alerted on.
_TREND_ALERT_ATTRS = (
    "Reallocated_Sector_Ct",
    "Current_Pending_Sector",
    "Offline_Uncorrectable",
)

# Records for devices unseen this long are pruned (unplugged / replaced disk).
_SMART_HISTORY_STALE_DAYS = 14
# Ring buffer per device: one sample per health check (default 5 min) → ~24 h-
# plus. Bounded so the store can never grow without limit.
_SMART_SAMPLES_MAX = 288


def _smart_history_file() -> Path:
    return Path(settings.DATA_DIR) / "smart_history.json"


def _load_smart_history() -> dict:
    """Persisted per-device attribute samples; never raises.

    Version 1 kept only the current and previous attribute maps (alert deltas).
    Version 2 adds ``samples`` — a bounded time series the SMART app charts.
    """
    try:
        with open(_smart_history_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        devs = data.get("devices") if isinstance(data, dict) else None
        return {"version": 2,
                "devices": ({k: v for k, v in devs.items() if isinstance(v, dict)}
                            if isinstance(devs, dict) else {})}
    except FileNotFoundError:
        return {"version": 2, "devices": {}}
    except Exception as exc:  # noqa: BLE001 — corrupt store must not break checks
        logger.warning("smart history unreadable (%s) — starting fresh", exc)
        return {"version": 2, "devices": {}}


def _save_smart_history(history: dict) -> None:
    """Atomic write (same pattern as the alert state)."""
    hist_file = _smart_history_file()
    hist_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(hist_file.parent),
                               prefix=".smart-hist-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({**history, "version": 2}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, hist_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best-effort cleanup — nothing to report
        raise


def _parse_iso(raw: str | None) -> datetime | None:
    """Parse an ISO timestamp (UTC assumed); None when missing/unparseable."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def smart_trends() -> dict | None:
    """Persisted SMART attribute samples for the API read path (no probing).

    ``None`` until the first readable sample has been recorded. Each device
    carries its current attrs, the previous sample, and ``changes`` — alert
    attributes that grew since the previous sample.
    """
    history = _load_smart_history()
    if not history["devices"]:
        return None
    devices = []
    for dev in sorted(history["devices"]):
        rec = history["devices"][dev]
        attrs = rec.get("attrs") or {}
        prev_attrs = rec.get("prev_attrs")
        changes = [a for a in _TREND_ALERT_ATTRS
                   if isinstance(prev_attrs, dict) and a in prev_attrs and a in attrs
                   and isinstance(prev_attrs[a], (int, float))
                   and isinstance(attrs[a], (int, float))
                   and attrs[a] > prev_attrs[a]]
        devices.append({"device": dev, "last_seen": rec.get("last_seen"),
                        "attrs": attrs, "prev_attrs": prev_attrs,
                        "changes": changes})
    return {"devices": devices}


def _next_samples(rec: dict, now: datetime, attrs: dict, temp) -> list[dict]:
    """Append one bounded sample to a device's time series."""
    raw = rec.get("samples") if isinstance(rec, dict) else None
    samples = [sample for sample in raw if isinstance(sample, dict)] if isinstance(raw, list) else []
    samples = samples[-_SMART_SAMPLES_MAX:]
    samples.append({
        "ts": now.isoformat(timespec="seconds"),
        "temp": temp if isinstance(temp, (int, float)) else None,
        "attrs": {k: v for k, v in attrs.items() if isinstance(v, (int, float))},
    })
    return samples[-_SMART_SAMPLES_MAX:]


def smart_history(hours: int = 24, device: str | None = None) -> dict:
    """Time series for the SMART app — persisted samples, no probing.

    ``hours`` is clamped to 1…168 (the ring holds roughly a day at the default
    interval; a longer window simply returns everything recorded).
    """
    hours = max(1, min(168, int(hours or 24)))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    history = _load_smart_history()
    out = []
    for dev in sorted(history["devices"]):
        if device and dev != device:
            continue
        rec = history["devices"][dev]
        samples = rec.get("samples") if isinstance(rec, dict) else None
        kept = []
        for sample in samples if isinstance(samples, list) else []:
            if not isinstance(sample, dict):
                continue
            ts = _parse_iso(sample.get("ts"))
            if ts is None or ts < cutoff:
                continue
            kept.append({
                "ts": sample.get("ts"),
                "temp": sample.get("temp"),
                "attrs": sample.get("attrs") if isinstance(sample.get("attrs"), dict) else {},
            })
        out.append({"device": dev, "samples": kept})
    return {"hours": hours, "devices": out}


def _collect_temps() -> list[dict]:
    """All temperature sensors (CPU package/cores, NVMe drives) as
    ``{label: "<source>/<entry>", current}`` readings. Degrades to []."""
    try:
        sensors = psutil.sensors_temperatures()
    except Exception as exc:  # noqa: BLE001 — health check must never crash the loop
        logger.debug("sensors_temperatures failed: %s", exc)
        return []
    temps: list[dict] = []
    for source, entries in (sensors or {}).items():
        for e in entries:
            if e.current is None:
                continue
            label = f"{source}/{e.label}" if e.label else source
            temps.append({"label": label, "current": round(e.current, 1)})
    return temps


def _collect_memory() -> dict:
    """Current RAM/swap usage + PSI stalls; degrades to zeros rather than raising."""
    try:
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
    except Exception as exc:  # noqa: BLE001 — health check must never crash the loop
        logger.debug("virtual_memory failed: %s", exc)
        return {"percent": 0.0, "used": 0, "total": 0, "swap_percent": 0.0,
                "swap_used": 0, "swap_total": 0, "psi_some": 0.0, "psi_full": 0.0}
    try:
        from backend.services.pressure_guard import read_psi
        psi_some, psi_full = read_psi()
    except Exception:  # noqa: BLE001 — PSI is optional (not every kernel/container)
        psi_some = psi_full = 0.0
    return {
        "percent": round(mem.percent, 1),
        "used": mem.used,
        "total": mem.total,
        "swap_percent": round(float(swap.percent), 1),
        "swap_used": int(swap.used),
        "swap_total": int(swap.total),
        "psi_some": psi_some,
        "psi_full": psi_full,
    }


def collect_snapshot() -> dict:
    """Current disks + failed units + memory + temps. Pure read — emits nothing."""
    return {
        "disks": _collect_disks(),
        "failed_units": _collect_failed_units(),
        "memory": _collect_memory(),
        "temps": _collect_temps(),
    }


# ── Alert state (persisted, survives restarts) ───────────────────────────────

def _default_state() -> dict:
    return {"version": 1, "alerts": {}}


def _load_state() -> dict:
    state_file = _state_file()
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("alerts"), dict):
            return _default_state()
        data["alerts"] = {k: v for k, v in data["alerts"].items() if isinstance(v, dict)}
        return data
    except FileNotFoundError:
        return _default_state()
    except Exception as exc:  # noqa: BLE001 — corrupt state must not crash checks
        logger.warning("health state unreadable (%s) — starting fresh", exc)
        return _default_state()


def _save_state(state: dict) -> None:
    """Atomic write: tmp file in same dir + os.replace."""
    state_file = _state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(state_file.parent), prefix=".health-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, state_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best-effort cleanup — nothing to report
        raise


# ── Runtime-tunable configuration (overrides env defaults) ───────────────────
# Ranges per key: (min, max). Values outside the range are rejected on write and
# ignored defensively when reading a hand-edited file.
_CONFIG_SPEC: dict[str, tuple[float, float]] = {
    "disk_warn_percent": (1, 100),
    "disk_recover_percent": (0, 100),
    "memory_warn_percent": (1, 100),
    "memory_recover_percent": (0, 100),
    "swap_warn_percent": (1, 100),
    "swap_recover_percent": (0, 100),
    "psi_full_warn": (1, 100),
    "temp_warn_celsius": (40, 125),           # below/above real silicon ranges
    "alert_cooldown_seconds": (60, 604800),   # up to 7 days
    "check_interval_seconds": (30, 86400),    # up to 24 h
}

# Fixed recovery margin below the temperature warning threshold — prevents
# flapping without a second user-facing knob.
TEMP_HYSTERESIS_C = 5.0

# SMART probing: per-device timeout + attributes surfaced alongside pass/fail.
SMART_TIMEOUT_S = 10
_KEY_SMART_ATTRS = (
    "Reallocated_Sector_Ct",
    "Current_Pending_Sector",
    "Offline_Uncorrectable",
    "Wear_Leveling_Count",
    "Percentage_Used",
)

# Threshold pairs that must satisfy recover < warn.
_WARN_RECOVER_PAIRS = (
    ("disk_warn_percent", "disk_recover_percent"),
    ("memory_warn_percent", "memory_recover_percent"),
    ("swap_warn_percent", "swap_recover_percent"),
)


def _config_file() -> Path:
    return Path(settings.DATA_DIR) / "health_config.json"


def _load_config() -> dict:
    """User overrides; missing/corrupt file yields {} — never raises."""
    try:
        with open(_config_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        ov = data.get("overrides") if isinstance(data, dict) else None
        return ov if isinstance(ov, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 — corrupt config must not break checks
        logger.warning("health config unreadable (%s) — using defaults", exc)
        return {}


def _save_config(overrides: dict) -> None:
    """Atomic write of the overrides map."""
    cfg_file = _config_file()
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(cfg_file.parent), prefix=".health-cfg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "overrides": overrides}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, cfg_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best-effort cleanup — nothing to report
        raise


def _defaults() -> dict:
    """Env-driven defaults (backend/config.py)."""
    return {
        "disk_warn_percent": settings.DISK_WARN_PERCENT,
        "disk_recover_percent": settings.DISK_RECOVER_PERCENT,
        "memory_warn_percent": settings.MEMORY_WARN_PERCENT,
        "memory_recover_percent": settings.MEMORY_RECOVER_PERCENT,
        "swap_warn_percent": settings.SWAP_WARN_PERCENT,
        "swap_recover_percent": settings.SWAP_RECOVER_PERCENT,
        "psi_full_warn": settings.PRESSURE_HARD_PSI_FULL,
        "temp_warn_celsius": settings.TEMP_WARN_CELSIUS,
        "alert_cooldown_seconds": settings.HEALTH_ALERT_COOLDOWN,
        "check_interval_seconds": settings.HEALTH_CHECK_INTERVAL,
    }


def _effective_config() -> dict:
    """Validated user overrides on top of env defaults.

    Invalid entries (wrong type / out of range) are silently dropped so a
    hand-edited file can never break the checker; the recover < warn invariant
    is enforced by clamping.
    """
    cfg = _defaults()
    for key, value in _load_config().items():
        spec = _CONFIG_SPEC.get(key)
        if spec is None:
            continue  # unknown key — ignore
        lo, hi = spec
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if lo <= value <= hi:
            cfg[key] = value
    for warn_key, recover_key in _WARN_RECOVER_PAIRS:
        if cfg[recover_key] >= cfg[warn_key]:
            cfg[recover_key] = max(0.0, cfg[warn_key] - 1)
    return cfg


def update_config(patch: dict) -> dict:
    """Validate + persist a partial override map; returns the new effective config.

    Raises ValueError with a human-readable message on any invalid entry — the
    whole patch is rejected atomically (nothing persisted).
    """
    merged = {**_load_config(), **patch}
    for key, value in patch.items():
        spec = _CONFIG_SPEC.get(key)
        if spec is None:
            raise ValueError(f"unknown setting: {key!r}")
        lo, hi = spec
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        if not (lo <= value <= hi):
            raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    # Cross-field invariant (recover < warn) per pair, against the fully
    # merged view. Explicitly setting an invalid recover → rejected with a
    # clear error; lowering warn below the existing recover → repaired
    # transparently by clamping recover to warn-1 (response shows it).
    defaults = _defaults()
    for warn_key, recover_key in _WARN_RECOVER_PAIRS:
        warn = merged.get(warn_key, defaults[warn_key])
        recover = merged.get(recover_key, defaults[recover_key])
        if not (isinstance(warn, (int, float)) and isinstance(recover, (int, float))):
            raise ValueError(f"{warn_key}/{recover_key} must be numbers")
        if recover >= warn:
            if recover_key in patch:
                raise ValueError(
                    f"{recover_key} ({recover:g}) must be below {warn_key} ({warn:g})")
            merged[recover_key] = max(0, warn - 1)
    _save_config(merged)
    return _effective_config()


def reset_config() -> dict:
    """Clear all overrides (back to env defaults); returns the effective config."""
    try:
        _config_file().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not remove health config: %s", exc)
    return _effective_config()


def _cooldown_elapsed(entry: dict, now: datetime, cooldown_seconds: float) -> bool:
    """True if the alert recorded in `entry` is old enough to re-alert."""
    raw = entry.get("last_alert_at") or ""
    try:
        last = datetime.fromisoformat(raw)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except ValueError:
        return True  # unparseable timestamp → allow re-alert rather than silence
    return (now - last) >= timedelta(seconds=cooldown_seconds)


# ── The check itself ─────────────────────────────────────────────────────────

def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b //= 1024
    return f"{b} PB"


def run_check() -> dict:
    """Run one full health check; emit Notification Center events as needed.

    Returns the snapshot plus ``emitted`` — titles of events created by this
    call (empty when everything is quiet or still within cooldown).
    """
    now = _now()
    cfg = _effective_config()  # runtime-tunable; env defaults as fallback
    warn, recover = cfg["disk_warn_percent"], cfg["disk_recover_percent"]
    cooldown = cfg["alert_cooldown_seconds"]
    snap = collect_snapshot()
    disks, failed_units = snap["disks"], snap["failed_units"]
    emitted: list[str] = []

    with _LOCK:
        state = _load_state()
        alerts = state["alerts"]

        # ── Disks ────────────────────────────────────────────────────────────
        current_disk_keys = set()
        for d in disks:
            key = f"disk:{d['mount']}"
            current_disk_keys.add(key)
            entry = alerts.get(key)
            if d["percent"] >= warn:
                if entry is None or _cooldown_elapsed(entry, now, cooldown):
                    title = f"Disk {d['mount']} at {int(d['percent'])}%"
                    msg = (f"{_fmt_bytes(d['free'])} free of {_fmt_bytes(d['total'])} "
                           f"({d['fstype']}, {d['device']}) — threshold {warn:g}%.")
                    if emit_event("warning", title, msg, source="health"):
                        emitted.append(title)
                    alerts[key] = {"state": "alerting",
                                   "last_alert_at": now.isoformat(timespec="seconds")}
            elif entry is not None and d["percent"] < recover:
                title = f"Disk {d['mount']} back to normal"
                msg = (f"Usage dropped to {int(d['percent'])}% "
                       f"(was ≥{warn:g}%).")
                if emit_event("info", title, msg, source="health"):
                    emitted.append(title)
                del alerts[key]
            # recover ≤ percent < warn: hysteresis band — keep state, stay quiet.

        # Drop stale keys for mounts that no longer exist (unmounted/removed).
        for key in [k for k in list(alerts)
                    if k.startswith("disk:") and k not in current_disk_keys]:
            del alerts[key]

        # ── Memory pressure ────────────────────────────────────────────────
        mem = snap["memory"]
        mkey = "memory"
        entry = alerts.get(mkey)
        if mem["percent"] >= cfg["memory_warn_percent"]:
            if entry is None or _cooldown_elapsed(entry, now, cooldown):
                title = f"Memory at {int(mem['percent'])}%"
                msg = (f"{_fmt_bytes(mem['used'])} used of {_fmt_bytes(mem['total'])} "
                       f"— threshold {cfg['memory_warn_percent']:g}%.")
                if emit_event("warning", title, msg, source="health"):
                    emitted.append(title)
                alerts[mkey] = {"state": "alerting",
                                "last_alert_at": now.isoformat(timespec="seconds")}
        elif entry is not None and mem["percent"] < cfg["memory_recover_percent"]:
            title = "Memory back to normal"
            msg = (f"Usage dropped to {int(mem['percent'])}% "
                   f"(was ≥{cfg['memory_warn_percent']:g}%).")
            if emit_event("info", title, msg, source="health"):
                emitted.append(title)
            del alerts[mkey]

        # ── Swap exhaustion ────────────────────────────────────────────────
        # Swap ~100% means the kernel has nowhere to page out: the next
        # allocation spike becomes an OOM kill (systemd-oomd or the kernel).
        swap_pct = float(mem.get("swap_percent") or 0.0)
        skey = "swap"
        entry = alerts.get(skey)
        if swap_pct >= cfg["swap_warn_percent"]:
            if entry is None or _cooldown_elapsed(entry, now, cooldown):
                title = f"Swap at {int(swap_pct)}%"
                msg = (f"{_fmt_bytes(mem.get('swap_used', 0))} of "
                       f"{_fmt_bytes(mem.get('swap_total', 0))} swap in use — the box "
                       "has no free RAM left to page out.")
                if emit_event("warning", title, msg, source="health"):
                    emitted.append(title)
                alerts[skey] = {"state": "alerting",
                                "last_alert_at": now.isoformat(timespec="seconds")}
        elif entry is not None and swap_pct < cfg["swap_recover_percent"]:
            title = "Swap back to normal"
            msg = (f"Swap usage dropped to {int(swap_pct)}% "
                   f"(was ≥{cfg['swap_warn_percent']:g}%).")
            if emit_event("info", title, msg, source="health"):
                emitted.append(title)
            del alerts[skey]

        # ── Memory stalls (PSI) ─────────────────────────────────────────────
        # systemd-oomd acts on sustained pressure, not on a single spike.
        psi_full = float(mem.get("psi_full") or 0.0)
        pkey = "psi:memory"
        entry = alerts.get(pkey)
        if psi_full >= cfg["psi_full_warn"]:
            if entry is None or _cooldown_elapsed(entry, now, cooldown):
                title = f"Memory stalls (PSI {psi_full:g}%)"
                msg = ("Processes spend that share of their time waiting on memory "
                       "reclaim — systemd-oomd starts killing services at this point.")
                if emit_event("error", title, msg, source="health"):
                    emitted.append(title)
                alerts[pkey] = {"state": "alerting",
                                "last_alert_at": now.isoformat(timespec="seconds")}
        elif entry is not None and psi_full < cfg["psi_full_warn"] / 2:
            title = "Memory stalls cleared"
            msg = f"PSI full dropped to {psi_full:g}%."
            if emit_event("info", title, msg, source="health"):
                emitted.append(title)
            del alerts[pkey]

        # ── Temperature sensors ────────────────────────────────────────
        temp_warn = cfg["temp_warn_celsius"]
        temp_recover = temp_warn - TEMP_HYSTERESIS_C
        current_temp_keys: set[str] = set()
        for t in snap["temps"]:
            key = f"temp:{t['label']}"
            current_temp_keys.add(key)
            entry = alerts.get(key)
            if t["current"] >= temp_warn:
                if entry is None or _cooldown_elapsed(entry, now, cooldown):
                    title = f"Sensor {t['label']} at {int(t['current'])}°C"
                    msg = f"Reading crossed the {temp_warn:g}°C threshold."
                    if emit_event("warning", title, msg, source="health"):
                        emitted.append(title)
                    alerts[key] = {"state": "alerting",
                                   "last_alert_at": now.isoformat(timespec="seconds")}
            elif entry is not None and t["current"] < temp_recover:
                title = f"Sensor {t['label']} back to normal"
                msg = (f"Reading dropped to {int(t['current'])}°C "
                       f"(was ≥{temp_warn:g}°C).")
                if emit_event("info", title, msg, source="health"):
                    emitted.append(title)
                del alerts[key]
            # temp_recover ≤ current < temp_warn: hysteresis band — stay quiet.

        for key in [k for k in list(alerts)
                    if k.startswith("temp:") and k not in current_temp_keys]:
            del alerts[key]


        # ── Failed units ─────────────────────────────────────────────────────
        failed_names = {u["name"] for u in failed_units}
        for u in failed_units:
            key = f"unit:{u['name']}"
            entry = alerts.get(key)
            if entry is None or _cooldown_elapsed(entry, now, cooldown):
                title = f"Service {u['name']} failed"
                msg = (f"{u['description']} entered the failed state."
                       if u["description"] else "Entered the failed state.")
                if emit_event("error", title, msg, source="health"):
                    emitted.append(title)
                # Only refresh the timestamp when we actually alert — otherwise
                # a persisting failure would reset its own cooldown every check.
                alerts[key] = {"state": "alerting",
                               "last_alert_at": now.isoformat(timespec="seconds")}

        # Units that were alerting but are no longer failing → recovery.
        for key in [k for k in list(alerts)
                    if k.startswith("unit:") and k[len("unit:"):] not in failed_names]:
            name = key[len("unit:"):]
            title = f"Service {name} recovered"
            if emit_event("info", title, "No longer in the failed state.", source="health"):
                emitted.append(title)
            del alerts[key]

        # ── SMART health (whole disks) ───────────────────────────────
        smart = _collect_smart()
        current_smart_keys: set[str] = set()
        for s in smart:
            key = f"smart:{s['device']}"
            current_smart_keys.add(key)
            if not s.get("ok"):
                # Unreadable (e.g. non-root test instance) — stay silent.
                continue
            entry = alerts.get(key)
            if not s["passed"]:
                if entry is None or _cooldown_elapsed(entry, now, cooldown):
                    attr_bits = ", ".join(f"{k}={v}" for k, v in s["attributes"].items())
                    msg = (f"smartctl reports the device as failed/failing."
                           + (f" Key attributes: {attr_bits}." if attr_bits else ""))
                    title = f"Disk {s['device']} failing (SMART)"
                    if emit_event("error", title, msg, source="health"):
                        emitted.append(title)
                    alerts[key] = {"state": "alerting",
                                   "last_alert_at": now.isoformat(timespec="seconds")}
            elif entry is not None:
                # Rare: device recovered (transient read failure cleared).
                title = f"Disk {s['device']} SMART back to normal"
                if emit_event("info", title, "smartctl reports the device as passed.",
                              source="health"):
                    emitted.append(title)
                del alerts[key]

        for key in [k for k in list(alerts)
                    if k.startswith("smart:") and k not in current_smart_keys]:
            del alerts[key]

        # ── SMART attribute trends (early degradation) ───────────────────────
        # Change-based: an alert fires exactly when an error-type attribute's
        # value differs from the last recorded sample — no cooldown state.
        history = _load_smart_history()
        hist_devs = history["devices"]
        seen_smart: set[str] = set()
        for s in smart:
            if not s.get("ok") or "attributes" not in s:
                continue  # unreadable — keep the last known sample untouched
            dev = s["device"]
            seen_smart.add(dev)
            attrs = {k: v for k, v in (s.get("attributes") or {}).items()
                     if isinstance(v, (int, float))}
            rec = hist_devs.get(dev)
            prev_attrs = rec.get("attrs") if isinstance(rec, dict) else None
            if isinstance(prev_attrs, dict):
                for attr in _TREND_ALERT_ATTRS:
                    old_v, new_v = prev_attrs.get(attr), attrs.get(attr)
                    if (isinstance(old_v, (int, float))
                            and isinstance(new_v, (int, float))
                            and new_v > old_v):
                        title = f"Disk {dev} SMART degradation"
                        msg = (f"{attr} increased {old_v:g} → {new_v:g} since the "
                               f"last check ({rec.get('last_seen', 'unknown')}).")
                        if emit_event("error", title, msg, source="health"):
                            emitted.append(title)
            hist_devs[dev] = {"last_seen": now.isoformat(timespec="seconds"),
                              "attrs": attrs,
                              "prev_attrs": prev_attrs,
                              "samples": _next_samples(rec, now, attrs,
                                                       s.get("temperature"))}
        # Prune devices unseen for a long time (unplugged / replaced disk).
        cutoff = now - timedelta(days=_SMART_HISTORY_STALE_DAYS)
        for dev in list(hist_devs):
            if dev in seen_smart:
                continue
            last_seen = _parse_iso(hist_devs[dev].get("last_seen"))
            if last_seen is None or last_seen < cutoff:
                del hist_devs[dev]
        _save_smart_history(history)

        _save_state(state)

    with _LOCK:
        _LAST_SMART["checked_at"] = now.isoformat(timespec="seconds")
        _LAST_SMART["results"] = list(smart)

    _status["last_check_at"] = now.isoformat(timespec="seconds")
    _status["checks_run"] += 1
    return {**snap, "smart": smart, "emitted": emitted}


# ── Background loop (lifespan-managed) ───────────────────────────────────────

_task: asyncio.Task | None = None


async def _loop() -> None:
    """Check immediately on start, then every <interval> seconds.

    The interval is re-read each cycle so a runtime config change takes effect
    from the next sleep without a restart.
    """
    while True:
        try:
            await asyncio.to_thread(run_check)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad check must not kill the loop
            logger.error("Health monitor check failed: %s", exc)
        await asyncio.sleep(_effective_config()["check_interval_seconds"])


def start_health_monitor() -> None:
    """Start the checker (idempotent). Called from app lifespan."""
    global _task
    if _task is not None and not _task.done():
        return
    logger.info("Health monitor starting (interval=%ss, disk warn=%.0f%%)",
                settings.HEALTH_CHECK_INTERVAL, settings.DISK_WARN_PERCENT)
    _task = asyncio.create_task(_loop())


async def stop_health_monitor() -> None:
    """Stop the checker. Called from app lifespan shutdown."""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None


def monitor_status() -> dict:
    """Effective config + env defaults + runtime state for GET /api/health/status."""
    eff = _effective_config()
    return {
        "interval_seconds": eff["check_interval_seconds"],
        "disk_warn_percent": eff["disk_warn_percent"],
        "disk_recover_percent": eff["disk_recover_percent"],
        "memory_warn_percent": eff["memory_warn_percent"],
        "memory_recover_percent": eff["memory_recover_percent"],
        "temp_warn_celsius": eff["temp_warn_celsius"],
        "alert_cooldown_seconds": eff["alert_cooldown_seconds"],
        "defaults": _defaults(),
        "last_check_at": _status["last_check_at"],
        "checks_run": _status["checks_run"],
    }
