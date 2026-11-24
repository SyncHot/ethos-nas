// EthOS Videos — standalone phone PWA at /videos.
//
// A deliberate sibling of /notes: mobile-first library + player, installable,
// with offline downloads that live in a dedicated Cache Storage bucket
// (`ethos-media-v1`, keys `/__media/<exportId>`) served back by sw.js.
//
// The desktop keeps its windowed Video Station package; both talk to the same
// /api/videos/* endpoints. Rules that apply here too:
//   • long work reports through core/progress.js (no bespoke bars);
//   • toasts come from core/notification-center.js;
//   • diagnostics go through core/logger.js (log context `videos`).

import { apiFetch, getToken, setToken, getCurrentClaims } from '../core/api.js';
import { notify } from '../core/notification-center.js';
import { logError, logInfo, logWarn, setLogContext } from '../core/logger.js';
import { startTask } from '../core/progress.js';
import '../core/error-reporter.js';
import {
    initOffline, downloadsSnapshot, queueDownload, pumpDownloads, syncDownloads,
    cancelDownload, retryDownload, deleteDownload, localMediaUrl, storageInfo,
    refreshStorage, localSubtitle, srtToVtt, saveLocalResume, clearLocalResume,
    allLocalResumes, resumeServerErrors, serverExports, adoptExport, hevcSupported,
} from './offline.js';

setLogContext({ app: 'videos' });

const $ = (id) => document.getElementById(id);
const HLS_SCRIPT = '/static/vendor/hls.min.js';
const RESUME_EVERY_MS = 5000;
const WAKE_LOCK_MS = 15 * 60 * 1000; // informational only; the browser decides

const state = {
    library: null,
    home: { continue_watching: [], recently_added: [] },
    resume: {},
    list: new Set(),
    query: '',
    tabs: 'all',          // all | movies | series | mylist
    sort: 'recent',       // recent | title
    genre: 'all',
    show: null,           // series name when browsing a show
    recentSearches: [],
    searchTimer: null,
    config: { options: {} },
    localResume: new Map(),
    offlineUnlocked: false,
    batchErrors: [],
    downloads: [],
    // "auto" = one tap: the NAS copies the file when it can, otherwise it makes
    // a small fast encode. Explicit versions live behind the Quality toggle.
    downloadQuality: 'auto',
    downloadQualities: [],
    info: new Map(),      // path → /api/videos/info (probe, cached on the NAS)
    exports: [],
    downloadTimer: null,
    selectMode: false,
    selected: new Set(),
    installPrompt: null,
    offline: !navigator.onLine,
    localOnly: false,
    player: null,
};

const els = {
    login: $('vd-login'),
    loginForm: $('vd-login-form'),
    loginError: $('vd-login-error'),
    app: $('vd-app'),
    net: $('vd-net'),
    install: $('vd-install'),
    downloadsBtn: $('vd-downloads-btn'),
    downloadsBadge: $('vd-downloads-badge'),
    menuBtn: $('vd-menu-btn'),
    menu: $('vd-menu'),
    search: $('vd-search'),
    content: $('vd-content'),
    selectBtn: $('vd-select-btn'),
    selectBar: $('vd-select-bar'),
    selectCount: $('vd-select-count'),
    selectAll: $('vd-select-all'),
    selectDownload: $('vd-select-download'),
    selectDownloadLabel: $('vd-select-download-label'),
    selectCancel: $('vd-select-cancel'),
    sheet: $('vd-sheet'),
    sheetBody: $('vd-sheet-body'),
    downloads: $('vd-downloads'),
    downloadsList: $('vd-downloads-list'),
    downloadsSummary: $('vd-downloads-summary'),
    storage: $('vd-storage'),
    settings: $('vd-settings'),
    qualityOptions: $('vd-quality-options'),
    player: $('vd-player'),
    video: $('vd-video'),
    playerUi: $('vd-player-ui'),
    playerTitle: $('vd-player-title'),
    playerMode: $('vd-player-mode'),
    playerEnc: $('vd-player-enc'),
    playerError: $('vd-player-error'),
    playerErrorText: $('vd-player-error-text'),
    seek: $('vd-seek'),
    buffered: $('vd-buffered'),
    time: $('vd-time'),
    playPause: $('vd-playpause'),
    bigPlay: $('vd-bigplay'),
    sub: $('vd-sub'),
    audio: $('vd-audio'),
    quality: $('vd-quality'),
    speed: $('vd-speed'),
    cast: $('vd-cast'),
    upnext: $('vd-upnext'),
    upnextText: $('vd-upnext-text'),
    upnextPlay: $('vd-upnext-play'),
    upnextCancel: $('vd-upnext-cancel'),
};

// ── helpers ────────────────────────────────────────────────────────────────

function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function fmtTime(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
        : `${m}:${String(sec).padStart(2, '0')}`;
}

function fmtBytes(n) {
    const v = Number(n) || 0;
    if (v >= 1e9) return `${(v / 1e9).toFixed(2)} GB`;
    if (v >= 1e6) return `${(v / 1e6).toFixed(1)} MB`;
    if (v >= 1e3) return `${Math.round(v / 1e3)} KB`;
    return `${v} B`;
}

function basename(path) {
    return String(path || '').split('/').filter(Boolean).pop() || '';
}

function token() {
    return getToken() || '';
}

function withToken(url) {
    return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token())}`;
}

function toast(message, kind = 'info') {
    try { notify({ kind, message }); } catch { /* host missing */ }
}

function apiError(error) {
    return error?.detail || error?.message || 'Request failed';
}

async function apiJson(path, options = {}) {
    const res = await apiFetch(path, options);
    if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
            const payload = await res.clone().json();
            detail = payload?.detail || payload?.error || detail;
            if (typeof detail !== 'string') detail = JSON.stringify(detail);
        } catch { /* non-JSON */ }
        const err = new Error(detail);
        err.status = res.status;
        throw err;
    }
    return res.json();
}

function apiPost(path, body) {
    return apiJson(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
}

// ── auth ───────────────────────────────────────────────────────────────────

function claims() {
    return getCurrentClaims() || null;
}

function authed() {
    const payload = claims();
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
    setTimeout(() => $('vd-username')?.focus(), 60);
}

function showApp() {
    els.login.hidden = true;
    els.app.hidden = false;
}

async function login(event) {
    event.preventDefault();
    els.loginError.textContent = '';
    const username = $('vd-username').value.trim();
    const password = $('vd-password').value;
    if (!username || !password) return;
    const submit = $('vd-login-submit');
    submit.disabled = true;
    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.token) throw new Error(data.detail || 'Sign-in failed');
        setToken(data.token);
        try { localStorage.setItem('token', data.token); } catch { /* ignore */ }
        $('vd-password').value = '';
        showApp();
        await boot();
    } catch (error) {
        els.loginError.textContent = apiError(error);
    } finally {
        submit.disabled = false;
    }
}

function logout() {
    try { localStorage.removeItem('token'); } catch { /* ignore */ }
    setToken(null);
    stopPlayer();
    if (els.selectBar) setSelectMode(false);
    state.offlineUnlocked = false;
    state.localOnly = false;
    state.show = null;
    state.library = null;
    state.home = { continue_watching: [], recently_added: [] };
    showLogin();
}

// ── library ────────────────────────────────────────────────────────────────

function displayTitle(item) {
    const tm = item?.tmdb || {};
    if (item?.kind === 'episode') {
        const show = item.show || tm.title || basename(String(item.path || '').split('/').slice(0, -1).join('/')) || item.title || item.name || '';
        const ep = episodeLabel(item);
        return ep ? `${show} · ${ep}` : show;
    }
    return tm.title || item?.title || item?.name || basename(item?.path);
}

function episodeLabel(item) {
    if (item?.season == null && item?.episode == null) return '';
    return `S${String(item.season ?? 0).padStart(2, '0')}E${String(item.episode ?? 0).padStart(2, '0')}`;
}

function artUrl(item, kind = 'poster') {
    const tm = item?.tmdb || {};
    const file = kind === 'backdrop'
        ? (tm.backdrop_file || tm.poster_file)
        : (tm.poster_file || tm.backdrop_file);
    if (file) return withToken(`/api/videos/metadata/image?file=${encodeURIComponent(file)}`);
    const width = kind === 'backdrop' ? 960 : 342;
    return withToken(`/api/videos/thumbnail?path=${encodeURIComponent(item.path)}&w=${width}`);
}

function downloadFor(path) {
    return state.downloads.find((d) => d.path === path && d.status === 'done') || null;
}

function pendingFor(path) {
    return state.downloads.find((d) => d.path === path
        && d.status !== 'done' && d.status !== 'failed') || null;
}

function qualityLabel(value) {
    const hit = (state.downloadQualities || []).find((q) => q.value === value);
    if (hit) return hit.label;
    if (value === 'auto') return 'Auto';
    if (value === 'original') return 'Original';
    if (value === 'tiny') return '480p';
    return value === 'fast' ? '720p' : '1080p';
}

// ── series grouping ───────────────────────────────────────────────────────

function episodeOrder(a, b) {
    return (Number(a.season) || 0) - (Number(b.season) || 0)
        || (Number(a.episode) || 0) - (Number(b.episode) || 0);
}

function groupShows(items = videos()) {
    const map = new Map();
    for (const item of items) {
        if (item.kind !== 'episode') continue;
        const show = item.show
            || basename(String(item.path || '').split('/').slice(0, -1).join('/'))
            || item.title;
        if (!show) continue;
        if (!map.has(show)) map.set(show, { show, items: [] });
        map.get(show).items.push(item);
    }
    return [...map.values()].map((group) => {
        const sorted = group.items.slice().sort(episodeOrder);
        const poster = sorted.find((item) => item.tmdb?.poster_file) || sorted[0];
        return { show: group.show, items: sorted, poster, latest: sorted[sorted.length - 1] };
    }).sort((a, b) => a.show.localeCompare(b.show));
}

function episodesOf(show) {
    return groupShows().find((group) => group.show === show)?.items || [];
}

function nextEpisodeOf(item) {
    if (item?.kind !== 'episode') return null;
    const episodes = episodesOf(item.show);
    const index = episodes.findIndex((episode) => episode.path === item.path);
    return index >= 0 ? (episodes[index + 1] || null) : null;
}

// ── progress (server + on-device) ─────────────────────────────────────────

function resumeFor(item) {
    const remote = state.resume[item.path];
    const local = state.localResume.get(item.path);
    if (local && (!remote || (local.updated_at || 0) / 1000 >= (remote.updated_at || 0))) {
        return { time: local.time, duration: local.duration };
    }
    return remote || null;
}

function isNearlyWatched(item) {
    const resume = resumeFor(item);
    return !!resume && resume.duration > 0 && resume.time >= resume.duration * 0.97;
}

// ── download size estimate (≈, from the source size/quality) ─────────────

const QUALITY_BPS = { tiny: 1_500_000, fast: 2_800_000, balanced: 5_000_000 };
const SOURCE_BPS = { '480p': 1_500_000, '720p': 3_000_000, '1080p': 5_000_000, '2160p': 15_000_000, '4k': 16_000_000, '8k': 40_000_000 };

function qualityBps(quality) {
    const hit = (state.downloadQualities || []).find((entry) => entry.value === quality);
    return Number(hit?.bps) || QUALITY_BPS[quality] || QUALITY_BPS.balanced;
}

function sourceBps(item) {
    const quality = String(item.quality || '').toLowerCase().replace(/\s+/g, '');
    return SOURCE_BPS[quality] || 5_000_000;
}

// Mirrors video_engine.copy_video_ok(): can the NAS copy instead of encoding?
const COPY_SAFE_PIX_FMTS = new Set(['yuv420p', 'yuvj420p']);

function copyableSource(info) {
    if (!info || !(Number(info.height) > 0)) return false;
    const codec = String(info.video_codec || '').toLowerCase();
    const pix = String(info.pix_fmt || '').toLowerCase();
    if (codec === 'h264') return !pix || COPY_SAFE_PIX_FMTS.has(pix);
    return codec === 'hevc' && hevcSupported();
}

/** Mirrors video_engine.resolve_quality("auto") for the size estimate/hint. */
function autoPreset(info) {
    if (copyableSource(info)) return 'original';
    const height = Number(info?.height) || 0;
    return height > 0 && height <= 480 ? 'tiny' : 'fast';
}

function sourceSize(item, info) {
    return Number(info?.size) || Number(item?.size) || 0;
}

function estimateSize(item, quality) {
    if (!item) return 0;
    const info = state.info.get(item.path);
    const preset = quality === 'auto' ? autoPreset(info) : quality;
    if (preset === 'original') return sourceSize(item, info);
    const bps = qualityBps(preset);
    const size = sourceSize(item, info);
    if (!size) return 0;
    return Math.round(size * (bps / sourceBps(item)));
}

function estimateLabel(item, quality) {
    const bytes = estimateSize(item, quality);
    return bytes ? `≈ ${fmtBytes(bytes)}` : '';
}

/** One short line explaining what "Download" will actually do for this file. */
function autoHint(item) {
    const info = state.info.get(item.path);
    if (info && copyableSource(info)) {
        const size = estimateLabel(item, 'auto');
        return `No conversion — the NAS copies the file${size ? ` (${size})` : ''}.`;
    }
    if (info) {
        const size = estimateLabel(item, 'auto');
        return `This source needs a re-encode — fast ${autoPreset(info) === 'tiny' ? '480p' : '720p'} copy${size ? ` (${size})` : ''}.`;
    }
    return 'Copy when the file allows it, otherwise a small fast encode — no settings needed.';
}

async function quotaAllows(needBytes) {
    if (!needBytes) return true;
    const info = await refreshStorage();
    if (!info?.quota) return true;
    const free = Math.max(0, info.quota - info.usage);
    if (needBytes > free * 0.95) {
        toast(`Not enough device storage: need ≈${fmtBytes(needBytes)}, free ≈${fmtBytes(free)}`, 'error');
        return false;
    }
    return true;
}

// ── search history ───────────────────────────────────────────────────────

function loadRecentSearches() {
    try {
        const raw = JSON.parse(localStorage.getItem('vd-recent-searches') || '[]');
        return Array.isArray(raw) ? raw.filter((q) => typeof q === 'string').slice(0, 8) : [];
    } catch {
        return [];
    }
}

function rememberSearch(query) {
    const q = String(query || '').trim();
    if (q.length < 2) return;
    state.recentSearches = [q, ...state.recentSearches.filter((old) => old.toLowerCase() !== q.toLowerCase())].slice(0, 8);
    try { localStorage.setItem('vd-recent-searches', JSON.stringify(state.recentSearches)); } catch { /* ignore */ }
}

async function loadConfig() {
    try {
        const cfg = await apiJson('/api/videos/config');
        state.config = cfg || { options: {} };
        const lang = cfg?.options?.preferred_audio_lang;
        if (lang && !localStorage.getItem('vd-audio-lang')) {
            try { localStorage.setItem('vd-audio-lang', lang); } catch { /* ignore */ }
        }
    } catch { /* offline or older core — defaults apply */ }
}

async function loadLocalResumes() {
    try {
        const rows = await allLocalResumes();
        state.localResume = new Map(rows.map((row) => [row.path, row]));
    } catch {
        state.localResume = new Map();
    }
}

async function flushLocalResumes() {
    if (!authed() || !state.localResume.size) return;
    for (const row of [...state.localResume.values()]) {
        try {
            const res = await apiFetch('/api/videos/resume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: row.path, time: row.time, duration: row.duration }),
            });
            if (!res.ok) continue;
            state.localResume.delete(row.path);
            await clearLocalResume(row.path);
            if (row.duration && row.time < row.duration * 0.97) {
                state.resume[row.path] = { time: row.time, duration: row.duration, updated_at: row.updated_at / 1000 };
            }
        } catch { /* retry on the next reconnect */ }
    }
}

async function boot() {
    renderLoading('Reading your library…');
    try {
        const bootData = await apiJson('/api/videos/bootstrap?limit=12');
        state.home = bootData.home || state.home;
        state.resume = bootData.resume || {};
        state.list = new Set(bootData.list || []);
        const data = await apiJson('/api/videos/library');
        state.library = data;
        state.localOnly = false;
        render();
        loadDownloadSettings();
        loadConfig();
    } catch (error) {
        if (error?.status === 401) { showLogin('Your session expired — sign in again.'); return; }
        // Offline (or the NAS is down): an installed PWA must still show what it
        // already has instead of a dead end — the download rows carry the item.
        const local = downloadsSnapshot()
            .filter((entry) => entry.status === 'done')
            .map((entry) => entry.item || { path: entry.path, title: entry.title })
            .filter((item) => item?.path);
        if (local.length) {
            state.library = { videos: local };
            state.localOnly = true;
            setOnline(false);
            render();
            toast('Offline — showing downloads on this device', 'warning');
            logWarn('Library unavailable — offline library', String(error));
            return;
        }
        els.content.innerHTML = `<div class="vd-empty"><b>Could not load the library</b>${esc(apiError(error))}</div>`;
        logWarn('Library load failed', String(error));
    }
}

function renderLoading(text) {
    els.content.innerHTML = `<div class="vd-empty"><b>Loading…</b>${esc(text)}</div>`;
}

function videos() {
    return state.library?.videos || [];
}

function searchText(item) {
    const tm = item?.tmdb || {};
    return [displayTitle(item), item?.name, item?.title, item?.show || '', tm.title || '', (tm.genres || []).join(' ')]
        .filter(Boolean).join(' ').toLowerCase();
}

function cardHtml(item) {
    const dm = downloadFor(item.path);
    const resume = resumeFor(item);
    const progress = resume && resume.duration ? Math.min(100, (resume.time / resume.duration) * 100) : 0;
    const sub = [item.quality, item.kind === 'episode' ? episodeLabel(item) : (item.tmdb?.year || '')]
        .filter(Boolean).join(' · ');
    return `<button class="vd-card" type="button" data-action="open" data-path="${esc(item.path)}">
        <span class="vd-card-art">
            <img src="${esc(dm?.posterUrl || artUrl(item))}" alt="" loading="lazy" decoding="async">
            ${item.quality ? `<span class="vd-card-badge">${esc(item.quality)}</span>` : ''}
            ${progress > 0 ? `<span class="vd-card-progress"><i style="width:${progress.toFixed(1)}%"></i></span>` : ''}
            ${dm ? '<span class="vd-card-offline">OFFLINE</span>' : ''}
        </span>
        <span class="vd-card-title">${esc(displayTitle(item))}</span>
        <span class="vd-card-sub">${esc(sub)}</span>
    </button>`;
}

function showCardHtml(group) {
    const sub = `${group.items.length} episodes · ${episodeLabel(group.latest)}`;
    return `<button class="vd-card" type="button" data-action="show" data-show="${esc(group.show)}">
        <span class="vd-card-art">
            <img src="${esc(artUrl(group.poster))}" alt="" loading="lazy" decoding="async">
            <span class="vd-card-badge">${group.items.length} eps</span>
        </span>
        <span class="vd-card-title">${esc(group.show)}</span>
        <span class="vd-card-sub">${esc(sub)}</span>
    </button>`;
}

function shelfHtml(title, items, { row = true } = {}) {
    if (!items || !items.length) return '';
    return `<section class="vd-shelf">
        <div class="vd-shelf-head"><h2>${esc(title)}</h2><span>${items.length}</span></div>
        <div class="${row ? 'vd-row' : 'vd-grid'}">${items.map((item) => cardHtml(item)).join('')}</div>
    </section>`;
}

function gridHtml(items, emptyText) {
    if (!items.length) return `<div class="vd-empty"><b>Nothing here yet</b>${esc(emptyText || '')}</div>`;
    return `<div class="vd-grid">${items.map((item) => cardHtml(item)).join('')}</div>`;
}

function filterSort(items) {
    let out = items;
    if (state.genre !== 'all') {
        out = out.filter((item) => (item.tmdb?.genres || []).includes(state.genre));
    }
    const sorted = out.slice();
    if (state.sort === 'title') {
        sorted.sort((a, b) => displayTitle(a).localeCompare(displayTitle(b)));
    } else {
        sorted.sort((a, b) => (Number(b.modified) || 0) - (Number(a.modified) || 0));
    }
    return sorted;
}

function genreOptions() {
    const genres = new Set();
    for (const item of videos()) {
        for (const genre of item.tmdb?.genres || []) genres.add(genre);
    }
    return [...genres].sort();
}

function tabsHtml() {
    const all = videos();
    const movies = all.filter((item) => item.kind !== 'episode');
    const shows = groupShows(all);
    const counts = { all: all.length, movies: movies.length, series: shows.length, mylist: state.list.size };
    const tabs = [['all', 'All'], ['movies', 'Movies'], ['series', 'Series'], ['mylist', 'My List']];
    const genres = genreOptions();
    return `<div class="vd-tabs" role="tablist" aria-label="Library sections">
        ${tabs.map(([value, label]) => `<button type="button" role="tab" aria-selected="${state.tabs === value}"
            class="vd-tab ${state.tabs === value ? 'is-active' : ''}" data-action="tab" data-tab="${value}">
            ${label}<span>${counts[value]}</span></button>`).join('')}
        <span class="vd-grow"></span>
        <select class="vd-select" data-action="sort" aria-label="Sort">
            <option value="recent" ${state.sort === 'recent' ? 'selected' : ''}>Recently added</option>
            <option value="title" ${state.sort === 'title' ? 'selected' : ''}>Title</option>
        </select>
        ${genres.length && state.tabs !== 'series' ? `<select class="vd-select" data-action="genre" aria-label="Genre">
            <option value="all">All genres</option>
            ${genres.map((genre) => `<option value="${esc(genre)}" ${state.genre === genre ? 'selected' : ''}>${esc(genre)}</option>`).join('')}
        </select>` : ''}
    </div>`;
}

function recentSearchesHtml() {
    if (!state.recentSearches.length) return '';
    return `<div class="vd-recent" aria-label="Recent searches">
        ${state.recentSearches.map((q) => `<button type="button" data-action="recent-search" data-q="${esc(q)}">${esc(q)}</button>`).join('')}
    </div>`;
}

function render() {
    const q = state.query.trim().toLowerCase();
    if (q) {
        const hits = videos().filter((item) => searchText(item).includes(q)).slice(0, 120);
        els.content.innerHTML = hits.length
            ? `<section class="vd-shelf"><div class="vd-shelf-head"><h2>Results</h2><span>${hits.length}</span></div>
               <div class="vd-grid">${hits.map((item) => cardHtml(item)).join('')}</div></section>`
            : `<div class="vd-empty"><b>No matches</b>Nothing in the library matches “${esc(state.query)}”.</div>`;
        paintSelection();
        return;
    }
    if (!state.library) { renderLoading('Reading your library…'); return; }
    if (state.show) { renderShow(state.show); return; }

    const all = videos();
    const movies = all.filter((item) => item.kind !== 'episode');
    const shows = groupShows(all);
    const pieces = [tabsHtml(), recentSearchesHtml()];

    if (state.tabs === 'series') {
        pieces.push(shows.length
            ? `<div class="vd-grid">${shows.map((group) => showCardHtml(group)).join('')}</div>`
            : `<div class="vd-empty"><b>No series found</b>Episode files are grouped by their SxxEyy name or folder.</div>`);
    } else if (state.tabs === 'mylist') {
        const items = [...state.list].map((path) => findItem(path)).filter(Boolean);
        pieces.push(gridHtml(filterSort(items), 'Open a title and choose “My List” to save it here.'));
    } else if (state.tabs === 'movies') {
        pieces.push(gridHtml(filterSort(movies), 'No movies match this filter.'));
    } else {
        const offline = state.downloads.filter((d) => d.status === 'done');
        if (state.offline && offline.length) {
            const items = offline.map((d) => findItem(d.path) || d.item).filter(Boolean);
            pieces.push(shelfHtml('On this device', items));
        }
        const serverCw = (state.home.continue_watching || []).filter((item) => item?.path);
        const localCw = [...state.localResume.keys()]
            .map((path) => findItem(path))
            .filter((item) => item && !serverCw.some((cw) => cw.path === item.path) && !isNearlyWatched(item));
        pieces.push(shelfHtml('Continue watching', [...serverCw, ...localCw].slice(0, 18)));
        pieces.push(shelfHtml('Recently added', (state.home.recently_added || []).slice(0, 18)));
        if (shows.length) {
            pieces.push(`<section class="vd-shelf">
                <div class="vd-shelf-head"><h2>Series</h2><span>${shows.length} shows</span>
                    <button class="vd-btn small" type="button" data-action="tab" data-tab="series">See all</button></div>
                <div class="vd-row">${shows.slice(0, 12).map((group) => showCardHtml(group)).join('')}</div>
            </section>`);
        }
        pieces.push(shelfHtml('Movies', filterSort(movies).slice(0, 300), { row: false }));
        if (!all.length) {
            pieces.push(`<div class="vd-empty"><b>No videos yet</b>Add a folder in the desktop Videos app (Settings → Library).</div>`);
        }
    }
    els.content.innerHTML = pieces.join('');
    paintSelection();
    updateDownloadsBadge();
}

function renderShow(show) {
    const episodes = episodesOf(show);
    const header = `<div class="vd-show-head">
        <button class="vd-icon-btn" type="button" data-action="back-home" aria-label="Back">←</button>
        <h1>${esc(show)}</h1>
        <span>${episodes.length} episodes</span>
    </div>`;
    if (!episodes.length) {
        els.content.innerHTML = header + `<div class="vd-empty"><b>No episodes</b>This show has no readable episode files.</div>`;
        return;
    }
    const seasons = [...new Set(episodes.map((ep) => Number(ep.season) || 0))].sort((a, b) => a - b);
    const next = episodes.find((ep) => !isNearlyWatched(ep)) || episodes[0];
    const sections = seasons.map((season) => {
        const list = episodes.filter((ep) => (Number(ep.season) || 0) === season);
        return `<section class="vd-shelf">
            <div class="vd-shelf-head"><h2>Season ${season || '—'}</h2><span>${list.length} episodes</span>
                <button class="vd-btn small" type="button" data-action="download-season"
                    data-show="${esc(show)}" data-season="${season}">⬇ Download season</button></div>
            <div class="vd-grid">${list.map((episode) => cardHtml(episode)).join('')}</div>
        </section>`;
    }).join('');
    els.content.innerHTML = `${header}
        <div class="vd-toolbar">
            <button class="vd-btn primary" type="button" data-action="play" data-path="${esc(next.path)}">▶ Play next · ${esc(episodeLabel(next))}</button>
        </div>
        ${sections}`;
    paintSelection();
    updateDownloadsBadge();
}

// ── multi-select (queue many films at once) ───────────────────────────────

function setSelectMode(on) {
    state.selectMode = !!on;
    if (!state.selectMode) state.selected.clear();
    els.content.classList.toggle('is-selecting', state.selectMode);
    els.selectBtn.textContent = state.selectMode ? 'Done' : 'Select';
    els.selectBtn.classList.toggle('primary', state.selectMode);
    els.selectBar.hidden = !state.selectMode;
    els.selectDownloadLabel.textContent = qualityLabel(state.downloadQuality);
    paintSelection();
}

function paintSelection() {
    els.content.querySelectorAll('.vd-card[data-path]').forEach((card) => {
        card.classList.toggle('is-selected', state.selected.has(card.dataset.path));
    });
    if (!els.selectBar) return;
    els.selectCount.textContent = `${state.selected.size} selected`;
    els.selectDownload.disabled = state.selected.size === 0;
    els.selectAll.textContent = allVisibleSelected() ? 'Clear all' : 'Select all';
    els.selectDownloadLabel.textContent = qualityLabel(state.downloadQuality);
}

function visibleItems() {
    const seen = new Set();
    const out = [];
    els.content.querySelectorAll('[data-path]').forEach((el) => {
        const path = el.dataset.path;
        if (!path || seen.has(path)) return;
        const item = findItem(path);
        if (!item) return;
        seen.add(path);
        out.push(item);
    });
    return out;
}

function allVisibleSelected() {
    const items = visibleItems();
    return items.length > 0 && items.every((item) => state.selected.has(item.path));
}

function toggleSelect(path, cardEl) {
    if (state.selected.has(path)) state.selected.delete(path);
    else state.selected.add(path);
    if (cardEl) cardEl.classList.toggle('is-selected', state.selected.has(path));
    paintSelection();
}

function selectAllVisible() {
    const items = visibleItems();
    if (allVisibleSelected()) state.selected.clear();
    else items.forEach((item) => state.selected.add(item.path));
    paintSelection();
}

async function queueMany(items, quality, label) {
    const need = items.reduce((sum, item) => sum + estimateSize(item, quality), 0);
    if (!(await quotaAllows(need))) return 0;
    setSelectMode(false);
    state.batchErrors = [];
    let queued = 0;
    for (const item of items) {
        try {
            await queueDownload(item, quality);
            queued += 1;
        } catch (error) {
            const reason = apiError(error);
            state.batchErrors.push({ title: displayTitle(item), reason });
            logWarn('Batch download failed', `${item.path}: ${reason}`);
        }
    }
    state.downloads = downloadsSnapshot();
    updateDownloadsBadge();
    render();
    renderDownloads(true);
    if (state.batchErrors.length) {
        const first = state.batchErrors[0];
        toast(`${queued} queued · ${state.batchErrors.length} not queued — ${first.title}: ${first.reason}`, 'warning');
    } else {
        toast(`${queued} queued · ${label || qualityLabel(quality)}`, 'success');
    }
    return queued;
}

async function downloadSelected() {
    const items = [...state.selected].map((path) => findItem(path)).filter(Boolean);
    if (!items.length) return;
    await queueMany(items, state.downloadQuality);
}

async function downloadSeason(show, season) {
    const items = episodesOf(show).filter((episode) => (Number(episode.season) || 0) === Number(season || 0));
    if (!items.length) return;
    const queued = await queueMany(items, state.downloadQuality);
    if (queued) toast(`${queued} episodes queued · Season ${season || '—'}`, 'success');
}

async function redownload(item, quality) {
    const entry = downloadFor(item.path);
    if (entry) await deleteDownload(entry.id);
    state.downloads = downloadsSnapshot();
    updateDownloadsBadge();
    await queueOne(item, quality);
}

async function removeResume(item) {
    try {
        await apiFetch(`/api/videos/resume?path=${encodeURIComponent(item.path)}`, { method: 'DELETE' });
    } catch { /* offline — the local copy still goes away */ }
    delete state.resume[item.path];
    state.localResume.delete(item.path);
    await clearLocalResume(item.path);
    closeSheets();
    render();
    toast('Removed from Continue watching', 'success');
}

function findItem(path) {
    return videos().find((item) => item.path === path)
        || (state.home.continue_watching || []).find((item) => item.path === path)
        || (state.home.recently_added || []).find((item) => item.path === path)
        || state.downloads.find((d) => d.path === path)?.item
        || null;
}

// ── detail sheet ──────────────────────────────────────────────────────────

function openSheet(item) {
    const tm = item.tmdb || {};
    const dm = downloadFor(item.path);
    const pending = pendingFor(item.path);
    const resume = resumeFor(item);
    const next = nextEpisodeOf(item);
    const inList = state.list.has(item.path);
    const meta = [
        tm.rating != null ? `★ ${tm.rating}` : '',
        tm.year || '',
        item.quality || '',
        item.kind === 'episode' ? episodeLabel(item) : '',
        (item.video_codec || '').toUpperCase(),
        (item.audio_codec || '').toUpperCase(),
    ].filter(Boolean);
    const downloadBlock = dm
        ? `<button class="vd-btn" type="button" data-action="delete-download" data-path="${esc(item.path)}" data-id="${esc(dm.id)}">Delete download</button>`
        : pending
            ? `<button class="vd-btn" type="button" data-action="cancel-download" data-id="${esc(pending.id)}">Cancel · ${esc(stageText(pending))}</button>`
            : `<button class="vd-btn primary" type="button" data-action="download" data-path="${esc(item.path)}">⬇ Download</button>`;
    const choices = state.downloadQualities.length ? state.downloadQualities : [
        { value: 'auto', label: 'Auto · fastest' },
        { value: 'original', label: 'Original · no re-encode' },
        { value: 'balanced', label: '1080p' },
        { value: 'fast', label: '720p · small' },
        { value: 'tiny', label: '480p · tiny' },
    ];
    // Quality stays one tap away, not in the way: the primary button uses the
    // account default and this shows the explicit versions with their sizes.
    const versionRow = pending ? '' : `<details class="vd-quality-row">
        <summary>${dm ? 'Replace download version' : 'Quality'} · ${esc(qualityLabel(state.downloadQuality))}</summary>
        <div class="vd-quality-choices">
        ${choices.map((q) => {
            const size = q.value === 'auto' ? '' : estimateLabel(item, q.value);
            return `<button class="vd-btn small" type="button" data-action="${dm ? 'redownload-q' : 'download-q'}"
                data-q="${esc(q.value)}" data-path="${esc(item.path)}">${esc(q.label)}${size ? ` · ${esc(size)}` : ''}</button>`;
        }).join('')}
        </div>
    </details>`;
    const episodeActions = item.kind === 'episode'
        ? `${next ? `<button class="vd-btn" type="button" data-action="play" data-path="${esc(next.path)}">Next episode</button>` : ''}
           <button class="vd-btn" type="button" data-action="show" data-show="${esc(item.show || '')}">Series</button>`
        : '';
    els.sheetBody.innerHTML = `<div class="vd-detail">
        <div class="vd-detail-head">
            <div class="vd-detail-poster"><img src="${esc(artUrl(item))}" alt=""></div>
            <div class="vd-detail-copy">
                <h2 id="vd-sheet-title">${esc(displayTitle(item))}</h2>
                <div class="vd-detail-tags">${meta.map((m) => `<span class="vd-pill">${esc(m)}</span>`).join('')}</div>
                <div class="vd-detail-tags">
                    ${(tm.genres || []).slice(0, 4).map((g) => `<span class="vd-pill">${esc(g)}</span>`).join('')}
                </div>
            </div>
        </div>
        ${tm.overview ? `<p class="vd-overview">${esc(tm.overview)}</p>` : ''}
        <div class="vd-detail-actions">
            <button class="vd-btn primary" type="button" data-action="play" data-path="${esc(item.path)}">▶ ${resume ? `Resume ${fmtTime(resume.time)}` : 'Play'}</button>
            ${episodeActions}
            <button class="vd-btn" type="button" data-action="list" data-path="${esc(item.path)}">${inList ? '✓ My List' : '+ My List'}</button>
            ${downloadBlock}
            ${resume ? `<button class="vd-btn" type="button" data-action="remove-resume" data-path="${esc(item.path)}">Remove from Continue watching</button>` : ''}
        </div>
        <p class="vd-hint">${esc(autoHint(item))}</p>
        ${versionRow}
    </div>`;
    els.sheet.dataset.path = item.path;
    els.sheet.hidden = false;
    loadSheetInfo(item.path);
}

/** Probe once per item so the sheet can show the real size and what "auto" does. */
async function loadSheetInfo(path) {
    if (state.info.has(path)) return;
    try {
        const data = await apiJson(`/api/videos/info?path=${encodeURIComponent(path)}`);
        if (!data?.info) return;
        state.info.set(path, data.info);
        if (!els.sheet.hidden && els.sheet.dataset.path === path) {
            const item = findItem(path);
            if (item) openSheet(item);
        }
    } catch (error) {
        logWarn('Video info failed', `${path}: ${apiError(error)}`);
    }
}

function closeSheets() {
    els.sheet.hidden = true;
    els.downloads.hidden = true;
    if (els.settings) els.settings.hidden = true;
}

function toggleList(path) {
    const add = !state.list.has(path);
    apiPost('/api/videos/list', { path, add })
        .then((data) => {
            state.list = new Set(data.paths || []);
            toast(add ? 'Added to My List' : 'Removed from My List', 'success');
            const item = findItem(path);
            if (item && !els.sheet.hidden) openSheet(item);
        })
        .catch((error) => toast(`My List failed: ${apiError(error)}`, 'error'));
}

// ── downloads ─────────────────────────────────────────────────────────────

function updateDownloadsBadge() {
    const active = state.downloads.filter((d) => d.status !== 'done' && d.status !== 'failed').length;
    const done = state.downloads.filter((d) => d.status === 'done').length;
    const badge = els.downloadsBadge;
    badge.hidden = !(active + done);
    badge.textContent = String(active + done);
    badge.dataset.tone = active ? 'active' : 'idle';
}

function formatEta(seconds) {
    const total = Math.max(1, Math.round(Number(seconds) || 0));
    if (total < 60) return '<1 min';
    const minutes = Math.round(total / 60);
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60);
    return `${hours} h ${minutes % 60} min`;
}

function stageText(d) {
    const progress = d.bytesTotal
        ? Math.min(100, (d.bytesDone / d.bytesTotal) * 100)
        : (Number(d.percent) || 0);
    if (d.status === 'queued') {
        return Number(d.queuePosition) > 1 ? `In the queue · #${d.queuePosition}` : 'In the queue';
    }
    if (d.status === 'rendering') {
        const speed = Number(d.speed) > 0 ? ` · ${Number(d.speed).toFixed(1)}×` : '';
        const eta = Number(d.etaS) > 0 ? ` · ~${formatEta(d.etaS)}` : '';
        return `Preparing on the NAS… ${Math.round(Number(d.percent) || 0)}%${speed}${eta}`;
    }
    if (d.status === 'ready') return 'Ready on the NAS — saving to this device…';
    if (d.status === 'downloading') {
        return d.bytesTotal
            ? `Saving… ${Math.round(progress)}% · ${fmtBytes(d.bytesDone)} / ${fmtBytes(d.bytesTotal)}`
            : `Saving… ${Math.round(progress)}%`;
    }
    if (d.status === 'done') return d.size ? `${fmtBytes(d.size)} · offline` : 'Offline';
    if (d.status === 'failed') return d.error || 'Failed';
    return d.status;
}

function stageTone(d) {
    if (d.status === 'done') return 'ok';
    if (d.status === 'failed') return 'bad';
    if (d.status === 'ready') return 'gold';
    return 'busy';
}

function progressOf(d) {
    return d.bytesTotal
        ? Math.min(100, (d.bytesDone / d.bytesTotal) * 100)
        : (Number(d.percent) || 0);
}

function nasRowHtml(job) {
    const item = findItem(job.path) || { path: job.path, title: job.name };
    const version = job.quality_label || qualityLabel(job.quality);
    return `<div class="vd-dl" data-testid="vd-nas-row" data-nas-id="${esc(job.id)}">
        <div class="vd-dl-art"><img src="${esc(artUrl(item))}" alt="" loading="lazy" decoding="async"></div>
        <div class="vd-dl-copy">
            <b>${esc(displayTitle(item))}</b>
            <span class="vd-dl-stage" data-tone="ok">On the NAS · ${esc(fmtBytes(job.size))}</span>
            <span class="vd-dl-meta">${esc(version)}${job.mode === 'passthrough' ? ' · original' : ''}</span>
        </div>
        <div class="vd-dl-actions">
            <button class="vd-icon-btn" type="button" data-action="adopt-export" data-id="${esc(job.id)}" aria-label="Save to this device">↓</button>
            <button class="vd-icon-btn" type="button" data-action="delete-export" data-id="${esc(job.id)}" aria-label="Delete from the NAS">🗑</button>
        </div>
    </div>`;
}

function downloadRowHtml(d) {
    const item = findItem(d.path) || d.item || { path: d.path, title: d.title };
    const progress = progressOf(d);
    const active = ['queued', 'rendering', 'ready', 'downloading'].includes(d.status);
    const version = d.qualityLabel || qualityLabel(d.quality);
    const meta = [version, d.note, d.status === 'queued' && d.queuePosition ? `#${d.queuePosition}` : '']
        .filter(Boolean).join(' · ');
    return `<div class="vd-dl" data-testid="vd-download-row" data-dl-id="${esc(d.id)}">
        <div class="vd-dl-art"><img src="${esc(d.posterUrl || artUrl(item))}" alt="" loading="lazy" decoding="async"></div>
        <div class="vd-dl-copy">
            <b>${esc(displayTitle(item))}</b>
            <span class="vd-dl-stage" data-role="stage" data-tone="${esc(stageTone(d))}">${esc(stageText(d))}</span>
            ${meta ? `<span class="vd-dl-meta" data-role="meta">${esc(meta)}</span>` : ''}
            ${active ? `<div class="vd-dl-bar"><i data-role="bar" style="width:${progress.toFixed(1)}%"></i></div>` : ''}
        </div>
        <div class="vd-dl-actions">
            ${d.status === 'done' ? `<button class="vd-icon-btn" type="button" data-action="play-local" data-path="${esc(d.path)}" aria-label="Play offline">▶</button>` : ''}
            ${d.status === 'failed' ? `<button class="vd-icon-btn" type="button" data-action="retry-download" data-id="${esc(d.id)}" aria-label="Retry">↻</button>` : ''}
            ${active ? `<button class="vd-icon-btn" type="button" data-action="cancel-download" data-id="${esc(d.id)}" aria-label="Cancel">✕</button>` : ''}
            <button class="vd-icon-btn" type="button" data-action="delete-download" data-path="${esc(d.path)}" data-id="${esc(d.id)}" aria-label="Delete">🗑</button>
        </div>
    </div>`;
}

function downloadsSignature() {
    return state.downloads
        .map((d) => `${d.id}:${d.status}:${d.queuePosition || 0}:${d.error || ''}`)
        .join('|')
        + `|nas:${(state.exports || []).map((job) => job.id).join(',')}`
        + `|batch:${state.batchErrors.map((e) => e.title).join(',')}`;
}

function downloadsSummaryText() {
    const active = state.downloads.filter((d) => ['queued', 'rendering', 'downloading'].includes(d.status)).length;
    const ready = state.downloads.filter((d) => d.status === 'ready').length;
    const done = state.downloads.filter((d) => d.status === 'done').length;
    return [
        active ? `${active} downloading` : '',
        ready ? `${ready} ready to save` : '',
        done ? `${done} on this device` : '',
    ].filter(Boolean).join(' · ') || 'Nothing queued';
}

/**
 * Refresh only the progress bits of an already rendered list.
 *
 * A full re-render every 250 ms recreated the <img> elements and made the
 * poster of the film being saved visibly flicker.
 */
function updateDownloadsProgress() {
    for (const d of state.downloads) {
        const row = els.downloadsList.querySelector(`[data-dl-id="${d.id}"]`);
        if (!row) { renderDownloads(true); return; }
        const stage = row.querySelector('[data-role="stage"]');
        if (stage) {
            const text = stageText(d);
            if (stage.textContent !== text) stage.textContent = text;
            const tone = stageTone(d);
            if (stage.dataset.tone !== tone) stage.dataset.tone = tone;
        }
        const bar = row.querySelector('[data-role="bar"]');
        if (bar) bar.style.width = `${progressOf(d).toFixed(1)}%`;
        const meta = row.querySelector('[data-role="meta"]');
        if (meta) {
            const version = d.qualityLabel || qualityLabel(d.quality);
            const text = [version, d.note, d.status === 'queued' && d.queuePosition ? `#${d.queuePosition}` : '']
                .filter(Boolean).join(' · ');
            if (meta.textContent !== text) meta.textContent = text;
        }
    }
    if (els.downloadsSummary) els.downloadsSummary.textContent = downloadsSummaryText();
}

function renderDownloads(force = false) {
    const info = storageInfo();
    els.storage.textContent = info
        ? `Device storage: ${fmtBytes(info.usage)} used · ${fmtBytes(info.quota)} available to this app`
        : '';
    if (els.downloadsSummary) els.downloadsSummary.textContent = downloadsSummaryText();
    if (!force && els.downloadsList.dataset.signature === downloadsSignature()
            && els.downloadsList.childElementCount) {
        updateDownloadsProgress();
        return;
    }
    if (!state.downloads.length) {
        els.downloadsList.dataset.signature = '';
        els.downloadsList.innerHTML = '<div class="vd-empty"><b>Nothing downloaded</b>Open a movie and choose Download to keep it on this device. You can queue as many as you like — the NAS prepares them one by one.</div>';
        return;
    }
    const active = state.downloads.filter((d) => ['queued', 'rendering', 'downloading'].includes(d.status));
    const ready = state.downloads.filter((d) => d.status === 'ready');
    const done = state.downloads.filter((d) => d.status === 'done');
    const failed = state.downloads.filter((d) => d.status === 'failed');
    const waiting = state.downloads.filter((d) => d.status === 'waiting');
    const localIds = new Set(state.downloads.map((d) => d.id));
    const nasReady = (state.exports || []).filter((job) => !localIds.has(job.id));
    const resumable = failed.length + waiting.length;
    const section = (title, rows, hint = '') => (rows.length
        ? `<section class="vd-dl-group"><h3>${esc(title)}</h3>${hint ? `<p class="vd-dl-hint">${esc(hint)}</p>` : ''}
           ${rows.map((d) => downloadRowHtml(d)).join('')}</section>`
        : '');
    const batchErrors = state.batchErrors.length
        ? `<section class="vd-dl-group"><h3>Not queued</h3>
            ${state.batchErrors.map((entry) => `<div class="vd-dl"><div class="vd-dl-copy">
                <b>${esc(entry.title)}</b>
                <span class="vd-dl-stage" data-tone="bad">${esc(entry.reason)}</span></div></div>`).join('')}
           </section>`
        : '';
    els.downloadsList.innerHTML = [
        resumable ? `<div class="vd-dl-toolbar"><button class="vd-btn primary" type="button"
            data-action="resume-all" data-testid="vd-resume-all">↻ Wznów wszystkie (${resumable})</button></div>` : '',
        section('Preparing', active, 'The NAS renders the next film while the previous one saves.'),
        section('Ready to save', ready),
        section('Waiting for the NAS', waiting, 'Automatic retries in progress — this button skips the wait.'),
        nasReady.length ? `<section class="vd-dl-group"><h3>Ready on the NAS</h3>
            <p class="vd-dl-hint">Kept for a week — save again without re-encoding.</p>
            ${nasReady.map((job) => nasRowHtml(job)).join('')}</section>` : '',
        section('On this device', done),
        section('Failed', failed),
        batchErrors,
    ].join('');
    els.downloadsList.dataset.signature = downloadsSignature();
}

async function syncDownloadState() {
    if (!authed()) return;
    try {
        const list = await syncDownloads();
        state.downloads = list;
        state.exports = serverExports();
        updateDownloadsBadge();
        if (!els.downloads.hidden) renderDownloads();
        pumpDownloads();
    } catch (error) {
        logWarn('Download sync failed', String(error));
    }
}

function startDownloadPolling() {
    if (state.downloadTimer) return;
    state.downloadTimer = setInterval(() => {
        const busy = state.downloads.some((d) => ['queued', 'rendering', 'ready', 'downloading'].includes(d.status));
        if (!busy && els.downloads.hidden) return;
        if (document.visibilityState === 'hidden' && els.downloads.hidden) return;
        syncDownloadState();
    }, 2000);
}

async function queueOne(item, quality) {
    try {
        const need = estimateSize(item, quality || state.downloadQuality);
        if (!(await quotaAllows(need))) return;
        await queueDownload(item, quality || undefined);
        state.downloads = downloadsSnapshot();
        updateDownloadsBadge();
        renderDownloads();
        render();
        toast(`${displayTitle(item)} queued`, 'success');
    } catch (error) {
        toast(`Download failed: ${apiError(error)}`, 'error');
        renderDownloads();
    }
}

async function removeDownload(path, id) {
    const entry = (id && state.downloads.find((d) => d.id === id)) || downloadFor(path);
    if (!entry) return;
    if (entry.status === 'done') await deleteDownload(entry.id);
    // Anything still active/ready also has a NAS job — deleting only the local
    // row would let the next sync bring it straight back.
    else await cancelDownload(entry.id);
    state.downloads = downloadsSnapshot();
    updateDownloadsBadge();
    renderDownloads(true);
    render();
    toast('Download deleted', 'success');
}

async function cancelOne(id) {
    await cancelDownload(id);
    state.downloads = downloadsSnapshot();
    updateDownloadsBadge();
    renderDownloads();
    render();
}

async function retryOne(id) {
    try {
        await retryDownload(id);
        state.downloads = downloadsSnapshot();
        updateDownloadsBadge();
        renderDownloads();
        toast('Download queued again', 'success');
    } catch (error) {
        toast(`Retry failed: ${apiError(error)}`, 'error');
    }
}

let _resumingAll = false;

async function adoptNasExport(id) {
    const job = (state.exports || []).find((entry) => entry.id === id);
    if (!job) return;
    try {
        await adoptExport(job);
        state.downloads = downloadsSnapshot();
        state.exports = serverExports().filter((entry) => entry.id !== id);
        updateDownloadsBadge();
        renderDownloads(true);
        render();
        toast('Saving the copy from the NAS', 'success');
    } catch (error) {
        toast(`Could not save the NAS copy: ${apiError(error)}`, 'error');
    }
}

async function deleteNasExport(id) {
    try {
        await apiFetch(`/api/videos/exports/${encodeURIComponent(id)}`, { method: 'DELETE' });
        await syncDownloadState();
        toast('Deleted from the NAS', 'success');
    } catch (error) {
        toast(`Could not delete the NAS copy: ${apiError(error)}`, 'error');
    }
}

/** One button for the aftermath of a NAS restart / an update from an older PWA. */
async function resumeAll() {
    if (_resumingAll) return;
    const rows = downloadsSnapshot().filter((d) => ['failed', 'waiting'].includes(d.status));
    if (!rows.length) {
        pumpDownloads();
        toast('Every download is already queued', 'info');
        return;
    }
    _resumingAll = true;
    const task = startTask({
        appId: 'videos',
        title: 'Resuming downloads',
        total: rows.length,
        detail: `0 of ${rows.length}`,
        testIdPrefix: 'vd-resume',
    });
    let queued = 0;
    for (let index = 0; index < rows.length; index += 1) {
        try {
            await retryDownload(rows[index].id);
            queued += 1;
        } catch (error) {
            logWarn('Could not resume a download', `${rows[index].id}: ${apiError(error)}`);
        }
        task.update({
            percent: Math.round(((index + 1) / rows.length) * 100),
            detail: `${index + 1} of ${rows.length} · ${queued} queued`,
        });
    }
    task.finish('done', { detail: `${queued} queued` });
    state.downloads = downloadsSnapshot();
    updateDownloadsBadge();
    renderDownloads(true);
    render();
    pumpDownloads();
    toast(`${queued} download${queued === 1 ? '' : 's'} queued`, 'success');
    _resumingAll = false;
}

// ── settings ──────────────────────────────────────────────────────────────

async function loadDownloadSettings() {
    try {
        const data = await apiJson('/api/videos/download-settings');
        if (data?.quality) state.downloadQuality = data.quality;
        if (Array.isArray(data?.qualities) && data.qualities.length) state.downloadQualities = data.qualities;
        if (els.selectBar) paintSelection();
        renderSettings();
    } catch (error) {
        // Older core / offline: keep the local default.
        logWarn('Download settings load failed', String(error));
    }
}

function renderSettings() {
    if (!els.qualityOptions) return;
    const choices = state.downloadQualities.length ? state.downloadQualities : [
        { value: 'auto', label: 'Auto · fastest' },
        { value: 'original', label: 'Original · no re-encode' },
        { value: 'balanced', label: '1080p' },
        { value: 'fast', label: '720p · small' },
        { value: 'tiny', label: '480p · tiny' },
    ];
    els.qualityOptions.innerHTML = choices.map((q) => `<label class="vd-choice">
        <input type="radio" name="vd-download-quality" value="${esc(q.value)}"
               ${state.downloadQuality === q.value ? 'checked' : ''}>
        <span>${esc(q.label)}</span>
    </label>`).join('');
}

async function openSettings() {
    els.menu.hidden = true;
    els.settings.hidden = false;
    renderSettings();
    await loadDownloadSettings();
}

async function saveDownloadQuality(value) {
    try {
        const data = await apiPost('/api/videos/download-settings', { quality: value });
        state.downloadQuality = data.quality || value;
        state.downloadQualities = Array.isArray(data.qualities) ? data.qualities : state.downloadQualities;
        renderSettings();
        if (els.selectBar) paintSelection();
        toast('Default download version saved', 'success');
    } catch (error) {
        toast(`Could not save: ${apiError(error)}`, 'error');
    }
}

// ── player ────────────────────────────────────────────────────────────────

function loadHls() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (state.hlsPromise) return state.hlsPromise;
    state.hlsPromise = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = HLS_SCRIPT;
        script.onload = () => (window.Hls ? resolve(window.Hls) : reject(new Error('hls.js did not load')));
        script.onerror = () => reject(new Error('Could not load the HLS player'));
        document.head.appendChild(script);
    });
    return state.hlsPromise;
}

function audioCodec() {
    if (state.acodec) return state.acodec;
    const hasMse = typeof window.MediaSource !== 'undefined';
    if (!hasMse) { state.acodec = 'aac'; return state.acodec; }
    const probe = (type) => { try { return !!window.MediaSource.isTypeSupported(type); } catch { return false; } };
    state.acodec = probe('audio/mp4; codecs="mp4a.40.2"') ? 'aac'
        : probe('audio/mp4; codecs="opus"') ? 'opus' : 'none';
    return state.acodec;
}

function acquireWakeLock(player) {
    if (!('wakeLock' in navigator) || document.visibilityState !== 'visible') return;
    navigator.wakeLock.request('screen').then((lock) => {
        if (state.player !== player) { lock.release().catch(() => {}); return; }
        player.wakeLock = lock;
        lock.addEventListener('release', () => {
            if (state.player === player && document.visibilityState === 'visible' && !player.video.paused) {
                acquireWakeLock(player);
            }
        });
    }).catch(() => { /* not allowed (insecure context / battery saver) */ });
}

function releaseWakeLock(player) {
    if (player?.wakeLock) { try { player.wakeLock.release(); } catch { /* ignore */ } player.wakeLock = null; }
}

function setupMediaSession(player) {
    if (!('mediaSession' in navigator)) return;
    try {
        navigator.mediaSession.metadata = new MediaMetadata({
            title: displayTitle(player.item),
            artist: player.item.show || 'EthOS Videos',
            artwork: [{ src: artUrl(player.item), sizes: '342x513', type: 'image/jpeg' }],
        });
        navigator.mediaSession.setActionHandler('play', () => player.video.play().catch(() => {}));
        navigator.mediaSession.setActionHandler('pause', () => player.video.pause());
        navigator.mediaSession.setActionHandler('seekbackward', () => seekBy(player, -10));
        navigator.mediaSession.setActionHandler('seekforward', () => seekBy(player, 10));
        navigator.mediaSession.setActionHandler('seekto', (details) => {
            if (typeof details.seekTime === 'number') seekTo(player, details.seekTime);
        });
    } catch { /* unsupported */ }
}

function updatePositionState(player) {
    if (!('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
    const duration = player.duration || player.video.duration;
    if (!duration || !Number.isFinite(duration)) return;
    try {
        navigator.mediaSession.setPositionState({
            duration,
            position: Math.min(duration, Math.max(0, player.video.currentTime % duration || 0)),
            playbackRate: player.video.playbackRate || 1,
        });
    } catch { /* ignore */ }
}

async function openPlayer(item, { local = false } = {}) {
    // Await the previous HLS stop (bounded): the server allows one session per
    // user, so a start racing the stop of the old movie answered 429 and fell
    // back to the legacy stream with a misleading warning.
    await Promise.race([
        stopPlayer({ quiet: true }),
        new Promise((resolve) => setTimeout(resolve, 1500)),
    ]);
    els.player.hidden = false;
    els.playerTitle.textContent = displayTitle(item);
    els.playerMode.hidden = true;
    els.playerEnc.hidden = true;
    els.playerError.hidden = true;
    els.upnext.hidden = true;
    els.sub.hidden = true;
    els.sub.innerHTML = '';
    els.quality.value = localStorage.getItem('vd-quality') || 'balanced';
    els.speed.value = localStorage.getItem('vd-speed') || '1';
    els.video.playbackRate = Number(els.speed.value) || 1;
    if (els.cast) els.cast.hidden = !('remote' in els.video);

    const player = {
        item, local, video: els.video, hls: null, session: null, base: 0,
        live: false, acodec: audioCodec(), lastSave: 0, wakeLock: null,
        retried: false, hideTimer: null, error: '', nextTimer: null,
        next: nextEpisodeOf(item), subtitleUrl: '',
    };
    state.player = player;
    bindPlayerEvents(player);
    acquireWakeLock(player);
    setupMediaSession(player);

    if (local) {
        const entry = downloadFor(item.path);
        const url = entry ? localMediaUrl(entry.id) : '';
        if (!url) { showPlayerError(player, 'This download is no longer on the device.'); return; }
        player.live = false;
        player.duration = entry.size ? 0 : (item.duration || 0);
        els.video.src = url;
        els.quality.hidden = true;
        els.audio.hidden = true;
        els.video.play().catch(() => {});
        els.playerMode.hidden = false;
        els.playerMode.textContent = `Offline · ${entry.qualityLabel || qualityLabel(entry.quality)}`;
        restoreResume(player);
        setupSubtitles(player);
        return;
    }

    els.quality.hidden = false;
    els.quality.disabled = false;
    els.audio.hidden = false;
    try {
        const [infoData, resumeData] = await Promise.all([
            apiJson(`/api/videos/info?path=${encodeURIComponent(item.path)}`).catch(() => ({ info: {} })),
            apiJson(`/api/videos/resume?path=${encodeURIComponent(item.path)}`).catch(() => ({ resume: null })),
        ]);
        if (state.player !== player) return;
        const info = infoData.info || {};
        player.info = info;
        player.duration = Number(info.duration) || 0;
        player.audioTracks = info.audio_tracks || [];
        player.audioIdx = defaultAudioIdx(player.audioTracks);
        renderAudioSelect(player);
        const resume = resumeData.resume;
        const resumeTime = resume && resume.time > 5 && (!resume.duration || resume.time < resume.duration * 0.97)
            ? Number(resume.time) : localStartFor(item);
        player.base = resumeTime;
        let mode = info.playback_mode || (info.needs_transcode ? 'transcode' : 'direct');
        if (mode === 'direct' && player.audioTracks.length > 1) mode = 'remux';
        player.mode = mode;
        if (mode !== 'direct') {
            await startHls(player, resumeTime, player.audioIdx);
        } else {
            els.video.src = withToken(`/api/videos/stream?path=${encodeURIComponent(item.path)}`);
            seekWhenReady(resumeTime);
            els.video.play().catch(() => {});
            els.playerMode.hidden = false;
            els.playerMode.textContent = 'Direct';
        }
        setupSubtitles(player);
        if (resumeTime > 5) toast(`Resumed at ${fmtTime(resumeTime)}`);
    } catch (error) {
        showPlayerError(player, apiError(error));
    }
}

function localStartFor(item) {
    const resume = resumeFor(item);
    return resume && resume.time > 5 && (!resume.duration || resume.time < resume.duration * 0.97)
        ? Number(resume.time) : 0;
}

// ── subtitles ─────────────────────────────────────────────────────────────

const _subCache = new Map();

function clearSubtitleTrack(player = state.player) {
    if (player?.subtitleUrl) {
        try { URL.revokeObjectURL(player.subtitleUrl); } catch { /* ignore */ }
        player.subtitleUrl = '';
    }
    els.video.querySelectorAll('track').forEach((track) => track.remove());
}

async function subtitleText(sub) {
    if (sub.text) return sub.text;
    if (_subCache.has(sub.path)) return _subCache.get(sub.path);
    const res = await apiFetch(withToken(`/api/files/raw?path=${encodeURIComponent(sub.path)}`));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    let text = await res.text();
    if (!String(sub.name || '').toLowerCase().endsWith('.vtt')) text = srtToVtt(text);
    _subCache.set(sub.path, text);
    return text;
}

async function setupSubtitles(player) {
    clearSubtitleTrack(player);
    els.sub.hidden = true;
    els.sub.innerHTML = '';
    let subs = [];
    if (player.local) {
        const entry = downloadFor(player.item.path);
        const text = localSubtitle(entry);
        if (text) subs = [{ name: entry?.subtitle?.name || 'Subtitle', text }];
    } else {
        try {
            const data = await apiJson(`/api/videos/subtitles?path=${encodeURIComponent(player.item.path)}`);
            subs = (data.subtitles || []).map((sub) => ({ name: sub.name, path: sub.path, preferred: !!sub.preferred }));
        } catch { subs = []; }
    }
    if (state.player !== player) return;
    player.subs = subs;
    if (!subs.length) return;
    const saved = localStorage.getItem('vd-sub') || '';
    const auto = state.config?.options?.auto_subtitles !== false;
    let index = -1;
    if (saved === 'off') index = -1;
    else if (saved && subs.some((sub) => sub.name === saved)) index = subs.findIndex((sub) => sub.name === saved);
    else if (auto) index = Math.max(0, subs.findIndex((sub) => sub.preferred));
    els.sub.hidden = false;
    els.sub.innerHTML = '<option value="-1">Subtitles off</option>' + subs.map((sub, i) =>
        `<option value="${i}" ${i === index ? 'selected' : ''}>${esc(sub.name)}</option>`).join('');
    if (index >= 0) await selectSubtitle(player, index);
}

async function selectSubtitle(player, index) {
    clearSubtitleTrack(player);
    if (index < 0 || !player.subs?.[index]) {
        try { localStorage.setItem('vd-sub', 'off'); } catch { /* ignore */ }
        return;
    }
    const sub = player.subs[index];
    try {
        const text = await subtitleText(sub);
        if (state.player !== player) return;
        const url = URL.createObjectURL(new Blob([text], { type: 'text/vtt' }));
        player.subtitleUrl = url;
        const track = document.createElement('track');
        track.kind = 'subtitles';
        track.label = sub.name;
        track.srclang = 'und';
        track.src = url;
        track.default = true;
        els.video.appendChild(track);
        try { track.track.mode = 'showing'; } catch { /* ignore */ }
        try { localStorage.setItem('vd-sub', sub.name); } catch { /* ignore */ }
    } catch (error) {
        logWarn('Subtitle failed', `${sub.name}: ${apiError(error)}`);
    }
}

// ── up next (series autoplay) ────────────────────────────────────────────

function clearUpNext(player) {
    if (player?.nextTimer) { clearInterval(player.nextTimer); player.nextTimer = null; }
    if (player) player.pendingNext = null;
    els.upnext.hidden = true;
}

function playNext(player) {
    const next = player?.pendingNext;
    if (!next) return;
    clearUpNext(player);
    const local = !!downloadFor(next.path);
    if (!local && !navigator.onLine) {
        toast('That episode is not downloaded — connect to stream it', 'warning');
        return;
    }
    openPlayer(next, { local });
}

function showUpNext(player, next) {
    if (state.player !== player || !next) return;
    clearUpNext(player);
    player.pendingNext = next;
    els.upnextText.textContent = `Up next · ${displayTitle(next)}`;
    els.upnext.hidden = false;
    if (state.config?.options?.autoplay_next === false) return;
    let left = 10;
    const tick = () => {
        if (state.player !== player || !player.pendingNext) return;
        if (left <= 0) { playNext(player); return; }
        els.upnextText.textContent = `Up next in ${left}s · ${displayTitle(next)}`;
        left -= 1;
    };
    player.nextTimer = setInterval(tick, 1000);
    tick();
}

function handleEnded(player) {
    if (player.local) saveLocalProgress(player, true);
    else saveResume(true, player);
    updatePlayIcon();
    if (player.next) showUpNext(player, player.next);
}

function defaultAudioIdx(tracks) {
    if (!tracks.length) return 0;
    const pref = (localStorage.getItem('vd-audio-lang') || '').toLowerCase();
    if (pref) {
        const found = tracks.findIndex((track) => (track.language || '').toLowerCase().startsWith(pref));
        if (found >= 0) return found;
    }
    const main = tracks.findIndex((track) => !track.role || track.role === 'main');
    return main >= 0 ? main : 0;
}

function renderAudioSelect(player) {
    const select = els.audio;
    if (!player.audioTracks || player.audioTracks.length < 2) { select.hidden = true; return; }
    select.hidden = false;
    select.innerHTML = player.audioTracks.map((track, index) => {
        const lang = track.language ? `${track.language.toUpperCase()} · ` : '';
        const codec = (track.codec || '').toUpperCase();
        const channels = track.channels === 6 ? ' 5.1' : track.channels === 8 ? ' 7.1' : '';
        return `<option value="${index}" ${index === player.audioIdx ? 'selected' : ''}>${esc(`${lang}${codec}${channels}`)}</option>`;
    }).join('');
}

async function startHls(player, start, audioIdx, { allowFallback = true } = {}) {
    player.seekReloading = true;
    try {
        const data = await apiPost('/api/videos/hls', {
            path: player.item.path,
            t: Math.max(0, start || 0),
            a: audioIdx || 0,
            q: els.quality.value,
            acodec: player.acodec,
        });
        if (state.player !== player) {
            apiFetch(`/api/videos/hls/${encodeURIComponent(data.session)}/stop`, { method: 'POST' }).catch(() => {});
            return;
        }
        player.session = data.session;
        player.mode = data.mode || player.mode;
        player.base = Math.max(0, Number(start) || 0);
        player.live = true;
        els.playerMode.hidden = false;
        els.playerMode.textContent = data.mode === 'remux' ? 'Remux' : 'Transcoding';
        if (data.encoder) {
            els.playerEnc.hidden = false;
            els.playerEnc.textContent = `${data.encoder === 'vaapi' ? 'VA-API' : data.encoder === 'qsv' ? 'QuickSync' : 'libx264'}${data.decode === 'hw' ? ' + HW' : ''}`;
        }
        if (data.acodec === 'none') toast('This browser cannot decode the audio — playing without sound', 'warning');
        const Hls = await loadHls();
        if (state.player !== player) return;
        if (player.hls) { try { player.hls.destroy(); } catch { /* ignore */ } }
        const hls = new Hls({ enableWorker: true, lowLatencyMode: true });
        player.hls = hls;
        hls.on(Hls.Events.ERROR, (_event, data2) => {
            if (!data2?.fatal) return;
            const detail = `${data2.type}/${data2.details}`;
            logError('HLS playback error', detail);
            player.error = detail;
            recoverPlayback(player);
        });
        hls.loadSource(withToken(data.playlist));
        hls.attachMedia(els.video);
        els.video.play().catch(() => {});
    } catch (error) {
        if (state.player !== player) return;
        player.error = apiError(error);
        logWarn('HLS start failed', player.error);
        if (!allowFallback) throw error;
        els.video.src = withToken(`/api/videos/transcode?path=${encodeURIComponent(player.item.path)}&q=${encodeURIComponent(els.quality.value)}&a=${player.audioIdx || 0}&acodec=${player.acodec}`);
        els.video.play().catch(() => {});
        toast('HLS unavailable — using the legacy stream', 'warning');
    } finally {
        player.seekReloading = false;
    }
}

async function stopHlsSession(player) {
    if (player.hls) { try { player.hls.destroy(); } catch { /* ignore */ } player.hls = null; }
    const sid = player.session;
    player.session = null;
    if (sid) await apiFetch(`/api/videos/hls/${encodeURIComponent(sid)}/stop`, { method: 'POST' }).catch(() => {});
}

async function recoverPlayback(player) {
    if (state.player !== player) return;
    await stopHlsSession(player);
    if (state.player !== player) return;
    if (!player.retried) {
        player.retried = true;
        toast('Retrying the stream…');
        try {
            await startHls(player, absTime(player), player.audioIdx, { allowFallback: false });
            return;
        } catch (error) {
            player.error = apiError(error);
            if (state.player !== player) return;
        }
    }
    showPlayerError(player, player.error || 'The stream could not be played.');
}

function showPlayerError(player, message) {
    if (state.player !== player) return;
    els.playerError.hidden = false;
    els.playerErrorText.textContent = message;
}

function absTime(player) {
    return (player.live ? player.base : 0) + (Number(els.video.currentTime) || 0);
}

function durationOf(player) {
    if (player.live) return player.duration || Number(player.info?.duration) || 0;
    return Number.isFinite(els.video.duration) ? els.video.duration : (player.duration || 0);
}

function seekTo(player, target) {
    const duration = durationOf(player);
    const clamped = duration ? Math.min(Math.max(0, target), duration - 0.5) : Math.max(0, target);
    if (!player.live) { try { els.video.currentTime = clamped; } catch { /* ignore */ } return; }
    const rel = clamped - player.base;
    try {
        const ranges = els.video.seekable;
        for (let i = 0; i < ranges.length; i += 1) {
            if (rel >= ranges.start(i) - 0.5 && rel <= ranges.end(i) + 0.5) {
                els.video.currentTime = rel;
                return;
            }
        }
    } catch { /* ignore */ }
    stopHlsSession(player).then(() => startHls(player, clamped, player.audioIdx)).catch(() => {});
}

function seekBy(player, delta) {
    seekTo(player, absTime(player) + delta);
}

function saveResume(force, player = state.player) {
    if (!player || player.local || !player.item) return;
    const now = Date.now();
    if (!force && now - (player.lastSave || 0) < RESUME_EVERY_MS) return;
    player.lastSave = now;
    const time = absTime(player);
    const duration = durationOf(player) || player.duration || 0;
    if (time < 5) return;
    apiFetch('/api/videos/resume', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: player.item.path, time, duration }),
    }).catch(() => {});
    if (duration && time < duration * 0.97) state.resume[player.item.path] = { time, duration };
}

function saveLocalProgress(player, force = false) {
    if (!player?.local || !player.item) return;
    const now = Date.now();
    if (!force && now - (player.lastSave || 0) < RESUME_EVERY_MS) return;
    player.lastSave = now;
    const time = absTime(player);
    const duration = durationOf(player) || player.duration || 0;
    if (time < 5) return;
    if (duration && time >= duration * 0.97) {
        state.localResume.delete(player.item.path);
        clearLocalResume(player.item.path);
        return;
    }
    state.localResume.set(player.item.path, {
        path: player.item.path, time, duration, updated_at: now,
    });
    saveLocalResume(player.item.path, time, duration);
}

function seekWhenReady(seconds) {
    if (!(seconds > 0)) return;
    const apply = () => { try { els.video.currentTime = seconds; } catch { /* ignore */ } };
    if (els.video.readyState >= 1) apply();
    else els.video.addEventListener('loadedmetadata', apply, { once: true });
}

function restoreResume(player) {
    seekWhenReady(localStartFor(player.item));
}

function stopPlayer({ quiet = false } = {}) {
    const player = state.player;
    if (!player) return Promise.resolve();
    state.player = null;
    if (!quiet) {
        if (player.local) saveLocalProgress(player, true);
        else saveResume(true, player);
    }
    clearUpNext(player);
    clearSubtitleTrack(player);
    try { els.video.pause(); } catch { /* ignore */ }
    if (player.hls) { try { player.hls.destroy(); } catch { /* ignore */ } }
    let stopped = Promise.resolve();
    if (player.session) {
        const sid = player.session;
        player.session = null;
        stopped = apiFetch(`/api/videos/hls/${encodeURIComponent(sid)}/stop`, { method: 'POST' })
            .catch(() => {});
    }
    releaseWakeLock(player);
    try { els.video.removeAttribute('src'); els.video.load(); } catch { /* ignore */ }
    if (player.hideTimer) clearTimeout(player.hideTimer);
    els.player.hidden = true;
    els.playerError.hidden = true;
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    return stopped;
}

function togglePlay(player = state.player) {
    if (!player) return;
    if (els.video.paused) els.video.play().catch(() => {});
    else els.video.pause();
}

function toggleFullscreen(player = state.player) {
    if (!player) return;
    const target = els.player;
    if (document.fullscreenElement) { document.exitFullscreen?.().catch(() => {}); return; }
    if (target.requestFullscreen) {
        target.requestFullscreen().then(() => {
            if (screen.orientation?.lock) screen.orientation.lock('landscape').catch(() => {});
        }).catch(() => {});
    } else if (els.video.webkitEnterFullscreen) {
        els.video.webkitEnterFullscreen();
    }
}

function updatePlayIcon() {
    const icon = els.video.paused ? '▶' : '❚❚';
    els.playPause.textContent = icon;
    els.bigPlay.textContent = icon;
    els.bigPlay.classList.toggle('hide', !els.video.paused);
}

function showUi(player) {
    els.playerUi.classList.remove('hidden');
    if (player.hideTimer) clearTimeout(player.hideTimer);
    if (!els.video.paused) {
        player.hideTimer = setTimeout(() => els.playerUi.classList.add('hidden'), 3200);
    }
}

function bindPlayerEvents(player) {
    const video = els.video;
    const onTime = () => {
        const absolute = absTime(player);
        const duration = durationOf(player);
        els.seek.max = String(Math.max(1, duration || 100));
        els.seek.value = String(Math.min(duration || 0, absolute));
        els.time.textContent = `${fmtTime(absolute)} / ${duration ? fmtTime(duration) : '--:--'}`;
        try {
            const b = video.buffered;
            if (b.length) {
                const end = b.end(b.length - 1) + (player.live ? player.base : 0);
                els.buffered.style.width = `${duration ? Math.min(100, (end / duration) * 100) : 0}%`;
            }
        } catch { /* ignore */ }
        if (player.local) saveLocalProgress(player);
        else saveResume(false, player);
        updatePositionState(player);
    };
    player.onTime = onTime;
    player.onPlay = () => { updatePlayIcon(); showUi(player); acquireWakeLock(player); };
    player.onPause = () => { updatePlayIcon(); showUi(player); };
    player.onError = () => {
        if (state.player !== player) return;
        const err = video.error;
        const detail = `code=${err ? err.code : '?'} message=${err ? err.message : ''} local=${player.local}`;
        logError('Video element error', detail);
        if (!player.seekReloading) showPlayerError(player, player.error || 'The stream could not be played.');
    };
    player.onEnded = () => {
        handleEnded(player);
    };
    video.addEventListener('timeupdate', onTime);
    video.addEventListener('loadedmetadata', onTime);
    video.addEventListener('play', player.onPlay);
    video.addEventListener('pause', player.onPause);
    video.addEventListener('error', player.onError);
    video.addEventListener('ended', player.onEnded);
}

function unbindPlayerEvents(player) {
    if (!player) return;
    const video = els.video;
    video.removeEventListener('timeupdate', player.onTime);
    video.removeEventListener('loadedmetadata', player.onTime);
    video.removeEventListener('play', player.onPlay);
    video.removeEventListener('pause', player.onPause);
    video.removeEventListener('error', player.onError);
    video.removeEventListener('ended', player.onEnded);
}

// ── events ────────────────────────────────────────────────────────────────

function bindEvents() {
    els.loginForm.addEventListener('submit', login);
    els.menuBtn.addEventListener('click', () => {
        els.menu.hidden = !els.menu.hidden;
        els.menuBtn.setAttribute('aria-expanded', String(!els.menu.hidden));
    });
    $('vd-theme').addEventListener('click', () => {
        const dark = !document.documentElement.classList.contains('dark');
        document.documentElement.className = dark ? 'dark' : 'light';
        try { localStorage.setItem('ethos-theme', dark ? 'dark' : 'light'); } catch { /* ignore */ }
        els.menu.hidden = true;
    });
    $('vd-logout').addEventListener('click', logout);
    $('vd-refresh').addEventListener('click', () => {
        els.menu.hidden = true;
        state.library = null;
        state.show = null;
        boot();
    });
    $('vd-settings-btn').addEventListener('click', openSettings);
    els.downloadsBtn.addEventListener('click', () => {
        state.downloads = downloadsSnapshot();
        renderDownloads();
        els.downloads.hidden = false;
        storeInfo().then(() => renderDownloads());
        syncDownloadState();
    });
    els.install.addEventListener('click', async () => {
        if (!state.installPrompt) return;
        state.installPrompt.prompt();
        try { await state.installPrompt.userChoice; } catch { /* ignore */ }
        state.installPrompt = null;
        els.install.hidden = true;
    });
    els.search.addEventListener('input', () => {
        state.query = els.search.value;
        state.show = null;
        clearTimeout(state.searchTimer);
        state.searchTimer = setTimeout(() => render(), 200);
    });
    els.search.addEventListener('change', () => {
        rememberSearch(state.query);
        render();
    });
    els.content.addEventListener('click', (event) => {
        const el = event.target.closest('[data-action]');
        if (!el) return;
        const action = el.dataset.action;
        if (action === 'tab') {
            state.tabs = el.dataset.tab || 'all';
            state.show = null;
            state.query = '';
            els.search.value = '';
            render();
            return;
        }
        if (action === 'show') {
            state.show = el.dataset.show || null;
            state.query = '';
            els.search.value = '';
            render();
            return;
        }
        if (action === 'back-home') { state.show = null; render(); return; }
        if (action === 'recent-search') {
            els.search.value = el.dataset.q || '';
            state.query = els.search.value;
            render();
            return;
        }
        if (action === 'download-season') {
            downloadSeason(el.dataset.show || '', Number(el.dataset.season) || 0);
            return;
        }
        const item = findItem(el.dataset.path);
        if (!item) return;
        if (action === 'play') { openPlayer(item); return; }
        if (action !== 'open') return;
        if (state.selectMode) {
            toggleSelect(item.path, el.closest('.vd-card'));
            return;
        }
        openSheet(item);
    });
    els.content.addEventListener('change', (event) => {
        const el = event.target.closest('[data-action]');
        if (!el) return;
        if (el.dataset.action === 'sort') { state.sort = el.value; render(); }
        else if (el.dataset.action === 'genre') { state.genre = el.value; render(); }
    });
    els.selectBtn.addEventListener('click', () => setSelectMode(!state.selectMode));
    els.selectCancel.addEventListener('click', () => setSelectMode(false));
    els.selectAll.addEventListener('click', selectAllVisible);
    els.selectDownload.addEventListener('click', downloadSelected);
    els.sheetBody.addEventListener('click', (event) => {
        const el = event.target.closest('[data-action]');
        if (!el) return;
        const action = el.dataset.action;
        if (action === 'delete-download') { removeDownload(el.dataset.path, el.dataset.id); return; }
        if (action === 'cancel-download') { cancelOne(el.dataset.id); return; }
        const item = findItem(el.dataset.path);
        if (!item) return;
        if (action === 'play') { closeSheets(); openPlayer(item); }
        else if (action === 'list') toggleList(item.path);
        else if (action === 'download') queueOne(item, state.downloadQuality);
        else if (action === 'download-q') queueOne(item, el.dataset.q || undefined);
        else if (action === 'redownload-q') redownload(item, el.dataset.q || state.downloadQuality);
        else if (action === 'remove-resume') removeResume(item);
        else if (action === 'show') {
            state.show = el.dataset.show || null;
            closeSheets();
            render();
        }
    });
    els.downloadsList.addEventListener('click', (event) => {
        const el = event.target.closest('[data-action]');
        if (!el) return;
        const action = el.dataset.action;
        if (action === 'delete-download') removeDownload(el.dataset.path, el.dataset.id);
        else if (action === 'cancel-download') cancelOne(el.dataset.id);
        else if (action === 'retry-download') retryOne(el.dataset.id);
        else if (action === 'resume-all') resumeAll();
        else if (action === 'adopt-export') adoptNasExport(el.dataset.id);
        else if (action === 'delete-export') deleteNasExport(el.dataset.id);
        else if (action === 'play-local') {
            const item = findItem(el.dataset.path);
            if (item) { closeSheets(); openPlayer(item, { local: true }); }
        }
    });
    if (els.qualityOptions) {
        els.qualityOptions.addEventListener('change', (event) => {
            const input = event.target.closest('input[name="vd-download-quality"]');
            if (input) saveDownloadQuality(input.value);
        });
    }
    document.querySelectorAll('[data-close]').forEach((el) => {
        el.addEventListener('click', closeSheets);
    });

    $('vd-player-back').addEventListener('click', () => stopPlayer());
    $('vd-player-close').addEventListener('click', () => stopPlayer());
    $('vd-player-retry').addEventListener('click', () => {
        const player = state.player;
        if (!player) return;
        els.playerError.hidden = true;
        player.retried = false;
        player.error = '';
        if (player.local) { openPlayer(player.item, { local: true }); return; }
        startHls(player, absTime(player), player.audioIdx);
    });
    els.playPause.addEventListener('click', () => togglePlay());
    els.bigPlay.addEventListener('click', () => togglePlay());
    $('vd-back10').addEventListener('click', () => seekBy(state.player, -10));
    $('vd-fwd10').addEventListener('click', () => seekBy(state.player, 10));
    $('vd-fullscreen').addEventListener('click', () => toggleFullscreen());
    els.seek.addEventListener('input', () => {
        const player = state.player;
        if (!player) return;
        els.time.textContent = `${fmtTime(els.seek.value)} / ${fmtTime(durationOf(player))}`;
    });
    els.seek.addEventListener('change', () => {
        const player = state.player;
        if (player) seekTo(player, Number(els.seek.value));
    });
    els.quality.addEventListener('change', () => {
        const player = state.player;
        if (!player || player.local) return;
        try { localStorage.setItem('vd-quality', els.quality.value); } catch { /* ignore */ }
        stopHlsSession(player).then(() => startHls(player, absTime(player), player.audioIdx));
    });
    els.audio.addEventListener('change', () => {
        const player = state.player;
        if (!player || player.local) return;
        player.audioIdx = Number(els.audio.value) || 0;
        stopHlsSession(player).then(() => startHls(player, absTime(player), player.audioIdx));
    });
    els.sub.addEventListener('change', () => {
        const player = state.player;
        if (player) selectSubtitle(player, Number(els.sub.value));
    });
    els.speed.addEventListener('change', () => {
        const rate = Number(els.speed.value) || 1;
        els.video.playbackRate = rate;
        try { localStorage.setItem('vd-speed', String(rate)); } catch { /* ignore */ }
    });
    if (els.cast) {
        els.cast.addEventListener('click', () => {
            if (els.video.remote) els.video.remote.prompt().catch(() => toast('Casting is not available', 'warning'));
        });
    }
    els.upnextPlay.addEventListener('click', () => playNext(state.player));
    els.upnextCancel.addEventListener('click', () => clearUpNext(state.player));
    ['mousemove', 'touchstart', 'click'].forEach((type) => {
        els.player.addEventListener(type, () => { if (state.player) showUi(state.player); }, { passive: true });
    });
    document.addEventListener('visibilitychange', () => {
        const player = state.player;
        if (player && document.visibilityState === 'visible' && !els.video.paused) acquireWakeLock(player);
        if (document.visibilityState === 'visible') {
            resumeServerErrors();
            syncDownloadState();
        }
    });
    window.addEventListener('pagehide', () => {
        const player = state.player;
        if (!player) return;
        if (player.local) saveLocalProgress(player, true);
        else saveResume(true, player);
        if (player.session) {
            try {
                fetch(`/api/videos/hls/${encodeURIComponent(player.session)}/stop`, {
                    method: 'POST', keepalive: true,
                    headers: token() ? { Authorization: `Bearer ${token()}` } : {},
                }).catch(() => {});
            } catch { /* ignore */ }
        }
    });
    document.addEventListener('keydown', (event) => {
        if (!state.player) return;
        const tag = (event.target?.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'select' || tag === 'textarea') return;
        if (event.key === ' ' || event.key.toLowerCase() === 'k') { event.preventDefault(); togglePlay(); }
        else if (event.key === 'ArrowLeft') { event.preventDefault(); seekBy(state.player, -10); }
        else if (event.key === 'ArrowRight') { event.preventDefault(); seekBy(state.player, 10); }
        else if (event.key.toLowerCase() === 'f') toggleFullscreen();
        else if (event.key === 'Escape' && !document.fullscreenElement) stopPlayer();
    });

    window.addEventListener('online', () => {
        setOnline(true);
        // Coming back from an offline-only library, re-read the real one.
        if (state.localOnly) { state.localOnly = false; boot(); return; }
        resumeServerErrors();
        syncResume();
        flushLocalResumes();
    });
    window.addEventListener('offline', () => setOnline(false));
    window.addEventListener('beforeinstallprompt', (event) => {
        event.preventDefault();
        state.installPrompt = event;
        els.install.hidden = false;
    });
}

function setOnline(online) {
    state.offline = !online;
    els.net.hidden = online;
    if (!online) els.net.textContent = state.offlineUnlocked ? 'Offline · downloads' : 'Offline';
}

async function syncResume() {
    if (!authed()) return;
    try {
        const data = await apiJson('/api/videos/resume/all').catch(() => null);
        if (data?.resume) { state.resume = data.resume; render(); }
    } catch { /* ignore */ }
    flushLocalResumes();
}

async function storeInfo() {
    try {
        await refreshStorage();
        renderDownloads();
    } catch { /* ignore */ }
}

// ── boot ──────────────────────────────────────────────────────────────────

function registerWorker() {
    // The desktop shell registers the same root-scoped worker, but a phone that
    // only ever opens /videos must install it itself — without it downloads
    // cannot be stored in Cache Storage and the install prompt never fires.
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
    navigator.serviceWorker.register('/sw.js').then((registration) => {
        logInfo('Service worker ready', registration.scope);
    }).catch((error) => {
        logWarn('Service worker registration failed', String(error));
    });
}

async function main() {
    bindEvents();
    setOnline(navigator.onLine);
    registerWorker();
    state.recentSearches = loadRecentSearches();
    await loadLocalResumes();
    // Loaded before boot so an offline start still has the downloaded items.
    try {
        await initOffline((list) => {
            state.downloads = list;
            updateDownloadsBadge();
            if (!els.downloads.hidden) renderDownloads();
            render();
        });
    } catch (error) {
        logWarn('Offline store unavailable', String(error)); // private mode / no IDB
    }
    state.downloads = downloadsSnapshot();
    updateDownloadsBadge();

    if (!authed()) {
        // A trip can outlast the JWT. If the device already holds downloads and
        // there is no network, open in a playback-only mode instead of a login
        // wall; every server call still needs a fresh token.
        const done = state.downloads.filter((entry) => entry.status === 'done');
        if (!navigator.onLine && done.length) {
            state.offlineUnlocked = true;
            state.localOnly = true;
            state.library = { videos: done.map((entry) => entry.item || { path: entry.path, title: entry.title }) };
            showApp();
            setOnline(false);
            if (els.net) els.net.textContent = 'Offline · downloads';
            render();
            startDownloadPolling();
            return;
        }
        showLogin();
        return;
    }
    showApp();
    await boot();
    state.downloads = downloadsSnapshot();
    updateDownloadsBadge();
    await syncDownloadState();
    startDownloadPolling();
    if (new URLSearchParams(location.search).get('view') === 'downloads') {
        renderDownloads();
        els.downloads.hidden = false;
    }
}

main().catch((error) => {
    logError('Videos PWA failed to start', String(error));
    showLogin('Something went wrong while starting — sign in again.');
});
