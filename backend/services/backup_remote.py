"""Offsite replication through rclone.

The primary backup is always a local hardlink snapshot. When a schedule enables
`remote`, the newest generation is mirrored to `<remote>:<path>/latest/` with
`rclone sync` (only the delta is transferred) and overwritten/deleted files are
moved into `<remote>:<path>/history/<generation>/`, pruned after `keep_days`.

Credentials live in rclone's own config, never in EthOS state or API payloads.
The API only lists remote names and tests connectivity.
"""

import asyncio
import logging
import os
import shutil
from typing import Optional

from backend.config import settings
from backend.services.backup_proc import OperationHandle, run
from backend.services.backup_store import REMOTE_RE

logger = logging.getLogger("ethos-nas.services.backup_remote")


class RemoteUnavailable(RuntimeError):
    """rclone is not installed or not configured for the service user."""


def _config_args() -> list[str]:
    path = getattr(settings, "RCLONE_CONFIG", "") or ""
    return ["--config", path] if path else []


def rclone_path() -> Optional[str]:
    return shutil.which("rclone")


def list_remotes_sync() -> list[dict]:
    binary = rclone_path()
    if not binary:
        raise RemoteUnavailable("rclone is not installed (run setup_scripts/install.sh)")
    import subprocess

    try:
        result = subprocess.run(
            [binary, "listremotes", "--long", *_config_args()],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RemoteUnavailable(str(exc)) from exc
    if result.returncode != 0:
        raise RemoteUnavailable((result.stderr or result.stdout or "rclone failed").strip()[-300:])
    remotes = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line or not line.split()[0].endswith(":"):
            continue
        name = line.split()[0][:-1]
        if not REMOTE_RE.match(name):
            continue
        parts = line.split()
        remotes.append({"name": name, "type": parts[1] if len(parts) > 1 else ""})
    return remotes


async def list_remotes() -> list[dict]:
    return await asyncio.to_thread(list_remotes_sync)


async def test_remote(name: str) -> dict:
    if not REMOTE_RE.match(name or ""):
        return {"ok": False, "detail": "Invalid remote name"}
    try:
        remotes = await asyncio.to_thread(list_remotes_sync)
    except RemoteUnavailable as exc:
        return {"ok": False, "detail": str(exc)}
    if name not in {remote["name"] for remote in remotes}:
        return {"ok": False, "detail": f"Unknown rclone remote: {name}"}
    binary = rclone_path()
    rc, _out, err = await run([binary, "lsd", f"{name}:", "--max-depth", "1", *_config_args()])
    detail = err.decode(errors="replace").strip()[-300:]
    return {"ok": rc == 0, "detail": detail or ("Remote is reachable" if rc == 0 else "rclone failed")}


async def replicate(local_dir: str, remote: dict, generation: str,
                    op: OperationHandle) -> dict:
    """Mirror `local_dir` to `<remote>:<path>/latest/` with versioned history."""
    if not remote.get("enabled"):
        return {"status": "disabled", "detail": ""}
    name = str(remote.get("remote") or "")
    path = str(remote.get("path") or "").strip("/")
    if not REMOTE_RE.match(name) or not path:
        return {"status": "failed", "detail": "Remote configuration is incomplete"}
    binary = rclone_path()
    if not binary:
        return {"status": "failed", "detail": "rclone is not installed"}

    try:
        remotes = await asyncio.to_thread(list_remotes_sync)
    except RemoteUnavailable as exc:
        return {"status": "failed", "detail": str(exc)}
    if name not in {entry["name"] for entry in remotes}:
        return {"status": "failed", "detail": f"Unknown rclone remote: {name}"}

    base = f"{name}:{path}"
    latest = f"{base}/latest"
    history = f"{base}/history/{generation}"
    args = [
        binary, "sync", local_dir, latest,
        "--checksum", "--create-empty-src-dirs",
        "--backup-dir", history,
        "--transfers", "4", "--checkers", "8",
        "--stats-one-line",
        *_config_args(),
    ]
    limit = str(remote.get("bwlimit") or "").strip()
    if limit:
        args += ["--bwlimit", limit]

    op.check()
    rc, out, err = await run(args, op=op, capture=True)
    if rc != 0:
        detail = (err or out).decode(errors="replace").strip()[-300:]
        logger.warning("Backup %s: remote replication failed: %s", op.schedule_id, detail)
        return {"status": "failed", "detail": detail}

    keep_days = int(remote.get("keep_days") or 30)
    prune = [binary, "delete", history, "--min-age", f"{keep_days}d", *_config_args()]
    rc_prune, _o, err_prune = await run(prune, op=op, capture=True)
    if rc_prune != 0:
        logger.warning("Backup %s: remote history prune failed: %s",
                       op.schedule_id, err_prune.decode(errors="replace")[-200:])
    await run([binary, "rmdirs", history, "--leave-root", *_config_args()], capture=True)

    spec = {"status": "success", "detail": f"{latest} · history kept {keep_days} days"}
    logger.info("Backup %s: replicated %s to %s", op.schedule_id,
                os.path.basename(local_dir), latest)
    return spec
