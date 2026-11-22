"""Safe, throttled HTTP access for metadata providers.

Every outbound request is HTTPS-only and restricted to an explicit host
allowlist. Redirects are validated before they are followed, response bodies
are size-capped, and images are written atomically with an image content-type
check. Provider health is tracked in-process so a dead/rate-limited source is
skipped for a while instead of slowing every batch.

API keys never appear in error messages or log lines.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from backend.services.metadata.base import (
    ProviderError,
    ProviderNotFound,
    ProviderRateLimited,
)

logger = logging.getLogger("ethos-nas.services.metadata.http")

USER_AGENT = "ethos-nas-metadata/1.0 (+https://github.com/SyncHot/ethos-nas)"
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024

_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass
class ProviderHealth:
    """Small in-memory circuit breaker for one provider host."""

    fail_streak: int = 0
    blocked_until: float = 0.0
    last_error: str = ""
    last_ok: float = 0.0
    last_status: Optional[int] = None

    def blocked(self) -> bool:
        return self.blocked_until > time.time()

    def snapshot(self) -> dict:
        return {
            "fail_streak": self.fail_streak,
            "blocked_until": round(self.blocked_until, 3),
            "blocked": self.blocked(),
            "last_error": self.last_error,
            "last_ok": round(self.last_ok, 3),
            "last_status": self.last_status,
        }


class _RedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect against the same host allowlist."""

    def __init__(self, client: "HttpClient"):
        self._client = client

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._client.check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpClient:
    """One provider's HTTP client: allowlist + throttle + health."""

    def __init__(
        self,
        name: str,
        allowed_hosts: Iterable[str],
        *,
        min_interval: float = 0.25,
        timeout: float = 15.0,
    ):
        self.name = name
        self.allowed_hosts = {h.lower().strip(".") for h in allowed_hosts if h}
        self.min_interval = max(0.0, float(min_interval))
        self.timeout = timeout
        self.health = ProviderHealth()
        self._throttle_lock = threading.Lock()
        self._last_request_at = 0.0

    # ── validation / throttle ─────────────────────────────────────────────
    def check_url(self, url: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError as exc:
            raise ProviderError(f"{self.name}: invalid URL") from exc
        if parsed.scheme != "https":
            raise ProviderError(f"{self.name}: HTTPS is required")
        host = (parsed.hostname or "").lower()
        if not host or host not in self.allowed_hosts:
            raise ProviderError(f"{self.name}: host is not allowlisted")
        if parsed.username or parsed.password:
            raise ProviderError(f"{self.name}: credentials in URL are not allowed")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ProviderError(f"{self.name}: invalid port") from exc
        if port not in (None, 443):
            raise ProviderError(f"{self.name}: only port 443 is allowed")

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        with self._throttle_lock:
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _open(self, url: str):
        self.check_url(url)
        opener = urllib.request.build_opener(_RedirectHandler(self))
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return opener.open(req, timeout=self.timeout)

    # ── health ────────────────────────────────────────────────────────────
    def _mark_ok(self, status: int = 200) -> None:
        self.health.fail_streak = 0
        self.health.blocked_until = 0.0
        self.health.last_ok = time.time()
        self.health.last_status = status

    def _mark_fail(self, message: str, *, status: int | None = None,
                   retry_after: float | None = None) -> None:
        self.health.fail_streak += 1
        self.health.last_error = message[:240]
        self.health.last_status = status
        if retry_after is None:
            retry_after = min(300.0, 15.0 * (2 ** min(self.health.fail_streak - 1, 4)))
        self.health.blocked_until = time.time() + max(0.0, retry_after)

    def _handle_http_error(self, exc: urllib.error.HTTPError) -> None:
        status = int(getattr(exc, "code", 0) or 0)
        if status == 404:
            self._mark_ok(404)
            raise ProviderNotFound(f"{self.name}: not found")
        if status == 429:
            retry_after = 60.0
            try:
                retry_after = float(exc.headers.get("Retry-After") or 60)
            except (TypeError, ValueError):
                retry_after = 60.0
            self._mark_fail("HTTP 429 (rate limited)", status=status, retry_after=retry_after)
            raise ProviderRateLimited(f"{self.name}: rate limited")
        self._mark_fail(f"HTTP {status}", status=status)
        raise ProviderError(f"{self.name}: HTTP {status}")

    @staticmethod
    def _raise_network(exc: Exception, name: str) -> None:
        # Never include the URL: it may contain an API key.
        reason = type(exc).__name__
        raise ProviderError(f"{name}: request failed ({reason})") from exc

    # ── JSON ──────────────────────────────────────────────────────────────
    def get_json(self, url: str, *, force: bool = False):
        if self.health.blocked() and not force:
            raise ProviderRateLimited(f"{self.name}: temporarily unavailable")
        self._throttle()
        try:
            with self._open(url) as resp:
                raw = resp.read(MAX_JSON_BYTES + 1)
                status = int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            self._handle_http_error(exc)
        except ProviderError:
            raise
        except Exception as exc:  # network/TLS/timeout/socket
            self._mark_fail("request failed")
            self._raise_network(exc, self.name)
        if len(raw) > MAX_JSON_BYTES:
            self._mark_fail("response too large")
            raise ProviderError(f"{self.name}: response too large")
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._mark_fail("invalid JSON")
            raise ProviderError(f"{self.name}: invalid JSON") from exc
        self._mark_ok(status)
        return data

    # ── images ────────────────────────────────────────────────────────────
    def download_image(self, url: str, dest_dir: Path, prefix: str) -> Optional[str]:
        """Download an image into `dest_dir`. Returns the local filename.

        Existing files are reused. The URL is hashed, so the same remote image
        never produces two files, and the extension follows the Content-Type.
        """
        if not url:
            return None
        self.check_url(url)
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        self._throttle()
        try:
            with self._open(url) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if not ctype.startswith("image/"):
                    self._mark_fail("non-image content")
                    raise ProviderError(f"{self.name}: not an image")
                data = resp.read(MAX_IMAGE_BYTES + 1)
                status = int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            self._handle_http_error(exc)
        except ProviderError:
            raise
        except Exception as exc:
            self._mark_fail("image request failed")
            self._raise_network(exc, self.name)
        if not data:
            self._mark_fail("empty image")
            raise ProviderError(f"{self.name}: empty image")
        if len(data) > MAX_IMAGE_BYTES:
            self._mark_fail("image too large")
            raise ProviderError(f"{self.name}: image too large")
        ext = _IMAGE_TYPES.get(ctype) or self._extension_from_url(url)
        digest = hashlib.md5(f"{self.name}:{url}".encode("utf-8")).hexdigest()[:16]
        name = f"{digest}{ext}"
        out = dest_dir / name
        if out.exists() and out.stat().st_size > 0:
            self._mark_ok(status)
            return name
        tmp = out.with_name(out.name + ".tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, out)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            self._mark_fail("image write failed")
            raise ProviderError(f"{self.name}: image write failed") from exc
        self._mark_ok(status)
        logger.debug("Cached metadata image %s from %s", name, prefix)
        return name

    @staticmethod
    def _extension_from_url(url: str) -> str:
        suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"

    def snapshot(self) -> dict:
        return {"name": self.name, **self.health.snapshot()}
