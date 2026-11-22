"""Signed package repositories: fetch, verify, cache and merge catalogs.

Trust chain: Ed25519 signature over the raw index bytes → manifest sha256 →
sha256 of every compose/file artifact. An unsigned source can be added only as
``trust: "unverified"``; a pinned source must carry a public key.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from backend.config import settings
from backend.services import packages_store
from backend.services.package_schema import (
    PackageError,
    require_sha256,
    sha256_bytes,
    validate_index,
    validate_manifest,
)

logger = logging.getLogger("ethos-nas.services.package_repo")

_GITHUB_REPO_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
_SIGNATURE_SUFFIX = ".sig"


# ── Safe HTTP ───────────────────────────────────────────────────────────────

def _is_private_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return True
    return False


def validate_url(url: str, *, allow_private: Optional[bool] = None) -> str:
    """Validate scheme and host; block SSRF targets unless explicitly allowed."""
    url = str(url or "").strip()
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        pass
    elif parsed.scheme == "http" and settings.PACKAGES_ALLOW_INSECURE:
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise PackageError("Insecure HTTP is only allowed for localhost test repos", 400)
    else:
        raise PackageError("Package URLs must use HTTPS", 400)
    if not parsed.hostname:
        raise PackageError("Package URL has no host", 400)
    private_ok = settings.PACKAGES_ALLOW_PRIVATE if allow_private is None else allow_private
    if not private_ok and _is_private_host(parsed.hostname):
        raise PackageError(f"Refusing to fetch from a private address: {parsed.hostname}", 400)
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get_bytes(url: str, max_bytes: int, *, allow_private: Optional[bool] = None) -> bytes:
    """Fetch a URL with a hard size cap and redirect re-validation."""
    validate_url(url, allow_private=allow_private)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(url, timeout=20) as response:
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        raise PackageError(f"Repository request failed (HTTP {exc.code})", 404) from None
    except Exception as exc:
        logger.warning("Package fetch failed for %s: %s", url, exc)
        raise PackageError(f"Failed to fetch {url}: {exc}", 502) from None
    if len(data) > max_bytes:
        raise PackageError(f"Artifact exceeds the {max_bytes} byte limit: {url}", 400)
    return data


# ── Signatures ──────────────────────────────────────────────────────────────

def verify_signature(payload: bytes, signature_b64: str, public_key_b64: str) -> None:
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public = base64.b64decode(public_key_b64, validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(signature, payload)
    except (ValueError, InvalidSignature) as exc:
        raise PackageError("Repository signature is invalid", 502) from exc


def key_fingerprint(public_key_b64: str) -> str:
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except ValueError:
        return ""
    return sha256_bytes(raw)[:16]


# ── Source shape ────────────────────────────────────────────────────────────

def resolve_index_url(source_url: str) -> str:
    source_url = str(source_url or "").strip()
    match = _GITHUB_REPO_RE.match(source_url)
    if match:
        return f"https://raw.githubusercontent.com/{match.group(1)}/{match.group(2)}/HEAD/index.json"
    if source_url.endswith(".json"):
        validate_url(source_url)
        return source_url
    raise PackageError(
        "Source must be a GitHub repository URL (https://github.com/<owner>/<repo>) "
        "or an index .json URL", 400,
    )


def _normalize_source(raw: dict) -> dict:
    url = str(raw.get("url") or "").strip()
    resolve_index_url(url)
    trust = str(raw.get("trust") or "unverified").lower()
    if trust not in ("pinned", "unverified"):
        raise PackageError("trust must be 'pinned' or 'unverified'", 400)
    public_key = str(raw.get("public_key") or "").strip()
    if trust == "pinned" and not public_key:
        raise PackageError("A pinned source requires a public key", 400)
    if public_key:
        try:
            base64.b64decode(public_key, validate=True)
        except ValueError as exc:
            raise PackageError("public_key must be base64", 400) from exc
    return {
        "url": url,
        "name": str(raw.get("name") or url).strip()[:120],
        "public_key": public_key,
        "key_id": str(raw.get("key_id") or key_fingerprint(public_key)).strip()[:80],
        "trust": trust,
        "enabled": bool(raw.get("enabled", True)),
    }


# ── Public source API ───────────────────────────────────────────────────────

def list_sources() -> list[dict]:
    return packages_store.load_sources()


def add_source(url: str, name: str = "", public_key: str = "",
               trust: str = "unverified", enabled: bool = True) -> list[dict]:
    source = _normalize_source({
        "url": url, "name": name, "public_key": public_key,
        "trust": trust, "enabled": enabled,
    })
    sources = [s for s in packages_store.load_sources() if s.get("url") != source["url"]]
    sources.append(source)
    packages_store.save_sources(sources)
    logger.info("Added package source %s (trust=%s)", source["url"], source["trust"])
    return sources


def update_source(index: int, **fields) -> list[dict]:
    sources = packages_store.load_sources()
    if index < 0 or index >= len(sources):
        raise PackageError("Source index out of range", 404)
    merged = {**sources[index], **{k: v for k, v in fields.items() if v is not None}}
    sources[index] = _normalize_source(merged)
    packages_store.save_sources(sources)
    return sources


def remove_source(index: int) -> dict:
    sources = packages_store.load_sources()
    if index < 0 or index >= len(sources):
        raise PackageError("Source index out of range", 404)
    removed = sources.pop(index)
    packages_store.save_sources(sources)
    logger.info("Removed package source %s", removed.get("url"))
    return removed


# ── Fetch / verify index ────────────────────────────────────────────────────

def _fetch_index(source: dict) -> dict:
    index_url = resolve_index_url(source["url"])
    payload = get_bytes(index_url, settings.PACKAGES_MAX_INDEX_BYTES)
    if source.get("trust") == "pinned":
        if not source.get("public_key"):
            raise PackageError("Pinned source has no public key", 400)
        signature_url = index_url + _SIGNATURE_SUFFIX
        signature = get_bytes(signature_url, 4096).decode("ascii", errors="ignore").strip()
        verify_signature(payload, signature, source["public_key"])
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackageError("Repository index is not valid JSON", 502) from exc
    index = validate_index(raw)
    return {
        "index": index,
        "index_sha256": sha256_bytes(payload),
        "fetched_at": time.time(),
    }


async def _get_index(source: dict, force: bool = False) -> dict:
    cache = packages_store.load_cache()
    entry = cache.get(source["url"]) or {}
    fresh = (time.time() - float(entry.get("fetched_at") or 0)) < settings.PACKAGES_TTL_SECONDS
    if entry and fresh and not force:
        return entry
    try:
        entry = await asyncio.to_thread(_fetch_index, source)
    except PackageError:
        if entry.get("packages"):
            logger.warning("Source %s failed; serving stale cache", source["url"])
            return {**entry, "stale": True}
        raise
    cache[source["url"]] = entry
    packages_store.save_cache(cache)
    return entry


# ── Browse / detail / manifest ──────────────────────────────────────────────

async def browse(force: bool = False) -> dict:
    sources = [s for s in packages_store.load_sources() if s.get("enabled", True)]
    errors = []
    merged: dict[str, dict] = {}
    for source in sources:
        try:
            entry = await _get_index(source, force=force)
        except PackageError as exc:
            errors.append({"source": source["url"], "error": exc.detail})
            continue
        for package in entry["index"]["packages"]:
            package_id = package["id"]
            if package_id in merged:
                continue
            merged[package_id] = {
                **package,
                "source": source["url"],
                "source_name": source.get("name") or source["url"],
                "trust": source.get("trust", "unverified"),
                "key_id": source.get("key_id", ""),
                "stale": bool(entry.get("stale")),
            }

    installed = {p.get("id"): p for p in packages_store.load_installed()}
    from backend.services.package_schema import version_gt
    packages = []
    for package in merged.values():
        record = installed.get(package["id"])
        item = {**package}
        if record:
            # The record carries the *effective* values: `ui.port` /
            # `healthcheck.port` are the published ones (a port taken at install
            # time is re-mapped), plus the runtime and the remap note.
            for key in ("ui", "healthcheck", "runtime", "port_remap"):
                if key in record:
                    item[key] = record[key]
        item["installed"] = bool(record)
        item["installed_version"] = record.get("version", "") if record else ""
        item["update_available"] = bool(
            record and version_gt(str(package.get("version", "")), str(record.get("version", "")))
        )
        packages.append(item)
    return {"sources": [s["url"] for s in sources], "packages": packages, "errors": errors}


async def get_catalog_entry(package_id: str) -> dict:
    catalog = await browse()
    for package in catalog["packages"]:
        if package["id"] == package_id:
            return package
    raise PackageError(f"Package {package_id!r} not found in any source", 404)


async def fetch_manifest(package: dict) -> dict:
    source = next((s for s in packages_store.load_sources() if s.get("url") == package.get("source")), None)
    if not source:
        raise PackageError("Package source is no longer configured", 409)
    manifest_url = package.get("manifest_url")
    payload = await asyncio.to_thread(get_bytes, manifest_url, settings.PACKAGES_MAX_MANIFEST_BYTES)
    expected = package.get("manifest_sha256")
    if expected:
        if sha256_bytes(payload) != require_sha256(expected, "manifest_sha256"):
            raise PackageError("Manifest checksum does not match the signed index", 502)
    elif source.get("trust") == "pinned":
        raise PackageError("Pinned packages require manifest_sha256 in the index", 502)
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackageError("Manifest is not valid JSON", 502) from exc
    manifest = validate_manifest(raw, allow_legacy=source.get("trust") != "pinned")
    if manifest["id"] != package["id"]:
        raise PackageError("Manifest id does not match the index entry", 502)
    ui = manifest.get("ui") or {}
    if ui.get("mode") == "native" and source.get("trust") != "pinned":
        raise PackageError(
            f"Native package UI requires a pinned, signed source — edit "
            f"'{source.get('name') or source.get('url')}', set Trust to Signed, "
            f"and paste its public key", 403,
        )
    if manifest.get("user_access") and source.get("trust") != "pinned":
        raise PackageError(
            "Packages that signed-in users can reach require a pinned, signed source", 403,
        )
    return manifest


def download_verified(url: str, destination, expected_sha256: str, *, max_bytes: Optional[int] = None) -> None:
    data = get_bytes(url, max_bytes or settings.PACKAGES_MAX_ARTIFACT_BYTES)
    if sha256_bytes(data) != require_sha256(expected_sha256, "artifact sha256"):
        raise PackageError(f"Checksum mismatch for {url}", 502)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def download_to_file(url: str, destination, *, max_bytes: int,
                     expected_sha256: str = "", progress=None) -> dict:
    """Stream a large artifact to disk (OS images are far too big for get_bytes).

    Same HTTPS/private-host/redirect guards as get_bytes, plus a hard size cap,
    streaming sha256 and an optional progress callback (bytes_done, total).
    The final file only appears after the checksum matches.
    """
    validate_url(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    written = 0
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        with opener.open(url, timeout=60) as response, open(tmp, "wb") as fh:
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise PackageError(f"Artifact exceeds the {max_bytes} byte limit: {url}", 400)
                fh.write(chunk)
                digest.update(chunk)
                if progress is not None:
                    try:
                        progress(written, total)
                    except Exception:
                        logger.debug("Download progress callback failed", exc_info=True)
            fh.flush()
            os.fsync(fh.fileno())
        if expected_sha256 and digest.hexdigest() != require_sha256(expected_sha256, "artifact sha256"):
            raise PackageError(f"Checksum mismatch for {url}", 502)
        os.replace(tmp, destination)
        return {"sha256": digest.hexdigest(), "size_bytes": written}
    except PackageError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        logger.warning("Artifact download failed for %s: %s", url, exc)
        raise PackageError(f"Failed to fetch {url}: {exc}", 502) from None
