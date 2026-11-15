# Photos — UI redesign (timeline / curated style)

Status: done (2026-11-08)

## Cel

Nowoczesny, „produktowy” interfejs aplikacji Photos (bez zmiany backendu i funkcji):
spójny design system, SVG-ikony zamiast emoji, glass/soft surfaces, grupowanie po
dniach, dopracowany lightbox i widoki Moments/Calendar.

## Zakres (frontend only — `frontend/static/apps/gallery.js`)

- [x] Design system w `_ensureStyles()`: klasy `.ps-*`, zmienne motywu, radiusy,
      cienie, animacje, pełne wsparcie light/dark (przez `var(--surface-*)`, `--accent`).
- [x] Sidebar: brand mark, sekcje (Library / Albums / Folders), ikony SVG,
      aktywny element z akcentem, akcje hover (share/remove/delete).
- [x] Topbar: tytuł + podtytuł widoku, wyszukiwarka z ikoną, sort, segmented
      grid/timeline, Select, ustawienia, licznik jako pill. Mobile: off-canvas sidebar.
- [x] Grid: kafelki 12px radius, hover zoom + gradient + meta (data, rozmiar),
      badge wideo, checkbox zaznaczania, gwiazdka ulubionych (SVG, stan `.on`).
- [x] Grupowanie po dniach (Today / Yesterday / data) w widoku siatki przy sortowaniu datą;
      timeline nadal grupuje po miesiącach (sticky nagłówki).
- [x] Empty / loading / welcome states z ikoną w „squircle” i CTA.
- [x] Action bar: floating glass pill z ikonami i etykietami (kompaktowo < 900px).
- [x] Settings popover: switch „Include videos”, rozmiar kafli, PIN, rescan.
- [x] Moments: gradient hero ze statystykami, Highlights, mozaikowe karty miesięcy.
- [x] Calendar: karta miesiąca, dni z tłem miniatury, badge liczby, szybka nawigacja dniem.
- [x] Lightbox: rozmyte tło z bieżącego zdjęcia, glass topbar, strzałki, dock z filmstripem,
      panel info (EXIF) w nowym stylu, stany ulubione/prywatne przez klasy.
- [x] Zachowane wszystkie `data-testid` i handlery inline (zgodność z automatyzacją).

## Poza zakresem

- Backend (`routers/gallery.py`, `routers/moments.py`) — bez zmian.
- Nowe funkcje produktowe (upload, edycja) — osobne zadanie.

## Weryfikacja

Headless Chromium (Playwright build) na produkcyjnej instancji :9000, ze świeżym JWT:

- Boot aplikacji bez błędów w konsoli; 90 kafli w pierwszej porcji, 14 nagłówków dni.
- Ustawienia: popover otwiera się (`display:flex`), switch + rozmiar kafli działają.
- Timeline: 2 nagłówki miesięcy, brak nagłówków dni (poprawna zamiana trybów).
- Tryb zaznaczania: action bar widoczny, „1 selected”, pierścień na kaflu.
- Kalendarz: 30 dni, miniatury tła, nawigacja miesiąca.
- Favorites: tytuł + 7 kafli.
- Mobile 430px: off-canvas sidebar, kompaktowy topbar, wysuwane menu działa.
- Lightbox: rozmyte tło, filmstrip, panel EXIF, ulubione/prywatne przez klasy.

