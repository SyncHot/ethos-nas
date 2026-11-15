"""Copy the generated catalogs into the ethos-music Android app bundle.

The app ships the catalogs as assets so search, the Radio tab and podcast
discovery work on first launch (and offline); it refreshes them from the NAS API
once a day. Run this after regenerating the backend catalogs:

    .venv/bin/python scripts/export_android_catalogs.py
    .venv/bin/python scripts/export_android_catalogs.py --target ~/apps/compose/ethos-music
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RADIO_FILE = ROOT / "backend" / "radio_stations.json"
PODCAST_FILE = ROOT / "backend" / "podcast_catalog.json"
DEFAULT_TARGET = ROOT.parent / "ethos-music"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET,
                        help="ethos-music repository root (default: sibling directory)")
    args = parser.parse_args()

    assets = Path(args.target).expanduser().resolve() / "app" / "src" / "main" / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    for source, name in ((RADIO_FILE, "radio_catalog.json"), (PODCAST_FILE, "podcast_catalog.json")):
        if not source.is_file():
            print(f"[FAIL] missing {source} — run the catalog builders first")
            return 1
        shutil.copyfile(source, assets / name)
        print(f"[OK] {source.name} -> {assets / name} ({source.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
