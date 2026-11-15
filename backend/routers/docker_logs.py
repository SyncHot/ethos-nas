"""Deprecated alias for the unified Logs WebSocket.

Container logs used to live in their own plain-text socket
(`/ws/logs/{container_name}`). Since the unified log system (docs/logs.md) every
log socket is served by ``backend/routers/logs_ws.py`` with JSON frames, the same
JWT check and an admin role check.

This module keeps the router symbol and the handler importable for one release;
`main.py` still includes it (it registers no routes). Prefer
`/ws/logs?source=container:<name>`.
"""

from backend.routers.logs_ws import (  # noqa: F401 — re-exported for callers
    CLOSE_FORBIDDEN,
    CLOSE_NOT_FOUND,
    CLOSE_UNAUTHORISED,
    stream_container_logs,
)
from fastapi import APIRouter

import logging

logger = logging.getLogger("ethos-nas.routers.docker_logs")  # shim, kept for the naming guard

router = APIRouter()


