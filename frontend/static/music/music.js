// EthOS Music — standalone phone PWA at /music.
//
// The only mobile Music client: it replaces the EthOS Music Android APK. The
// desktop keeps its windowed package; both talk to the same /api/music/*
// endpoints, so likes, playlists, history and podcast subscriptions stay in
// sync across devices.
//
// Rules shared with every app: toasts come from core/notification-center.js,
// long work reports through core/progress.js, diagnostics through
// core/logger.js. Device downloads live in ./offline.js (IndexedDB Blobs);
// playback lives in ./player.js.

import { apiFetch, getToken, setToken, getCurrentClaims } from '../core/api.js';
import { notify } from '../core/notification-center.js';
import { logError, logInfo, logWarn, setLogContext } from '../core/logger.js';
import '../core/error-reporter.js';
import { engine } from './player.js';
import * as offline from './offline.js';
import { json, tkey, fmtDur, fmtBytes, artUrl, isEpisode, sha16 } from './api.js';

setLogContext({ app: 'music' });

const $ = (id) => document.getElementById(id);

const els = {
    login: $('mu-login'),
    loginForm: $('mu-login-form'),
    loginError: $('mu-login-error'),
    app: $('mu-app'),
    net: $('mu-net'),
    install: $('mu-install'),
    menuBtn: $('mu-menu-btn'),
    menu: $('mu-menu'),
    content: $('mu-content'),
    nav: $('mu-nav'),
    mini: $('mu-mini'),
    miniArt: $('mu-mini-art'),
    miniTitle: $('mu-mini-title'),
    miniSub: $('mu-mini-sub'),
    miniPlay: $('mu-mini-play'),
    miniProgress: $('mu-mini-progress'),
    player: $('mu-player'),
    playerBg: $('mu-player-bg'),
    playerArt: $('mu-player-art'),
    playerTitle: $('mu-player-title'),
    playerSub: $('mu-player-sub'),
    playerLabel: $('mu-player-label'),
    pos: $('mu-pos'),
    dur: $('mu-dur'),
    seek: $('mu-seek'),
    live: $('mu-player-live'),
    like: $('mu-player-like'),
    shuffle: $('mu-shuffle'),
    repeat: $('mu-repeat'),
    play: $('mu-play'),
    sleep: $('mu-sleep'),
    sleepHint: $('mu-sleep-hint'),
    queue: $('mu-queue'),
    queueList: $('mu-queue-list'),
    sheet: $('mu-sheet'),
    sheetTitle: $('mu-sheet-title'),
    sheetBody: $('mu-sheet-body'),
};

const CATEGORIES = [
    { name: 'Polskie hity', query: 'polskie hity 2025', color: '#e13300' },
    { name: 'Polski rap', query: 'polski rap', color: '#d80073' },
    { name: 'Disco polo', query: 'disco polo klasyki', color: '#8d67ab' },
    { name: 'Rock', query: 'polski rock klasyka', color: '#da2b60' },
    { name: 'Elektronika', query: 'polska muzyka elektroniczna', color: '#148a08' },
    { name: 'Jazz', query: 'polski jazz', color: '#f5a800' },
    { name: 'Klasyka', query: 'muzyka klasyczna chopin', color: '#537eec' },
    { name: 'Podcasty', query: 'kryminał', color: '#b0c4de' },
];

const HISTORY_KEY = 'ethos.music.searchHistory';
const HISTORY_MAX = 12;
// Backed by the server as a real playlist (id "liked"); the heart is just a
// shortcut for adding/removing a track there, which also makes it download.
const LIKED_PLAYLIST_ID = 'liked';
const LIKED_PLAYLIST_NAME = 'Polubione';

const store = {
    liked: [],
    playlists: [],
    history: [],
    podcasts: { subscribed: [], episodes: {} },
    sharedPlaylists: [],
    radioFavorites: [],
    radioRecents: [],
    loaded: false,
};

const ui = {
    view: 'home',
    trackIndex: new Map(),
    searchSeq: 0,
    suggestSeq: 0,
    searchState: null,
    queueContext: { tracks: [], key: '' },
    stations: null,
    stationCategories: [],
    searchStations: [],
    podcastCatalog: null,
    activePodcast: null,
    podcastFailedId: null,
    activePlaylist: null,
    installPrompt: null,
    wired: false,
    engineWired: false,
};

// ── helpers ────────────────────────────────────────────────────────────────

function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function toast(message, kind = 'info') {
    try { notify({ kind, message }); } catch { /* host missing */ }
}

function errorText(error) {
    return error?.detail || error?.message || 'Nie udało się wykonać operacji';
}

function apiPost(path, body) {
    return json(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
}

function nowGreeting() {
    const hour = new Date().getHours();
    if (hour < 12) return 'Dzień dobry';
    if (hour < 18) return 'Dobry dzień';
    return 'Dobry wieczór';
}

function trackDuration(track) {
    return Number(track?.duration || track?.dur || 0);
}

function indexTracks(tracks) {
    (tracks || []).forEach((track) => { if (track) ui.trackIndex.set(tkey(track), track); });
}

function trackByKey(key) {
    return ui.trackIndex.get(key) || null;
}

/** The list a tapped row belongs to, so "next" keeps playing the album/list. */
function setQueueContext(tracks, currentKey = '') {
    ui.queueContext = {
        tracks: (tracks || []).filter((track) => track && !track.is_live),
        key: currentKey || '',
    };
}

/** Replace remote artwork with the offline copy once it exists. */
async function hydrateCovers(root = document) {
    const images = [...root.querySelectorAll('img[data-cover-key]')];
    await Promise.all(images.map(async (img) => {
        const url = await offline.localThumb(img.dataset.coverKey).catch(() => null);
        if (url && img.isConnected) img.src = url;
    }));
}

function searchHistory() {
    try {
        const raw = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
        return Array.isArray(raw) ? raw.slice(0, HISTORY_MAX) : [];
    } catch { return []; }
}

function pushSearchHistory(query) {
    const q = String(query || '').trim();
    if (!q) return;
    const next = [q, ...searchHistory().filter((item) => item.toLowerCase() !== q.toLowerCase())].slice(0, HISTORY_MAX);
    try { localStorage.setItem(HISTORY_KEY, JSON.stringify(next)); } catch { /* private mode */ }
}

// ── auth ───────────────────────────────────────────────────────────────────

function authed() {
    const payload = getCurrentClaims();
    if (!payload) return false;
    if (payload.exp && payload.exp * 1000 <= Date.now() + 5000) {
        try { localStorage.removeItem('token'); } catch { /* ignore */ }
        return false;
    }
    return true;
}

function showLogin(message = '') {
    els.login.hidden = false;
    els.app.hidden = true;
    els.loginError.textContent = message;
    setTimeout(() => $('mu-username')?.focus(), 60);
}

function showApp() {
    els.login.hidden = true;
    els.app.hidden = false;
}

async function login(event) {
    event.preventDefault();
    els.loginError.textContent = '';
    const username = $('mu-username').value.trim();
    const password = $('mu-password').value;
    if (!username || !password) return;
    const submit = $('mu-login-submit');
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
        $('mu-password').value = '';
        showApp();
        await startApp();
    } catch (error) {
        els.loginError.textContent = errorText(error);
    } finally {
        submit.disabled = false;
    }
}

function logout() {
    try { localStorage.removeItem('token'); } catch { /* ignore */ }
    setToken(null);
    engine.audio.pause();
    try { localStorage.removeItem('ethos.music.session'); } catch { /* ignore */ }
    showLogin();
}

// ── state ──────────────────────────────────────────────────────────────────

function applyState(state) {
    store.liked = state.liked || [];
    store.playlists = state.playlists || [];
    store.history = state.history || [];
    store.podcasts = state.podcasts || { subscribed: [], episodes: {} };
    if (!store.podcasts.episodes) store.podcasts.episodes = {};
    // The server owns "Polubione"; synthesise it only against a server that
    // predates the change (or an empty/missing response), so likes still
    // download instead of silently sitting in a list nothing mirrors.
    let likedPlaylist = store.playlists.find((playlist) => playlist.id === LIKED_PLAYLIST_ID);
    if (!likedPlaylist) {
        likedPlaylist = { id: LIKED_PLAYLIST_ID, name: LIKED_PLAYLIST_NAME, tracks: store.liked.slice() };
        store.playlists.unshift(likedPlaylist);
    }
    likedPlaylist.name = LIKED_PLAYLIST_NAME;
    likedPlaylist.tracks = likedPlaylist.tracks || [];
    if (likedPlaylist.tracks.length < store.liked.length) {
        const seen = new Set(likedPlaylist.tracks.map((track) => tkey(track)));
        store.liked.forEach((track) => {
            if (!seen.has(tkey(track))) likedPlaylist.tracks.push(track);
        });
    }
    store.loaded = true;
}

function likedPlaylist() {
    return store.playlists.find((playlist) => playlist.id === LIKED_PLAYLIST_ID) || null;
}

function updateDownloadsMenu() {
    const button = els.menu?.querySelector('[data-action="downloads"]');
    if (!button) return;
    const pending = offline.downloadsSnapshot()
        .filter((row) => ['queued', 'downloading', 'waiting'].includes(row.status)).length;
    button.textContent = pending ? `Pobrane · ${pending} w toku` : 'Pobrane';
}

/** How much of a playlist already lives on this device. */
function playlistOfflineStats(playlist) {
    const tracks = (playlist?.tracks || []).filter((track) => track && (track.id || track.file_url));
    const total = tracks.length;
    if (!total) return { total: 0, done: 0, pending: 0, percent: 0, state: 'empty' };
    const statuses = new Map(offline.downloadsSnapshot().map((row) => [row.key, row.status]));
    let done = 0;
    let pending = 0;
    tracks.forEach((track) => {
        const status = statuses.get(tkey(track));
        if (status === 'done') done += 1;
        else if (status === 'queued' || status === 'downloading' || status === 'waiting') pending += 1;
    });
    const state = done === total ? 'full' : (done > 0 || pending > 0) ? 'partial' : 'none';
    return { total, done, pending, percent: Math.round((done / total) * 100), state };
}

function playlistBadgeHtml(stats) {
    if (!stats.total) return '';
    const icon = stats.state === 'full' ? '✓' : '↓';
    return `<span class="mu-card-badge ${stats.state}">${icon} ${stats.done}/${stats.total}</span>`;
}

function playlistBadgeTitle(stats) {
    if (stats.state === 'full') return `Cała playlista (${stats.total}) jest na tym urządzeniu`;
    if (stats.state === 'partial') return `${stats.done} z ${stats.total} na urządzeniu`
        + (stats.pending ? `, ${stats.pending} w kolejce` : '');
    return `Nic z ${stats.total} utworów nie jest jeszcze pobrane`;
}

/** Refresh the badges in place so a running download does not re-render the view. */
function updatePlaylistBadges() {
    document.querySelectorAll('[data-playlist-badge]').forEach((el) => {
        const playlist = store.playlists.find((item) => item.id === el.dataset.playlistBadge);
        const stats = playlistOfflineStats(playlist);
        el.innerHTML = playlistBadgeHtml(stats);
        el.title = playlistBadgeTitle(stats);
        el.hidden = !stats.total;
    });
    const detail = document.querySelector('[data-playlist-download]');
    if (detail && ui.activePlaylist) {
        const stats = playlistOfflineStats(ui.activePlaylist);
        detail.textContent = stats.total
            ? `${stats.done} z ${stats.total} na urządzeniu${stats.pending ? ` · ${stats.pending} w kolejce` : ''}`
            : '';
        const meter = document.querySelector('[data-playlist-meter] i');
        if (meter) meter.style.width = `${stats.percent}%`;
    }
}

function setNet(offlineMode) {
    if (!els.net) return;
    els.net.hidden = !offlineMode;
}

async function loadState() {
    try {
        const state = await json('/api/music/state');
        applyState(state);
        offline.saveLibraryState(state);
        setNet(false);
    } catch (error) {
        const cached = await offline.loadLibraryState();
        if (cached) {
            applyState(cached);
            setNet(!navigator.onLine);
            logWarn('Music state from cache', errorText(error));
        } else {
            setNet(true);
            logWarn('Music state unavailable', errorText(error));
        }
    }
    try {
        const radio = await json('/api/music/radio/state');
        store.radioFavorites = radio.favorites || [];
        store.radioRecents = radio.recents || [];
    } catch { /* radio state is optional offline */ }
    try {
        const shared = await json('/api/sharing/incoming?kind=playlist');
        store.sharedPlaylists = shared.grants || [];
    } catch { store.sharedPlaylists = []; }
    offline.syncPlaylists(store.playlists);
    updateDownloadsMenu();
}

let _lastRefresh = 0;

/** Quiet refresh for a PWA that was left in the background.
 *
 * Playlists edited on the desktop (or likes added there) must reach the phone
 * without a full reload — that is what starts the offline mirror. Throttled so
 * switching tabs does not hammer the NAS.
 */
async function refreshLibrary() {
    if (!getToken() || Date.now() - _lastRefresh < 15000) return;
    _lastRefresh = Date.now();
    try {
        const state = await json('/api/music/state');
        applyState(state);
        offline.saveLibraryState(state);
        await persistPlaylists();
        updateDownloadsMenu();
        if (['home', 'library', 'playlist'].includes(ui.view)) renderView();
        if (engine.current) updatePlayer();
    } catch { /* offline — keep the cached library */ }
}

async function persistPlaylists() {
    offline.syncPlaylists(store.playlists);
}

// ── app start ──────────────────────────────────────────────────────────────

async function startApp() {
    showApp();
    await offline.initOffline(() => {
        updateDownloadsMenu();
        updatePlaylistBadges();
        if (ui.view === 'downloads' || ui.view === 'settings') renderView();
    });
    await loadState();
    engine.restoreSession();
    wireEngine();
    wirePublicUi();
    const params = new URLSearchParams(location.search);
    const initial = params.get('view');
    const detailId = params.get('id') || '';
    if (initial === 'playlist' && detailId) {
        ui.view = 'home';
        renderView();
        openPlaylistById(detailId, { push: false });
    } else if (initial === 'podcast' && detailId) {
        ui.view = 'home';
        renderView();
        openPodcast(detailId, { push: false });
    } else {
        ui.view = ['search', 'downloads', 'library', 'radio', 'podcasts', 'settings'].includes(initial) ? initial : 'home';
        renderView();
    }
    updateMini();
    updatePlayer();
}

// ── routing ────────────────────────────────────────────────────────────────

function navView(view) {
    if (view === 'playlist' || view === 'podcast') return view === 'playlist' ? 'library' : 'podcasts';
    return view;
}

function setNavActive(view) {
    const active = navView(view);
    els.nav.querySelectorAll('.mu-navitem').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.view === active);
    });
}

function go(view) {
    ui.view = view;
    if (view !== 'playlist') ui.activePlaylist = null;
    if (view !== 'podcast') ui.activePodcast = null;
    setNavActive(view);
    renderView();
    els.content.scrollTop = 0;
}

function renderView() {
    setNavActive(ui.view);
    switch (ui.view) {
        case 'search': renderSearch(); break;
        case 'podcasts': renderPodcasts(); break;
        case 'radio': renderRadio(); break;
        case 'library': renderLibrary(); break;
        case 'downloads': renderDownloads(); break;
        case 'settings': renderSettings(); break;
        case 'playlist': renderPlaylistDetail(); break;
        case 'podcast': renderPodcastDetail(); break;
        default: renderHome();
    }
    updatePlaylistBadges();
    updateDownloadsMenu();
    hydrateCovers();
}

function loadingHtml(text = 'Ładowanie…') {
    return `<div class="mu-spin" role="status" aria-label="${esc(text)}"></div>`;
}

function emptyHtml(text) {
    return `<p class="mu-empty">${esc(text)}</p>`;
}

// ── shared markup ──────────────────────────────────────────────────────────

function rowHtml(track, { sub = '', badge = '', ctx = 'search', duration = true } = {}) {
    const key = tkey(track);
    const art = artUrl(track);
    const local = offline.hasLocal(track);
    const dur = trackDuration(track);
    const badgeCls = badge?.cls ? ` ${esc(badge.cls)}` : '';
    return `
    <div class="mu-row idle-hidden" data-act="play-track" data-key="${esc(key)}" data-ctx="${esc(ctx)}" role="button" tabindex="0">
        ${art ? `<img class="mu-row-art" src="${esc(art)}" data-cover-key="${esc(key)}" alt="" loading="lazy" decoding="async">`
        : '<span class="mu-row-art" aria-hidden="true"></span>'}
        <div class="mu-row-main">
            <div class="mu-row-title">${esc(track.title || track.name || '')}</div>
            <div class="mu-row-sub">${esc(sub || track.artist || '')}</div>
        </div>
        ${local ? '<span class="mu-row-badge">offline</span>' : ''}
        ${badge ? `<span class="mu-row-badge${badgeCls}">${esc(badge.text)}</span>` : ''}
        ${duration && dur ? `<span class="mu-row-dur">${fmtDur(dur)}</span>` : ''}
        <button type="button" class="mu-row-more" data-act="track-menu" data-key="${esc(key)}" data-ctx="${esc(ctx)}" aria-label="Opcje utworu">⋮</button>
    </div>`;
}

function cardHtml({ image, label, sub, act, id, playlistId = '', coverKey = '' }) {
    return `
    <div class="mu-card" data-act="${esc(act)}" data-id="${esc(id || '')}" role="button" tabindex="0">
        ${image ? `<img class="mu-card-cover" src="${esc(image)}" ${coverKey ? `data-cover-key="${esc(coverKey)}"` : ''} alt="" loading="lazy" decoding="async">`
        : '<span class="mu-card-cover" aria-hidden="true"></span>'}
        <div class="mu-card-label">${esc(label || '')}</div>
        <div class="mu-card-sub">${esc(sub || '')}</div>
        ${playlistId ? `<span class="mu-card-status" data-playlist-badge="${esc(playlistId)}" hidden></span>` : ''}
    </div>`;
}

// ── home ───────────────────────────────────────────────────────────────────

function renderHome() {
    const recent = store.history.slice(0, 8);
    indexTracks(recent);
    setQueueContext(recent);
    const playlistCards = store.playlists.slice(0, 6).map((playlist) => cardHtml({
        image: artUrl(playlist.tracks?.[0] || {}),
        label: playlist.name,
        sub: `${playlist.tracks?.length || 0} utworów`,
        act: 'open-playlist',
        id: playlist.id,
        playlistId: playlist.id,
        coverKey: playlist.tracks?.[0] ? tkey(playlist.tracks[0]) : '',
    })).join('');

    els.content.innerHTML = `
        <h1 class="mu-h1">${esc(nowGreeting())}</h1>
        <div class="mu-grid">
            <div class="mu-tile tile-radio" data-act="go" data-view="radio" role="button" tabindex="0">Radio</div>
            <div class="mu-tile tile-podcasts" data-act="go" data-view="podcasts" role="button" tabindex="0">Podcasty</div>
            <div class="mu-tile tile-downloads" data-act="go" data-view="downloads" role="button" tabindex="0">Pobrane</div>
            <div class="mu-tile tile-library" data-act="go" data-view="library" role="button" tabindex="0">Biblioteka</div>
        </div>
        ${recent.length ? `<h2 class="mu-section-title">Ostatnio odtwarzane</h2>${recent.map((track) => rowHtml(track, { ctx: 'home' })).join('')}` : ''}
        ${playlistCards ? `<h2 class="mu-section-title">Twoje playlisty</h2><div class="mu-grid">${playlistCards}</div>` : ''}
        ${!recent.length && !playlistCards ? emptyHtml('Zacznij od wyszukania utworu — wszystko, co dodasz do playlisty, pobierze się na telefon.') : ''}
    `;
}

// ── search ─────────────────────────────────────────────────────────────────

function emptySearchState(query) {
    return {
        query: query || '',
        tracks: [],
        tracksTotal: 0,
        hasMore: false,
        nextOffset: 0,
        podcasts: [],
        radio: [],
        library: [],
        playlists: [],
    };
}

function paintSearchIntro() {
    const results = $('mu-results');
    if (!results) return;
    const history = searchHistory();
    results.innerHTML = history.length
        ? `<h2 class="mu-section-title">Ostatnie wyszukiwania</h2>${history.map((q) => `
            <div class="mu-row" data-act="history" data-query="${esc(q)}" role="button" tabindex="0">
                <span class="mu-row-art" aria-hidden="true"></span>
                <div class="mu-row-main"><div class="mu-row-title">${esc(q)}</div></div>
            </div>`).join('')}`
        : emptyHtml('Wpisz tytuł, wykonawcę, podcast albo stację radiową.');
}

/** Render the accumulated search state — appending a page only extends it. */
function paintSearchResults() {
    const state = ui.searchState;
    const results = $('mu-results');
    if (!state || !results) return;
    const suggest = $('mu-suggest');
    if (suggest) suggest.hidden = true;
    const empty = !state.tracks.length && !state.podcasts.length && !state.radio.length
        && !state.library.length && !state.playlists.length;
    results.innerHTML = `
        ${state.tracks.length ? `<h2 class="mu-section-title">Utwory</h2>${state.tracks.map((track) => rowHtml(track, { ctx: 'search' })).join('')}` : ''}
        ${state.hasMore ? `<button type="button" class="mu-more" data-act="more-tracks" data-offset="${state.nextOffset}">Więcej wyników</button>` : ''}
        ${state.podcasts.length ? `<h2 class="mu-section-title">Podcasty</h2><div class="mu-grid">${state.podcasts.slice(0, 6).map((podcast) => cardHtml({
        image: podcast.cover_url || podcast.image,
        label: podcast.name,
        sub: podcast.author || '',
        act: 'open-podcast',
        id: podcast.id,
    })).join('')}</div>` : ''}
        ${state.radio.length ? `<h2 class="mu-section-title">Stacje radiowe</h2>${state.radio.slice(0, 8).map((station) => stationRowHtml(station)).join('')}` : ''}
        ${state.library.length ? `<h2 class="mu-section-title">Biblioteka NAS</h2>${state.library.map((track) => rowHtml(track, { ctx: 'library' })).join('')}` : ''}
        ${state.playlists.length ? `<h2 class="mu-section-title">Playlisty</h2>${state.playlists.map((playlist) => `
            <div class="mu-row" data-act="open-playlist" data-id="${esc(playlist.id || '')}" role="button" tabindex="0">
                <span class="mu-row-art" aria-hidden="true"></span>
                <div class="mu-row-main">
                    <div class="mu-row-title">${esc(playlist.name || '')}</div>
                    <div class="mu-row-sub">${esc(String(playlist.tracks_count || 0))} utworów</div>
                </div>
            </div>`).join('')}` : ''}
        ${empty ? emptyHtml('Brak wyników.') : ''}
    `;
    setQueueContext(state.tracks);
    hydrateCovers(results);
}

function renderSearch() {
    const state = ui.searchState;
    els.content.innerHTML = `
        <input class="mu-searchfield" id="mu-search-input" type="search" placeholder="Czego chcesz posłuchać?"
               autocomplete="off" aria-label="Szukaj muzyki" value="${esc(state?.query || '')}">
        <div class="mu-chips" id="mu-search-chips">
            ${CATEGORIES.map((category, index) => `<button type="button" class="mu-chip" data-act="category" data-index="${index}">${esc(category.name)}</button>`).join('')}
        </div>
        <div class="mu-suggest" id="mu-suggest" hidden></div>
        <div id="mu-results"></div>
    `;
    if (state) paintSearchResults();
    else paintSearchIntro();
    const input = $('mu-search-input');
    if (!input) return;
    let suggestTimer = null;
    let searchTimer = null;
    input.addEventListener('input', () => {
        const value = input.value.trim();
        clearTimeout(suggestTimer);
        clearTimeout(searchTimer);
        if (value.length < 2) {
            const suggest = $('mu-suggest');
            if (suggest) suggest.hidden = true;
            return;
        }
        suggestTimer = setTimeout(() => loadSuggestions(value), 140);
        // Android searches while you type; Enter still submits immediately.
        searchTimer = setTimeout(() => runSearch(value, { record: false }), 350);
    });
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            clearTimeout(searchTimer);
            const value = input.value.trim();
            if (value) runSearch(value);
        }
    });
    input.focus({ preventScroll: true });
}

async function loadSuggestions(query) {
    const seq = ++ui.suggestSeq;
    try {
        const data = await json(`/api/music/search/suggest?q=${encodeURIComponent(query)}&limit=8`);
        if (seq !== ui.suggestSeq) return;
        const items = data.items || [];
        const box = $('mu-suggest');
        if (!box) return;
        if (!items.length) { box.hidden = true; return; }
        box.innerHTML = items.map((item) => `
            <div class="mu-suggest-item" data-act="suggest" data-type="${esc(item.type)}"
                 data-id="${esc(item.id || '')}" data-title="${esc(item.title || '')}"
                 data-sub="${esc(item.subtitle || '')}" data-image="${esc(item.image || '')}">
                <span class="mu-suggest-kind">${esc(item.type)}</span>
                ${item.image ? `<img class="mu-suggest-thumb" src="${esc(item.image)}" alt="" loading="lazy">` : ''}
                <span class="mu-suggest-main">
                    <span class="mu-suggest-title">${esc(item.title || '')}</span>
                    <span class="mu-suggest-sub">${esc(item.subtitle || '')}</span>
                </span>
            </div>`).join('');
        box.hidden = false;
    } catch (error) {
        logWarn('Music suggestions failed', errorText(error));
    }
}

async function runSearch(query, { append = false, record = true } = {}) {
    const q = String(query || '').trim();
    if (!q) return;
    const seq = ++ui.searchSeq;
    if (!append) {
        ui.searchState = emptySearchState(q);
        ui.searchStations = [];
        if (record) pushSearchHistory(q);
        const box = $('mu-results');
        if (box) box.innerHTML = loadingHtml();
    }
    const state = ui.searchState;
    if (!state) return;
    const offset = append ? state.nextOffset : 0;
    try {
        const data = await json(`/api/music/search?type=all&q=${encodeURIComponent(q)}&limit=20&offset=${offset}`);
        if (seq !== ui.searchSeq || ui.searchState !== state) return;
        const page = data.tracks?.items || [];
        const seen = new Set(state.tracks.map((track) => tkey(track)));
        page.forEach((track) => {
            if (!track || seen.has(tkey(track))) return;
            seen.add(tkey(track));
            state.tracks.push(track);
        });
        state.tracksTotal = Number(data.tracks?.total) || state.tracks.length;
        state.hasMore = !!data.tracks?.has_more;
        state.nextOffset = offset + page.length;
        if (!append) {
            state.podcasts = data.podcasts?.items || [];
            state.radio = data.radio?.items || [];
            state.library = data.library?.items || [];
            state.playlists = data.playlists?.items || [];
            ui.searchStations = state.radio;
        }
        indexTracks(state.tracks);
        indexTracks(state.library);
        paintSearchResults();
    } catch (error) {
        if (seq !== ui.searchSeq) return;
        if (append) {
            toast(`Nie udało się wczytać kolejnych wyników: ${errorText(error)}`, 'warning');
            return;
        }
        const box = $('mu-results');
        if (box) box.innerHTML = emptyHtml(`Wyszukiwanie nie powiodło się: ${errorText(error)}`);
        logError('Music search failed', `${q}: ${errorText(error)}`);
    }
}

// ── podcasts ───────────────────────────────────────────────────────────────

async function ensurePodcastCatalog() {
    if (ui.podcastCatalog) return ui.podcastCatalog;
    const data = await json('/api/music/podcasts/catalog?limit=80&sort=popular');
    ui.podcastCatalog = data;
    return data;
}

async function renderPodcasts() {
    const subscribed = store.podcasts.subscribed || [];
    els.content.innerHTML = `
        ${subscribed.length ? `<h2 class="mu-section-title">Twoje podcasty</h2>
            <div class="mu-grid">${subscribed.map((pod) => cardHtml({
        image: pod.cover, label: pod.title, sub: 'Subskrybowane', act: 'open-podcast', id: pod.id,
    })).join('')}</div>` : ''}
        <h2 class="mu-section-title">Katalog</h2>
        <input class="mu-searchfield" id="mu-podcast-filter" type="search" placeholder="Filtruj podcasty…" aria-label="Filtruj podcasty">
        <div class="mu-chips" id="mu-podcast-chips"></div>
        <div id="mu-podcast-catalog">${loadingHtml()}</div>
        <button type="button" class="mu-more" data-act="add-feed">Dodaj własny kanał RSS</button>
    `;
    try {
        const catalog = await ensurePodcastCatalog();
        const box = $('mu-podcast-catalog');
        if (!box) return;
        const items = catalog.items || [];
        const categories = catalog.categories || [];
        const chips = $('mu-podcast-chips');
        if (chips) {
            chips.innerHTML = `<button type="button" class="mu-chip active" data-podcast-cat="">Wszystkie</button>`
                + categories.map((category) => `<button type="button" class="mu-chip" data-podcast-cat="${esc(category)}">${esc(category)}</button>`).join('');
        }
        let filter = '';
        let category = '';
        const paint = () => {
            const needle = filter.toLowerCase();
            const visible = items.filter((item) => {
                const matchesQuery = !needle || `${item.name} ${item.author}`.toLowerCase().includes(needle);
                const matchesCategory = !category || item.category === category;
                return matchesQuery && matchesCategory;
            });
            box.innerHTML = visible.length ? `<div class="mu-grid">${visible.map((pod) => cardHtml({
                image: pod.cover_url, label: pod.name, sub: `${pod.author || ''}${pod.episode_count ? ` · ${pod.episode_count} odc.` : ''}`,
                act: 'open-podcast', id: pod.id,
            })).join('')}</div>` : emptyHtml('Brak podcastów w tym filtrze.');
        };
        paint();
        const input = $('mu-podcast-filter');
        if (input) input.addEventListener('input', () => { filter = input.value.trim(); paint(); });
        if (chips) {
            chips.addEventListener('click', (event) => {
                const button = event.target.closest('[data-podcast-cat]');
                if (!button) return;
                category = button.dataset.podcastCat || '';
                chips.querySelectorAll('.mu-chip').forEach((chip) => chip.classList.toggle('active', chip === button));
                paint();
            });
        }
    } catch (error) {
        const box = $('mu-podcast-catalog');
        if (box) box.innerHTML = emptyHtml(`Katalog niedostępny: ${errorText(error)}`);
        logError('Podcast catalog failed', errorText(error));
    }
}

function podcastFromCatalog(id) {
    const items = ui.podcastCatalog?.items || [];
    return items.find((item) => item.id === id) || null;
}

async function openPodcast(id, { push = true } = {}) {
    const subscribed = (store.podcasts.subscribed || []).find((pod) => pod.id === id);
    const item = podcastFromCatalog(id);
    const catalog = subscribed || (item ? {
        id: item.id,
        title: item.name,
        desc: item.description || '',
        rss_url: item.rss_url,
        cover: item.cover_url || '',
    } : null);
    if (!catalog) { toast('Nie znaleziono podcastu', 'error'); return; }
    ui.activePodcast = catalog;
    ui.view = 'podcast';
    setNavActive('podcast');
    if (push) { try { history.pushState({ musicView: 'podcast', id }, ''); } catch { /* ignore */ } }
    renderPodcastDetail();
    els.content.scrollTop = 0;
}

async function renderPodcastDetail() {
    const pod = ui.activePodcast;
    if (!pod) { go('podcasts'); return; }
    const subscribed = (store.podcasts.subscribed || []).find((item) => item.id === pod.id);
    const episodes = (subscribed && store.podcasts.episodes?.[subscribed.id]) || [];
    indexTracks(episodes);
    setQueueContext(episodes);
    els.content.innerHTML = `
        <button type="button" class="mu-icon-btn mu-back" data-act="back" aria-label="Wstecz">←</button>
        <div class="mu-detail-head">
            ${pod.cover ? `<img class="mu-detail-cover" src="${esc(pod.cover)}" alt="" loading="lazy">` : ''}
            <div>
                <div class="mu-detail-label">Podcast</div>
                <div class="mu-detail-title">${esc(pod.title || 'Podcast')}</div>
                <div class="mu-detail-meta">${esc(pod.desc || '')}</div>
            </div>
        </div>
        <div class="mu-detail-actions">
            <button type="button" class="mu-smcircle" data-act="podcast-refresh" data-id="${esc(pod.id)}">Odśwież odcinki</button>
            ${subscribed ? `<button type="button" class="mu-smcircle" data-act="podcast-unsubscribe" data-id="${esc(subscribed.id)}">Odsubskrybuj</button>` : ''}
        </div>
        ${episodes.length ? episodes.map((episode) => rowHtml(episode, {
        ctx: 'episode', sub: episode.artist || '', duration: true,
    })).join('') : ui.podcastFailedId === pod.id
        ? emptyHtml('Nie udało się pobrać odcinków. Użyj „Odśwież odcinki”.')
        : `<div class="mu-spin" role="status" aria-label="Ładowanie odcinków"></div>`}
    `;
    if (!episodes.length && ui.podcastFailedId !== pod.id) await refreshPodcast(pod, subscribed);
}

async function refreshPodcast(pod, subscribed) {
    try {
        if (!subscribed) {
            await apiPost('/api/music/podcasts/subscribe', { podcast: pod });
            const state = await json('/api/music/state');
            applyState(state);
            offline.saveLibraryState(state);
        }
        const sub = (store.podcasts.subscribed || []).find((item) => item.rss_url === pod.rss_url);
        if (!sub) throw new Error('Subskrypcja nie została zapisana');
        const feed = await json(`/api/music/podcast-feed?url=${encodeURIComponent(sub.rss_url)}&limit=25`);
        const episodes = (feed.episodes || []).map((episode) => ({
            id: episode.id,
            title: episode.title,
            artist: episode.artist || '',
            thumbnail: episode.thumb || '',
            duration: Number(episode.dur) || 0,
            file_url: episode.file_url,
            date: episode.date,
            is_episode: true,
            podcast_id: sub.id,
        }));
        store.podcasts.episodes = store.podcasts.episodes || {};
        store.podcasts.episodes[sub.id] = episodes;
        await json(`/api/music/podcasts/${sub.id}/episodes`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ episodes }),
        }).catch(() => {});
        ui.activePodcast = sub;
        ui.podcastFailedId = null;
        renderPodcastDetail();
    } catch (error) {
        toast(`Nie udało się pobrać odcinków: ${errorText(error)}`, 'error');
        logError('Podcast feed failed', errorText(error));
        ui.podcastFailedId = pod.id;
        renderPodcastDetail();
    }
}

async function addCustomFeed() {
    const url = await promptText('Kanał RSS', 'https://…/feed.xml');
    if (!url) return;
    try {
        const feed = await json(`/api/music/podcast-feed?url=${encodeURIComponent(url)}&limit=25`);
        const pod = {
            id: await sha16(url),
            title: feed.title || 'Podcast',
            desc: feed.description || '',
            rss_url: url,
            cover: feed.cover_url || '',
        };
        await apiPost('/api/music/podcasts/subscribe', { podcast: pod });
        const state = await json('/api/music/state');
        applyState(state);
        offline.saveLibraryState(state);
        ui.podcastCatalog = null;
        const sub = (store.podcasts.subscribed || []).find((item) => item.rss_url === url) || pod;
        ui.activePodcast = sub;
        ui.view = 'podcast';
        await refreshPodcast(sub, sub);
    } catch (error) {
        toast(`Nie udało się dodać kanału: ${errorText(error)}`, 'error');
        logError('Custom feed failed', errorText(error));
    }
}

// ── radio ──────────────────────────────────────────────────────────────────

async function ensureStations() {
    if (ui.stations) return ui.stations;
    const data = await json('/api/music/radio/stations');
    ui.stations = data.stations || [];
    ui.stationCategories = data.categories || [];
    return ui.stations;
}

function stationRowHtml(station, { favourite = store.radioFavorites.includes(station.id) } = {}) {
    return `
    <div class="mu-row idle-hidden" data-act="play-station" data-station="${esc(station.id)}" role="button" tabindex="0">
        ${station.logo ? `<img class="mu-row-art logo" src="${esc(station.logo)}" alt="" loading="lazy">`
        : '<span class="mu-row-art" aria-hidden="true"></span>'}
        <div class="mu-row-main">
            <div class="mu-row-title">${esc(station.name)}</div>
            <div class="mu-row-sub">${esc([station.city, station.category].filter(Boolean).join(' · '))}</div>
        </div>
        <button type="button" class="mu-row-more" data-act="station-fav" data-station="${esc(station.id)}"
                aria-label="Ulubione">${favourite ? '★' : '☆'}</button>
    </div>`;
}

async function renderRadio() {
    els.content.innerHTML = `
        <h1 class="mu-h1">Radio</h1>
        <input class="mu-searchfield" id="mu-radio-filter" type="search" placeholder="Szukaj stacji…" aria-label="Szukaj stacji">
        <div class="mu-chips" id="mu-radio-chips"></div>
        <div id="mu-radio-recent"></div>
        <div id="mu-radio-fav"></div>
        <div id="mu-radio-list">${loadingHtml()}</div>
    `;
    try {
        const stations = await ensureStations();
        const box = $('mu-radio-list');
        const chips = $('mu-radio-chips');
        if (!box) return;
        let filter = '';
        let category = '';
        if (chips) {
            chips.innerHTML = '<button type="button" class="mu-chip active" data-radio-cat="">Wszystkie</button>'
                + ui.stationCategories.map((name) => `<button type="button" class="mu-chip" data-radio-cat="${esc(name)}">${esc(name)}</button>`).join('');
            chips.addEventListener('click', (event) => {
                const button = event.target.closest('[data-radio-cat]');
                if (!button) return;
                category = button.dataset.radioCat || '';
                chips.querySelectorAll('.mu-chip').forEach((chip) => chip.classList.toggle('active', chip === button));
                paint();
            });
        }
        const paint = () => {
            const needle = filter.toLowerCase();
            const visible = stations.filter((station) => {
                const text = `${station.name} ${station.city} ${station.tags}`.toLowerCase();
                return (!needle || text.includes(needle)) && (!category || station.category === category);
            });
            box.innerHTML = visible.length ? visible.map((station) => stationRowHtml(station, {
                favourite: store.radioFavorites.includes(station.id),
            })).join('') : emptyHtml('Brak stacji w tym filtrze.');
        };
        paint();
        const input = $('mu-radio-filter');
        if (input) input.addEventListener('input', () => { filter = input.value.trim(); paint(); });
        paintRadioRecents();
        paintFavourites();
    } catch (error) {
        const box = $('mu-radio-list');
        if (box) box.innerHTML = emptyHtml(`Katalog stacji niedostępny: ${errorText(error)}`);
        logError('Radio catalog failed', errorText(error));
    }
}

function paintFavourites() {
    const box = $('mu-radio-fav');
    if (!box || !ui.stations) return;
    const favourites = ui.stations.filter((station) => store.radioFavorites.includes(station.id));
    if (!favourites.length) { box.innerHTML = ''; return; }
    box.innerHTML = `<h2 class="mu-section-title">Ulubione</h2>${favourites.map((station) => stationRowHtml(station, { favourite: true })).join('')}`;
}

function paintRadioRecents() {
    const box = $('mu-radio-recent');
    if (!box || !ui.stations) return;
    const recent = store.radioRecents
        .map((id) => ui.stations.find((station) => station.id === id))
        .filter(Boolean)
        .slice(0, 5);
    box.innerHTML = recent.length
        ? `<h2 class="mu-section-title">Ostatnio słuchane</h2>${recent.map((station) => stationRowHtml(station)).join('')}`
        : '';
}

function stationById(id) {
    return (ui.stations || []).find((station) => station.id === id)
        || (ui.searchStations || []).find((station) => station.id === id)
        || null;
}

async function playStation(station) {
    if (!station) return;
    const track = {
        id: `radio:${station.id}`,
        title: station.name,
        artist: [station.city, station.category].filter(Boolean).join(' · '),
        thumbnail: station.logo || '',
        file_url: station.stream_url,
        is_live: true,
        hls: !!station.hls,
    };
    engine.playQueue([track]);
    store.radioRecents = [station.id, ...store.radioRecents.filter((id) => id !== station.id)].slice(0, 8);
    json('/api/music/radio/state', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ favorites: store.radioFavorites, recents: store.radioRecents }),
    }).catch(() => {});
    openPlayer();
}

async function toggleFavourite(stationId) {
    if (store.radioFavorites.includes(stationId)) {
        store.radioFavorites = store.radioFavorites.filter((id) => id !== stationId);
    } else {
        store.radioFavorites = [...store.radioFavorites, stationId].slice(-500);
    }
    try {
        await json('/api/music/radio/state', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ favorites: store.radioFavorites, recents: store.radioRecents }),
        });
    } catch (error) {
        toast(`Ulubione niezsynchronizowane: ${errorText(error)}`, 'warning');
    }
    if (ui.view === 'radio') renderRadio();
}

// ── library ────────────────────────────────────────────────────────────────

function renderLibrary() {
    els.content.innerHTML = `
        <h1 class="mu-h1">Biblioteka</h1>
        <button type="button" class="mu-more" data-act="new-playlist">+ Nowa playlista</button>
        <h2 class="mu-section-title">Playlisty</h2>
        ${store.playlists.length ? `<div class="mu-grid">${store.playlists.map((playlist) => cardHtml({
        image: artUrl(playlist.tracks?.[0] || {}),
        label: playlist.name,
        sub: `${playlist.tracks?.length || 0} utworów`,
        act: 'open-playlist',
        id: playlist.id,
        playlistId: playlist.id,
        coverKey: playlist.tracks?.[0] ? tkey(playlist.tracks[0]) : '',
    })).join('')}</div>` : emptyHtml('Nie masz jeszcze playlist.')}
        ${store.sharedPlaylists.length ? `<h2 class="mu-section-title">Udostępnione Tobie</h2>${store.sharedPlaylists.map((grant) => `
            <div class="mu-row" data-act="open-shared" data-id="${esc(grant.id || '')}" role="button" tabindex="0">
                <span class="mu-row-art" aria-hidden="true"></span>
                <div class="mu-row-main">
                    <div class="mu-row-title">${esc(grant.label || grant.resource || 'Playlista')}</div>
                    <div class="mu-row-sub">${esc(grant.owner || '')} · tylko odtwarzanie</div>
                </div>
            </div>`).join('')}` : ''}
    `;
}

async function renderPlaylistDetail() {
    const playlist = ui.activePlaylist;
    if (!playlist) { go('library'); return; }
    const tracks = playlist.tracks || [];
    indexTracks(tracks);
    setQueueContext(tracks);
    const downloaded = tracks.filter((track) => offline.hasLocal(track)).length;
    const readOnly = !!playlist.read_only;
    els.content.innerHTML = `
        <button type="button" class="mu-icon-btn mu-back" data-act="back" aria-label="Wstecz">←</button>
        <div class="mu-detail-head">
            ${artUrl(tracks[0] || {}) ? `<img class="mu-detail-cover" src="${esc(artUrl(tracks[0]))}" data-cover-key="${esc(tkey(tracks[0]))}" alt="" loading="lazy">` : ''}
            <div>
                <div class="mu-detail-label">${readOnly ? 'Udostępniona playlista' : 'Playlista'}</div>
                <div class="mu-detail-title">${esc(playlist.name || 'Playlista')}</div>
                <div class="mu-detail-meta">${tracks.length} utworów · <span data-playlist-download>${downloaded} z ${tracks.length} na urządzeniu</span></div>
                ${tracks.length ? `<div class="mu-meter mu-detail-meter" data-playlist-meter><i style="width:${Math.round((downloaded / tracks.length) * 100)}%"></i></div>` : ''}
            </div>
        </div>
        <div class="mu-detail-actions">
            <button type="button" class="mu-playcircle" data-act="play-playlist" aria-label="Odtwórz">▶</button>
            <button type="button" class="mu-smcircle" data-act="shuffle-playlist">Losowo</button>
            <button type="button" class="mu-smcircle" data-act="download-playlist">Pobierz na telefon</button>
            ${readOnly ? '' : `<button type="button" class="mu-smcircle" data-act="delete-playlist">${playlist.id === LIKED_PLAYLIST_ID ? 'Wyczyść polubienia' : 'Usuń playlistę'}</button>`}
        </div>
        ${tracks.length ? tracks.map((track) => rowHtml(track, { ctx: readOnly ? 'shared' : 'playlist' })).join('') : emptyHtml('Playlista jest pusta.')}
    `;
}

// ── downloads ──────────────────────────────────────────────────────────────

function badgeFor(row) {
    if (row.status === 'done') return { text: fmtBytes(row.bytes), cls: '' };
    if (row.status === 'downloading') return { text: 'pobieranie', cls: 'waiting' };
    if (row.status === 'queued') return { text: 'w kolejce', cls: 'waiting' };
    if (row.status === 'waiting') return { text: 'brak miejsca', cls: 'failed' };
    if (row.status === 'failed') return { text: 'błąd', cls: 'failed' };
    return null;
}

function renderDownloads() {
    const rows = offline.downloadsSnapshot();
    const storage = offline.storageInfo();
    const percent = storage?.quota ? Math.min(100, (storage.usage / storage.quota) * 100) : 0;
    const paused = offline.isPaused();
    setQueueContext(rows.filter((row) => row.status === 'done' && row.track).map((row) => row.track));
    els.content.innerHTML = `
        <h1 class="mu-h1">Pobrane</h1>
        <div class="mu-settings-card">
            <h3>Miejsce na urządzeniu</h3>
            <div class="mu-meter"><i style="width:${percent.toFixed(1)}%"></i></div>
            <p>${storage ? `${fmtBytes(storage.usage)} z ${fmtBytes(storage.quota)} wykorzystane` : 'Brak danych o pamięci'} · muzyka: ${fmtBytes(offline.totalBytes())}</p>
            <p>Pobieranie jest najszybsze przy otwartej aplikacji — w tle system mobilny może je wstrzymać.</p>
            ${paused ? `<p>Pobieranie wstrzymane: ${esc(offline.pauseReason() === 'quota' ? 'pamięć urządzenia prawie pełna' : 'ręcznie')}.</p>
                <button type="button" class="mu-btn small" data-act="resume-downloads">Wznów pobieranie</button>` : ''}
        </div>
        ${rows.length ? `<div class="mu-dl-list">${rows.map((row) => `
            <div class="mu-row idle-hidden" data-act="play-download" data-key="${esc(row.key)}" role="button" tabindex="0">
                ${artUrl(row.track) ? `<img class="mu-row-art" src="${esc(artUrl(row.track))}" data-cover-key="${esc(row.key)}" alt="" loading="lazy">` : '<span class="mu-row-art" aria-hidden="true"></span>'}
                <div class="mu-row-main">
                    <div class="mu-row-title">${esc(row.track?.title || row.key)}</div>
                    <div class="mu-row-sub">${esc(row.track?.artist || '')}${row.pinned ? ' · przypięte' : ''}${row.error ? ` · ${esc(row.error)}` : ''}</div>
                </div>
                ${badgeFor(row) ? `<span class="mu-row-badge ${esc(badgeFor(row).cls)}">${esc(badgeFor(row).text)}</span>` : ''}
                <button type="button" class="mu-row-more" data-act="download-menu" data-key="${esc(row.key)}" aria-label="Opcje pobrania">⋮</button>
            </div>`).join('')}</div>`
        : emptyHtml('Nic nie jest jeszcze pobrane. Utwory z playlist pobierają się automatycznie.')}
    `;
}

// ── settings ───────────────────────────────────────────────────────────────

function renderSettings() {
    const storage = offline.storageInfo();
    const rows = offline.downloadsSnapshot();
    const percent = storage?.quota ? Math.min(100, (storage.usage / storage.quota) * 100) : 0;
    els.content.innerHTML = `
        <h1 class="mu-h1">Ustawienia</h1>
        <div class="mu-settings-card">
            <h3>Muzyka offline</h3>
            <p>Każdy utwór z playlisty jest pobierany na to urządzenie i znika, gdy usuniesz go ze wszystkich playlist.</p>
            <div class="mu-toggle-row">
                <span>Pobieraj playlisty automatycznie</span>
                <input type="checkbox" id="mu-auto-offline" ${offline.isAutoDownload() ? 'checked' : ''} aria-label="Pobieraj playlisty automatycznie">
            </div>
            <div class="mu-toggle-row">
                <span>Równoległe pobierania</span>
                <select id="mu-parallel" class="mu-select" aria-label="Ile utworów pobierać równolegle">
                    ${[1, 2, 4, 6].map((n) => `<option value="${n}" ${offline.downloadParallelism() === n ? 'selected' : ''}>${n}</option>`).join('')}
                </select>
            </div>
            <div class="mu-meter"><i style="width:${percent.toFixed(1)}%"></i></div>
            <p>${storage ? `${fmtBytes(storage.usage)} z ${fmtBytes(storage.quota)}` : ''} · ${rows.filter((row) => row.status === 'done').length} plików</p>
            <div class="mu-row-actions">
                <button type="button" class="mu-btn small" data-act="open-downloads">Pobrane</button>
                <button type="button" class="mu-btn small danger" data-act="clear-downloads">Wyczyść pobrane</button>
            </div>
        </div>
        <div class="mu-settings-card">
            <h3>Wygląd</h3>
            <p>Motyw jasny lub ciemny, wspólny z resztą EthOS na tym urządzeniu.</p>
            <button type="button" class="mu-btn small" data-act="toggle-theme">Zmień motyw</button>
        </div>
        <div class="mu-settings-card">
            <h3>Konto</h3>
            <p>Zalogowany jako <strong>${esc(getCurrentClaims()?.username || '')}</strong>. Wylogowanie usuwa token z tego urządzenia; pobrane pliki zostają.</p>
            <div class="mu-row-actions">
                <button type="button" class="mu-btn small" data-act="install-app" ${ui.installPrompt ? '' : 'hidden'}>Zainstaluj aplikację</button>
                <button type="button" class="mu-btn small danger" data-act="logout">Wyloguj</button>
            </div>
        </div>
        <div class="mu-settings-card">
            <h3>O aplikacji</h3>
            <p>EthOS Music PWA — telefon korzysta z tego samego konta i biblioteki co aplikacja na komputerze. Odtwarzanie w tle działa przez Media Session; przy braku sieci grają pobrane playlisty i odcinki.</p>
        </div>
    `;
    const toggle = $('mu-auto-offline');
    if (toggle) {
        toggle.addEventListener('change', () => {
            offline.setAutoDownload(toggle.checked);
            if (toggle.checked) {
                offline.syncPlaylists(store.playlists);
                toast('Playlisty będą pobierane na to urządzenie');
            }
        });
    }
    const parallel = $('mu-parallel');
    if (parallel) {
        parallel.addEventListener('change', () => {
            const value = offline.setDownloadParallelism(Number(parallel.value));
            toast(value === 1 ? 'Pobieranie po kolei' : `Pobieranie po ${value} równolegle`);
        });
    }
}

// ── sheets / prompts ───────────────────────────────────────────────────────

function openSheet(title, nodes) {
    els.sheetTitle.textContent = title;
    els.sheetBody.replaceChildren(...nodes);
    els.sheet.hidden = false;
}

function closeSheet() {
    els.sheet.hidden = true;
    els.sheetBody.replaceChildren();
}

function promptText(title, placeholder = '') {
    return new Promise((resolve) => {
        const input = document.createElement('input');
        input.className = 'mu-sheet-input';
        input.type = 'text';
        input.placeholder = placeholder;
        const ok = document.createElement('button');
        ok.type = 'button';
        ok.className = 'mu-btn primary';
        ok.textContent = 'Zapisz';
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'mu-btn';
        cancel.textContent = 'Anuluj';
        const finish = (value) => { closeSheet(); resolve(value); };
        ok.addEventListener('click', () => finish(input.value.trim()));
        cancel.addEventListener('click', () => finish(''));
        input.addEventListener('keydown', (event) => { if (event.key === 'Enter') finish(input.value.trim()); });
        openSheet(title, [input, ok, cancel]);
        setTimeout(() => input.focus(), 60);
    });
}

function sheetButton(label, onClick) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'mu-sheet-item';
    button.textContent = label;
    button.addEventListener('click', () => { closeSheet(); onClick(); });
    return button;
}

function openRowMenu(track, ctx) {
    if (!track) return;
    const nodes = [
        sheetButton('Odtwórz teraz', () => engine.playQueue([track])),
        sheetButton('Odtwórz następne', () => engine.playNext(track)),
        sheetButton('Dodaj do kolejki', () => engine.addToQueue(track)),
        sheetButton('Dodaj do playlisty', () => pickPlaylist(track)),
    ];
    if (!isEpisode(track) && !track.is_live && track.id) {
        nodes.push(sheetButton('Pobierz na NAS (m4a)', () => downloadToNas(track)));
    }
    if (!track.is_live) {
        nodes.push(sheetButton(offline.hasLocal(track) ? 'Usuń z tego urządzenia' : 'Pobierz na to urządzenie', async () => {
            if (offline.hasLocal(track)) {
                await offline.deleteDownload(track);
                toast('Usunięto z urządzenia');
            } else if (isEpisode(track)) {
                offline.downloadEpisode(track);
                toast('Pobieranie odcinka…');
            } else {
                offline.downloadTrack(track);
                toast('Pobieranie utworu…');
            }
            if (ui.view === 'downloads') renderDownloads();
        }));
    }
    if (ctx === 'playlist' && ui.activePlaylist && !ui.activePlaylist.read_only) {
        nodes.push(sheetButton('Usuń z playlisty', () => removeFromPlaylist(track)));
    }
    if (ctx === 'liked') {
        nodes.push(sheetButton('Usuń z polubionych', () => toggleLike(track)));
    }
    openSheet(track.title || 'Utwór', nodes);
}

function openDownloadMenu(key) {
    const row = offline.rowFor(key);
    if (!row) return;
    const nodes = [];
    if (row.status === 'done') {
        nodes.push(sheetButton('Odtwórz', () => engine.playQueue([row.track])));
    }
    if (row.status === 'failed' || row.status === 'waiting') {
        nodes.push(sheetButton('Ponów pobieranie', () => { offline.retryDownload(key); toast('Wznowiono pobieranie'); }));
    }
    nodes.push(sheetButton('Usuń z urządzenia', async () => {
        await offline.deleteDownload(key);
        renderDownloads();
        toast('Usunięto z urządzenia');
    }));
    openSheet(row.track?.title || 'Pobrane', nodes);
}

async function pickPlaylist(track) {
    const nodes = store.playlists.map((playlist) => sheetButton(playlist.name, async () => {
        try {
            await json(`/api/music/playlists/${playlist.id}/tracks`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ track }),
            });
            playlist.tracks = playlist.tracks || [];
            if (!playlist.tracks.some((item) => tkey(item) === tkey(track))) playlist.tracks.push(track);
            if (playlist.id === LIKED_PLAYLIST_ID
                    && !store.liked.some((item) => tkey(item) === tkey(track))) {
                store.liked.push(track);
            }
            await persistPlaylists();
            toast(`Dodano do „${playlist.name}”`);
        } catch (error) {
            toast(`Nie udało się dodać: ${errorText(error)}`, 'error');
        }
    }));
    nodes.push(sheetButton('+ Nowa playlista', async () => {
        const name = await promptText('Nowa playlista', 'Nazwa playlisty');
        if (name) await createPlaylist(name, track);
    }));
    openSheet('Dodaj do playlisty', nodes);
}

async function createPlaylist(name, firstTrack = null) {
    try {
        const playlist = await apiPost('/api/music/playlists', { name });
        playlist.tracks = playlist.tracks || [];
        if (firstTrack) {
            await json(`/api/music/playlists/${playlist.id}/tracks`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ track: firstTrack }),
            });
            playlist.tracks.push(firstTrack);
        }
        store.playlists.push(playlist);
        await persistPlaylists();
        toast(`Utworzono „${name}”`);
        if (ui.view === 'library') renderLibrary();
    } catch (error) {
        toast(`Nie udało się utworzyć playlisty: ${errorText(error)}`, 'error');
    }
}

async function removeFromPlaylist(track) {
    const playlist = ui.activePlaylist;
    if (!playlist) return;
    try {
        await json(`/api/music/playlists/${playlist.id}/tracks`, {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: tkey(track) }),
        });
        playlist.tracks = (playlist.tracks || []).filter((item) => tkey(item) !== tkey(track));
        await persistPlaylists();
        renderPlaylistDetail();
    } catch (error) {
        toast(`Nie udało się usunąć utworu: ${errorText(error)}`, 'error');
    }
}

async function downloadToNas(track) {
    try {
        await apiPost('/api/music/download', { video_id: track.id, url: track.file_url || '' });
        toast('Pobieranie na NAS rozpoczęte');
    } catch (error) {
        toast(`Pobieranie na NAS nie powiodło się: ${errorText(error)}`, 'error');
    }
}

async function toggleLike(track) {
    const liked = store.liked.some((item) => tkey(item) === tkey(track));
    try {
        if (liked) {
            await json('/api/music/liked', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ key: tkey(track) }),
            });
            store.liked = store.liked.filter((item) => tkey(item) !== tkey(track));
        } else {
            await apiPost('/api/music/liked', { track });
            store.liked.push(track);
        }
        // Keep the local "Polubione" playlist in step so the offline mirror
        // downloads (or drops) the audio straight away.
        const playlist = likedPlaylist();
        if (playlist) {
            if (liked) playlist.tracks = playlist.tracks.filter((item) => tkey(item) !== tkey(track));
            else if (!playlist.tracks.some((item) => tkey(item) === tkey(track))) playlist.tracks.push(track);
            await persistPlaylists();
        }
        updatePlayer();
        if (ui.view === 'library' || ui.view === 'home') renderView();
    } catch (error) {
        toast(`Polubienia niezsynchronizowane: ${errorText(error)}`, 'warning');
    }
}

// ── player UI ──────────────────────────────────────────────────────────────

const FALLBACK_ART = '/static/icons/music-512.png';

function updateMini() {
    const track = engine.current;
    if (!track) { els.mini.hidden = true; return; }
    els.mini.hidden = false;
    const key = tkey(track);
    const art = artUrl(track) || FALLBACK_ART;
    if (els.miniArt.dataset.coverKey !== key) {
        els.miniArt.dataset.coverKey = key;
        els.miniArt.src = art;
        offline.localThumb(key).then((url) => {
            if (url && engine.current && tkey(engine.current) === key) els.miniArt.src = url;
        }).catch(() => {});
    }
    els.miniTitle.textContent = track.title || '';
    els.miniSub.textContent = track.artist || (isEpisode(track) ? 'Odcinek' : '');
    els.miniPlay.textContent = engine.isPlaying ? '❚❚' : '▶';
}

function updatePlayer() {
    const track = engine.current;
    if (!track) return;
    const key = tkey(track);
    const art = artUrl(track) || FALLBACK_ART;
    if (els.playerArt.dataset.coverKey !== key) {
        els.playerArt.dataset.coverKey = key;
        els.playerArt.src = art;
        els.playerBg.style.backgroundImage = `url("${art}")`;
        offline.localThumb(key).then((url) => {
            if (!url || !engine.current || tkey(engine.current) !== key) return;
            els.playerArt.src = url;
            els.playerBg.style.backgroundImage = `url("${url}")`;
        }).catch(() => {});
    }
    els.playerTitle.textContent = track.title || '';
    els.playerSub.textContent = track.artist || (isEpisode(track) ? 'Odcinek podcastu' : '');
    els.playerLabel.textContent = track.is_live ? 'Radio na żywo' : isEpisode(track) ? 'Odcinek' : 'Odtwarzanie';
    els.live.hidden = !track.is_live;
    els.seek.disabled = !!track.is_live;
    els.like.classList.toggle('on', store.liked.some((item) => tkey(item) === tkey(track)));
    els.play.textContent = engine.isPlaying ? '❚❚' : '▶';
    els.shuffle.classList.toggle('on', engine.shuffle);
    els.shuffle.setAttribute('aria-pressed', String(engine.shuffle));
    els.repeat.classList.toggle('on', engine.repeat !== 'off');
    els.repeat.setAttribute('aria-pressed', String(engine.repeat !== 'off'));
    els.repeat.title = engine.repeat === 'off' ? 'Powtarzanie wyłączone'
        : engine.repeat === 'all' ? 'Powtarzaj wszystko' : 'Powtarzaj jeden utwór';
    updateMini();
    updateSleepHint();
}

function updateProgress({ pos = 0, dur = 0 }) {
    const track = engine.current;
    if (!track) return;
    if (!track.is_live && Number.isFinite(dur) && dur > 0) {
        els.seek.max = '1000';
        els.seek.value = String(Math.round((pos / dur) * 1000));
        els.pos.textContent = fmtDur(pos);
        els.dur.textContent = fmtDur(dur);
        els.miniProgress.style.width = `${Math.min(100, (pos / dur) * 100)}%`;
    } else {
        els.seek.value = '0';
        els.pos.textContent = '0:00';
        els.dur.textContent = track.is_live ? 'na żywo' : '0:00';
        els.miniProgress.style.width = '0%';
    }
    els.play.textContent = engine.isPlaying ? '❚❚' : '▶';
    els.miniPlay.textContent = engine.isPlaying ? '❚❚' : '▶';
}

function updateSleepHint() {
    if (!engine.sleepMinutes) { els.sleepHint.hidden = true; return; }
    const left = Math.ceil(engine.sleepLeftMs / 60000);
    els.sleepHint.hidden = false;
    els.sleepHint.textContent = `Wyłącznik czasowy: ${left} min`;
}

function renderQueue() {
    const rows = engine.queue.map((track, index) => `
        <div class="mu-row${index === engine.index ? ' playing' : ''}" data-act="queue-jump" data-index="${index}" role="button" tabindex="0">
            <span class="mu-row-art" aria-hidden="true"></span>
            <div class="mu-row-main">
                <div class="mu-row-title">${esc(track.title || '')}</div>
                <div class="mu-row-sub">${esc(track.artist || '')}</div>
            </div>
            <button type="button" class="mu-row-more" data-act="queue-remove" data-index="${index}" aria-label="Usuń z kolejki">✕</button>
        </div>`).join('');
    els.queueList.innerHTML = rows || emptyHtml('Kolejka jest pusta.');
}

function openPlayer() {
    if (!engine.current) return;
    renderQueue();
    els.player.hidden = false;
    updatePlayer();
}

function closePlayer() {
    els.player.hidden = true;
    els.queue.hidden = true;
}

function cycleSleep() {
    const order = [0, 15, 30, 60];
    const index = order.indexOf(engine.sleepMinutes);
    engine.setSleepTimer(order[(index + 1) % order.length]);
    toast(engine.sleepMinutes ? `Wyłącznik: ${engine.sleepMinutes} min` : 'Wyłącznik wyłączony');
    updateSleepHint();
}

function wireEngine() {
    if (ui.engineWired) return;
    ui.engineWired = true;
    engine.on('track', () => { updatePlayer(); updateMini(); renderQueue(); });
    engine.on('playing', () => { updatePlayer(); updateMini(); });
    engine.on('time', (data) => updateProgress(data || {}));
    engine.on('queue', () => renderQueue());
    engine.on('mode', () => updatePlayer());
    engine.on('sleep', () => { updateSleepHint(); updatePlayer(); });
    engine.on('error', ({ track } = {}) => {
        notify({
            kind: 'error',
            title: 'Odtwarzanie',
            message: `Nie udało się odtworzyć „${track?.title || 'utworu'}”. Sprawdź połączenie i spróbuj ponownie.`,
        });
    });
    engine.on('track', () => { if (ui.view === 'downloads') renderDownloads(); });
}

function wirePublicUi() {
    if (ui.wired) return;
    ui.wired = true;
    // nav
    els.nav.addEventListener('click', (event) => {
        const button = event.target.closest('[data-view]');
        if (button) go(button.dataset.view);
    });
    // content actions
    els.content.addEventListener('click', onContentClick);
    els.content.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
            const target = event.target.closest('[data-act]');
            if (target) { event.preventDefault(); onContentClick(event); }
        }
    });
    // menu
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
    // mini player
    els.mini.addEventListener('click', (event) => {
        if (event.target.closest('#mu-mini-play')) return;
        openPlayer();
    });
    els.miniPlay.addEventListener('click', (event) => {
        event.stopPropagation();
        engine.toggle();
        updateMini();
    });
    // full player
    $('mu-player-close').addEventListener('click', closePlayer);
    els.play.addEventListener('click', () => { engine.toggle(); updatePlayer(); });
    $('mu-prev').addEventListener('click', () => engine.prev());
    $('mu-next').addEventListener('click', () => engine.next());
    els.shuffle.addEventListener('click', () => { engine.toggleShuffle(); updatePlayer(); });
    els.repeat.addEventListener('click', () => { engine.cycleRepeat(); updatePlayer(); });
    els.like.addEventListener('click', () => { if (engine.current) toggleLike(engine.current); });
    els.sleep.addEventListener('click', cycleSleep);
    els.seek.addEventListener('input', () => {
        const dur = engine.audio.duration || 0;
        if (dur) engine.seek((Number(els.seek.value) / 1000) * dur);
    });
    $('mu-queue-btn').addEventListener('click', () => {
        els.queue.hidden = !els.queue.hidden;
        if (!els.queue.hidden) renderQueue();
    });
    $('mu-queue-close').addEventListener('click', () => { els.queue.hidden = true; });
    $('mu-queue-clear')?.addEventListener('click', () => {
        engine.clearQueue();
        renderQueue();
        closePlayer();
    });
    els.queueList.addEventListener('click', (event) => {
        const remove = event.target.closest('[data-act="queue-remove"]');
        if (remove) {
            event.stopPropagation();
            engine.removeAt(Number(remove.dataset.index));
            renderQueue();
            return;
        }
        const row = event.target.closest('[data-index]');
        if (row) { engine.jumpTo(Number(row.dataset.index)); renderQueue(); }
    });
    // sheet
    els.sheet.addEventListener('click', (event) => {
        if (event.target.closest('[data-close="sheet"]')) closeSheet();
    });
    // login / online / install / theme
    els.loginForm.addEventListener('submit', login);
    window.addEventListener('online', () => { setNet(false); loadState().then(renderView); });
    window.addEventListener('offline', () => setNet(true));
    window.addEventListener('popstate', () => {
        if (ui.view === 'playlist') go('library');
        else if (ui.view === 'podcast') go('podcasts');
    });
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') refreshLibrary();
    });
    window.addEventListener('beforeinstallprompt', (event) => {
        event.preventDefault();
        ui.installPrompt = event;
        els.install.hidden = false;
    });
    els.install.addEventListener('click', async () => {
        if (!ui.installPrompt) return;
        ui.installPrompt.prompt();
        try { await ui.installPrompt.userChoice; } catch { /* ignore */ }
        ui.installPrompt = null;
        els.install.hidden = true;
    });
    if ('serviceWorker' in navigator && window.isSecureContext) {
        navigator.serviceWorker.register('/sw.js').catch((error) => logWarn('SW registration failed', String(error)));
    }
    if (window.matchMedia('(display-mode: standalone)').matches) els.install.hidden = true;
}

function toggleTheme() {
    const root = document.documentElement;
    const dark = !root.classList.contains('dark');
    root.className = dark ? 'dark' : 'light';
    try { localStorage.setItem('ethos-theme', root.className); } catch { /* ignore */ }
}

// ── content click handling ─────────────────────────────────────────────────

async function onContentClick(event) {
    const target = event.target.closest('[data-act]');
    if (!target) return;
    const act = target.dataset.act;
    const key = target.dataset.key || '';
    if (act === 'play-track') { playTrackByKey(key); return; }
    if (act === 'track-menu') { openRowMenu(trackByKey(key), target.dataset.ctx || 'search'); return; }
    if (act === 'go') { go(target.dataset.view); return; }
    if (act === 'category') {
        const category = CATEGORIES[Number(target.dataset.index)];
        if (category) { const input = $('mu-search-input'); if (input) input.value = category.query; runSearch(category.query); }
        return;
    }
    if (act === 'history') { runSearch(target.dataset.query); return; }
    if (act === 'more-tracks') { if (ui.searchState) runSearch(ui.searchState.query, { append: true }); return; }
    if (act === 'suggest') {
        const type = target.dataset.type;
        if (type === 'playlist') { openPlaylistById(target.dataset.id); return; }
        if (type === 'podcast') { openPodcast(target.dataset.id); return; }
        const query = target.dataset.title || '';
        runSearch(query);
        return;
    }
    if (act === 'open-podcast') { openPodcast(target.dataset.id); return; }
    if (act === 'add-feed') { addCustomFeed(); return; }
    if (act === 'podcast-refresh') { const pod = ui.activePodcast; if (pod) { ui.podcastFailedId = null; refreshPodcast(pod, (store.podcasts.subscribed || []).find((item) => item.id === pod.id)); } return; }
    if (act === 'podcast-unsubscribe') {
        try {
            await json(`/api/music/podcasts/${target.dataset.id}`, { method: 'DELETE' });
            store.podcasts.subscribed = (store.podcasts.subscribed || []).filter((item) => item.id !== target.dataset.id);
            toast('Subskrypcja usunięta');
            go('podcasts');
        } catch (error) { toast(errorText(error), 'error'); }
        return;
    }
    if (act === 'play-station') { playStation(stationById(target.dataset.station)); return; }
    if (act === 'station-fav') { toggleFavourite(target.dataset.station); return; }
    if (act === 'open-playlist') { openPlaylistById(target.dataset.id); return; }
    if (act === 'open-shared') { openSharedPlaylist(target.dataset.id); return; }
    if (act === 'new-playlist') { const name = await promptText('Nowa playlista', 'Nazwa playlisty'); if (name) await createPlaylist(name); return; }
    if (act === 'play-playlist') { if (ui.activePlaylist?.tracks?.length) { engine.playQueue(ui.activePlaylist.tracks); openPlayer(); } return; }
    if (act === 'shuffle-playlist') {
        const tracks = ui.activePlaylist?.tracks || [];
        if (tracks.length) {
            const shuffled = tracks.slice().sort(() => Math.random() - 0.5);
            engine.playQueue(shuffled);
            openPlayer();
        }
        return;
    }
    if (act === 'download-playlist') {
        const tracks = ui.activePlaylist?.tracks || [];
        tracks.forEach((track) => offline.downloadTrack(track));
        toast(`${tracks.length} utworów w kolejce pobierania`);
        return;
    }
    if (act === 'delete-playlist') {
        const playlist = ui.activePlaylist;
        if (!playlist) return;
        openSheet('Usunąć playlistę?', [
            sheetButton(playlist.id === LIKED_PLAYLIST_ID ? 'Tak, wyczyść' : 'Tak, usuń', async () => {
                try {
                    await json(`/api/music/playlists/${playlist.id}`, { method: 'DELETE' });
                    store.playlists = store.playlists.filter((item) => item.id !== playlist.id);
                    if (playlist.id === LIKED_PLAYLIST_ID) store.liked = [];
                    await persistPlaylists();
                    toast(playlist.id === LIKED_PLAYLIST_ID ? 'Polubienia wyczyszczone' : 'Playlista usunięta');
                    go('library');
                } catch (error) { toast(errorText(error), 'error'); }
            }),
            sheetButton('Anuluj', () => {}),
        ]);
        return;
    }
    if (act === 'play-download' || act === 'download-menu') {
        const row = offline.rowFor(key);
        if (act === 'download-menu') { openDownloadMenu(key); return; }
        if (row?.status === 'done') { engine.playQueue([row.track]); openPlayer(); }
        else toast('Utwór nie jest jeszcze pobrany', 'warning');
        return;
    }
    if (act === 'resume-downloads') { offline.resumeDownloads(); renderDownloads(); toast('Wznowiono pobieranie'); return; }
    if (act === 'open-downloads') { go('downloads'); return; }
    if (act === 'clear-downloads') {
        openSheet('Wyczyścić pobrane?', [
            sheetButton('Tak, usuń wszystko', async () => {
                await offline.clearAll();
                toast('Pobrane usunięte');
                renderSettings();
            }),
            sheetButton('Anuluj', () => {}),
        ]);
        return;
    }
    if (act === 'toggle-theme') { toggleTheme(); return; }
    if (act === 'install-app') {
        if (ui.installPrompt) els.install.click();
        else toast('Użyj menu przeglądarki → „Dodaj do ekranu głównego”', 'info');
        return;
    }
    if (act === 'logout') { logout(); return; }
    if (act === 'back') {
        if (ui.view === 'playlist') go('library');
        else if (ui.view === 'podcast') go('podcasts');
        else go('home');
        return;
    }
    if (act === 'queue-jump') { engine.jumpTo(Number(target.dataset.index)); renderQueue(); }
}

function playTrackByKey(key) {
    const track = trackByKey(key);
    if (!track || track.is_live) return;
    const context = ui.queueContext;
    const index = context.tracks.findIndex((item) => tkey(item) === key);
    if (index >= 0) {
        // Play the list the row belongs to, so “next” keeps going.
        engine.playQueue(context.tracks, index);
    } else {
        engine.playQueue([track]);
    }
    openPlayer();
}

function openPlaylistById(id, { push = true } = {}) {
    const playlist = store.playlists.find((item) => item.id === id);
    if (playlist) {
        ui.activePlaylist = playlist;
        ui.view = 'playlist';
        setNavActive('playlist');
        if (push) { try { history.pushState({ musicView: 'playlist', id }, ''); } catch { /* ignore */ } }
        renderPlaylistDetail();
        els.content.scrollTop = 0;
    }
}

async function openSharedPlaylist(grantId) {
    const grant = store.sharedPlaylists.find((item) => String(item.id) === String(grantId));
    if (!grant) { toast('Nie znaleziono udostępnionej playlisty', 'warning'); return; }
    try {
        const shared = await json(`/api/music/playlists/shared?owner=${encodeURIComponent(grant.owner)}&id=${encodeURIComponent(grant.resource)}`);
        ui.activePlaylist = { ...shared, read_only: true, name: shared.name || grant.label };
        ui.view = 'playlist';
        setNavActive('playlist');
        try { history.pushState({ musicView: 'playlist', id: shared.id }, ''); } catch { /* ignore */ }
        renderPlaylistDetail();
        els.content.scrollTop = 0;
    } catch (error) {
        toast(errorText(error), 'error');
    }
}

// ── boot ───────────────────────────────────────────────────────────────────

if (authed() && getToken()) {
    showApp();
    startApp().catch((error) => {
        logError('Music PWA boot failed', String(error));
        showLogin('Nie udało się wczytać aplikacji');
    });
} else {
    showLogin();
}
