#!/usr/bin/env python3
"""Downloads ownership + per-user config split (unit, no server).

Run:
    .venv/bin/python tests/test_downloads_owner.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="ethos-dl-owner-")
os.environ["ETHOS_DATA_DIR"] = _TMP

from backend.routers import downloads as dl  # noqa: E402
from backend.services import download_config as dc  # noqa: E402
from backend.services import path_scope  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    try:
        dl._tasks[:] = [
            {"id": "alice-task", "url": "https://x/a", "name": "a", "path": "/home/alice/a",
             "status": "waiting", "created_at": 1, "owner": "alice"},
            {"id": "bob-task", "url": "https://x/b", "name": "b", "path": "/home/bob/b",
             "status": "waiting", "created_at": 2, "owner": "bob"},
            {"id": "legacy-task", "url": "https://x/c", "name": "c", "path": "/srv/c",
             "status": "waiting", "created_at": 3},
        ]

        print("\n[task ownership]")
        with path_scope.identity_scope("alice", "user"):
            check("alice finds her task", dl._find("alice-task") is not None)
            check("alice cannot find bob's task", dl._find("bob-task") is None)
            check("alice cannot find an ownerless task", dl._find("legacy-task") is None)
        with path_scope.identity_scope("bob", "user"):
            check("bob finds his task", dl._find("bob-task") is not None)
            check("bob cannot find alice's task", dl._find("alice-task") is None)
        with path_scope.system_scope():
            check("system sees every task",
                  all(dl._find(i) is not None
                      for i in ("alice-task", "bob-task", "legacy-task")))

        check("_public reports the owner", dl._tasks[0]["owner"] == "alice"
              and dl._public(dl._tasks[0]).get("owner") == "alice")

        print("\n[config split]")
        dc._CONFIG_STORE.save({"max_concurrent": 7, "download_dir": "/srv/downloads"})
        with path_scope.identity_scope("alice", "user"):
            cfg = dc.load_config()
            check("engine settings come from the global store", cfg["max_concurrent"] == 7)
            check("the user gets their own default download dir",
                  cfg["download_dir"] == str(Path(path_scope.home_dir("alice")) / "Downloads"),
                  cfg["download_dir"])
            cfg["download_dir"] = str(Path(path_scope.home_dir("alice")) / "Downloads")
            cfg["duplicate_policy"] = "skip"
            cfg["categories"] = [{"name": "tv", "save_path": ""}]
            dc.save_config(cfg)
        with path_scope.identity_scope("bob", "user"):
            bob_cfg = dc.load_config()
            check("bob does not inherit alice's policy",
                  bob_cfg["duplicate_policy"] == "rename", str(bob_cfg)[:120])
            check("bob does not inherit alice's categories",
                  not bob_cfg.get("categories"), str(bob_cfg)[:120])
        with path_scope.identity_scope("alice", "user"):
            alice_cfg = dc.load_config()
            check("alice keeps her saved policy",
                  alice_cfg["duplicate_policy"] == "skip", str(alice_cfg)[:120])
            check("alice keeps her saved categories",
                  any(c.get("name") == "tv" for c in alice_cfg.get("categories") or []),
                  str(alice_cfg)[:120])
        check("user slice lives under DATA_DIR/users/<user>",
              (Path(_TMP) / "users" / "alice" / "downloads.json").is_file())
        check("engine settings stayed global",
              dc._CONFIG_STORE.load().get("max_concurrent") == 7)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)

    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
