"""Unit + live tests — TMDB metadata service (Phase 7).

Covers:
  A. extract_imdb_id — IMDb ids embedded in filenames (the /find exact-match key).
  B. _pick_movie / _pick_tv — gated candidate picking (no blind top-hit picks;
     token-overlap threshold; lone-result fallback for translated titles).
  C. _clean_query regression (release-name noise stripping).
  D. Router _sanitize_tmdb — partial patches keep previously stored values
     (api_key, auto_fetch, language); invalid language codes rejected.
  E. Live TMDB (skipped when no API key is stored in video_station_config.json):
     connection test, /find by IMDb id (movie + tv), localized search, and a
     full fetch_match with detail extras (genres/cast) + original-size artwork.

Run:  .venv/bin/python tests/test_tmdb.py
"""
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.routers.video_station import _sanitize_tmdb
from backend.services import tmdb_service as t


def main() -> int:
    failures = []

    def check(name: str, ok: bool, detail: str = ""):
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    # ── A. extract_imdb_id ────────────────────────────────────────────────
    print("A. extract_imdb_id")
    check("bracketed id", t.extract_imdb_id("Movie (2020) [tt1234567].mkv") == "tt1234567")
    check("bare id in name", t.extract_imdb_id("tt1234567.mkv") == "tt1234567")
    check("no id → None", t.extract_imdb_id("Movie 2020.mkv") is None)
    check("embedded, no word boundary", t.extract_imdb_id("xtt1234567.mkv") is None)
    check("too short (6 digits)", t.extract_imdb_id("tt123456.mkv") is None)
    check("empty/None input", t.extract_imdb_id("") is None and t.extract_imdb_id(None) is None)

    # ── B. gated candidate picking ────────────────────────────────────────
    print("B. _pick_movie / _pick_tv")
    def R(title, date):
        return {"title": title, "release_date": date}

    def T(name):
        return {"name": name}

    # exact title in the requested year wins outright
    res = [R("John Wick", "2014-10-24"), R("John Wick: Chapter 4", "2023-03-24")]
    check("exact+year beats same-year sequel", t._pick_movie(res, "John Wick", 2014)["title"] == "John Wick")

    # same-year candidates need ≥50% token overlap — a random same-year film is skipped
    res = [R("Some Random Movie", "2023-01-01"), R("John Wick: Chapter 4", "2023-03-24")]
    check("same-year pick requires overlap", t._pick_movie(res, "John Wick", 2023)["title"] == "John Wick: Chapter 4")

    # no exact match + nothing above threshold → None (missing beats a wrong poster)
    res = [R("Alpha Beta", "2019-01-01"), R("Gamma Delta", "2019-02-02")]
    check("no overlap, multi results → None", t._pick_movie(res, "Zeta Omega", 2019) is None)

    # a lone result is still accepted (translated/foreign title)
    res = [R("Interstellar", "2014-11-07")]
    check("lone result accepted without overlap", t._pick_movie(res, "Interstella", 2014)["title"] == "Interstellar")

    # exact title in any year beats a higher-overlap wrong-year candidate
    res = [R("The Matrix Reloaded", "2003-05-15"), R("The Matrix", "1999-03-31")]
    check("exact-any-year over overlap", t._pick_movie(res, "The Matrix", 1999)["title"] == "The Matrix")

    # TV: exact name first, then gated overlap, lone fallback
    res = [T("Breaking Bad"), T("Better Call Saul")]
    check("tv exact name wins", t._pick_tv(res, "Breaking Bad")["name"] == "Breaking Bad")
    res = [T("Alpha Beta"), T("Gamma Delta")]
    check("tv no overlap → None", t._pick_tv(res, "Zeta Omega") is None)
    res = [T("Some Show")]
    check("tv lone result accepted", t._pick_tv(res, "Show X")["name"] == "Some Show")

    # ── C. _clean_query regression ────────────────────────────────────────
    print("C. _clean_query")
    check("group tag stripped", t._clean_query("Interstellar WEB DL GTBA", "movie").lower() == "interstellar")
    check("trailing year dropped (sent as param)", t._clean_query("Inception 2010", "movie").lower() == "inception")
    check("edge title '2012' survives", t._clean_query("2012", "movie").strip().isdigit())

    # ── D. _sanitize_tmdb partial patches ─────────────────────────────────
    print("D. _sanitize_tmdb")
    prev = {"api_key": "stored-key-abc", "auto_fetch": False, "language": "pl-PL"}
    out = _sanitize_tmdb({"language": "en-US"}, prev)
    check("partial: key kept", out["api_key"] == "stored-key-abc")
    check("partial: auto_fetch=False kept (not reset to True)", out["auto_fetch"] is False)
    check("partial: language updated", out["language"] == "en-US")

    out = _sanitize_tmdb({"language": "pl_PL"}, prev)   # underscore, not a TMDB code
    check("invalid language keeps previous", out["language"] == "pl-PL")
    out = _sanitize_tmdb({"language": ""}, prev)        # empty → keep previous
    check("empty language keeps previous", out["language"] == "pl-PL")
    out = _sanitize_tmdb(None, prev)                    # no section at all
    check("missing section keeps everything", out == {"api_key": "stored-key-abc", "auto_fetch": False, "language": "pl-PL"})

    # ── E. live TMDB (needs a stored key) ─────────────────────────────────
    cfg_file = Path(os.environ.get("ETHOS_DATA_DIR", "backend/data")) / "video_station_config.json"
    key = ""
    with contextlib.suppress(OSError, ValueError):
        key = json.loads(cfg_file.read_text()).get("tmdb", {}).get("api_key", "")
    if not key:
        print("\nE. live TMDB — SKIPPED (no api_key in video_station_config.json)")
        return 1 if failures else 0

    print(f"\nE. live TMDB (key …{key[-4:]})")
    conn = t.test_connection(key)
    check("test_connection", conn.get("ok") is True, str(conn))

    m = t._find_by_imdb(key, "tt1375666", expect_kind="movie")
    check("/find movie (Inception)", bool(m and m["tmdb_id"] == 27205), str(m)[:120])

    tv = t._find_by_imdb(key, "tt0903747", expect_kind="tv")
    check("/find tv (Breaking Bad)", bool(tv and tv["kind"] == "tv"), str(tv)[:120])

    m_pl = t.search_movie(key, "Interstellar", 2014, language="pl-PL")
    m_en = t.search_movie(key, "Interstellar", 2014)
    check("localized search matches same movie",
          bool(m_pl and m_en and m_pl["tmdb_id"] == m_en["tmdb_id"] == 157336),
          f"pl={m_pl and m_pl.get('title')!r} en={m_en and m_en.get('title')!r}")

    with tempfile.TemporaryDirectory(prefix="ethos-tmdb-test-") as td:
        fm = t.fetch_match(key, "movie", "Inception", 2010, Path(td), language="pl-PL", imdb_id="tt1375666")
        check("fetch_match via IMDb id", bool(fm and fm["tmdb_id"] == 27205), str(fm)[:120])
        if fm:
            check("detail extras: genres non-empty", isinstance(fm.get("genres"), list) and len(fm["genres"]) >= 2, str(fm.get("genres")))
            check("detail extras: cast non-empty", isinstance(fm.get("cast"), list) and len(fm["cast"]) >= 3, str((fm.get("cast") or [])[:3]))
            pf = fm.get("poster_file")
            poster_ok = bool(pf) and (Path(td) / pf).is_file() and (Path(td) / pf).stat().st_size > 10_000
            check("original-size poster downloaded locally", poster_ok, f"file={pf!r}")

    print(f"\n{'FAIL' if failures else 'OK'} — {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
