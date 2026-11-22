"""Downloads — URL and destination helpers (TODO.md P2-01).

Everything a user (or a provider) can put into a download: scheme validation,
premium-hoster detection, the default file name, the destination that never
overwrites an existing file, and the two guards that keep attacker-controlled
paths and hosts from escaping — ``safe_join`` (torrent metadata paths) and
``is_blocked_ip`` (internal-network proxy protection).

Pure functions only; the router keeps the HTTP error mapping and the admin
exemption for private targets.
"""

import ipaddress
import os
import urllib.parse

#: Only plain web links may enter the engine.
ALLOWED_SCHEMES = {"http", "https"}

#: Links from these hosts are routed through the configured debrid provider;
#: any other http(s) URL is downloaded directly by the built-in engine.
FILE_HOSTERS = {
    "rapidgator.net", "mega.nz", "mediafire.com", "1fichier.com", "uptobox.com",
    "uploadever.com", "filefactory.com", "depositfiles.com", "filer.net",
    "gigapeta.com", "krakenfiles.com", "pixeldrain.com", "1024terabytes.com",
    "turbobit.net", "nowvideo.sx", "doodstream.com", "vidhide.com", "ok.ru",
    "dropbox.com", "we.tl", "sendgb.com", "filemoon.in", "files.to",
}


def is_file_host(url: str) -> bool:
    """True when the URL's host is a known premium file hoster."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    except Exception:  # noqa: BLE001 — malformed URL is simply "not a hoster"
        return False
    if not host:
        return False
    return any(host == known or host.endswith("." + known) for known in FILE_HOSTERS)


def name_from_url(url: str) -> str:
    """Default file name from a URL path (never empty)."""
    try:
        path = urllib.parse.urlparse(url).path
    except Exception:  # noqa: BLE001 — fall back to the raw string
        path = url
    base = os.path.basename(path.split("?")[0].split("#")[0]).strip()
    return base if base and base != "/" else "download.bin"


def validate_url(url: str, allowed_schemes: set[str] | None = None) -> str:
    """Return the trimmed URL or raise ``ValueError`` with a user-facing message."""
    text = (url or "").strip()
    if not text:
        raise ValueError("URL is required")
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme.lower() not in (allowed_schemes or ALLOWED_SCHEMES):
        raise ValueError("Only http:// and https:// URLs are allowed")
    if not parsed.netloc:
        raise ValueError("URL must include a host")
    return text


def is_blocked_ip(raw: str) -> bool:
    """True for loopback/private/link-local/reserved/multicast/unspecified IPs."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def safe_join(base: str, *parts: str) -> str:
    """Join provider-supplied relative paths under `base`, rejecting escapes.

    Torrent metadata (`info.files[].path`) and provider file lists are
    attacker-controlled. ``os.path.join(base, "../../etc/cron.d/x")`` must never
    leave the task's content directory; symlinks are checked too.

    Raises ``ValueError`` on an escape.
    """
    base_abs = os.path.abspath(base)
    dest = os.path.abspath(os.path.join(base_abs, *parts))
    if dest != base_abs and not dest.startswith(base_abs + os.sep):
        raise ValueError("unsafe path in torrent metadata")
    real_base = os.path.realpath(base_abs)
    real_parent = os.path.realpath(os.path.dirname(dest))
    if real_parent != real_base and not real_parent.startswith(real_base + os.sep):
        raise ValueError("unsafe path in torrent metadata (symlink escape)")
    return dest


def unique_dest(download_dir: str, name: str) -> str:
    """Destination that does not overwrite an existing file ("name (1).ext")."""
    target = os.path.join(download_dir, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    counter = 1
    while os.path.exists(os.path.join(download_dir, f"{stem} ({counter}){ext}")):
        counter += 1
    return os.path.join(download_dir, f"{stem} ({counter}){ext}")
