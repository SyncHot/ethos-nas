"""Loopback client for ``runtime: venv`` package services (core → package).

The core never imports a package's code: it talks HTTP to ``127.0.0.1:<port>``
of a supervised package process (``services/package_runtime.py``). This module
is the one place that knows how to find that port, fail fast when the package
is not installed/running, and turn package errors into user-facing exceptions.

All calls are synchronous on purpose — callers already run them inside
``asyncio.to_thread`` (Music search/stream/download, video thumbnails), which is
exactly how the previous in-process yt-dlp/OpenCV calls were scheduled.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from backend.services import package_runtime, packages_store

logger = logging.getLogger("ethos-nas.services.package_client")

DEFAULT_TIMEOUT = 30
_MAX_RESPONSE = 8 * 1024 * 1024


class PackageUnavailable(Exception):
    """The package is not installed, not a venv package, or not running."""


class PackageCallError(Exception):
    """The package service answered with an error or an unusable payload."""


def _record(pkg_id: str) -> Optional[dict]:
    record = packages_store.get_package(pkg_id)
    if not record or str(record.get("runtime") or "compose") != "venv":
        return None
    return record


def is_available(pkg_id: str) -> bool:
    """True when the package is installed and its service process is alive."""
    return _record(pkg_id) is not None and package_runtime.is_running(pkg_id)


def service_port(pkg_id: str) -> int:
    record = _record(pkg_id)
    if not record:
        raise PackageUnavailable(f"Package {pkg_id!r} is not installed")
    python_cfg = record.get("python") or {}
    port = int(python_cfg.get("port") or ((record.get("ui") or {}).get("port") or 0))
    if not 1 <= port <= 65535:
        raise PackageCallError(f"Package {pkg_id!r} has no service port")
    return port


def _request(pkg_id: str, method: str, path: str, body: Optional[bytes],
             content_type: str, timeout: int) -> bytes:
    if not is_available(pkg_id):
        raise PackageUnavailable(f"Package {pkg_id!r} is not running")
    url = f"http://127.0.0.1:{service_port(pkg_id)}{path if path.startswith('/') else '/' + path}"
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(_MAX_RESPONSE + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(2000).decode(errors="replace").strip()
        except OSError:
            logger.debug("Package %s error body unreadable", pkg_id, exc_info=True)
        logger.warning("Package %s %s %s failed (HTTP %s): %s",
                       pkg_id, method, path, exc.code, detail[:300])
        raise PackageCallError(detail[:300] or f"HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("Package %s %s %s unreachable: %s", pkg_id, method, path, exc)
        raise PackageUnavailable(f"Package {pkg_id!r} is not reachable") from None
    if len(data) > _MAX_RESPONSE:
        raise PackageCallError("Package response is too large")
    return data


def get_json(pkg_id: str, path: str, timeout: int = DEFAULT_TIMEOUT, **query) -> Any:
    if query:
        path = f"{path}?{urllib.parse.urlencode(query)}"
    return _decode(_request(pkg_id, "GET", path, None, "application/json", timeout), pkg_id, path)


def post_json(pkg_id: str, path: str, payload: dict, timeout: int = DEFAULT_TIMEOUT) -> Any:
    body = json.dumps(payload).encode()
    return _decode(
        _request(pkg_id, "POST", path, body, "application/json", timeout), pkg_id, path,
    )


def post_bytes(pkg_id: str, path: str, data: bytes, content_type: str,
               timeout: int = DEFAULT_TIMEOUT) -> Any:
    return _decode(_request(pkg_id, "POST", path, data, content_type, timeout), pkg_id, path)


def _decode(data: bytes, pkg_id: str, path: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("Package %s returned invalid JSON for %s", pkg_id, path)
        raise PackageCallError("Package returned invalid JSON") from None
