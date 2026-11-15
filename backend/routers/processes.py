"""EthOS NAS — Process Manager Router

REST:
- GET /api/processes          — list running processes + RAM/swap snapshot
- DELETE /api/processes/{pid} — send SIGTERM to a process
"""

import asyncio
import logging
import re
import time
from typing import Optional

import psutil
from fastapi import APIRouter, Depends, HTTPException, Query

from backend.routers.auth import require_admin

logger = logging.getLogger("ethos-nas.routers.processes")
router = APIRouter()

_MB = 1024 * 1024
_CGROUP_ID_RE = re.compile(r"([0-9a-f]{64})")

# Some processes report a useless `comm` (VS Code's server processes all show up
# as "MainThread"). Map well-known command-line markers to a readable label.
_CMD_MARKERS = (
    (".vscode-server", "VS Code Server"),
    ("code-server", "VS Code Server"),
    ("uvicorn backend.main", "ethos-nas backend"),
    ("homeassistant", "Home Assistant"),
    ("firecrawl", "Firecrawl"),
    ("dotnet test", "dotnet test"),
    ("pytest", "pytest"),
)


def _display_name(name: str, cmdline: str) -> str:
    low = (cmdline or "").lower()
    for marker, label in _CMD_MARKERS:
        if marker in low:
            return label
    return name

# pid → container name, refreshed lazily: a NAS operator looking at "node 620 MB"
# needs to know WHICH container that is, not just the binary name.
_container_cache: dict = {"at": 0.0, "map": {}}


def _container_map() -> dict[str, str]:
    now = time.time()
    if now - _container_cache["at"] < 30:
        return _container_cache["map"]
    mapping: dict[str, str] = {}
    try:
        import docker
        client = docker.from_env()
        for c in client.containers.list():
            mapping[c.id] = c.name
    except Exception as exc:  # docker missing/permission — not fatal
        logger.debug("container attribution unavailable: %s", exc)
    _container_cache.update({"at": now, "map": mapping})
    return mapping


def _container_of(pid: int, mapping: dict[str, str]) -> str:
    if not mapping:
        return ""
    try:
        with open(f"/proc/{pid}/cgroup", "r", errors="replace") as f:
            m = _CGROUP_ID_RE.search(f.read())
    except OSError:
        return ""
    return mapping.get(m.group(1), "") if m else ""


def _safe_proc_info(proc: psutil.Process) -> Optional[dict]:
    """Collect safe process information, returning None on access errors."""
    try:
        with proc.oneshot():
            mem = proc.memory_info()
            try:
                threads = proc.num_threads()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                threads = 0
            try:
                started = round(proc.create_time())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                started = 0
            cmdline = " ".join(proc.cmdline()[:6]) or proc.name()
            name = proc.name()
            return {
                "pid": proc.pid,
                "name": name,
                "display_name": _display_name(name, cmdline),
                "status": proc.status(),
                "cpu_percent": round(proc.cpu_percent(interval=0), 1),
                "memory_percent": round(proc.memory_percent(), 2),
                "memory_rss": mem.rss,
                # Absolute figures: percentages hide which processes matter on a
                # 14 GB box shared by containers and the IDE.
                "memory_rss_mb": round(mem.rss / _MB, 1),
                "memory_vsz_mb": round(mem.vms / _MB, 1),
                "threads": threads,
                "started": started,
                "username": proc.username(),
                "cmdline": cmdline,
            }
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _system_snapshot() -> dict:
    """RAM + swap totals — the context needed to read the process list."""
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    try:
        load1, load5, load15 = psutil.getloadavg()
    except (AttributeError, OSError):
        load1 = load5 = load15 = 0.0
    return {
        "mem_total_mb": round(vm.total / _MB),
        "mem_used_mb": round((vm.total - vm.available) / _MB),
        "mem_available_mb": round(vm.available / _MB),
        "mem_cached_mb": round(getattr(vm, "cached", 0) / _MB),
        "mem_percent": vm.percent,
        "swap_total_mb": round(sw.total / _MB),
        "swap_used_mb": round(sw.used / _MB),
        "swap_percent": sw.percent,
        "cpu_count": psutil.cpu_count() or 1,
        "load_1": round(load1, 2),
        "load_5": round(load5, 2),
        "load_15": round(load15, 2),
    }


def _group_by_name(processes: list[dict], top: int = 10) -> list[dict]:
    """Aggregate RAM per program name.

    Ten `node` workers of one container read as ten small rows in a flat list;
    users looking for "what eats my RAM" need them summed (and counted).
    """
    groups: dict[str, dict] = {}
    for p in processes:
        # Containerised processes are grouped by container, not by binary: five
        # `node` workers of one stack are one entry, not five.
        container = p.get("container") or ""
        key = container or p.get("display_name") or p["name"]
        g = groups.setdefault(key, {
            "name": key, "container": container, "process_count": 0,
            "memory_rss_mb": 0.0, "cpu_percent": 0.0, "pids": [],
        })
        g["process_count"] += 1
        g["memory_rss_mb"] = round(g["memory_rss_mb"] + p["memory_rss_mb"], 1)
        g["cpu_percent"] = round(g["cpu_percent"] + p["cpu_percent"], 1)
        if len(g["pids"]) < 5:
            g["pids"].append(p["pid"])
    ordered = sorted(groups.values(), key=lambda g: g["memory_rss_mb"], reverse=True)
    return ordered[:top]


def _collect(sort: str, limit: int) -> dict:
    """Snapshot process table (blocking — always called via to_thread)."""
    # cpu_percent is a delta since the previous call per Process object: prime
    # every process, wait briefly, then read. Without the gap every CPU value
    # read as 0.0 and the CPU column looked broken.
    for proc in psutil.process_iter():
        try:
            proc.cpu_percent(interval=0)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass  # intentional: a failure here must not break the caller
    time.sleep(0.15)

    processes = []
    for proc in psutil.process_iter():
        info = _safe_proc_info(proc)
        if info:
            processes.append(info)

    containers = _container_map()
    if containers:
        for p in processes:
            p["container"] = _container_of(p["pid"], containers)

    sort_keys = {
        "cpu": lambda p: p["cpu_percent"],
        "mem": lambda p: p["memory_rss"],
        "name": lambda p: p["name"].lower(),
        "pid": lambda p: p["pid"],
    }
    processes.sort(key=sort_keys[sort], reverse=(sort != "name"))
    total = len(processes)

    return {
        "processes": processes[:limit],
        "total": total,
        "returned": min(total, limit),
        "sort": sort,
        "system": _system_snapshot(),
        "groups": _group_by_name(processes),
    }


@router.get("")
async def list_processes(
    sort: str = Query("mem", pattern="^(cpu|mem|name|pid)$"),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    """Return running processes plus a RAM/swap snapshot.

    Args:
        sort: Sort key — cpu | mem | name | pid (defaults to memory)
        limit: Maximum number of processes to return
    """
    # psutil walks /proc for every process twice; keep that off the event loop.
    return await asyncio.to_thread(_collect, sort, limit)


@router.delete("/{pid}", dependencies=[Depends(require_admin)])
async def kill_process(pid: int) -> dict:
    """Send SIGTERM to a process by PID.

    Args:
        pid: Target process ID
    """
    if pid <= 1:
        raise HTTPException(status_code=400, detail="Cannot terminate system process")
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        return {"ok": True, "pid": pid, "message": f"SIGTERM sent to PID {pid}"}
    except psutil.NoSuchProcess:
        raise HTTPException(status_code=404, detail=f"Process {pid} not found")
    except psutil.AccessDenied:
        raise HTTPException(status_code=403, detail=f"Permission denied for PID {pid}")
