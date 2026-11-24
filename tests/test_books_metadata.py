#!/usr/bin/env python3
"""Offline tests for the Books package naming parser.

The parser lives in the sibling package checkout
(`~/apps/compose/ethos-packages/ethos-books/server/naming.py`); this test skips
when that checkout is missing. No network, no server.

Run: .venv/bin/python tests/test_books_metadata.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PACKAGE_SERVER = Path.home() / "apps/compose/ethos-packages/ethos-books/server"
if not (PACKAGE_SERVER / "naming.py").is_file():
    print("SKIP — sibling package checkout not found")
    sys.exit(0)
sys.path.insert(0, str(PACKAGE_SERVER))

import naming  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="ethos-books-meta-"))
os.environ["ETHOS_PACKAGE_DATA_DIR"] = str(TMP)
import providers  # noqa: E402
import main as service  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def parsed(name):
    return naming.parse_name(name)


def fields(value):
    return {key: value.get(key) for key in ("title", "author", "narrator", "series",
                                            "series_index", "year")}


def run() -> int:
    print("A. inner folder names (Author - Title)")
    book = parsed("Yoda Hiroko - Osiem milionów dróg do szczęścia (czyta Małgorzata Klara)")
    check("author swapped to given-surname", book["author"] == "Hiroko Yoda", fields(book))
    check("title without narrator", book["title"] == "Osiem milionów dróg do szczęścia", fields(book))
    check("narrator captured", book["narrator"] == "Małgorzata Klara", fields(book))

    book = parsed("Nesbø Jo - Cykl Harry Hole Tom 14 Cięcie (czyta Mariusz Bonaszewski)")
    check("series + volume", (book["series"], book["series_index"]) == ("Harry Hole", 14.0), fields(book))
    check("series removed from title", book["title"] == "Cięcie", fields(book))
    check("diacritics kept in author", book["author"] == "Jo Nesbø", fields(book))
    check("narrator", book["narrator"] == "Mariusz Bonaszewski", fields(book))

    book = parsed("Mróz Remigiusz - Konfrontacja (czyta Krzysztof Gosztyła, Filip Kosior)")
    check("author diacritics", book["author"] == "Remigiusz Mróz", fields(book))
    check("two narrators", book["narrator"] == "Krzysztof Gosztyła, Filip Kosior", fields(book))

    book = parsed("Krajewski Andrzej - Ropa.Krew cywilizacji")
    check("dot title repaired", book["title"] == "Ropa. Krew cywilizacji", fields(book))
    check("author without narrator", book["author"] == "Andrzej Krajewski", fields(book))

    book = parsed("Mist A. P. - Nikt (czyta Agata Bieńkowska, Marcin Stec) 192kbps EM (2026)")
    check("initials author", book["author"] == "A. P. Mist", fields(book))
    check("tag noise stripped", book["title"] == "Nikt", fields(book))
    check("year captured", book["year"] == 2026, fields(book))

    book = parsed("Guardbro - Saga o Malfoid - Harry Potter i kamień filozoficzny czyta P. Fronczewski 320kbps EM")
    check("single-word author", book["author"] == "Guardbro", fields(book))
    check("inline narrator", book["narrator"] == "P. Fronczewski", fields(book))
    check("multi-part title", book["title"] == "Saga o Malfoid - Harry Potter i kamień filozoficzny", fields(book))

    print("B. release names (dotted)")
    book = parsed("Yoda.Hiroko.Osiem.milionow.drog.do.szczescia.136vbrp")
    check("dotted author", book["author"] == "Hiroko Yoda", fields(book))
    check("dotted title", book["title"] == "Osiem milionow drog do szczescia", fields(book))

    book = parsed("Nesbo.Jo.Cykl.Harry.Hole.Tom.14.Ciecie.czyta.Mariusz.Bonaszewski.128vbr.EM.2026")
    check("dotted series", (book["series"], book["series_index"]) == ("Harry Hole", 14.0), fields(book))
    check("dotted narrator", book["narrator"] == "Mariusz Bonaszewski", fields(book))
    check("dotted year", book["year"] == 2026, fields(book))

    book = parsed("Andrzej.Krajewski.Ropa.Krew.cywilizacji.2023")
    check("given-surname release", book["author"] == "Andrzej Krajewski", fields(book))
    check("release year", book["year"] == 2023, fields(book))

    print("C. slugs and plain folders")
    book = parsed("eryk-zdobywca-swiatow-podloga-to-lawa-piotr-borlik-ebookpoint")
    check("vendor suffix stripped", "ebookpoint" not in book["title"].lower(), fields(book))
    check("trailing author", book["author"] == "Piotr Borlik", fields(book))

    book = parsed("basia_wielka_ksiega")
    check("plain folder title", book["title"] == "Basia wielka ksiega", fields(book))
    check("plain folder has no author", book["author"] == "", fields(book))

    print("D. release detection")
    check("release name detected",
          naming.looks_like_release("Nesbo.Jo.Cykl.Harry.Hole.Tom.14.Ciecie.128vbr.EM") is True)
    check("clean inner name is not a release",
          naming.looks_like_release("Nesbø Jo - Cykl Harry Hole Tom 14 Cięcie") is False)

    print("D2. author folder convention")
    book = service._clean_book({
        "path": "/mnt/books/audiobooks/Andrzej Krajewski/Harry Hole/Tom 14",
        "format": "mp3",
        "tracks": [{"path": "/mnt/books/audiobooks/Andrzej Krajewski/Harry Hole/Tom 14/01.mp3",
                    "size": 1, "duration": 1}],
    }, "/mnt/books/audiobooks")
    check("author from the top folder", book["author"] == "Andrzej Krajewski", book["author"])
    book = service._clean_book({
        "path": "/mnt/books/audiobooks/basia_wielka_ksiega",
        "format": "mp3",
        "tracks": [{"path": "/mnt/books/audiobooks/basia_wielka_ksiega/01.mp3",
                    "size": 1, "duration": 1}],
    }, "/mnt/books/audiobooks")
    check("root-level folder has no author", book["author"] == "", book["author"])

    print("E. provider parsing (fixture server)")
    server, base = _fixture_server()
    os.environ["ETHOS_BOOKS_ENRICH_BASE"] = base
    providers.reset()
    try:
        apple = providers.apple_search("Cięcie", "Jo Nesbø")
        check("apple result parsed", bool(apple) and apple[0]["title"] == "Cięcie", str(apple[:1]))
        check("apple description cleaned", bool(apple) and "<p>" not in apple[0]["description"])
        check("apple cover upscaled", bool(apple) and apple[0]["cover_url"].endswith("600x600bb.jpg"),
              apple[0]["cover_url"] if apple else "")
        check("apple genres drop 'Books'", bool(apple) and "Books" not in apple[0]["genres"])
        google = providers.google_search("Cięcie", "Jo Nesbø")
        check("google result parsed", bool(google) and google[0]["isbn"] == "9788300000000")
        check("google cover forced to https", bool(google) and google[0]["cover_url"].startswith("https://"))
        openlibrary = providers.openlibrary_search("Cięcie", "Jo Nesbø")
        check("openlibrary fallback parsed", bool(openlibrary) and openlibrary[0]["year"] == 2026)

        sample = {"title": "Cięcie", "author": "Jo Nesbø"}
        check("exact match scores full", providers.score(sample, apple[0]) >= 0.95,
              str(providers.score(sample, apple[0])))
        wrong = dict(apple[0], author="Someone Else")
        check("wrong author rejected", providers.score(sample, wrong) < 0.75,
              str(providers.score(sample, wrong)))

        candidates, tried, errors = providers.search("Cięcie", "Jo Nesbø", "auto")
        check("all providers tried", tried == ["apple", "google", "openlibrary"], str(tried))
        check("three candidates", len(candidates) == 3, str(len(candidates)))
        check("no errors", errors == [], str(errors))
    finally:
        server.shutdown()
        server.server_close()
        os.environ.pop("ETHOS_BOOKS_ENRICH_BASE", None)

    print("F. enrichment through the service")
    server, base = _fixture_server()
    os.environ["ETHOS_BOOKS_ENRICH_BASE"] = base
    providers.reset()
    try:
        folder = ("/mnt/books/audiobooks/Nesbo.Jo.Cykl.Harry.Hole.Tom.14.Ciecie.128vbr/"
                  "Nesbø Jo - Cykl Harry Hole Tom 14 Cięcie (czyta Mariusz Bonaszewski)")
        book = service._clean_book({
            "path": folder,
            "format": "mp3",
            "tracks": [{"path": folder + "/01.mp3", "size": 10, "duration": 5}],
        }, "/mnt/books/audiobooks")
        check("scan metadata parsed", book["title"] == "Cięcie" and book["author"] == "Jo Nesbø"
              and book["narrator"] == "Mariusz Bonaszewski" and book["series"] == "Harry Hole",
              str({k: book.get(k) for k in ("title", "author", "narrator", "series")}))
        service._save(service.LIBRARY_FILE, {"version": 2, "users": {"marcin": {
            "folders": [{"path": "/mnt/books/audiobooks", "added_at": service._now(),
                          "last_scan": "", "count": 1}],
            "books": {book["id"]: book}}}})
        result = service.enrich_book("marcin", book["id"], {"force": True})
        enriched = result["book"]
        check("enrichment updated", result["status"] == "updated", str(result.get("status")))
        check("provider recorded", enriched["source"] == "apple", enriched.get("source", ""))
        check("description stored", "Harry Hole" in enriched["description"],
              enriched.get("description", "")[:60])
        check("genres stored", enriched["genres"] == ["Crime & Thrillers", "Police Procedural"],
              str(enriched.get("genres")))
        check("cover stored", enriched["cover"] is True
              and (TMP / "covers" / f"{book['id']}.jpg").is_file())
        check("cover attributed", enriched["cover_source"] == "apple", enriched.get("cover_source", ""))

        entry = {
            "path": folder, "format": "mp3",
            "tracks": [{"path": folder + "/01.mp3", "size": 10, "duration": 5}],
        }
        service.scan("marcin", {"root": "/mnt/books/audiobooks", "entries": [entry], "done": True})
        stored = service._user_library(service._load(service.LIBRARY_FILE), "marcin")["books"][book["id"]]
        check("rescan keeps description", stored.get("description") == enriched["description"])
        check("rescan keeps cover", stored.get("cover") is True)
        check("rescan keeps manual flags", stored.get("fields_manual") == [])

        found = service.search_books("marcin", book["id"], {})
        check("search returns candidates", bool(found["candidates"]), str(found.get("errors")))
        check("preview token issued", bool(found["candidates"][0].get("cover_preview")))
        token = found["candidates"][0]["cover_preview"].rsplit("/", 1)[-1]
        blob, media = service.read_preview(token)
        check("preview proxied", bool(blob) and media == "image/jpeg", media)

        service.delete_cover("marcin", book["id"])
        poster, media = service.read_cover("marcin", book["id"])
        check("poster fallback", media == "image/svg+xml" and b"<svg" in poster, media)

        manual = service.save_meta("marcin", book["id"], {"title": "Cięcie"})
        check("manual field recorded", manual["book"]["fields_manual"] == ["title"])
        service.scan("marcin", {"root": "/mnt/books/audiobooks", "entries": [
            dict(entry, title="inny tytuł")], "done": True})
        stored = service._user_library(service._load(service.LIBRARY_FILE), "marcin")["books"][book["id"]]
        check("manual title survives scan", stored["title"] == "Cięcie", stored["title"])
    finally:
        server.shutdown()
        server.server_close()
        os.environ.pop("ETHOS_BOOKS_ENRICH_BASE", None)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


APPLE_FIXTURE = {
    "resultCount": 1,
    "results": [{
        "trackId": 684000001,
        "trackName": "Cięcie",
        "trackCensoredName": "Cięcie",
        "artistName": "Jo Nesbø",
        "releaseDate": "2026-11-12T00:00:00Z",
        "description": "<p>Harry Hole wraca do gry w 14. tomie.</p>",
        "genres": ["Crime & Thrillers", "Books", "Police Procedural"],
        "artworkUrl100": "https://itunes.apple.com/image/thumb/x/100x100bb.jpg",
        "trackViewUrl": "https://books.apple.com/pl/book/x/id684000001",
        "averageUserRating": 4.5,
    }],
}

GOOGLE_FIXTURE = {
    "items": [{
        "id": "vol1",
        "volumeInfo": {
            "title": "Cięcie",
            "authors": ["Jo Nesbø"],
            "publishedDate": "2026-11-12",
            "publisher": "Wydawnictwo Dolnośląskie",
            "pageCount": 480,
            "categories": ["Kryminał"],
            "averageRating": 4.2,
            "description": "Harry Hole wraca.",
            "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9788300000000"}],
            "imageLinks": {"thumbnail": "http://books.google.com/books/content?id=vol1&zoom=1&edge=curl"},
            "canonicalVolumeLink": "https://books.google.com/books?id=vol1",
        },
    }],
}

OPENLIB_FIXTURE = {
    "docs": [{
        "key": "/works/OL1W",
        "title": "Cięcie",
        "author_name": ["Jo Nesbø"],
        "first_publish_year": 2026,
        "cover_i": 12345,
        "language": ["pol"],
        "series": ["Harry Hole"],
    }],
}


def _fixture_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — stdlib API
            path = urllib.parse.urlparse(self.path).path
            if path == "/search":
                return self._json(APPLE_FIXTURE)
            if path == "/books/v1/volumes":
                return self._json(GOOGLE_FIXTURE)
            if path == "/search.json":
                return self._json(OPENLIB_FIXTURE)
            if path.startswith("/image/thumb/") or path.endswith(".jpg"):
                body = b"\xff\xd8\xff\xe0" + b"jpeg-fixture".ljust(64, b"0")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            self.send_response(404)
            self.end_headers()
            return None

        def _json(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    try:
        code = run()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
