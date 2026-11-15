"""EthOS NAS — Network Throughput History Service

Background sampler that records total RX/TX byte counters (psutil) every
NET_STATS_INTERVAL seconds into a capped ring buffer, so the Network app can
chart throughput over time. Mirrors two established patterns:

- lifespan-managed loop with catch-all error handling (health monitor),
- atomic JSON store under DATA_DIR (notifications / health state).

Store file: <DATA_DIR>/net_stats.json  {"version":1, "samples":[{ts, rx, tx}]}
Samples are cumulative counters; throughput is derived as deltas between
consecutive samples in get_history(). Counter resets (reboot, interface flap)
produce a zero interval rather than negative garbage.
"""

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import psutil

from backend.config import settings

logger = logging.getLogger("ethos-nas.services.net_stats_service")

MAX_SAMPLES = 720  # 12 h at the default 60 s interval


def _now_ts() -> int:
    """Epoch seconds — module-level so tests can monkeypatch it."""
    return int(time.time())


_LOCK = threading.Lock()


def _store_file() -> Path:
    return Path(settings.DATA_DIR) / "net_stats.json"


def _load_samples() -> list[dict]:
    try:
        with open(_store_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        samples = data.get("samples") if isinstance(data, dict) else None
        if not isinstance(samples, list):
            return []
        # Defensive: keep only well-formed entries.
        return [s for s in samples
                if isinstance(s, dict) and isinstance(s.get("ts"), (int, float))
                and isinstance(s.get("rx"), (int, float))
                and isinstance(s.get("tx"), (int, float))]
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 — corrupt store must not crash callers
        logger.warning("net stats store unreadable (%s) — starting fresh", exc)
        return []


def _save_samples(samples: list[dict]) -> None:
    """Atomic write: tmp file in same dir + os.replace."""
    path = _store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".netstats-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "samples": samples}, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best-effort cleanup — nothing to report
        raise


def record_sample() -> dict | None:
    """Append one counter sample (trimmed to MAX_SAMPLES). Returns the sample."""
    try:
        io = psutil.net_io_counters()
    except Exception as exc:  # noqa: BLE001 — sampling must never crash the loop
        logger.debug("net_io_counters failed: %s", exc)
        return None
    sample = {"ts": _now_ts(), "rx": io.bytes_recv, "tx": io.bytes_sent}
    with _LOCK:
        samples = _load_samples()
        if samples and samples[-1]["ts"] == sample["ts"]:
            # Same-second duplicate (e.g. manual + scheduled) — replace, don't double.
            samples[-1] = sample
        else:
            samples.append(sample)
            del samples[:-MAX_SAMPLES]
        _save_samples(samples)
    return sample


def get_history(minutes: int = 60) -> list[dict]:
    """Throughput points {ts, rx_bps, tx_bps} for the last `minutes` minutes.

    Deltas between consecutive stored samples; a negative delta (counter reset)
    yields zero for that interval.
    """
    with _LOCK:
        samples = _load_samples()
    if len(samples) < 2:
        return []
    cutoff = _now_ts() - minutes * 60
    points: list[dict] = []
    prev = None
    for s in samples:
        # The first sample at/after the cutoff uses the one just before it as
        # its delta baseline, so no interval is lost at the window edge.
        if prev is not None and s["ts"] >= cutoff:
            dt = s["ts"] - prev["ts"]
            if dt > 0:
                drx = max(0, int(s["rx"]) - int(prev["rx"]))
                dtx = max(0, int(s["tx"]) - int(prev["tx"]))
                points.append({
                    "ts": s["ts"],
                    "rx_bps": round(drx / dt, 1),
                    "tx_bps": round(dtx / dt, 1),
                })
        prev = s
    return points


# ── Background sampler (lifespan-managed) ────────────────────────────────────

_task: asyncio.Task | None = None


async def _loop() -> None:
    """Sample immediately on start, then every NET_STATS_INTERVAL seconds."""
    while True:
        try:
            await asyncio.to_thread(record_sample)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad sample must not kill the loop
            logger.error("Net stats sampling failed: %s", exc)
        await asyncio.sleep(settings.NET_STATS_INTERVAL)


def start_net_stats_sampler() -> None:
    """Start the sampler (idempotent). Called from app lifespan."""
    global _task
    if _task is not None and not _task.done():
        return
    logger.info("Net stats sampler starting (interval=%ss, cap=%d samples)",
                settings.NET_STATS_INTERVAL, MAX_SAMPLES)
    _task = asyncio.create_task(_loop())


async def stop_net_stats_sampler() -> None:
    """Stop the sampler. Called from app lifespan shutdown."""
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _task = None
