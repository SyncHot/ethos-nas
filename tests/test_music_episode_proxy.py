#!/usr/bin/env python3
"""Music episode proxy + retired APK channel — security and streaming checks.

Runs entirely in-process (no server, no network): DNS and the upstream opener
are faked, so the SSRF guard, redirect re-validation, content-type filter and
the size cap are exercised without touching the internet.

    .venv/bin/python tests/test_music_episode_proxy.py
"""

import asyncio
import io
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Importing the routers registers JSON stores — keep them out of backend/data/.
os.environ.setdefault("ETHOS_DATA_DIR", tempfile.mkdtemp(prefix="ethos-music-episode-"))

from fastapi import HTTPException  # noqa: E402

from backend.routers import music as M  # noqa: E402
from backend.routers import updates as U  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class FakeRequest:
    headers: dict[str, str] = {}


class FakeUpstream:
    def __init__(self, ctype="audio/mpeg", chunks=(b"abc", b""), length="3"):
        self.headers = {"Content-Type": ctype, "Content-Length": length}
        self.status = 200
        self._chunks = list(chunks)
        self.closed = False

    def read(self, size=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, error):
        self.error = error

    def open(self, request, timeout=0):
        raise self.error


def expect_422(url: str) -> tuple[bool, str]:
    try:
        M._validate_public_url(url, kind="Adres odcinka")
        return False, "no exception"
    except HTTPException as exc:
        return exc.status_code == 422, f"HTTP {exc.status_code}"


def test_url_guard() -> None:
    print("\n[url guard]")
    for bad in ("ftp://example.com/a.mp3", "file:///etc/passwd", "http://127.0.0.1/x.mp3",
                "http://localhost/x.mp3", "http://[::1]/x.mp3", "http://169.254.169.254/latest/meta-data"):
        ok, detail = expect_422(bad)
        check(f"rejects {bad}", ok, detail)

    original = M.socket.getaddrinfo
    M.socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    try:
        accepted = M._validate_public_url("https://feeds.example.com/a.mp3", kind="Adres odcinka")
        check("accepts a public https host", accepted.startswith("https://feeds.example.com"), accepted)
    finally:
        M.socket.getaddrinfo = original


def test_redirect_guard() -> None:
    print("\n[redirect guard]")
    calls: list[str] = []

    def fake_validate(url, *, kind="RSS"):
        calls.append(url)
        if "169.254.0.1" in url:
            raise HTTPException(status_code=422, detail="blocked")
        return url

    headers = {"Location": "http://169.254.0.1/secret.mp3"}
    error = urllib.error.HTTPError("https://example.com/a.mp3", 302, "Found", headers,
                                   io.BytesIO(b""))
    original_validate = M._validate_public_url
    original_opener = M.urllib.request.build_opener
    M._validate_public_url = fake_validate
    M.urllib.request.build_opener = lambda *a, **k: FakeOpener(error)
    try:
        blocked = False
        try:
            M._open_public_media("https://example.com/a.mp3", None)
        except HTTPException as exc:
            blocked = exc.status_code == 422
        check("a redirect to a private address is rejected", blocked)
        check("every hop is validated", len(calls) == 2, str(calls))
    finally:
        M._validate_public_url = original_validate
        M.urllib.request.build_opener = original_opener


def test_content_type() -> None:
    print("\n[content type]")
    original = M._open_public_media
    M._open_public_media = lambda url, rng: FakeUpstream(ctype="text/html", chunks=(b"<html>", b""))
    try:
        rejected = False
        try:
            asyncio.run(M.episode_audio(FakeRequest(), "https://example.com/a.mp3"))
        except HTTPException as exc:
            rejected = exc.status_code == 502
        check("an HTML error page is not served as audio", rejected)
    finally:
        M._open_public_media = original


def test_streaming_and_cap() -> None:
    print("\n[streaming]")
    original = M._open_public_media
    original_cap = M.EPISODE_AUDIO_MAX_BYTES

    async def collect():
        response = await M.episode_audio(FakeRequest(), "https://example.com/a.mp3")
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        return response, body

    M._open_public_media = lambda url, rng: FakeUpstream(chunks=(b"abc", b"de", b""), length="5")
    try:
        response, body = asyncio.run(collect())
        check("audio passes through untouched", body == b"abcde", repr(body))
        check("content type is forwarded", response.headers.get("content-type") == "audio/mpeg",
              str(response.headers.get("content-type")))
        check("range support is advertised", response.headers.get("accept-ranges") == "bytes")
        check("proxied audio is never cached by the NAS",
              response.headers.get("cache-control") == "no-store")

        M.EPISODE_AUDIO_MAX_BYTES = 4
        _, capped = asyncio.run(collect())
        check("the size cap truncates the body", capped == b"abc", repr(capped))
    finally:
        M.EPISODE_AUDIO_MAX_BYTES = original_cap
        M._open_public_media = original


def test_retired_apk() -> None:
    print("\n[retired APK]")
    for label, call in (("version", U.get_app_version), ("download", U.download_apk)):
        retired = False
        try:
            asyncio.run(call(app="music"))
        except HTTPException as exc:
            retired = exc.status_code == 404 and "web app" in str(exc.detail)
        check(f"music {label} endpoint answers 404 with a pointer", retired)
    check("moments/video stay enabled", set(U.RETIRED_APPS) == {"music"}, str(U.RETIRED_APPS))


def main() -> int:
    test_url_guard()
    test_redirect_guard()
    test_content_type()
    test_streaming_and_cap()
    test_retired_apk()
    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
