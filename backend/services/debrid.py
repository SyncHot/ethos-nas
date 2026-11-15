"""Debrid provider clients (Real-Debrid, AllDebrid, TorBox).

Extracted from `backend/routers/downloads.py` (TODO.md P2-01). Every function is
blocking and runs in the caller's worker thread; the provider-agnostic
`add_magnet`/`torrent_status`/... dispatchers select the right client by name.

The shared `_http_json` adds retries for 429/5xx (honouring `Retry-After`) and
rate-limits calls per provider host so a burst cannot trip their quota.
"""

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("ethos-nas.services.debrid")

_PROVIDER_MIN_INTERVAL = {
    "api.real-debrid.com": 0.25,
    "api.alldebrid.com": 0.20,
    "api.torbox.app": 0.20,
}
_provider_rate_lock = threading.Lock()
_provider_next_call: dict = {}


class ProviderHTTPError(RuntimeError):
    """A provider answered with a non-retryable HTTP error.

    `status` and `detail` let callers tell "your key is wrong" (401/403) apart
    from "the provider is down" (5xx/network), which the old blanket RuntimeError
    could not. `detail` is the provider's own message, already trimmed.
    """

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = int(status)
        self.detail = (detail or "").strip()
        super().__init__(f"HTTP {self.status} from provider API: {self.detail or 'no detail'}")


def _provider_error_detail(raw: str) -> str:
    """Pull the human-readable message out of a provider error body.

    Real-Debrid: {\"error\": \"bad_token\", \"error_code\": 8}
    AllDebrid/TorBox: {\"error\": {\"message\": ...}} / {\"detail\": ...}
    Anything else falls back to the raw (already truncated) body.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return raw.strip()
    if not isinstance(data, dict):
        return raw.strip()
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("code") or "")
    detail = data.get("detail") or data.get("message")
    if isinstance(err, str) and err:
        code = data.get("error_code")
        label = f"{err} (code {code})" if code not in (None, "") else err
        # TorBox answers {error: "BAD_TOKEN", detail: "Your token is invalid…"}.
        if isinstance(detail, str) and detail.strip():
            return f"{label}: {detail.strip()}"
        return label
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return raw.strip()


def _throttle_provider(url: str) -> None:
    """Serialise calls per provider host so a burst cannot trip a 429."""
    host = urllib.parse.urlparse(url).hostname or ""
    interval = _PROVIDER_MIN_INTERVAL.get(host, 0.0)
    if not interval:
        return
    with _provider_rate_lock:
        now = time.time()
        wait = max(0.0, _provider_next_call.get(host, 0.0) - now)
        _provider_next_call[host] = now + wait + interval
    if wait > 0:
        time.sleep(wait)


def _http_json(url: str, method: str = "GET", headers: dict = None, data: bytes = None,
               timeout: int = 30, retries: int = 3):
    """Small stdlib HTTP JSON helper for debrid provider APIs (runs in worker threads).

    Retries 429/5xx with exponential backoff (honouring `Retry-After`) instead of
    failing the whole task on one transient hiccup.
    """
    for attempt in range(retries + 1):
        _throttle_provider(url)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "EthOS-NAS-DownloadStation/2.0")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            return json.loads(body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass  # best-effort cleanup — nothing to report
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = min(30.0, max(1.0, float(retry_after))) if retry_after else min(30.0, 2.0 ** attempt)
                except (TypeError, ValueError):
                    delay = min(30.0, 2.0 ** attempt)
                logger.warning("Provider HTTP %s — retrying in %.1fs (%d/%d)",
                               e.code, delay, attempt + 1, retries)
                time.sleep(delay)
                continue
            raise ProviderHTTPError(e.code, _provider_error_detail(detail)) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                delay = min(30.0, 2.0 ** attempt)
                logger.warning("Provider network error (%s) — retrying in %.1fs (%d/%d)",
                               e, delay, attempt + 1, retries)
                time.sleep(delay)
                continue
            raise RuntimeError(f"Provider is unreachable: {e}") from e
    raise RuntimeError("Provider request failed after retries")


# ── Real-Debrid (https://api.real-debrid.com/rest/1.0) ───────────────────────

def _rd_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "User-Agent": "EthOS-NAS-DownloadStation/2.0"}


def _rd_add_magnet(token: str, magnet: str) -> int:
    data = urllib.parse.urlencode({"magnet": magnet}).encode()
    res = _http_json("https://api.real-debrid.com/rest/1.0/torrents/addMagnet", "POST", _rd_headers(token), data)
    return int(res["id"])


def _rd_torrent_status(token: str, tid: int) -> dict:
    """Returns {progress:int 0-100, status:str, files:[{id,name,size,path,is_selected}]}.

    RD statuses: magnet_upload|queued|downloading|waiting_files_selection|
    downloading|completed|error|dead|virus|compressing|uploaded.
    """
    res = _http_json(f"https://api.real-debrid.com/rest/1.0/torrents/info/{tid}", "GET", _rd_headers(token))
    files = []
    for f in (res.get("files") or []):
        try:
            files.append({
                "id": int(f["id"]),
                "name": f.get("filename") or os.path.basename(f.get("path", "")),
                "size": int(f.get("bytes") or 0),
                "path": f.get("path", ""),
                "is_selected": bool(f.get("selected")),
            })
        except (KeyError, TypeError, ValueError):
            continue
    # `/torrents/info` is the only source of download URLs: `links[]` holds one
    # unrestricted URL per *selected* file, in `files[]` order. Attach them now
    # so phase 3 never calls the non-existent `/torrents/instantDownload/{id}`.
    link_iter = iter([str(x) for x in (res.get("links") or []) if x])
    for f in files:
        if not f.get("is_selected"):
            continue
        try:
            f["_link_url"] = next(link_iter)
        except StopIteration:
            break
    return {
        "progress": int(res.get("progress") or 0),
        "status": str(res.get("status") or "").lower(),
        "name": str(res.get("filename") or ""),
        "files": files,
    }


def _rd_select_files(token: str, tid: int, file_ids: list) -> None:
    # Real-Debrid expects `files=`, not `ids=`.
    data = urllib.parse.urlencode({"files": ",".join(str(i) for i in file_ids)}).encode()
    _http_json(f"https://api.real-debrid.com/rest/1.0/torrents/selectFiles/{tid}", "POST", _rd_headers(token), data)


def _rd_delete_torrent(token: str, tid: int) -> None:
    try:
        _http_json(f"https://api.real-debrid.com/rest/1.0/torrents/delete/{tid}", "DELETE", _rd_headers(token))
    except Exception as exc:
        logger.warning("Real-Debrid delete torrent %s failed: %s", tid, exc)


def _rd_verify_token(token: str) -> bool:
    # Exceptions propagate so /verify can report an outage as 502, not "bad key".
    res = _http_json("https://api.real-debrid.com/rest/1.0/user", "GET", _rd_headers(token))
    return bool(res.get("id"))


# ── AllDebrid (https://api.alldebrid.com/v4) ─────────────────────────────────

def _ad_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "User-Agent": "EthOS-NAS-DownloadStation/2.0"}


def _ad_add_magnet(key: str, magnet: str) -> int:
    # Response: {status, data:{magnets:[{id,hash,name,size,ready} | {error:{code,message}}]}}
    data = urllib.parse.urlencode({"magnets[]": magnet}).encode()
    res = _http_json("https://api.alldebrid.com/v4/magnet/upload", "POST", _ad_headers(key), data)
    if str(res.get("status") or "").lower() == "error":
        err = res.get("error") or {}
        raise RuntimeError(f"AllDebrid: {err.get('message') or err.get('code')}")
    magnets = (res.get("data") or {}).get("magnets") or []
    if not magnets:
        raise RuntimeError("AllDebrid returned no result for this magnet")
    entry = magnets[0]
    err = entry.get("error")
    if isinstance(err, dict):
        raise RuntimeError(f"AllDebrid: {err.get('message') or err.get('code')}")
    try:
        return int(entry["id"])
    except (KeyError, TypeError, ValueError):
        # Surface the raw entry so provider-side oddities are diagnosable.
        raise RuntimeError(f"AllDebrid returned no magnet id (entry={str(entry)[:200]})") from None


# AllDebrid magnet statusCode → engine status (docs: /v4.1/magnet/status).
_AD_STATUS_CODES = {0: "queued", 1: "downloading", 2: "compressing", 3: "uploading", 4: "completed"}


def _ad_torrent_status(key: str, mid: int) -> dict:
    """Returns {progress:int 0-100, status:str, name:str, size:int, files:[], detail:str}.

    Uses the current /v4.1/magnet/status endpoint (POST, `id` param). AD has no
    file-selection state — every file of a torrent is cached automatically; the
    per-file list + links come from ad_magnet_files() once ready.
    statusCode: 0 queued | 1 downloading | 2 compressing/moving | 3 uploading |
    4 ready | 5+ error (human message in `status`).
    """
    data = urllib.parse.urlencode({"id": str(mid)}).encode()
    res = _http_json("https://api.alldebrid.com/v4.1/magnet/status", "POST", _ad_headers(key), data)
    if str(res.get("status") or "").lower() == "error":
        err = res.get("error") or {}
        raise RuntimeError(f"AllDebrid: {err.get('message') or err.get('code')}")
    d = (res.get("data") or {}).get("magnets") or {}
    if isinstance(d, list):  # some responses wrap a single magnet in an array
        m = next((x for x in d if int(x.get("id") or 0) == mid), {})
    else:
        m = d
    try:
        code = int(m.get("statusCode"))
    except (TypeError, ValueError):
        code = -1
    size = int(m.get("size") or 0)
    downloaded = int(m.get("downloaded") or 0)
    if code == 4 and size > 0:
        downloaded = max(downloaded, size)  # Ready magnets don't report `downloaded`
    if code == 4:
        progress = 100
    elif size > 0:
        progress = max(0, min(99, int(downloaded * 100 / size)))
    else:
        progress = 0
    return {
        "progress": progress,
        "status": _AD_STATUS_CODES.get(code, "error"),
        "name": str(m.get("filename") or ""),
        "size": size,
        "files": [],
        "detail": str(m.get("status") or "") if code not in _AD_STATUS_CODES else "",
        # Provider-side transfer bytes/speed (reported while the magnet is being
        # cached on AllDebrid) so the UI can show live progress for that phase.
        "downloaded": downloaded,
        "speed": int(m.get("downloadSpeed") or 0),
    }


def _ad_select_files(key: str, mid: int, file_ids: list) -> None:
    # AllDebrid has no file-selection API — the provider caches every file of
    # the torrent. Selection in EthOS is applied locally (which files get pulled
    # to the NAS), so there is nothing to tell the provider.
    return None


def _ad_magnet_files(key: str, mid: int) -> list:
    """POST /v4/magnet/files → flat [{id,name,size,path,_link_url}] for a ready magnet.

    The endpoint takes MAGNET ids (not file ids) and returns the torrent's folder
    tree: file nodes are {n:name, s:size, l:link}, folder nodes are {n:name,
    e:[children]}. Links are alldebrid.com/f/… URLs — unlock them with
    ad_unlock() before downloading.
    """
    data = urllib.parse.urlencode({"id[]": str(mid)}).encode()
    res = _http_json("https://api.alldebrid.com/v4/magnet/files", "POST", _ad_headers(key), data)
    magnets = (res.get("data") or {}).get("magnets") or []
    entry = next((x for x in magnets if str(x.get("id")) == str(mid)), {})
    err = entry.get("error")
    if isinstance(err, dict):
        raise RuntimeError(f"AllDebrid: {err.get('message') or err.get('code')}")
    out = []

    def _walk(nodes, prefix):
        for n in nodes or []:
            name = str(n.get("n") or "").strip("/")
            if not name:
                continue
            rel = f"{prefix}/{name}" if prefix else name
            if isinstance(n.get("e"), list):  # folder node
                _walk(n["e"], rel)
            elif n.get("l"):                 # file node with a link
                out.append({
                    "id": int(hashlib.md5(rel.encode("utf-8")).hexdigest()[:12], 16),
                    "name": os.path.basename(rel) or "file.bin",
                    "size": int(n.get("s") or 0),
                    "path": rel,
                    "is_selected": True,
                    "_link_url": str(n["l"]),
                })

    _walk(entry.get("files"), "")
    return out


def _ad_delete_torrent(key: str, mid: int) -> None:
    try:
        data = urllib.parse.urlencode({"id": str(mid)}).encode()
        _http_json("https://api.alldebrid.com/v4/magnet/delete", "POST", _ad_headers(key), data)
    except Exception as exc:
        logger.warning("AllDebrid delete magnet %s failed: %s", mid, exc)


def _ad_verify_token(key: str) -> bool:
    res = _http_json("https://api.alldebrid.com/v4/user", "GET", _ad_headers(key))
    if str(res.get("status") or "").lower() == "error":
        # AllDebrid answers 200 with an error object for a bad key — turn it into
        # the same 401 path the other providers use so /verify shows the reason.
        err = res.get("error") or {}
        message = str(err.get("message") or err.get("code") or "invalid API key") if isinstance(err, dict) else str(err)
        raise ProviderHTTPError(401, message)
    data = res.get("data") or {}
    return bool(data.get("user"))


def _ad_link_info(key: str, url: str) -> dict:
    """POST /v4/link/infos — {name,size,host} for a file-hosting link.

    Raises RuntimeError with the provider's message when the host is not
    supported or the link is down/password-protected.
    """
    data = urllib.parse.urlencode({"link[]": url}).encode()
    res = _http_json("https://api.alldebrid.com/v4/link/infos", "POST", _ad_headers(key), data)
    infos = (res.get("data") or {}).get("infos") or []
    if not infos:
        raise RuntimeError("AllDebrid returned no info for this link")
    info = infos[0]
    err = info.get("error")
    if isinstance(err, dict):
        raise RuntimeError(f"AllDebrid: {err.get('message') or err.get('code')}")
    return {
        "name": str(info.get("filename") or ""),
        "size": int(info.get("size") or 0),
        "host": str(info.get("hostDomain") or info.get("host") or ""),
    }


def _ad_unlock(key: str, url: str, stop=None) -> dict:
    """POST /v4/link/unlock → direct download URL (polls /link/delayed if needed).

    Returns {url,name,size}. Raises RuntimeError on provider errors.
    `stop` is an optional threading.Event checked while polling delayed links.
    """
    data = urllib.parse.urlencode({"link": url}).encode()
    res = _http_json("https://api.alldebrid.com/v4/link/unlock", "POST", _ad_headers(key), data)
    d = res.get("data") or {}
    err = d.get("error")
    if isinstance(err, dict):
        raise RuntimeError(f"AllDebrid: {err.get('message') or err.get('code')}")
    link = str(d.get("link") or "")
    name = os.path.basename(str(d.get("filename") or ""))
    size = int(d.get("filesize") or 0)
    if not link and d.get("id"):
        # Delayed link — poll until the provider has generated it (~5s cadence).
        did = int(d["id"])
        for _ in range(120):  # up to ~6 minutes
            time.sleep(3)
            if stop is not None and stop.is_set():
                return {"url": "", "name": name, "size": size}
            dd_res = _http_json("https://api.alldebrid.com/v4/link/delayed", "POST",
                                _ad_headers(key), urllib.parse.urlencode({"id": did}).encode())
            dd = dd_res.get("data") or {}
            st = int(dd.get("status") or 0)
            if st == 2:
                link = str(dd.get("link") or "")
                break
            if st == 3:
                raise RuntimeError("AllDebrid could not generate a download link for this file")
        else:
            raise RuntimeError("Timed out waiting for AllDebrid to prepare the download link")
    if not link:
        raise RuntimeError("AllDebrid returned no download link")
    return {"url": link, "name": name, "size": size}


# ── TorBox (https://api.torbox.app/v1/api) ───────────────────────────────────

def _tb_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "User-Agent": "EthOS-NAS-DownloadStation/2.0"}


def _tb_add_magnet(key: str, magnet: str) -> int:
    data = urllib.parse.urlencode({"magnet": magnet}).encode()
    res = _http_json("https://api.torbox.app/v1/api/torrents/createtorrent", "POST", _tb_headers(key), data)
    if not res.get("success"):
        raise RuntimeError(res.get("detail") or "TorBox createtorrent failed")
    d = res.get("data") or {}
    return int(d.get("id") or 0)


def _tb_torrent_status(key: str, tid: int) -> dict:
    """Returns {progress:int 0-100, status:str, files:[{id,name,size,path,is_selected}]}.

    TorBox downloads ALL files (no file selection). Statuses include:
    downloading|cached|completed|paused|metaDL|stalled (no seeds)|error.
    """
    res = _http_json(f"https://api.torbox.app/v1/api/torrents/mylist?id={tid}", "GET", _tb_headers(key))
    if not res.get("success"):
        raise RuntimeError(res.get("detail") or "TorBox mylist failed")
    items = res.get("data") or []
    if isinstance(items, list):
        t = items[0] if items else {}
    elif isinstance(items, dict):
        t = items
    else:
        t = {}
    files = []
    for f in (t.get("files") or []):
        try:
            files.append({
                "id": int(f["id"]),
                "name": f.get("name") or os.path.basename(str(f.get("path", ""))),
                "size": int(f.get("size") or 0),
                "path": str(f.get("path", "")),
                "is_selected": True,  # TorBox always downloads everything
            })
        except (KeyError, TypeError, ValueError):
            continue
    prog = t.get("progress")
    try:
        progress = int(float(prog)) if prog is not None else 0
    except (TypeError, ValueError):
        progress = 100 if str(t.get("status", "")).lower() in ("completed", "cached") else 0
    return {
        "progress": max(0, min(100, progress)),
        "status": str(t.get("status") or "").lower(),
        "files": files,
    }


def _tb_select_files(key: str, tid: int, file_ids: list) -> None:
    # TorBox has no file selection — torrents download all files. No-op.
    return None


def _tb_download_link(key: str, fid: int, tid: int = 0) -> dict:
    """Returns {url, name} for a cached file (link valid ~3h)."""
    url = f"https://api.torbox.app/v1/api/torrents/requestdl?token={urllib.parse.quote(key)}&torrent_id={tid}&file_id={fid}"
    res = _http_json(url, "GET", _tb_headers(key))
    if not res.get("success"):
        raise RuntimeError(res.get("detail") or "TorBox requestdl failed")
    d = res.get("data") or {}
    return {"url": str(d.get("link") or ""), "name": os.path.basename(str(d.get("filename") or ""))}


def _tb_delete_torrent(key: str, tid: int) -> None:
    try:
        data = urllib.parse.urlencode({"id": str(tid), "operation": "delete"}).encode()
        _http_json("https://api.torbox.app/v1/api/torrents/controltorrent", "POST", _tb_headers(key), data)
    except Exception as exc:
        logger.warning("TorBox delete torrent %s failed: %s", tid, exc)


def _tb_verify_token(key: str) -> bool:
    # TorBox has no /user/info (that path 404s); the account endpoint is /user/me.
    res = _http_json("https://api.torbox.app/v1/api/user/me", "GET", _tb_headers(key))
    return bool(res.get("success"))


# ── Provider-agnostic dispatch ───────────────────────────────────────────────

def add_magnet(provider: str, key: str, magnet: str) -> int:
    if provider == "realdebrid":
        return _rd_add_magnet(key, magnet)
    if provider == "alldebrid":
        return _ad_add_magnet(key, magnet)
    if provider == "torbox":
        return _tb_add_magnet(key, magnet)
    raise RuntimeError(f"Unknown debrid provider: {provider!r}")


def torrent_status(provider: str, key: str, tid: int) -> dict:
    if provider == "realdebrid":
        return _rd_torrent_status(key, tid)
    if provider == "alldebrid":
        return _ad_torrent_status(key, tid)
    if provider == "torbox":
        return _tb_torrent_status(key, tid)
    raise RuntimeError(f"Unknown debrid provider: {provider!r}")


def select_files(provider: str, key: str, tid: int, file_ids: list) -> None:
    if provider == "realdebrid":
        return _rd_select_files(key, tid, file_ids)
    if provider == "alldebrid":
        return _ad_select_files(key, tid, file_ids)
    if provider == "torbox":
        return _tb_select_files(key, tid, file_ids)  # no-op: TorBox downloads all files
    raise RuntimeError(f"Unknown debrid provider: {provider!r}")


def download_link(provider: str, key: str, fid: int, tid: int = 0) -> dict:
    if provider == "torbox":
        return _tb_download_link(key, fid, tid)
    # Real-Debrid URLs come from `links[]` in /torrents/info (attached to each
    # file as `_link_url`); AllDebrid links are resolved from the file page.
    raise RuntimeError(f"No per-file link API for debrid provider: {provider!r}")


def delete_torrent(provider: str, key: str, tid: int) -> None:
    if provider == "realdebrid":
        return _rd_delete_torrent(key, tid)
    if provider == "alldebrid":
        return _ad_delete_torrent(key, tid)
    if provider == "torbox":
        return _tb_delete_torrent(key, tid)
    raise RuntimeError(f"Unknown debrid provider: {provider!r}")


def verify(provider: str, key: str) -> bool:
    if provider == "realdebrid":
        return _rd_verify_token(key)
    if provider == "alldebrid":
        return _ad_verify_token(key)
    if provider == "torbox":
        return _tb_verify_token(key)
    return False


def ad_link_info(key: str, url: str) -> dict:
    return _ad_link_info(key, url)


def ad_unlock(key: str, url: str, stop=None) -> dict:
    return _ad_unlock(key, url, stop)


def ad_magnet_files(key: str, mid: int) -> list:
    return _ad_magnet_files(key, mid)
