"""Cameras — global settings (storage, retention, defaults).

Stored under `<DATA_DIR>/surveillance_settings.json` through `json_store`
(atomic write, 0600, corruption quarantine). One record for the whole
appliance: where recordings live, the disk quota the janitor enforces and the
defaults a new camera inherits.
"""

import logging
from pathlib import Path

from backend.config import settings
from backend.services import json_store

logger = logging.getLogger("ethos-nas.services.camera_settings")

STORE_PATH = settings.DATA_DIR / "surveillance_settings.json"

DEFAULTS: dict = {
    # None → `<DATA_DIR>/surveillance` (see camera_storage.storage_root).
    "storage_dir": None,
    "quota_pct": 80,            # max % of the recordings volume the janitor keeps
    "default_retention_days": 7,
    "segment_s": 60,            # archive segment length
    "pre_s": 10,                # motion event pre-roll (playback only)
    "post_s": 30,               # motion event post-roll
    "notify_motion": True,
    "notify_offline": True,
}

_STORE = json_store.get(STORE_PATH, dict, version=1)


def _clamp(value, low, high, fallback):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def load() -> dict:
    data = _STORE.load()
    if not isinstance(data, dict):
        data = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    return merged


def save(patch: dict) -> dict:
    """Validate and persist a partial settings update. Raises ValueError on bad input."""
    current = load()
    allowed = set(DEFAULTS)
    unknown = set(patch or {}) - allowed
    if unknown:
        raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
    if "quota_pct" in patch:
        current["quota_pct"] = int(_clamp(patch["quota_pct"], 50, 95, current["quota_pct"]))
    if "default_retention_days" in patch:
        current["default_retention_days"] = int(
            _clamp(patch["default_retention_days"], 0, 365, current["default_retention_days"]))
    if "segment_s" in patch:
        current["segment_s"] = int(_clamp(patch["segment_s"], 10, 600, current["segment_s"]))
    if "pre_s" in patch:
        current["pre_s"] = int(_clamp(patch["pre_s"], 0, 120, current["pre_s"]))
    if "post_s" in patch:
        current["post_s"] = int(_clamp(patch["post_s"], 0, 300, current["post_s"]))
    for flag in ("notify_motion", "notify_offline"):
        if flag in patch:
            current[flag] = bool(patch[flag])
    if "storage_dir" in patch:
        from backend.services import camera_storage

        raw = patch["storage_dir"]
        current["storage_dir"] = camera_storage.validate_storage_dir(raw) if raw else None
    def _mutate(data) -> None:
        data.clear()
        data.update({**current, "version": 1})

    _STORE.update(_mutate)
    logger.info("Camera settings updated (storage=%s quota=%s%% retention=%sd)",
                current["storage_dir"] or "default", current["quota_pct"],
                current["default_retention_days"])
    return current


def storage_dir() -> Path | None:
    value = load().get("storage_dir")
    return Path(value) if value else None
