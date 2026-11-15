"""EthOS NAS — TMDB metadata service for Videos (Phase 6).

Display-only enrichment via The Movie Database (https://www.themoviedb.org):
search movies by parsed title (+ year) and series by show name, then download
poster/backdrop artwork to local disk so the UI never hotlinks TMDB's CDN.

Stdlib `urllib` only — no new dependencies. All functions are synchronous;
callers run them via `asyncio.to_thread()`. Nothing on disk is ever renamed or
moved: TMDB data is display metadata attached to library items by file path.
"""

import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ethos-nas.services.tmdb_service")

API_BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p"
USER_AGENT = "ethos-nas-video-station/1.0"

# Be polite to the free tier (~10 req/s): minimum gap between outbound calls.
MIN_REQUEST_INTERVAL = 0.15
_last_request_at = 0.0

# Folder-derived show names this generic are never sent to TMDB — a folder
# called "TV" would match some random show and slap its poster on everything.
GENERIC_SHOW_NAMES = {
    "tv", "series", "show", "shows", "anime", "movies", "movie", "film",
    "films", "video", "videos", "media", "watch", "downloads", "download",
    "torrents", "torrent", "home", "root", "misc", "other",
}

# Tokens that leak into parsed titles from release names but never appear in a
# real TMDB title: resolution/source/codec tags, streaming sources and the most
# common release groups. Stripped before searching — this is what makes messy
# scene-style filenames ("Interstellar.2014.2160p.WEB-DL.DDP5.1.x265-GTBA")
# match at all.
NOISE_TOKENS = {
    # resolution / quality
    "1080p", "720p", "480p", "2160p", "4k", "uhd", "hd", "sd", "10bit", "8bit",
    # source / container / codec
    "web", "dl", "bluray", "blu-ray", "bdrip", "brrip", "webrip", "hdtv", "dvd",
    "x264", "x265", "h264", "h265", "hevc", "avc", "aac", "ac3", "eac3", "ddp5.1",
    "ddp", "dts", "flac", "hdr", "hdr10", "sdr", "dv", "dolby", "vision", "atmos",
    # streaming sources
    "nf", "amzn", "hmax", "prime", "disney", "dsny", "crint", "wbd", "mxf",
    "peacock", "plutotv", "tubitv",

    # capture/source tags (kept in sync with _JUNK_TOKENS in video_metadata)
    "cam", "cams", "telescync", "telesync", "hdrip", "screener", "scr",
    "pdtv",

    # language / subtitle tags from scene + Polish release names — these are
    # what turned "Evil Dead Burn (2026) iTA EnG Sub iTA EnG" into a failed
    # search (and therefore a missing poster)
    "ita", "eng", "sub", "subs", "subbed", "hardsub", "hsub", "dub", "dubbed",
    "dubbing", "lektor", "napisy", "nap", "pl", "pldub", "plsub", "multi",
    "dual", "audio",

    # release groups + scene tags (most common)
    "group", "yify", "rarbg", "rarbt", "afk", "ntb", "crw", "fht", "glhf",
    "t64m", "sic", "dhd", "ev0", "odessey", "seed4u", "monolith", "sva", "xwt",
    "bone", "pig", "gaz", "gtba", "amiable", "flux", "hybrid", "legion",
    "remastered", "repack", "internal", "fanres", "proper", "rerip",
}

def extract_imdb_id(filename: str) -> str | None:
    """IMDb id embedded in a filename ("Movie (2020) [tt1234567].mkv") — the
    exact-match key Radarr/Jellyfin use to skip fuzzy search entirely."""
    m = re.search(r"\b(tt\d{7})\b", filename or "")
    return m.group(1) if m else None

def _clean_query(q: str, kind: str) -> str:
    """Strip release-name noise from a parsed title before searching TMDB.

    Movies: drop NOISE_TOKENS anywhere + a trailing bare year (the year is sent
    as a separate search parameter). Shows: same, plus stray SxxExx tokens and
    a trailing bare year. Falls back to the raw query when cleaning empties it.
    """
    raw = re.sub(r"\s+", " ", q).strip()
    if not raw:
        return ""
    kept = []
    for t in re.split(r"[\s._:\-]+", raw):
        if not t or t.lower() in NOISE_TOKENS:
            continue
        kept.append(t)
    out = " ".join(kept).strip(" .")
    if kind == "tv":
        out = re.sub(r"\bS?\d{1,2}E\d{1,3}\b", " ", out)   # stray SxxExx in folder names
    m = re.search(r"\s(\d{4})$", out)
    if m and len(out) > 5:                                   # trailing bare year ("My Show 2020")
        out = out[:m.start()].strip()
    return re.sub(r"\s+", " ", out).strip(" .") or raw


class TmdbError(Exception):
    """Network/HTTP/API failure — callers degrade to filename-only display."""


def _throttle() -> None:
    global _last_request_at
    wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _http_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as exc:
        raise TmdbError(f"TMDB request failed: {exc}") from exc


def _http_download(url: str, dest: Path, timeout: int = 20) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if not data:
            raise RuntimeError("empty image")
        dest.write_bytes(data)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise TmdbError(f"image download failed: {exc}") from exc


def _year_from_date(date_str: Optional[str]) -> Optional[int]:
    m = re.match(r"^(\d{4})", date_str or "")
    return int(m.group(1)) if m else None


def _normalize(result: dict, kind: str) -> dict:
    """Reduce a TMDB search hit to the fields the UI + match cache need."""
    if not isinstance(result, dict):   # picker declined (no confident match)
        return None
    title = (result.get("title") or result.get("name") or "").strip()
    date_field = "release_date" if kind == "movie" else "first_air_date"
    return {
        "kind": kind,
        "tmdb_id": result.get("id"),
        "title": title,
        "year": _year_from_date(result.get(date_field)),
        "rating": round(float(result["vote_average"]), 1) if result.get("vote_average") else None,
        "overview": (result.get("overview") or "").strip()[:600],
        "poster_path": result.get("poster_path"),
        "backdrop_path": result.get("backdrop_path"),
    }


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

# Fraction of query tokens that must appear in a candidate's title to be
# considered a real match. Guards against blind "results[0]" picks when a
# query is too short or noisy for an exact normalized match.
_SIM_THRESHOLD = 0.5

def _token_set(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))

def _overlap(q: str, title: str) -> float:
    """Fraction of query tokens present in the candidate title (0..1)."""
    qt = _token_set(q)
    if not qt:
        return 1.0
    return len(qt & _token_set(title)) / len(qt)

def _title_keys(kind: str) -> tuple[str, str]:
    """(localized, original) title keys for a TMDB result dict."""
    return ("title", "original_title") if kind == "movie" else ("name", "original_name")


def _exact_title(qn: str, r: dict, kind: str) -> bool:
    return bool(qn) and any(_norm_title(r.get(k)) == qn for k in _title_keys(kind))


def _best_overlap(q: str, r: dict, kind: str) -> float:
    """Best token overlap against the localized *or* the original title.

    Searching with language=pl-PL returns Polish titles, but the query is
    parsed from an English/directory name — comparing both keys is what stops
    localized results from looking like noise.
    """
    return max((_overlap(q, r.get(k) or "") for k in _title_keys(kind)), default=0.0)


def _rank(r: dict, q: str, kind: str) -> tuple:
    """Sort key for tie-breaks: overlap, then popularity, then vote average.

    Popularity is the crowd's answer to "which one did people mean" — it is
    what separates "Up" (2009) from "Up in the Air" (2009) when both titles
    contain every query token.
    """
    return (_best_overlap(q, r, kind),
            float(r.get("popularity") or 0),
            float(r.get("vote_average") or 0))


def _pick_movie(results: list, q: str, year: Optional[int]) -> Optional[dict]:
    """Pick the best candidate. TMDB's `year` search param is a soft preference,
    not a hard filter — so rank explicitly: exact title in the requested year
    > requested-year candidates with enough token overlap (sequels like
    'John Wick: Chapter 4') > exact title any year > best ranked overlap.
    A lone result (e.g. a translated title) is still accepted; otherwise a
    result is only taken when it shares tokens with the query — missing is
    better than a wrong poster. Ties break on popularity, not TMDB order."""
    if not results:
        return None
    qn = _norm_title(q)
    exact = [r for r in results if _exact_title(qn, r, "movie")]
    if year:
        same_year_exact = [r for r in exact if _year_from_date(r.get("release_date")) == year]
        if same_year_exact:
            return max(same_year_exact, key=lambda r: _rank(r, q, "movie"))
        cands = [r for r in results
                 if _year_from_date(r.get("release_date")) == year
                 and _best_overlap(q, r, "movie") >= _SIM_THRESHOLD]
        if cands:   # e.g. "John Wick" + 2023 → "John Wick: Chapter 4"
            return max(cands, key=lambda r: _rank(r, q, "movie"))
    if exact:
        return max(exact, key=lambda r: _rank(r, q, "movie"))
    best = max(results, key=lambda r: _rank(r, q, "movie"))
    if _best_overlap(q, best, "movie") >= _SIM_THRESHOLD:
        return best
    if len(results) == 1:
        return results[0]
    return None

def _pick_tv(results: list, q: str) -> Optional[dict]:
    """Pick the best TV candidate: exact show name first, then the best ranked
    token overlap above the threshold, then a lone result (fuzzy/translated)."""
    if not results:
        return None
    qn = _norm_title(q)
    exact = [r for r in results if _exact_title(qn, r, "tv")]
    if exact:
        return max(exact, key=lambda r: _rank(r, q, "tv"))
    best = max(results, key=lambda r: _rank(r, q, "tv"))
    if _best_overlap(q, best, "tv") >= _SIM_THRESHOLD:
        return best
    if len(results) == 1:
        return results[0]
    return None

def search_movie(api_key: str, title: str, year: int | None = None,
                 language: str | None = None) -> dict | None:
    """Best non-adult movie match for a parsed title (+ optional year).
    `language` (e.g. 'pl-PL') localizes titles/overviews/posters when TMDB has them."""
    params = {"api_key": api_key, "query": title[:120], "include_adult": "false"}
    if year:
        params["year"] = str(year)
    if language:
        params["language"] = language
    _throttle()
    data = _http_json(API_BASE + "/search/movie?" + urllib.parse.urlencode(params))
    results = [r for r in (data.get("results") or []) if isinstance(r, dict) and not r.get("adult")]
    pick = _pick_movie(results, title, year) if results else None
    return _normalize(pick, "movie") if pick else None

def search_tv(api_key: str, show: str, language: str | None = None) -> dict | None:
    """Best non-adult TV match for a show name (exact-name preference)."""
    params = {"api_key": api_key, "query": show[:120], "include_adult": "false"}
    if language:
        params["language"] = language
    _throttle()
    data = _http_json(API_BASE + "/search/tv?" + urllib.parse.urlencode(params))
    results = [r for r in (data.get("results") or []) if isinstance(r, dict) and not r.get("adult")]
    pick = _pick_tv(results, show) if results else None
    return _normalize(pick, "tv") if pick else None

def _find_by_imdb(api_key: str, imdb_id: str, expect_kind: str = "movie",
                  language: str | None = None) -> dict | None:
    """Exact match via /find/{imdb_id}?external_source=imdb_id — zero ambiguity
    when the filename carries an IMDb id (the Radarr/Jellyfin path). Returns a
    normalized hit preferring `expect_kind`, or None when TMDB has no record."""
    params = {"api_key": api_key, "external_source": "imdb_id"}
    if language:
        params["language"] = language
    _throttle()
    data = _http_json(f"{API_BASE}/find/{urllib.parse.quote(imdb_id)}?" + urllib.parse.urlencode(params))
    pairs = (("movie_results", "movie"), ("tv_results", "tv")) if expect_kind == "movie" \
        else (("tv_results", "tv"), ("movie_results", "movie"))
    for key, kind in pairs:
        hits = [r for r in (data.get(key) or []) if isinstance(r, dict) and not r.get("adult")]
        if hits:
            return _normalize(hits[0], kind)
    return None

def _fetch_details(api_key: str, kind: str, tmdb_id: int,
                   language: str | None = None) -> dict:
    """Full record for display extras (genres/cast/runtime). Raises TmdbError on failure."""
    params = {"api_key": api_key, "append_to_response": "credits"}
    if language:
        params["language"] = language
    _throttle()
    data = _http_json(f"{API_BASE}/{kind}/{tmdb_id}?" + urllib.parse.urlencode(params))
    genres = [g.get("name") for g in (data.get("genres") or []) if g.get("name")]
    cast = [c.get("name") for c in ((data.get("credits") or {}).get("cast") or [])[:5] if c.get("name")]
    out: dict = {"genres": genres, "cast": cast}
    runtime = data.get("runtime")
    if isinstance(runtime, int) and runtime > 0:
        out["runtime"] = runtime
    return out


def test_connection(api_key: str) -> dict:
    """Cheap key validation via /configuration. Returns {ok, message}."""
    try:
        data = _http_json(API_BASE + "/configuration?api_key=" + urllib.parse.quote(api_key))
        return {"ok": True, "message": f"Connected — base image URL {data.get('images', {}).get('base_url', '')}".strip()}
    except TmdbError as exc:
        msg = str(exc)
        if "401" in msg:
            return {"ok": False, "message": "Invalid API key (TMDB returned 401)"}
        return {"ok": False, "message": msg}


def image_filename(remote_path: str, size: str) -> str:
    """Stable local filename for a TMDB remote image path."""
    h = hashlib.md5(f"tmdb:{size}:{remote_path}".encode()).hexdigest()[:16]
    return f"{h}.jpg"


def download_image(remote_path: Optional[str], size: str, dest_dir: Path) -> Optional[str]:
    """Download one TMDB image into `dest_dir` (skips existing). Returns the
    local filename or None. Never raises — artwork is optional."""
    if not remote_path:
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    fn = image_filename(remote_path, size)
    out = dest_dir / fn
    try:
        if out.exists() and out.stat().st_size > 0:
            return fn
        tmp = out.with_suffix(".jpg.tmp")
        _http_download(f"{IMAGE_BASE}/{size}{remote_path}", tmp)
        os.replace(tmp, out)
        return fn
    except TmdbError as exc:
        logger.warning("TMDB image download failed (%s): %s", remote_path, exc)
        return None


def _query_variants(q: str):
    """Yield progressively shorter prefixes of a title query.

    Parsed titles often carry release-group tags ("The Matrix GROUP") that break
    TMDB search — dropping trailing words recovers the real title. Max 2 fallbacks.
    """
    seen = {q}
    cur = q
    for _ in range(2):
        parts = cur.split()
        if len(parts) < 2:
            return
        cur = " ".join(parts[:-1]).strip()
        if len(cur) < 4 or cur in seen:
            return
        seen.add(cur)
        yield cur

def _lang_codes(language: str | None) -> str:
    """TMDB `include_image_language` value: 'pl,en,null' for a pl-PL library."""
    codes: list[str] = []
    if language:
        base = language.split("-")[0].lower()
        if base:
            codes.append(base)
    if "en" not in codes:
        codes.append("en")
    codes.append("null")     # textless posters — usually the prettiest
    return ",".join(codes)


def _pick_poster(api_key: str, kind: str, tmdb_id, language: str | None = None) -> str | None:
    """Best poster path for this title, preferring the library language.

    `/search` returns a single locale poster, which is often textless or English
    even when a proper localized poster exists. One extra call to `/images`
    fetches the whole set; ranking is: requested language → textless → English,
    then community vote.
    """
    if not tmdb_id:
        return None
    params = {"api_key": api_key, "include_image_language": _lang_codes(language)}
    _throttle()
    data = _http_json(f"{API_BASE}/{kind}/{tmdb_id}/images?" + urllib.parse.urlencode(params))
    posters = [p for p in (data.get("posters") or [])
               if isinstance(p, dict) and p.get("file_path")]
    if not posters:
        return None
    want = (language or "").split("-")[0].lower()

    def score(p: dict) -> tuple:
        lang = (p.get("iso_639_1") or "").lower()
        prio = 0 if (want and lang == want) else (1 if not lang else 2)
        return (prio, -float(p.get("vote_average") or 0), -int(p.get("vote_count") or 0))

    return min(posters, key=score)["file_path"]


def fetch_match(api_key: str, kind: str, query: str, year: int | None = None,
                image_dir: Path | None = None, language: str | None = None,
                imdb_id: str | None = None) -> dict | None:
    """Match one title against TMDB and (optionally) cache its artwork locally.

    Exact IMDb-id match (/find) is tried first when `imdb_id` is given — that is
    the Radarr/Jellyfin path with zero ambiguity; otherwise a language-aware
    title search with best-match ranking. Returns the normalized match plus
    display extras (genres/cast/runtime) and `poster_file`/`backdrop_file`
    local filenames when downloaded, or None when nothing matched. Raises
    TmdbError on network/API failure so callers can count it as a failed check.
    """
    q = _clean_query(query, kind)[:120]
    if not api_key or not q:
        return None
    match = None
    if imdb_id:
        try:
            match = _find_by_imdb(api_key, imdb_id, expect_kind=kind, language=language)
        except TmdbError as exc:
            logger.warning("TMDB /find failed for %s: %s — falling back to search", imdb_id, exc)
    if not match and kind == "movie":
        for attempt in [q, *_query_variants(q)]:
            match = search_movie(api_key, attempt, year, language=language)
            if match:
                break
        if not match:
            # Anthology films and specials are sometimes only registered as
            # TV shows — one plain TV lookup as a last resort (gated picking,
            # so it can still return None instead of a blind top hit).
            match = search_tv(api_key, q, language=language)
    elif not match:
        for attempt in [q, *_query_variants(q)]:
            match = search_tv(api_key, attempt, language=language)
            if match:
                break
    if not match:
        return None
    try:
        match.update(_fetch_details(api_key, match["kind"], match["tmdb_id"], language))
    except TmdbError as exc:
        # Match is still usable without extras; the entry stays "stale" (no
        # genres key) so the next batch refresh retries the detail fetch.
        logger.warning("TMDB detail fetch failed for %s/%s: %s", match["kind"], match.get("title"), exc)
    if image_dir is not None:
        # Prefer a poster in the library language — search hits often carry a
        # textless/English poster even when a localized one exists.
        if language and not language.lower().startswith("en"):
            try:
                better = _pick_poster(api_key, match["kind"], match.get("tmdb_id"), language)
                if better:
                    match["poster_path"] = better
            except TmdbError as exc:
                logger.warning("TMDB poster lookup failed for %s: %s", match.get("title"), exc)
        # Card-sized artwork rather than `original`: a w500 poster is tens of KB
        # instead of several MB, which keeps shelves instant to paint while a
        # 342 px portrait tile still looks sharp. Backdrops feed the hero, so
        # they get w1280.
        match["backdrop_file"] = download_image(match.get("backdrop_path"), "w1280", Path(image_dir)) or ""
        match["poster_file"] = download_image(match.get("poster_path"), "w500", Path(image_dir)) or ""
        if not match["poster_file"] and match["backdrop_file"]:
            # No poster on TMDB at all — reuse the backdrop so portrait tiles
            # show artwork instead of an ffmpeg frame grab.
            match["poster_file"] = match["backdrop_file"]
    else:
        match["backdrop_file"] = ""
        match["poster_file"] = ""
    return match
