"""Backup Manager v2 — rsync hardlink snapshots, Docker projects, verify, restore.

Design rules:
- Each run writes into `.tmp-<op_id>/` and is renamed to `snapshots/<timestamp>/`
  only after `manifest.json` is written. A killed run can never look complete.
- The previous complete generation is passed to `rsync --link-dest`, so
  unchanged files are hardlinks (cheap incrementals, several restore points).
- `--delete` is used inside a new snapshot only; older generations keep their
  hardlinks and remain intact.
- Docker volumes are tarred through throwaway alpine containers, images are
  re-hardlinked when their id did not change.
"""

import asyncio
import logging
import os
import re
import shutil
import time
from datetime import datetime
from typing import Optional

from backend.services import backup_docker, backup_layout, backup_remote
from backend.services.backup_docker import DockerUnavailable
from backend.services.backup_proc import OperationHandle, OpCancelled, run, run_streaming
from backend.services.backup_store import backup_target, now_iso, safe_name

logger = logging.getLogger("ethos-nas.services.backup_engine")

DEFAULT_EXCLUDES = [
    ".ethos-partial", "lost+found", "#recycle", "@eaDir", ".Trash-*",
    ".snapshot", ".snapshots", ".gvfs", ".@Recycle",
]

_PERCENT_RE = re.compile(r"(\d{1,3})%")
_PROGRESS_BYTES_RE = re.compile(r"([\d,]+)\s+\d{1,3}%")
_TOTAL_SIZE_RE = re.compile(r"(?:total size|Total file size) is?\s*:?[= ]+([\d,]+)")
_TRANSFERRED_RE = re.compile(r"Total transferred file size:\s*([\d,]+)")
_FILES_RE = re.compile(r"Number of (?:regular )?files transferred:\s*([\d,]+)")
_ITEMIZE_RE = re.compile(r"^(?:\*deleting|[<>ch][fdLDS])")


class BackupError(RuntimeError):
    """A backup/restore step failed with an operator-readable message."""


# ── rsync helpers ────────────────────────────────────────────────────────────

def _rsync_excludes(schedule: dict) -> list[str]:
    options = schedule.get("options") or {}
    patterns: list[str] = []
    if options.get("exclude_defaults", True):
        patterns.extend(DEFAULT_EXCLUDES)
    patterns.extend(str(p) for p in (options.get("exclude") or []) if str(p).strip())
    return patterns


def _parse_stats(text: str) -> dict:
    """Extract sizes/counts from `rsync --stats` output."""
    total = transferred = files = 0
    for line in text.splitlines():
        match = _TOTAL_SIZE_RE.search(line)
        if match:
            total = int(match.group(1).replace(",", ""))
        match = _TRANSFERRED_RE.search(line)
        if match:
            transferred = int(match.group(1).replace(",", ""))
        match = _FILES_RE.search(line)
        if match:
            files = int(match.group(1).replace(",", ""))
    return {"size_bytes": total, "bytes_copied": transferred, "files_total": files}


def _rsync_progress(op: OperationHandle):
    def _on_line(line: str) -> None:
        percent = _PERCENT_RE.search(line)
        if not percent:
            return
        done = _PROGRESS_BYTES_RE.search(line)
        op.set_progress(
            percent=int(percent.group(1)),
            bytes_done=int(done.group(1).replace(",", "")) if done else None,
        )
    return _on_line


async def _rsync(schedule: dict, source: str, dest: str, *, op: OperationHandle,
                 link_dest: Optional[str] = None, delete: bool = False,
                 dry_run: bool = False, detail: str = "",
                 extra: Optional[list[str]] = None) -> tuple[int, str]:
    preserve_acls = bool((schedule.get("options") or {}).get("preserve_acls", True))

    def _args(acls: bool) -> list[str]:
        args = ["rsync", "-aHA" if acls else "-aH", "--numeric-ids", "-S",
                "--no-inc-recursive", "--partial-dir=.ethos-partial"]
        if dry_run:
            args.append("-n")
        else:
            args.append("--info=progress2")
        args.append("--stats")
        if delete:
            args.append("--delete")
        if link_dest:
            args.append(f"--link-dest={link_dest}")
        for pattern in _rsync_excludes(schedule):
            args += ["--exclude", pattern]
        args += list(extra or [])
        args += [source, dest]
        return args

    op.set_phase("copying", detail or f"{os.path.basename(source.rstrip('/'))} → {dest}")
    rc, text = await run_streaming(_args(preserve_acls), op=op, on_line=_rsync_progress(op))
    if op.cancel_requested:
        raise OpCancelled("Cancelled by the operator")
    if rc in (23, 24) and preserve_acls and re.search(r"ACL|xattr", text, re.IGNORECASE):
        logger.warning("Backup %s: ACL copy failed, retrying without -A", schedule.get("id"))
        rc, text = await run_streaming(_args(False), op=op, on_line=_rsync_progress(op))
        if op.cancel_requested:
            raise OpCancelled("Cancelled by the operator")
    return rc, text


async def _rsync_verify(source: str, dest: str, *, deep: bool, schedule: dict) -> tuple[int, str]:
    args = ["rsync", "-rlptn", "--delete", "--itemize-changes", "--no-inc-recursive"]
    if deep:
        args.append("-c")
    for pattern in _rsync_excludes(schedule):
        args += ["--exclude", pattern]
    args += [source, dest]
    rc, out, err = await run(args, capture=True)
    return rc, (out + b"\n" + err).decode(errors="replace")


def _diff_count(text: str) -> int:
    count = 0
    for line in text.splitlines():
        if line.startswith("Number of") or line.startswith("sent ") or not line.strip():
            continue
        if _ITEMIZE_RE.match(line):
            count += 1
    return count


# ── generation helpers ───────────────────────────────────────────────────────

def _previous_generation(schedule: dict) -> Optional[dict]:
    return backup_layout.latest_generation(schedule, complete_only=True)


def _previous_dir(schedule: dict, previous: Optional[dict], *parts: str) -> Optional[str]:
    if not previous:
        return None
    path = os.path.join(backup_layout.generation_path(schedule, previous["generation"]), *parts)
    return path if os.path.isdir(path) else None


def _manifest_for(schedule: dict, generation: str, trigger: str) -> dict:
    return {
        "version": 2,
        "generation": generation,
        "schedule_id": schedule.get("id"),
        "schedule_name": schedule.get("name", ""),
        "type": schedule.get("type"),
        "trigger": trigger,
        "status": "incomplete",
        "source": schedule.get("source"),
        "destination": schedule.get("destination"),
        "target": backup_target(schedule),
        "created_at": now_iso(),
        "finished_at": "",
        "duration_seconds": None,
        "size_bytes": 0,
        "bytes_copied": 0,
        "files_total": 0,
        "rsync": {},
        "docker": None,
        "verify": {},
        "remote": {},
        "error": "",
    }


def _write(gen_dir: str, manifest: dict) -> None:
    backup_layout.write_manifest(gen_dir, manifest)


def _remove_dir(path: str) -> None:
    if not os.path.isdir(path):
        return
    try:
        shutil.rmtree(path)
    except OSError:
        logger.error("Could not remove backup workspace %s", path, exc_info=True)


# ── snapshots ────────────────────────────────────────────────────────────────

async def _snapshot_folder(schedule: dict, workspace: str, previous: Optional[dict],
                           op: OperationHandle) -> dict:
    data_dir = os.path.join(workspace, "data")
    os.makedirs(data_dir, exist_ok=True)
    link_dest = _previous_dir(schedule, previous, "data")
    source = os.path.abspath(schedule["source"])
    if not os.path.isdir(source):
        raise BackupError(f"Source path not found: {source}")
    rc, text = await _rsync(schedule, source + "/", data_dir + "/", op=op,
                            link_dest=link_dest, delete=True,
                            detail=f"{os.path.basename(source)} → snapshot")
    if rc != 0:
        raise BackupError(text[-300:].strip() or f"rsync exited {rc}")
    stats = _parse_stats(text)
    return {"stats": stats, "docker": None}


async def _snapshot_docker(schedule: dict, workspace: str, previous: Optional[dict],
                           op: OperationHandle, include_images: Optional[bool] = None) -> dict:
    options = schedule.get("options") or {}
    if include_images is None:
        include_images = bool(options.get("include_images", True))
    try:
        discovered = await backup_docker.discover_projects(schedule["source"])
    except DockerUnavailable as exc:
        raise BackupError(f"Docker is unavailable: {exc}") from exc
    projects, missing = backup_docker.pick_projects(schedule, discovered)
    if missing:
        logger.warning("Backup %s: selected projects not found: %s",
                       schedule.get("id"), ", ".join(missing))
    if not projects:
        raise BackupError(f"No compose projects found under {schedule['source']}")

    previous_manifest = {}
    if previous:
        previous_manifest = backup_layout.read_manifest(
            backup_layout.generation_path(schedule, previous["generation"]))
    previous_projects = {
        project["name"]: project
        for project in ((previous_manifest.get("docker") or {}).get("projects") or [])
    }

    totals = {"size_bytes": 0, "bytes_copied": 0, "files_total": 0}
    entries = []
    for project in projects:
        name = project["name"]
        working_dir = (project.get("working_dir") or "").rstrip("/")
        if not working_dir or not os.path.isdir(working_dir):
            raise BackupError(f"Compose project {name}: folder not found ({working_dir or 'unknown'})")
        base = os.path.join(workspace, "projects", safe_name(name))
        config_dir = os.path.join(base, "config")
        os.makedirs(config_dir, exist_ok=True)

        op.set_phase("config", f"{name} → config")
        previous_config = _previous_dir(schedule, previous, "projects", safe_name(name), "config")
        rc, text = await _rsync(schedule, working_dir + "/", config_dir + "/", op=op,
                                link_dest=previous_config, delete=True,
                                detail=f"{name} → config")
        if rc != 0:
            raise BackupError(f"{name}: config copy failed: {text[-300:].strip()}")
        stats = _parse_stats(text)
        for key in totals:
            totals[key] += stats.get(key, 0)

        volumes = []
        images = []
        stopped: list[str] = []
        try:
            if options.get("include_volumes", True) and project.get("volumes"):
                if options.get("quiesce"):
                    stopped = await backup_docker.stop_containers(project["containers"], op)
                for volume in project["volumes"]:
                    op.check()
                    archive = os.path.join(base, "volumes", f"{safe_name(volume)}.tar.gz")
                    op.set_phase("volumes", f"snapshotting {volume}")
                    info = await backup_docker.archive_volume(volume, archive, op)
                    volumes.append(info)
                    totals["size_bytes"] += info["size"]
                    totals["bytes_copied"] += info["size"]
            if include_images and project.get("images"):
                previous_images = {
                    image["tag"]: image
                    for image in (previous_projects.get(name, {}).get("images") or [])
                }
                for image in project["images"]:
                    op.check()
                    filename = safe_name(image["tag"].replace("/", "__")) + ".tar"
                    archive = os.path.join(base, "images", filename)
                    op.set_phase("images", f"saving {image['tag']}")
                    info = await backup_docker.link_or_save_image(
                        image["tag"], image.get("id") or "", archive,
                        previous_images.get(image["tag"]), op)
                    info["id"] = image.get("id") or info.get("id", "")
                    images.append(info)
                    if not info.get("reused"):
                        totals["size_bytes"] += info["size"]
                        totals["bytes_copied"] += info["size"]
        finally:
            if stopped:
                await backup_docker.start_containers(stopped, op)

        entries.append({
            "name": name,
            "working_dir": working_dir,
            "containers": project.get("containers") or [],
            "volumes": volumes,
            "images": images,
        })

    return {"stats": totals, "docker": {"projects": entries}}


# ── retention ────────────────────────────────────────────────────────────────

def _generation_datetime(entry: dict) -> Optional[datetime]:
    value = entry.get("created_at") or ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # intentional: fall back to parsing the generation directory name
        pass
    try:
        return datetime.strptime(entry.get("generation", ""), "%Y-%m-%dT%H-%M-%S")
    except ValueError:
        return None


def select_generations(generations: list[dict], retention: dict) -> set[str]:
    """GFS selection: newest + keep_last + newest per day/week/month."""
    ordered = sorted(generations, key=lambda g: (g.get("created_at") or g.get("generation") or ""),
                     reverse=True)
    complete = [g for g in ordered if g.get("status") in ("complete", "legacy")]
    if not complete:
        return set()

    keep = {complete[0]["generation"]}
    keep_last = int(retention.get("keep_last") or 0)
    for entry in complete[:keep_last]:
        keep.add(entry["generation"])

    def add_buckets(key_fn, limit: int) -> None:
        if limit <= 0:
            return
        seen: set = set()
        added = 0
        for entry in complete:
            when = _generation_datetime(entry)
            if when is None:
                continue
            key = key_fn(when)
            if key in seen:
                continue
            seen.add(key)
            keep.add(entry["generation"])
            added += 1
            if added >= limit:
                return

    add_buckets(lambda d: d.date(), int(retention.get("keep_daily") or 0))
    add_buckets(lambda d: (d.isocalendar().year, d.isocalendar().week),
                int(retention.get("keep_weekly") or 0))
    add_buckets(lambda d: (d.year, d.month), int(retention.get("keep_monthly") or 0))

    native = [entry for entry in complete
              if not (entry.get("legacy") or entry.get("status") == "legacy")]
    if len(native) < 2:
        for entry in complete:
            if entry.get("legacy") or entry.get("status") == "legacy":
                keep.add(entry["generation"])
    return keep


def _prune(schedule: dict, keep: set[str]) -> list[str]:
    removed = []
    for entry in backup_layout.list_generations(schedule):
        name = entry["generation"]
        if name in keep:
            continue
        path = backup_layout.generation_path(schedule, name)
        try:
            shutil.rmtree(path)
            removed.append(name)
            logger.info("Backup %s: pruned generation %s", schedule.get("id"), name)
        except OSError:
            logger.error("Backup %s: could not prune %s", schedule.get("id"), path, exc_info=True)
    return removed


async def apply_retention(schedule: dict, keep_generation: str) -> list[str]:
    generations = backup_layout.list_generations(schedule)
    complete = [g for g in generations if g.get("status") in ("complete", "legacy")]
    if len(complete) <= 2:
        return []
    keep = select_generations(generations, schedule.get("retention") or {})
    keep.add(keep_generation)
    return await asyncio.to_thread(_prune, schedule, keep)


# ── verify ───────────────────────────────────────────────────────────────────

async def verify_generation(schedule: dict, generation: str, *, deep: bool,
                            op: OperationHandle) -> dict:
    op.set_phase("verify", f"{'deep' if deep else 'quick'} verify of {generation}")
    gen_dir = backup_layout.generation_path(schedule, generation)
    manifest = backup_layout.read_manifest(gen_dir)
    if not manifest or manifest.get("status") not in ("complete", "legacy"):
        return {"status": "failed", "mode": "deep" if deep else "quick",
                "checked_at": now_iso(), "diffs": 0,
                "detail": "Generation is incomplete or missing a manifest"}

    result = {"status": "ok", "mode": "deep" if deep else "quick",
              "checked_at": now_iso(), "diffs": 0, "detail": ""}

    if schedule.get("type") == "folder":
        data_dir = os.path.join(gen_dir, "data")
        if not os.path.isdir(data_dir):
            return {**result, "status": "failed", "detail": "Snapshot data folder is missing"}
        source = os.path.abspath(schedule["source"])
        if not os.path.isdir(source):
            return {**result, "status": "failed", "detail": f"Source is not available: {source}"}
        rc, text = await _rsync_verify(source + "/", data_dir + "/", deep=deep, schedule=schedule)
        if rc != 0:
            return {**result, "status": "failed", "detail": text[-300:].strip()}
        diffs = _diff_count(text)
        result.update({"diffs": diffs,
                       "status": "ok" if diffs == 0 else "differs",
                       "detail": "" if diffs == 0 else f"{diffs} difference(s) found"})
        return result

    projects = (manifest.get("docker") or {}).get("projects") or []
    if not projects:
        return {**result, "status": "failed", "detail": "Manifest lists no compose projects"}
    diffs = 0
    for project in projects:
        name = project.get("name") or "project"
        working_dir = (project.get("working_dir") or "").rstrip("/")
        config_dir = os.path.join(gen_dir, "projects", safe_name(name), "config")
        if working_dir and os.path.isdir(working_dir) and os.path.isdir(config_dir):
            rc, text = await _rsync_verify(working_dir + "/", config_dir + "/",
                                           deep=deep, schedule=schedule)
            if rc != 0:
                return {**result, "status": "failed",
                        "detail": f"{name}: {text[-300:].strip()}"}
            diffs += _diff_count(text)
        for archive in (project.get("volumes") or []) + (project.get("images") or []):
            path = os.path.join(gen_dir, "projects", safe_name(name),
                                "volumes" if archive in (project.get("volumes") or []) else "images",
                                archive.get("file") or "")
            if not archive.get("file") or not os.path.isfile(path):
                diffs += 1
                continue
            if deep and not await backup_docker.verify_archive(path):
                diffs += 1
    result.update({"diffs": diffs,
                   "status": "ok" if diffs == 0 else "differs",
                   "detail": "" if diffs == 0 else f"{diffs} difference(s) found"})
    return result


# ── remote ───────────────────────────────────────────────────────────────────

def _payload_dir(schedule: dict, gen_dir: str) -> str:
    return os.path.join(gen_dir, "data" if schedule.get("type") == "folder" else "projects")


# ── backup ───────────────────────────────────────────────────────────────────

def _skip_result(schedule: dict, message: str, started: float) -> dict:
    logger.warning("Backup %s skipped: %s", schedule.get("id"), message)
    return {
        "status": "skipped", "error": message, "generation": None,
        "bytes_total": 0, "bytes_copied": 0, "files_total": 0,
        "duration_seconds": round(time.time() - started, 1), "remote": {},
    }


async def run_backup(schedule: dict, op: OperationHandle) -> dict:
    started = time.time()
    op.set_phase("preflight", "checking source and destination")
    try:
        pre = await asyncio.to_thread(backup_layout.preflight, schedule)
        if pre["skip"]:
            return _skip_result(schedule, pre["error"], started)
        if not pre["ok"]:
            raise BackupError(pre["error"])

        layout = await asyncio.to_thread(backup_layout.ensure_layout, schedule)
        if not layout["ok"]:
            raise BackupError(f"Could not prepare the destination: {layout['error']}")

        previous = await asyncio.to_thread(_previous_generation, schedule)
        generation = await asyncio.to_thread(backup_layout.unique_generation_name, schedule)
        workspace = backup_layout.workspace_path(schedule, op.op_id)
        if os.path.isdir(workspace):
            await asyncio.to_thread(shutil.rmtree, workspace, True)
        os.makedirs(workspace, exist_ok=True)

        try:
            if schedule.get("type") == "docker-compose":
                snapshot = await _snapshot_docker(schedule, workspace, previous, op)
            else:
                snapshot = await _snapshot_folder(schedule, workspace, previous, op)
            op.check()
            stats = snapshot.get("stats") or {}
            manifest = _manifest_for(schedule, generation, op.trigger)
            manifest.update({
                "status": "complete",
                "finished_at": now_iso(),
                "duration_seconds": round(time.time() - started, 1),
                "size_bytes": stats.get("size_bytes", 0),
                "bytes_copied": stats.get("bytes_copied", 0),
                "files_total": stats.get("files_total", 0),
                "rsync": stats,
                "docker": snapshot.get("docker"),
            })
            await asyncio.to_thread(_write, workspace, manifest)

            op.set_phase("finalize", f"publishing {generation}")
            final_dir = backup_layout.generation_path(schedule, generation)
            await asyncio.to_thread(os.replace, workspace, final_dir)
            op.generation = generation
        except BaseException:
            _remove_dir(workspace)
            raise

        verify_result = {}
        if schedule.get("verify_after_run") == "quick":
            try:
                verify_result = await verify_generation(schedule, generation, deep=False, op=op)
            except OpCancelled:
                raise
            except Exception:
                logger.error("Backup %s: post-run verify failed", schedule.get("id"), exc_info=True)
        manifest["verify"] = verify_result
        await asyncio.to_thread(_write, final_dir, manifest)

        pruned = []
        try:
            pruned = await apply_retention(schedule, generation)
        except Exception:
            logger.error("Backup %s: retention failed", schedule.get("id"), exc_info=True)

        remote_result = {}
        if (schedule.get("remote") or {}).get("enabled"):
            try:
                remote_result = await backup_remote.replicate(
                    _payload_dir(schedule, final_dir), schedule["remote"], generation, op)
            except OpCancelled:
                raise
            except Exception as exc:
                logger.error("Backup %s: remote replication failed", schedule.get("id"), exc_info=True)
                remote_result = {"status": "failed", "detail": str(exc)[-300:]}
        manifest["remote"] = remote_result
        await asyncio.to_thread(_write, final_dir, manifest)

        return {
            "status": "success",
            "error": "",
            "generation": generation,
            "bytes_total": stats.get("size_bytes", 0),
            "bytes_copied": stats.get("bytes_copied", 0),
            "files_total": stats.get("files_total", 0),
            "duration_seconds": round(time.time() - started, 1),
            "verified": verify_result,
            "pruned": pruned,
            "remote": remote_result,
        }
    except OpCancelled as exc:
        return {"status": "cancelled", "error": str(exc), "generation": None,
                "bytes_total": 0, "bytes_copied": 0, "files_total": 0,
                "duration_seconds": round(time.time() - started, 1), "remote": {}}
    except BackupError as exc:
        if op.cancel_requested:
            return {"status": "cancelled", "error": "Cancelled by the operator", "generation": None,
                    "bytes_total": 0, "bytes_copied": 0, "files_total": 0,
                    "duration_seconds": round(time.time() - started, 1), "remote": {}}
        return {"status": "failed", "error": str(exc), "generation": None,
                "bytes_total": 0, "bytes_copied": 0, "files_total": 0,
                "duration_seconds": round(time.time() - started, 1), "remote": {}}
    except Exception as exc:
        logger.error("Backup %s crashed", schedule.get("id"), exc_info=True)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "generation": None,
                "bytes_total": 0, "bytes_copied": 0, "files_total": 0,
                "duration_seconds": round(time.time() - started, 1), "remote": {}}


# ── pre-restore snapshot + restore ──────────────────────────────────────────

async def _create_pre_restore(schedule: dict, op: OperationHandle) -> Optional[str]:
    """Snapshot the live state before an in-place restore; abort on failure."""
    source = os.path.abspath(schedule.get("source") or "")
    if not os.path.isdir(source):
        logger.info("Restore %s: source missing — skipping the pre-restore snapshot",
                    schedule.get("id"))
        return None
    workspace = backup_layout.workspace_path(schedule, f"{op.op_id}-pre")
    if os.path.isdir(workspace):
        await asyncio.to_thread(shutil.rmtree, workspace, True)
    os.makedirs(workspace, exist_ok=True)
    previous = _previous_generation(schedule)
    try:
        if schedule.get("type") == "docker-compose":
            snapshot = await _snapshot_docker(schedule, workspace, previous, op,
                                              include_images=False)
        else:
            snapshot = await _snapshot_folder(schedule, workspace, previous, op)
        stats = snapshot.get("stats") or {}
        generation = await asyncio.to_thread(backup_layout.unique_generation_name, schedule)
        manifest = _manifest_for(schedule, generation, "pre-restore")
        manifest.update({
            "status": "complete",
            "finished_at": now_iso(),
            "size_bytes": stats.get("size_bytes", 0),
            "bytes_copied": stats.get("bytes_copied", 0),
            "files_total": stats.get("files_total", 0),
            "rsync": stats,
            "docker": snapshot.get("docker"),
            "note": "Automatic snapshot taken before an in-place restore.",
        })
        await asyncio.to_thread(_write, workspace, manifest)
        final_dir = backup_layout.generation_path(schedule, generation)
        await asyncio.to_thread(os.replace, workspace, final_dir)
        logger.info("Restore %s: pre-restore generation %s", schedule.get("id"), generation)
        return generation
    except BaseException:
        _remove_dir(workspace)
        raise


async def _restore_docker(schedule: dict, gen_dir: str, manifest: dict,
                          delete_extra: bool, op: OperationHandle) -> int:
    projects = (manifest.get("docker") or {}).get("projects") or []
    if not projects:
        raise BackupError("This generation contains no compose projects")
    try:
        discovered = await backup_docker.discover_projects(schedule.get("source"))
    except DockerUnavailable as exc:
        raise BackupError(f"Docker is unavailable: {exc}") from exc
    live = {project["name"]: project for project in discovered}

    stopped: list[str] = []
    for project in projects:
        containers = (live.get(project["name"]) or {}).get("containers") or []
        stopped.extend(await backup_docker.stop_containers(containers, op))

    total = 0
    try:
        for project in projects:
            name = project.get("name") or "project"
            working_dir = (project.get("working_dir") or "").rstrip("/")
            if not working_dir:
                raise BackupError(f"Project {name}: no working folder in the manifest")
            config_dir = os.path.join(gen_dir, "projects", safe_name(name), "config")
            if os.path.isdir(config_dir):
                os.makedirs(working_dir, exist_ok=True)
                rc, text = await _rsync(schedule, config_dir + "/", working_dir + "/", op=op,
                                        delete=delete_extra, detail=f"{name} → config")
                if rc != 0:
                    raise BackupError(f"{name}: config restore failed: {text[-300:].strip()}")
            for volume in project.get("volumes") or []:
                archive = os.path.join(gen_dir, "projects", safe_name(name), "volumes",
                                       volume.get("file") or "")
                if not volume.get("file") or not os.path.isfile(archive):
                    raise BackupError(f"{name}: volume archive missing ({volume.get('file')})")
                op.set_phase("restoring", f"volume {volume.get('volume')}")
                await backup_docker.restore_volume(volume["volume"], archive, op)
            for image in project.get("images") or []:
                archive = os.path.join(gen_dir, "projects", safe_name(name), "images",
                                       image.get("file") or "")
                if not image.get("file") or not os.path.isfile(archive):
                    raise BackupError(f"{name}: image archive missing ({image.get('file')})")
                op.set_phase("restoring", f"image {image.get('tag')}")
                await backup_docker.load_image(archive, op)
    finally:
        if stopped:
            await backup_docker.start_containers(stopped, op)
    return total


async def run_restore(schedule: dict, generation: Optional[str], *, mode: str,
                      target: Optional[str], delete_extra: bool,
                      op: OperationHandle) -> dict:
    started = time.time()
    try:
        op.set_phase("preflight", "checking the destination")
        pre = await asyncio.to_thread(backup_layout.preflight, schedule)
        if not pre["ok"]:
            raise BackupError(pre["error"] or "Destination is not available")
        layout = await asyncio.to_thread(backup_layout.ensure_layout, schedule)
        if not layout["ok"]:
            raise BackupError(f"Could not read the backup folder: {layout['error']}")

        generations = backup_layout.list_generations(schedule)
        if not generation or generation == "latest":
            entry = next((g for g in generations
                          if g.get("status") in ("complete", "legacy")), None)
        else:
            entry = next((g for g in generations if g["generation"] == generation), None)
        if entry is None or entry.get("status") not in ("complete", "legacy"):
            raise BackupError("Generation not found or incomplete")

        gen_dir = backup_layout.generation_path(schedule, entry["generation"])
        manifest = backup_layout.read_manifest(gen_dir)
        payload = _payload_dir(schedule, gen_dir)
        if not os.path.isdir(payload):
            raise BackupError("This generation has no restorable data")

        bytes_total = 0
        if mode == "to_folder":
            if not target:
                raise BackupError("Choose a target folder for the restore")
            destination = os.path.realpath(os.path.abspath(os.path.expanduser(target)))
            if os.path.commonpath([destination, gen_dir]) == gen_dir:
                raise BackupError("The restore target cannot be inside the snapshot")
            if os.path.exists(destination) and not os.path.isdir(destination):
                raise BackupError(f"Restore target is not a folder: {destination}")
            os.makedirs(destination, exist_ok=True)
            op.set_phase("restoring", f"snapshot → {destination}")
            rc, text = await _rsync(schedule, payload + "/", destination + "/", op=op,
                                    delete=delete_extra, detail=f"{entry['generation']} → restore")
            if rc != 0:
                raise BackupError(text[-300:].strip() or f"rsync exited {rc}")
            bytes_total = _parse_stats(text).get("size_bytes", 0)
            return {"status": "success", "error": "", "generation": entry["generation"],
                    "bytes_total": bytes_total,
                    "duration_seconds": round(time.time() - started, 1)}

        pre_generation = await _create_pre_restore(schedule, op)
        if schedule.get("type") == "docker-compose":
            bytes_total = await _restore_docker(schedule, gen_dir, manifest, delete_extra, op)
        else:
            source = os.path.abspath(schedule["source"])
            os.makedirs(source, exist_ok=True)
            op.set_phase("restoring", f"{entry['generation']} → {source}")
            rc, text = await _rsync(schedule, os.path.join(gen_dir, "data") + "/", source + "/",
                                    op=op, delete=delete_extra, detail="snapshot → source")
            if rc != 0:
                raise BackupError(text[-300:].strip() or f"rsync exited {rc}")
            bytes_total = _parse_stats(text).get("size_bytes", 0)
        return {"status": "success", "error": "", "generation": entry["generation"],
                "pre_restore_generation": pre_generation, "bytes_total": bytes_total,
                "duration_seconds": round(time.time() - started, 1)}
    except OpCancelled as exc:
        return {"status": "cancelled", "error": str(exc), "generation": None,
                "bytes_total": 0, "duration_seconds": round(time.time() - started, 1)}
    except BackupError as exc:
        if op.cancel_requested:
            return {"status": "cancelled", "error": "Cancelled by the operator", "generation": None,
                    "bytes_total": 0, "duration_seconds": round(time.time() - started, 1)}
        return {"status": "failed", "error": str(exc), "generation": None,
                "bytes_total": 0, "duration_seconds": round(time.time() - started, 1)}
    except Exception as exc:
        logger.error("Restore %s crashed", schedule.get("id"), exc_info=True)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "generation": None,
                "bytes_total": 0, "duration_seconds": round(time.time() - started, 1)}


# ── dry run ─────────────────────────────────────────────────────────────────

async def run_dry_run(schedule: dict, op: OperationHandle) -> dict:
    started = time.time()
    try:
        pre = await asyncio.to_thread(backup_layout.preflight, schedule)
        if not pre["ok"]:
            return {"status": "skipped" if pre["skip"] else "failed",
                    "error": pre["error"], "detail": pre["error"],
                    "duration_seconds": round(time.time() - started, 1)}
        await asyncio.to_thread(backup_layout.ensure_layout, schedule)
        previous = _previous_generation(schedule)
        details: list[str] = []
        changes = 0
        if schedule.get("type") == "folder":
            workspace = backup_layout.workspace_path(schedule, f"{op.op_id}-dry")
            data_dir = os.path.join(workspace, "data")
            os.makedirs(data_dir, exist_ok=True)
            try:
                link = _previous_dir(schedule, previous, "data")
                rc, text = await _rsync(schedule, os.path.abspath(schedule["source"]) + "/",
                                        data_dir + "/", op=op, link_dest=link,
                                        delete=True, dry_run=True, detail="dry run")
                if rc != 0:
                    raise BackupError(text[-300:].strip() or f"rsync exited {rc}")
                changes = _diff_count(text)
                details.append(f"{changes} file(s) would change")
            finally:
                await asyncio.to_thread(_remove_dir, workspace)
        else:
            discovered = await backup_docker.discover_projects(schedule["source"])
            projects, missing = backup_docker.pick_projects(schedule, discovered)
            if missing:
                details.append(f"missing projects: {', '.join(missing)}")
            if not projects:
                raise BackupError(f"No compose projects found under {schedule['source']}")
            for project in projects:
                details.append(
                    f"{project['name']}: {len(project.get('volumes') or [])} volume(s), "
                    f"{len(project.get('images') or [])} image(s), "
                    f"{len(project.get('containers') or [])} container(s)")
        return {"status": "success", "error": "", "changes": changes,
                "detail": " · ".join(details),
                "duration_seconds": round(time.time() - started, 1)}
    except OpCancelled as exc:
        return {"status": "cancelled", "error": str(exc), "detail": str(exc),
                "duration_seconds": round(time.time() - started, 1)}
    except BackupError as exc:
        if op.cancel_requested:
            return {"status": "cancelled", "error": "Cancelled by the operator", "detail": "Cancelled",
                    "duration_seconds": round(time.time() - started, 1)}
        return {"status": "failed", "error": str(exc), "detail": str(exc),
                "duration_seconds": round(time.time() - started, 1)}
    except Exception as exc:
        logger.error("Dry run %s crashed", schedule.get("id"), exc_info=True)
        return {"status": "failed", "error": str(exc), "detail": str(exc),
                "duration_seconds": round(time.time() - started, 1)}
