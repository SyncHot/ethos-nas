#!/usr/bin/env python3
"""Offline tests for the Books package naming parser.

The parser lives in the sibling package checkout
(`~/apps/compose/ethos-packages/ethos-books/server/naming.py`); this test skips
when that checkout is missing. No network, no server.

Run: .venv/bin/python tests/test_books_metadata.py
"""
import sys
from pathlib import Path

PACKAGE_SERVER = Path.home() / "apps/compose/ethos-packages/ethos-books/server"
if not (PACKAGE_SERVER / "naming.py").is_file():
    print("SKIP — sibling package checkout not found")
    sys.exit(0)
sys.path.insert(0, str(PACKAGE_SERVER))

import naming  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def parsed(name):
    return naming.parse_name(name)


def fields(value):
    return {key: value.get(key) for key in ("title", "author", "narrator", "series",
                                            "series_index", "year")}


def main() -> int:
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

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
