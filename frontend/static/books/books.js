// EthOS Books — standalone phone PWA at /books.
//
// The mobile counterpart of the Books package window: the same per-account
// library, reading progress and reader settings, streamed straight from the NAS
// (`/api/files/raw`) or played from the phone's own downloaded copy.
//
// Rules shared with every app: toasts come from core/notification-center.js,
// long work reports through core/progress.js, diagnostics through
// core/logger.js. Device downloads, covers and the progress outbox live in
// ./offline.js; playback lives in ./player.js; the readers are ./epub.js and
// ./pdf.js.
//
// Folder management and scanning stay in the desktop window (the package walks
// the library through the file API); this page is the reading/listening client.

import { getCurrentClaims, setToken } from '../core/api.js';
import { notify } from '../core/notification-center.js';
import { logError, logInfo, logWarn, setLogContext } from '../core/logger.js';
import '../core/error-reporter.js';
import { engine } from './player.js';
import * as offline from './offline.js';
import { mountEpub, readerThemeName } from './epub.js';
import { mountPdf } from './pdf.js';
import {
    baseName, bookTracks, esc, fmtBytes, fmtTime, isAudiobook, library, matchesQuery, pct,
    percentOf, putSettings, trackTitle,
} from './api.js';

setLogContext({ app: 'books' });

const $ = (id) => document.getElementById(id);

const els = {
    login: $('bk-login'),
    loginForm: $('bk-login-form'),
    loginError: $('bk-login-error'),
    loginOffline: $('bk-login-offline'),
    app: $('bk-app'),
    net: $('bk-net'),
    install: $('bk-install'),
    menuBtn: $('bk-menu-btn'),
    menu: $('bk-menu'),
    content: $('bk-content'),
    nav: $('bk-nav'),
    mini: $('bk-mini'),
    miniArt: $('bk-mini-art'),
    miniTitle: $('bk-mini-title'),
    miniSub: $('bk-mini-sub'),
    miniPlay: $('bk-mini-play'),
    miniProgress: $('bk-mini-progress'),
    player: $('bk-player'),
    playerBg: $('bk-player-bg'),
    playerArt: $('bk-player-art'),
    playerTitle: $('bk-player-title'),
    playerSub: $('bk-player-sub'),
    playerTrack: $('bk-player-track'),
    playerLabel: $('bk-player-label'),
    pos: $('bk-pos'),
    dur: $('bk-dur'),
    seek: $('bk-seek'),
    play: $('bk-play'),
    speed: $('bk-speed'),
    sleep: $('bk-sleep'),
    sleepHint: $('bk-sleep-hint'),
    back: $('bk-back'),
    fwd: $('bk-fwd'),
    tracks: $('bk-tracks'),
    tracksList: $('bk-tracks-list'),
    reader: $('bk-reader'),
    readerTitle: $('bk-reader-title'),
    readerActions: $('bk-reader-actions'),
    readerBody: $('bk-reader-body'),
    readerFoot: $('bk-reader-foot'),
    sheet: $('bk-sheet'),
    sheetTitle: $('bk-sheet-title'),
    sheetBody: $('bk-sheet-body'),
};

const state = {
    library: { folders: [], books: [], shared_books: [], progress: {}, settings: {}, stats: {} },
    view: 'library',
    query: '',
    filter: 'all',
    loaded: false,
    offlineMode: false,
    reading: null,
    installPrompt: null,
    speeds: [0.75, 1, 1.25, 1.5, 1.75, 2],
    sleeps: [0, 15, 30, 45, 60],
    saveTimer: null,
};

// ── helpers ────────────────────────────────────────────────────────────────

function errorText(error) {
    return error?.detail || error?.message || 'Nie udało się wykonać operacji';
}

function toast(message, kind = 'info', title = 'Books') {
    try { notify({ kind, title, message }); } catch { /* host missing */ }
}

function settings() {
    return state.library.settings || {};
}

function allBooks() {
    return [...(state.library.books || []), ...(state.library.shared_books || [])];
}

function bookById(id) {
    return allBooks().find((book) => book.id === id) || null;
}

function progressFor(book) {
    return (state.library.progress || {})[book?.id] || null;
}

function coverHtml(book, className = 'bk-cover') {
    const fallback = isAudiobook(book) ? '🎧' : '📖';
    return `<span class="${className}" data-cover="${esc(book.id)}"><span class="fallback">${fallback}</span></span>`;
}

function badgesHtml(book) {
    const local = offline.isDownloaded(book);
    const audio = isAudiobook(book);
    return `${audio ? '<span class="bk-badge">Audio</span>' : ''}`
        + `${local ? '<span class="bk-badge is-local" title="Pobrane na telefon">⤓</span>' : ''}`;
}

/** Cover object URLs are async (IndexedDB, then the package proxy). */
function hydrateCovers(root) {
    root.querySelectorAll('[data-cover]').forEach(async (node) => {
        const book = bookById(node.dataset.cover);
        if (!book) return;
        const url = await offline.coverUrl(book);
        if (!url || !node.isConnected) return;
        node.innerHTML = `<img src="${esc(url)}" alt="" loading="lazy" decoding="async">`
            + (node.classList.contains('bk-cover') ? badgesHtml(book) : '');
    });
}

function netPill(offlineMode) {
    state.offlineMode = offlineMode;
    if (!els.net) return;
    els.net.hidden = !offlineMode;
    els.net.textContent = offlineMode ? 'Offline' : '';
}

// ── auth ───────────────────────────────────────────────────────────────────

function authed() {
    const claims = getCurrentClaims();
    if (!claims) return false;
    if (claims.exp && claims.exp * 1000 <= Date.now() + 5000) {
        try { localStorage.removeItem('token'); } catch { /* ignore */ }
        return false;
    }
    return true;
}

function showLogin(message = '') {
    els.login.hidden = false;
    els.app.hidden = true;
    els.loginError.textContent = message;
    els.loginOffline.hidden = navigator.onLine;
    setTimeout(() => $('bk-username')?.focus(), 60);
}

function showApp() {
    els.login.hidden = true;
    els.app.hidden = false;
}

async function login(event) {
    event.preventDefault();
    els.loginError.textContent = '';
    const username = $('bk-username').value.trim();
    const password = $('bk-password').value;
    if (!username || !password) return;
    const submit = $('bk-login-submit');
    submit.disabled = true;
    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.token) throw new Error(data.detail || 'Nie udało się zalogować');
        setToken(data.token);
        try { localStorage.setItem('token', data.token); } catch { /* ignore */ }
        $('bk-password').value = '';
        showApp();
        await startApp();
    } catch (error) {
        els.loginError.textContent = errorText(error);
    } finally {
        submit.disabled = false;
    }
}

function logout() {
    engine.stop();
    offline.flushProgress().catch(() => {});
    try { localStorage.removeItem('token'); } catch { /* ignore */ }
    setToken(null);
    showLogin();
}

// ── data ──────────────────────────────────────────────────────────────────

function applyLibrary(data) {
    if (!data || typeof data !== 'object') return;
    state.library = {
        folders: Array.isArray(data.folders) ? data.folders : [],
        books: Array.isArray(data.books) ? data.books : [],
        shared_books: Array.isArray(data.shared_books) ? data.shared_books : [],
        progress: data.progress && typeof data.progress === 'object' ? data.progress : {},
        settings: data.settings && typeof data.settings === 'object' ? data.settings : {},
        stats: data.stats && typeof data.stats === 'object' ? data.stats : {},
    };
    state.loaded = true;
}

async function loadLibrary() {
    try {
        const data = await library();
        applyLibrary(data);
        netPill(false);
        await offline.saveLibrary(data);
        offline.flushProgress().catch(() => {});
    } catch (error) {
        const cached = await offline.loadLibrary();
        if (cached) {
            applyLibrary(cached);
            netPill(true);
            logWarn('Books library from cache', errorText(error));
        } else {
            netPill(true);
            logWarn('Books library unavailable', errorText(error));
        }
    }
}

async function refreshLibrary() {
    await loadLibrary();
    renderView();
}

// ── boot ──────────────────────────────────────────────────────────────────

async function startApp() {
    showApp();
    await offline.initOffline(() => {
        if (state.view === 'downloads' || state.view === 'settings') renderView();
    });
    await loadLibrary();
    engine.restoreSession();
    wireEngine();
    wirePublicUi();
    const params = new URLSearchParams(location.search);
    const initial = params.get('view');
    state.view = ['library', 'continue', 'downloads', 'settings'].includes(initial) ? initial : 'library';
    setNavActive(state.view);
    renderView();
    updateMini();
    const deepLink = params.get('book');
    if (deepLink) {
        const book = bookById(deepLink);
        if (book) openSheet(book);
        else toast('Nie znaleziono tej książki w bibliotece.', 'warning');
    }
}

// ── routing ───────────────────────────────────────────────────────────────

function setNavActive(view) {
    els.nav.querySelectorAll('.bk-navitem').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.view === view);
    });
}

function go(view) {
    state.view = view;
    setNavActive(view);
    renderView();
    els.content.scrollTop = 0;
}

function renderView() {
    setNavActive(state.view);
    switch (state.view) {
        case 'continue': renderContinue(); break;
        case 'downloads': renderDownloads(); break;
        case 'settings': renderSettings(); break;
        default: renderLibrary();
    }
}

function loadingHtml() {
    return '<div class="bk-spin" role="status" aria-label="Ładowanie"></div>';
}

// ── library ───────────────────────────────────────────────────────────────

function renderLibrary() {
    if (!state.loaded) { els.content.innerHTML = loadingHtml(); return; }
    const books = state.library.books || [];
    const shared = state.library.shared_books || [];
    const stats = state.library.stats || {};
    const filtered = books.filter((book) => {
        if (state.filter === 'audiobooks' && !isAudiobook(book)) return false;
        if (state.filter === 'books' && isAudiobook(book)) return false;
        return matchesQuery(book, state.query);
    }).sort((a, b) => String(a.title || '').localeCompare(String(b.title || ''), 'pl', { sensitivity: 'base' }));

    const continueRows = books
        .filter((book) => Number(progressFor(book)?.percent) > 0.001)
        .sort((a, b) => String(progressFor(b)?.updated_at || '').localeCompare(String(progressFor(a)?.updated_at || '')))
        .slice(0, 5);

    els.content.innerHTML = `
      <section class="bk-hero" data-testid="books-hero">
        <h1>Twoja biblioteka</h1>
        <p>${Number(stats.audiobooks) || 0} audiobooków · ${Number(stats.books) || 0} książek · ${esc(fmtBytes(stats.bytes))}</p>
      </section>
      ${continueRows.length ? `
        <div class="bk-section-title"><h2>Słuchaj dalej</h2><span class="count">${continueRows.length}</span></div>
        <div class="bk-list">${continueRows.map((book) => itemHtml(book)).join('')}</div>` : ''}
      <div class="bk-section-title">
        <h2>Wszystkie</h2>
        <span class="count" data-testid="books-count">${filtered.length}</span>
      </div>
      <input class="bk-input" id="bk-search" type="search" placeholder="Szukaj tytułu lub autora…"
             value="${esc(state.query)}" data-testid="books-search" autocomplete="off">
      <div class="bk-row" style="margin:10px 0 12px;flex-wrap:wrap">
        ${[['all', 'Wszystko'], ['audiobooks', 'Audiobooki'], ['books', 'Książki i PDF']].map(([key, label]) => `
          <button class="bk-btn small${state.filter === key ? ' primary' : ''}" type="button"
                  data-act="filter" data-filter="${key}">${label}</button>`).join('')}
      </div>
      <div class="bk-grid" id="bk-cards">${cardsHtml(filtered)}</div>
      ${shared.length ? `
        <div class="bk-section-title"><h2>Udostępnione mi</h2><span class="count">${shared.length}</span></div>
        <div class="bk-list">${shared.filter((book) => matchesQuery(book, state.query)).map((book) => itemHtml(book, true)).join('')}</div>` : ''}
      ${!books.length ? `
        <div class="bk-empty" data-testid="books-empty">
          <span class="big">📚</span>
          <div>Biblioteka jest pusta.</div>
          <div>Dodaj foldery w oknie <b>Books</b> na komputerze (Folders &amp; Scan) — telefon korzysta z tej samej biblioteki.</div>
        </div>` : ''}`;
    hydrateCovers(els.content);
}

function cardsHtml(books) {
    if (!books.length) return '<div class="bk-empty" style="grid-column:1/-1">Brak wyników.</div>';
    return books.map((book) => {
        const progress = progressFor(book);
        const value = percentOf(book, progress);
        return `
        <button class="bk-card" type="button" data-act="book" data-id="${esc(book.id)}" data-testid="books-card">
          ${coverHtml(book)}
          <span class="bk-card-body">
            <span class="bk-card-title">${esc(book.title || baseName(book.path))}</span>
            <span class="bk-card-sub">${esc(book.author || (isAudiobook(book) ? 'Audiobook' : 'Książka'))}</span>
            ${value > 0 && value < 100 ? `<span class="bk-progress"><i style="width:${value}%"></i></span>` : ''}
          </span>
        </button>`;
    }).join('');
}

function itemHtml(book, shared = false) {
    const progress = progressFor(book);
    const value = percentOf(book, progress);
    const audio = isAudiobook(book);
    const sub = audio
        ? `${progress ? `Rozdział ${(Number(progress.locator) || 0) + 1} · ${fmtTime(progress.position_s)}` : `${bookTracks(book).length} rozdziałów`}`
        : (book.series || book.author || 'Książka');
    return `
      <button class="bk-item" type="button" data-act="book" data-id="${esc(book.id)}" data-testid="books-item">
        ${coverHtml(book, 'thumb')}
        <span class="bk-item-main">
          <span class="bk-item-title">${esc(book.title || baseName(book.path))}</span>
          <span class="bk-item-sub">${esc(sub)}</span>
          ${value > 0 ? `<span class="bk-progress" style="margin-top:4px"><i style="width:${value}%"></i></span>` : ''}
        </span>
        <span class="bk-item-side">${shared ? 'tylko odczyt' : `${value}%`}</span>
      </button>`;
}

// ── continue ──────────────────────────────────────────────────────────────

function renderContinue() {
    if (!state.loaded) { els.content.innerHTML = loadingHtml(); return; }
    const rows = allBooks()
        .filter((book) => Number(progressFor(book)?.percent) > 0.001 && !progressFor(book)?.finished)
        .sort((a, b) => String(progressFor(b)?.updated_at || '').localeCompare(String(progressFor(a)?.updated_at || '')));
    els.content.innerHTML = `
      <div class="bk-section-title"><h2>W toku</h2><span class="count">${rows.length}</span></div>
      ${rows.length
        ? `<div class="bk-list">${rows.map((book) => itemHtml(book)).join('')}</div>`
        : `<div class="bk-empty"><span class="big">🎧</span><div>Nic tu jeszcze nie ma.</div>
             <div>Zacznij słuchać audiobooka albo czytać książkę — pozycja wróci tutaj.</div></div>`}`;
    hydrateCovers(els.content);
}

// ── downloads ─────────────────────────────────────────────────────────────

function renderDownloads() {
    const storage = offline.storageInfo();
    const rows = offline.snapshot();
    const byBook = new Map();
    rows.forEach((row) => {
        const entry = byBook.get(row.bookId) || { book: row.book, rows: [] };
        entry.rows.push(row);
        byBook.set(row.bookId, entry);
    });
    const paused = offline.isPaused();

    els.content.innerHTML = `
      <div class="bk-section-title"><h2>Na telefonie</h2><span class="count">${esc(fmtBytes(offline.totalBytes()))}</span></div>
      ${!window.isSecureContext ? `
        <div class="bk-hero" data-testid="books-https-note">
          <h1>Pobieranie wymaga HTTPS</h1>
          <p>Otwórz ten adres przez <b>https://</b> (lub localhost). Po HTTP dalej słuchasz i czytasz online,
             ale pliki nie zapiszą się na urządzeniu.</p>
        </div>` : ''}
      <div class="bk-stat"><span>Zajęte przez Books</span><b>${esc(fmtBytes(storage?.usage))}</b></div>
      <div class="bk-stat"><span>Limit przeglądarki</span><b>${storage?.quota ? esc(fmtBytes(storage.quota)) : 'n/d'}</b></div>
      ${paused ? `
        <div class="bk-hero">
          <h1>Pobieranie wstrzymane</h1>
          <p>${offline.pauseReason() === 'quota' ? 'Brak miejsca na urządzeniu.' : 'Pobieranie zostało zatrzymane.'}</p>
          <button class="bk-btn primary" type="button" data-act="resume-downloads">Wznów pobieranie</button>
        </div>` : ''}
      ${byBook.size ? `<div class="bk-list" style="margin-top:12px">${[...byBook.values()].map(({ book, rows: bookRows }) => {
        const done = bookRows.filter((row) => row.status === 'done').length;
        const total = bookRows.length;
        const percent = total ? Math.round((done / total) * 100) : 0;
        const failed = bookRows.find((row) => row.status === 'failed');
        return `
          <div class="bk-item" style="cursor:default" data-testid="books-download-row">
            ${coverHtml(book || { id: '', kind: 'audiobook' }, 'thumb')}
            <span class="bk-item-main">
              <span class="bk-item-title">${esc(book?.title || 'Pozycja')}</span>
              <span class="bk-item-sub">${done}/${total} plików · ${esc(fmtBytes(bookRows.reduce((sum, row) => sum + (Number(row.bytes) || 0), 0)))}</span>
              <span class="bk-progress" style="margin-top:4px"><i style="width:${percent}%"></i></span>
              ${failed ? `<span class="bk-item-sub" style="color:#ff8a80">${esc(failed.error || 'Błąd pobierania')}</span>` : ''}
            </span>
            <span class="bk-item-side">
              <button class="bk-btn small" type="button" data-act="open-download" data-id="${esc(book?.id || '')}">Otwórz</button>
              <button class="bk-btn small danger" type="button" data-act="delete-download" data-id="${esc(book?.id || '')}">Usuń</button>
            </span>
          </div>`;
    }).join('')}</div>` : `
        <div class="bk-empty" data-testid="books-downloads-empty">
          <span class="big">⤓</span><div>Nic nie pobrano.</div>
          <div>Otwórz pozycję i wybierz <b>Pobierz na telefon</b> — potem działa w trybie samolotowym.</div>
        </div>`}`;
    hydrateCovers(els.content);
}

// ── settings ──────────────────────────────────────────────────────────────

function renderSettings() {
    const value = settings();
    const options = (list, current) => list.map(([key, label]) => (
        `<option value="${esc(key)}"${String(current) === String(key) ? ' selected' : ''}>${esc(label)}</option>`
    )).join('');
    els.content.innerHTML = `
      <div class="bk-section-title"><h2>Czytnik</h2></div>
      <div class="bk-sheet-body" data-testid="books-settings">
        <div class="bk-setting"><label for="bk-set-theme">Motyw</label>
          <select class="bk-select" id="bk-set-theme" data-set="theme">
            ${options([['system', 'Systemowy'], ['light', 'Jasny'], ['sepia', 'Sepia'], ['dark', 'Ciemny']], value.theme || 'system')}
          </select></div>
        <div class="bk-setting"><label for="bk-set-font">Czcionka</label>
          <select class="bk-select" id="bk-set-font" data-set="font_family">
            ${options([['serif', 'Szeryfowa'], ['sans', 'Bezszeryfowa']], value.font_family || 'serif')}
          </select></div>
        <div class="bk-setting"><label for="bk-set-size">Rozmiar tekstu (%)</label>
          <input class="bk-input" id="bk-set-size" type="number" min="70" max="220" step="5"
                 data-set="font_size" value="${Number(value.font_size) || 100}"></div>
        <div class="bk-setting"><label for="bk-set-margin">Margines</label>
          <input class="bk-input" id="bk-set-margin" type="number" min="1" max="4" step="1"
                 data-set="margin" value="${Number(value.margin) || 2}"></div>
        <div class="bk-setting"><label for="bk-set-line">Wysokość linii</label>
          <input class="bk-input" id="bk-set-line" type="number" min="1.2" max="2.4" step="0.1"
                 data-set="line_height" value="${Number(value.line_height) || 1.6}"></div>
        <div class="bk-setting"><label for="bk-set-flow">Układ</label>
          <select class="bk-select" id="bk-set-flow" data-set="flow">
            ${options([['paginated', 'Strony'], ['scrolled', 'Przewijanie']], value.flow || 'paginated')}
          </select></div>
      </div>
      <div class="bk-section-title"><h2>Odtwarzacz</h2></div>
      <div class="bk-sheet-body">
        <div class="bk-setting"><label for="bk-set-skip">Skok (sekundy)</label>
          <input class="bk-input" id="bk-set-skip" type="number" min="5" max="300" step="5"
                 data-set="skip_seconds" value="${Number(value.skip_seconds) || 30}"></div>
        <div class="bk-setting"><label for="bk-set-speed">Prędkość</label>
          <select class="bk-select" id="bk-set-speed" data-set="speed">
            ${options([[0.75, '0.75×'], [1, '1×'], [1.25, '1.25×'], [1.5, '1.5×'], [1.75, '1.75×'], [2, '2×']], value.speed || 1)}
          </select></div>
      </div>
      <div class="bk-section-title"><h2>Aplikacja</h2></div>
      <div class="bk-sheet-body">
        <div class="bk-stat"><span>Biblioteka</span><b>${(state.library.books || []).length} pozycji</b></div>
        <div class="bk-stat"><span>Foldery skanowania</span><b>${(state.library.folders || []).length}</b></div>
        <div class="bk-stat"><span>Pobrane</span><b>${esc(fmtBytes(offline.totalBytes()))}</b></div>
        <div class="bk-row" style="flex-wrap:wrap;margin-top:6px">
          <button class="bk-btn small" type="button" data-act="refresh">Odśwież bibliotekę</button>
          <button class="bk-btn small" type="button" data-act="theme">Motyw jasny / ciemny</button>
          <button class="bk-btn small danger" type="button" data-act="logout">Wyloguj</button>
        </div>
        <p class="bk-muted" style="font-size:12px;margin:8px 0 0">
          Foldery i skanowanie ustawisz w oknie Books na komputerze. Zmiany w czytniku są wspólne dla obu.
        </p>
      </div>`;
}

function saveSettings(patch) {
    state.library.settings = { ...settings(), ...patch };
    if (state.saveTimer) clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(async () => {
        try {
            const saved = await putSettings(state.library.settings);
            if (saved && typeof saved === 'object') state.library.settings = { ...state.library.settings, ...saved };
            logInfo('Books settings saved', Object.keys(patch).join(','));
        } catch (error) {
            logWarn('Books settings not saved', errorText(error));
            toast('Nie udało się zapisać ustawień — spróbuj ponownie.', 'warning');
        }
    }, 400);
}

// ── sheet ─────────────────────────────────────────────────────────────────

function openSheet(book) {
    const progress = progressFor(book);
    const value = percentOf(book, progress);
    const audio = isAudiobook(book);
    const tracks = bookTracks(book);
    const downloaded = offline.isDownloaded(book);
    const partial = offline.downloadPercent(book);
    els.sheetTitle.textContent = book.title || baseName(book.path);
    els.sheetBody.innerHTML = `
      <div class="bk-detail">
        ${coverHtml(book, 'thumb')}
        <div>
          <h3>${esc(book.title || baseName(book.path))}</h3>
          <p>${esc(book.author || 'Nieznany autor')}</p>
          ${book.series ? `<p>${esc(book.series)}${book.series_index ? ` · tom ${esc(book.series_index)}` : ''}</p>` : ''}
          <p>${audio ? `${tracks.length} rozdziałów` : String(book.format || '').toUpperCase()} · ${esc(fmtBytes(book.size))}</p>
          ${value > 0 ? `<p>${value}%${audio && progress ? ` · rozdział ${(Number(progress.locator) || 0) + 1}, ${esc(fmtTime(progress.position_s))}` : ''}</p>` : ''}
        </div>
      </div>
      <div class="bk-actions">
        <button class="bk-btn primary" type="button" data-act="open" data-id="${esc(book.id)}">
          ${audio ? (value > 0 ? '▶ Słuchaj dalej' : '▶ Słuchaj') : (value > 0 ? '📖 Czytaj dalej' : '📖 Czytaj')}
        </button>
        ${downloaded
            ? `<button class="bk-btn danger" type="button" data-act="delete-download" data-id="${esc(book.id)}">Usuń z telefonu</button>`
            : `<button class="bk-btn" type="button" data-act="download" data-id="${esc(book.id)}">⤓ Pobierz na telefon</button>`}
      </div>
      ${!downloaded && partial > 0 ? `<div><div class="bk-progress"><i style="width:${partial}%"></i></div>
        <p class="bk-muted" style="font-size:12px">Pobieranie: ${partial}%</p></div>` : ''}
      <p class="bk-muted" style="font-size:12px">
        ${book.shared ? 'Pozycja udostępniona przez inny profil — tylko odczyt.' : `Plik: ${esc(baseName(book.path))}`}
      </p>`;
    els.sheet.hidden = false;
    hydrateCovers(els.sheetBody);
}

function closeSheet() {
    els.sheet.hidden = true;
}

// ── readers ───────────────────────────────────────────────────────────────

function closeReader() {
    if (state.reading) {
        try { state.reading.destroy?.(); } catch { /* already gone */ }
        state.reading = null;
    }
    els.reader.hidden = true;
    els.readerBody.innerHTML = '';
    els.readerFoot.innerHTML = '';
    els.readerActions.innerHTML = '';
    offline.flushProgress().catch(() => {});
    renderView();
}

function readerNote(message, tone = 'info') {
    toast(message, tone);
}

function openReader(book) {
    if (state.reading) closeReader();
    const value = settings();
    els.readerTitle.textContent = book.title || baseName(book.path);
    els.reader.className = `bk-reader theme-${readerThemeName(value)}`;
    els.reader.hidden = false;
    els.readerBody.innerHTML = '';
    els.readerFoot.innerHTML = '<span>Wczytywanie…</span>';
    els.readerActions.innerHTML = '';

    const onProgress = (payload) => {
        queue(book.id, payload);
    };
    const mount = book.format === 'pdf' ? mountPdf : mountEpub;
    state.reading = mount({
        book,
        body: els.readerBody,
        foot: els.readerFoot,
        actions: els.readerActions,
        settings: value,
        progress: progressFor(book),
        onProgress,
        note: readerNote,
    });
    logInfo('Books reader opened', `${book.id} ${book.format}`);
}

function queue(bookId, payload) {
    state.library.progress = state.library.progress || {};
    state.library.progress[bookId] = {
        ...(state.library.progress[bookId] || {}),
        ...payload,
        updated_at: new Date().toISOString(),
    };
    offline.queueProgress(bookId, payload);
}

// ── player wiring ─────────────────────────────────────────────────────────

let _engineWired = false;

function wireEngine() {
    if (_engineWired) return;
    _engineWired = true;
    engine.onProgress = (bookId, payload) => {
        if (!bookId) return;
        queue(bookId, payload);
    };
    engine.on('time', () => { updatePlayer(); updateMini(); });
    engine.on('playing', () => { updatePlayer(); updateMini(); });
    engine.on('track', () => { renderTracks(); updatePlayer(); updateMini(); });
    engine.on('ended', () => {
        toast('Koniec audiobooka.', 'success', 'Books');
        renderView();
    });
    engine.on('sleep', ({ minutes }) => {
        toast(`Wyłącznik czasowy zatrzymał odtwarzanie po ${minutes} min.`, 'info', 'Books');
        els.sleepHint.hidden = true;
    });
    engine.on('error', () => {
        toast('Nie udało się odtworzyć tego rozdziału — plik mógł zostać przeniesiony.', 'error', 'Books');
    });
    engine.on('blocked', () => {
        toast('Naciśnij ▶, żeby zacząć słuchać.', 'info', 'Books');
    });
    updatePlayer();
    updateMini();
}

function openPlayer(book, { autoplay = true } = {}) {
    engine.playBook(book, { settings: settings(), progress: progressFor(book), autoplay });
    els.player.hidden = false;
    els.tracks.hidden = true;
    renderTracks();
    updatePlayer();
    updateMini();
    const cover = offline.coverUrl(book);
    cover.then((url) => {
        if (!url) { els.playerArt.classList.add('is-empty'); els.playerArt.removeAttribute('src'); els.playerArt.alt = '🎧'; return; }
        els.playerArt.classList.remove('is-empty');
        els.playerArt.src = url;
        els.playerArt.alt = '';
    }).catch(() => {});
    if (!autoplay) return;
    engine.play();
}

function closePlayer() {
    els.player.hidden = true;
    els.tracks.hidden = true;
    engine.saveProgress();
}

function updatePlayer() {
    if (els.player.hidden) return;
    const book = engine.book;
    if (!book) return;
    const track = engine.current;
    els.playerTitle.textContent = book.title || 'Audiobook';
    els.playerSub.textContent = [book.author, book.series].filter(Boolean).join(' · ');
    els.playerTrack.textContent = engine.tracks.length > 1
        ? `Rozdział ${engine.index + 1} z ${engine.tracks.length} · ${trackTitle(track)}`
        : trackTitle(track);
    els.play.textContent = engine.isPlaying ? '⏸' : '▶';
    els.speed.textContent = `${String(engine.speed).replace('.', ',')}×`;
    const pos = Number(engine.audio.currentTime) || 0;
    const dur = Number(engine.audio.duration) || 0;
    els.pos.textContent = fmtTime(pos);
    els.dur.textContent = fmtTime(dur);
    if (dur) els.seek.value = String(Math.round((pos / dur) * 1000));
    els.back.textContent = `−${engine.skip}s`;
    els.fwd.textContent = `+${engine.skip}s`;
    els.sleep.textContent = engine.sleepMinutesLeft() ? `⏱ ${engine.sleepMinutesLeft()}` : '⏱';
    els.sleepHint.hidden = !engine.sleepMinutesLeft();
}

function renderTracks() {
    if (!engine.book) { els.tracksList.innerHTML = ''; return; }
    els.tracksList.innerHTML = engine.tracks.map((track, index) => `
      <button class="bk-track${index === engine.index ? ' is-active' : ''}" type="button" data-track="${index}">
        <span class="num">${index + 1}</span>
        <span>${esc(trackTitle(track))}</span>
        <span class="dur">${esc(fmtBytes(track.size))}</span>
      </button>`).join('');
}

function updateMini() {
    const book = engine.book;
    const show = !!book;
    els.mini.hidden = !show;
    els.app.classList.toggle('has-mini', show);
    if (!show) {
        try { localStorage.removeItem('ethos.books.mini'); } catch { /* ignore */ }
        return;
    }
    els.miniTitle.textContent = book.title || 'Audiobook';
    els.miniSub.textContent = engine.tracks.length > 1
        ? `Rozdział ${engine.index + 1} · ${trackTitle(engine.current)}`
        : (book.author || 'Audiobook');
    els.miniPlay.textContent = engine.isPlaying ? '⏸' : '▶';
    const value = pct(engine.percent());
    els.miniProgress.style.width = `${value}%`;
    offline.coverUrl(book).then((url) => {
        if (url) els.miniArt.src = url;
    }).catch(() => {});
}

// ── public UI wiring ──────────────────────────────────────────────────────

function wirePublicUi() {
    els.loginForm.addEventListener('submit', login);
    els.menuBtn.addEventListener('click', () => {
        const open = els.menu.hidden;
        els.menu.hidden = !open;
        els.menuBtn.setAttribute('aria-expanded', String(open));
    });
    els.menu.addEventListener('click', (event) => {
        const action = event.target.closest('[data-action]')?.dataset.action;
        els.menu.hidden = true;
        els.menuBtn.setAttribute('aria-expanded', 'false');
        if (action === 'downloads') go('downloads');
        else if (action === 'theme') toggleTheme();
        else if (action === 'logout') logout();
    });

    els.nav.addEventListener('click', (event) => {
        const view = event.target.closest('[data-view]')?.dataset.view;
        if (view) go(view);
    });

    els.mini.addEventListener('click', (event) => {
        if (event.target.closest('#bk-mini-play')) return;
        if (engine.book) { els.player.hidden = false; updatePlayer(); }
    });
    els.miniPlay.addEventListener('click', (event) => {
        event.stopPropagation();
        engine.toggle();
        updateMini();
    });

    $('bk-player-close').addEventListener('click', closePlayer);
    els.play.addEventListener('click', () => { engine.toggle(); updatePlayer(); });
    $('bk-prev').addEventListener('click', () => { engine.prev(); updatePlayer(); });
    $('bk-next').addEventListener('click', () => { engine.next(); updatePlayer(); });
    $('bk-back').addEventListener('click', () => { engine.seekBy(-engine.skip); updatePlayer(); });
    $('bk-fwd').addEventListener('click', () => { engine.seekBy(engine.skip); updatePlayer(); });
    els.seek.addEventListener('input', () => {
        const dur = Number(engine.audio.duration) || 0;
        if (dur) engine.seek((Number(els.seek.value) / 1000) * dur);
    });
    els.speed.addEventListener('click', () => {
        const index = state.speeds.findIndex((value) => Math.abs(value - engine.speed) < 0.01);
        const next = state.speeds[(index + 1) % state.speeds.length];
        engine.setSpeed(next);
        saveSettings({ speed: next });
        updatePlayer();
    });
    els.sleep.addEventListener('click', () => {
        const current = Number(els.sleep.dataset.minutes) || 0;
        const nextIndex = (state.sleeps.indexOf(current) + 1) % state.sleeps.length;
        const minutes = state.sleeps[nextIndex];
        els.sleep.dataset.minutes = String(minutes);
        engine.sleep(minutes);
        updatePlayer();
        if (minutes) toast(`Wyłącznik czasowy: ${minutes} min.`, 'info', 'Books');
    });
    $('bk-tracks-btn').addEventListener('click', () => {
        els.tracks.hidden = !els.tracks.hidden;
        if (!els.tracks.hidden) renderTracks();
    });
    $('bk-tracks-close').addEventListener('click', () => { els.tracks.hidden = true; });
    els.tracksList.addEventListener('click', (event) => {
        const row = event.target.closest('[data-track]');
        if (!row) return;
        engine.jumpTo(Number(row.dataset.track));
        els.tracks.hidden = true;
        updatePlayer();
    });

    $('bk-reader-back').addEventListener('click', closeReader);
    els.sheet.addEventListener('click', (event) => {
        if (event.target.closest('[data-close="sheet"]')) closeSheet();
    });

    els.content.addEventListener('click', onContentClick);
    els.sheetBody.addEventListener('click', onSheetAction);
    els.sheetBody.addEventListener('change', onSettingsChange);
    els.content.addEventListener('change', onSettingsChange);
    els.content.addEventListener('input', (event) => {
        if (event.target.id !== 'bk-search') return;
        state.query = event.target.value;
        const cards = $('bk-cards');
        if (!cards) return;
        const books = (state.library.books || []).filter((book) => {
            if (state.filter === 'audiobooks' && !isAudiobook(book)) return false;
            if (state.filter === 'books' && isAudiobook(book)) return false;
            return matchesQuery(book, state.query);
        }).sort((a, b) => String(a.title || '').localeCompare(String(b.title || ''), 'pl', { sensitivity: 'base' }));
        cards.innerHTML = cardsHtml(books);
        hydrateCovers(cards);
    });

    window.addEventListener('online', () => {
        netPill(false);
        offline.flushProgress().catch(() => {});
        refreshLibrary().catch(() => {});
    });
    window.addEventListener('offline', () => netPill(true));
    window.addEventListener('pagehide', () => { engine.saveProgress(); offline.flushProgress().catch(() => {}); });
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') engine.saveProgress();
        else offline.flushProgress().catch(() => {});
    });

    window.addEventListener('beforeinstallprompt', (event) => {
        event.preventDefault();
        state.installPrompt = event;
        els.install.hidden = false;
    });
    els.install.addEventListener('click', async () => {
        if (!state.installPrompt) return;
        state.installPrompt.prompt();
        try { await state.installPrompt.userChoice; } catch { /* ignore */ }
        state.installPrompt = null;
        els.install.hidden = true;
    });
    if (window.matchMedia('(display-mode: standalone)').matches) els.install.hidden = true;
    if ('serviceWorker' in navigator && window.isSecureContext) {
        navigator.serviceWorker.register('/sw.js').catch((error) => logWarn('SW registration failed', String(error)));
    }
}

function onContentClick(event) {
    const target = event.target.closest('[data-act]');
    if (!target) return;
    const action = target.dataset.act;
    if (action === 'book') {
        const book = bookById(target.dataset.id);
        if (book) openSheet(book);
    } else if (action === 'filter') {
        state.filter = target.dataset.filter || 'all';
        renderLibrary();
    } else if (action === 'resume-downloads') {
        offline.resumeDownloads();
        renderDownloads();
    } else if (action === 'delete-download') {
        deleteDownload(target.dataset.id);
    } else if (action === 'open-download') {
        const book = bookById(target.dataset.id);
        if (book) openSheet(book);
    } else if (action === 'refresh') {
        refreshLibrary().then(() => toast('Biblioteka odświeżona.', 'success')).catch((error) => toast(errorText(error), 'error'));
    } else if (action === 'theme') {
        toggleTheme();
    } else if (action === 'logout') {
        logout();
    }
}

function onSheetAction(event) {
    const target = event.target.closest('[data-act]');
    if (!target) return;
    const book = bookById(target.dataset.id);
    const action = target.dataset.act;
    if (action === 'open' && book) {
        closeSheet();
        if (isAudiobook(book)) openPlayer(book);
        else openReader(book);
    } else if (action === 'download' && book) {
        if (!window.isSecureContext) {
            toast('Pobieranie wymaga HTTPS — otwórz Books przez adres z certyfikatem.', 'warning');
            return;
        }
        const queued = offline.downloadBook(book);
        toast(queued ? 'Pobieranie w kolejce — zobacz „Pobrane”.' : 'To już jest na telefonie.', 'success');
        closeSheet();
        go('downloads');
    } else if (action === 'delete-download') {
        deleteDownload(target.dataset.id);
    }
}

async function deleteDownload(bookId) {
    const book = bookById(bookId);
    if (!book) return;
    await offline.deleteBook(book);
    toast('Usunięto z telefonu.', 'success');
    if (state.view === 'downloads') renderDownloads();
    else if (!els.sheet.hidden) openSheet(book);
}

function onSettingsChange(event) {
    const field = event.target.closest('[data-set]');
    if (!field) return;
    const key = field.dataset.set;
    let value = field.value;
    if (['font_size', 'margin', 'skip_seconds'].includes(key)) value = Number(value) || 0;
    else if (['line_height', 'speed'].includes(key)) value = Number(value) || 0;
    saveSettings({ [key]: value });
    if (key === 'speed') engine.setSpeed(Number(value) || 1);
}

function toggleTheme() {
    const root = document.documentElement;
    const dark = !root.classList.contains('dark');
    root.className = dark ? 'dark' : 'light';
    try { localStorage.setItem('ethos-theme', root.className); } catch { /* ignore */ }
}

// ── start ─────────────────────────────────────────────────────────────────

if (authed()) {
    startApp().catch((error) => {
        logError('Books boot failed', errorText(error));
        showLogin(errorText(error));
    });
} else {
    showLogin();
    els.loginForm.addEventListener('submit', login);
    els.loginOffline.hidden = navigator.onLine;
}
