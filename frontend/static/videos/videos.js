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
import '../core/error-reporter.js';
import {
    initOffline, downloadsSnapshot, queueDownload, pumpDownloads, syncDownloads,
    cancelDownload, retryDownload, deleteDownload, localMediaUrl, storageInfo,
    refreshStorage,
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
    downloads: [],
    downloadQuality: 'balanced',
    downloadQualities: [],
    downloadTimer: null,
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
    audio: $('vd-audio'),
    quality: $('vd-quality'),
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
    return value === 'original' ? 'Original' : value === 'fast' ? '720p' : '1080p';
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
    const resume = state.resume[item.path];
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

function shelfHtml(title, items, { row = true } = {}) {
    if (!items || !items.length) return '';
    return `<section class="vd-shelf">
        <div class="vd-shelf-head"><h2>${esc(title)}</h2><span>${items.length}</span></div>
        <div class="${row ? 'vd-row' : 'vd-grid'}">${items.map((item) => cardHtml(item)).join('')}</div>
    </section>`;
}

function render() {
    const q = state.query.trim().toLowerCase();
    if (q) {
        const hits = videos().filter((item) => searchText(item).includes(q)).slice(0, 120);
        els.content.innerHTML = hits.length
            ? `<section class="vd-shelf"><div class="vd-shelf-head"><h2>Results</h2><span>${hits.length}</span></div>
               <div class="vd-grid">${hits.map((item) => cardHtml(item)).join('')}</div></section>`
            : `<div class="vd-empty"><b>No matches</b>Nothing in the library matches “${esc(state.query)}”.</div>`;
        return;
    }
    if (!state.library) { renderLoading('Reading your library…'); return; }

    const cw = (state.home.continue_watching || []).filter((item) => item?.path);
    const recent = (state.home.recently_added || []).slice(0, 18);
    const all = videos().filter((item) => item.kind !== 'episode');
    const offline = state.downloads.filter((d) => d.status === 'done');

    const pieces = [];
    if (state.offline && offline.length) {
        const items = offline.map((d) => videos().find((v) => v.path === d.path) || d.item).filter(Boolean);
        pieces.push(shelfHtml('On this device', items.length ? items : offline.map((d) => d.item || { path: d.path, title: d.title })));
    }
    pieces.push(shelfHtml('Continue watching', cw));
    pieces.push(shelfHtml('Recently added', recent));
    pieces.push(shelfHtml('All videos', all.slice(0, 300), { row: false }));
    if (!all.length && !cw.length) {
        pieces.push(`<div class="vd-empty"><b>No videos yet</b>Add a folder in the desktop Videos app (Settings → Library).</div>`);
    }
    els.content.innerHTML = pieces.join('');
    updateDownloadsBadge();
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
    const inList = state.list.has(item.path);
    const meta = [
        tm.rating != null ? `★ ${tm.rating}` : '',
        tm.year || '',
        item.quality || '',
        (item.video_codec || '').toUpperCase(),
        (item.audio_codec || '').toUpperCase(),
    ].filter(Boolean);
    const downloadBlock = dm
        ? `<button class="vd-btn" type="button" data-action="delete-download" data-path="${esc(item.path)}" data-id="${esc(dm.id)}">Delete download</button>`
        : pending
            ? `<button class="vd-btn" type="button" data-action="cancel-download" data-id="${esc(pending.id)}">Cancel · ${esc(stageText(pending))}</button>`
            : `<button class="vd-btn primary" type="button" data-action="download" data-path="${esc(item.path)}">⬇ Download · ${esc(qualityLabel(state.downloadQuality))}</button>`;
    const versionRow = dm || pending ? '' : `<div class="vd-quality-row" aria-label="Download version">
            ${(state.downloadQualities.length ? state.downloadQualities : [
                { value: 'original', label: 'Original · no re-encode' },
                { value: 'balanced', label: '1080p' },
                { value: 'fast', label: '720p · small' },
            ]).map((q) => `<button class="vd-btn small" type="button" data-action="download-q"
                    data-q="${esc(q.value)}" data-path="${esc(item.path)}">${esc(q.label)}</button>`).join('')}
        </div>`;
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
            <button class="vd-btn primary" type="button" data-action="play" data-path="${esc(item.path)}">▶ Play</button>
            <button class="vd-btn" type="button" data-action="list" data-path="${esc(item.path)}">${inList ? '✓ My List' : '+ My List'}</button>
            ${downloadBlock}
        </div>
        <p class="vd-hint">Downloads are prepared by the NAS (H.264 + AAC) and stored on this device. “Original” copies the video without re-encoding when the file allows it.</p>
        ${versionRow}
    </div>`;
    els.sheet.hidden = false;
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

function stageText(d) {
    const progress = d.bytesTotal
        ? Math.min(100, (d.bytesDone / d.bytesTotal) * 100)
        : (Number(d.percent) || 0);
    if (d.status === 'queued') {
        return Number(d.queuePosition) > 1 ? `In the queue · #${d.queuePosition}` : 'In the queue';
    }
    if (d.status === 'rendering') return `Preparing on the NAS… ${Math.round(Number(d.percent) || 0)}%`;
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

function downloadRowHtml(d) {
    const item = findItem(d.path) || d.item || { path: d.path, title: d.title };
    const progress = d.bytesTotal
        ? Math.min(100, (d.bytesDone / d.bytesTotal) * 100)
        : (Number(d.percent) || 0);
    const active = ['queued', 'rendering', 'ready', 'downloading'].includes(d.status);
    const version = d.qualityLabel || qualityLabel(d.quality);
    const meta = [version, d.note, d.status === 'queued' && d.queuePosition ? `#${d.queuePosition}` : '']
        .filter(Boolean).join(' · ');
    return `<div class="vd-dl" data-testid="vd-download-row">
        <div class="vd-dl-art"><img src="${esc(d.posterUrl || artUrl(item))}" alt="" loading="lazy"></div>
        <div class="vd-dl-copy">
            <b>${esc(displayTitle(item))}</b>
            <span class="vd-dl-stage" data-tone="${esc(stageTone(d))}">${esc(stageText(d))}</span>
            ${meta ? `<span class="vd-dl-meta">${esc(meta)}</span>` : ''}
            ${active ? `<div class="vd-dl-bar"><i style="width:${progress.toFixed(1)}%"></i></div>` : ''}
        </div>
        <div class="vd-dl-actions">
            ${d.status === 'done' ? `<button class="vd-icon-btn" type="button" data-action="play-local" data-path="${esc(d.path)}" aria-label="Play offline">▶</button>` : ''}
            ${d.status === 'failed' ? `<button class="vd-icon-btn" type="button" data-action="retry-download" data-id="${esc(d.id)}" aria-label="Retry">↻</button>` : ''}
            ${active ? `<button class="vd-icon-btn" type="button" data-action="cancel-download" data-id="${esc(d.id)}" aria-label="Cancel">✕</button>` : ''}
            <button class="vd-icon-btn" type="button" data-action="delete-download" data-path="${esc(d.path)}" data-id="${esc(d.id)}" aria-label="Delete">🗑</button>
        </div>
    </div>`;
}

function renderDownloads() {
    const info = storageInfo();
    els.storage.textContent = info
        ? `Device storage: ${fmtBytes(info.usage)} used · ${fmtBytes(info.quota)} available to this app`
        : '';
    const groups = {
        active: state.downloads.filter((d) => ['queued', 'rendering', 'downloading'].includes(d.status)),
        ready: state.downloads.filter((d) => d.status === 'ready'),
        done: state.downloads.filter((d) => d.status === 'done'),
        failed: state.downloads.filter((d) => d.status === 'failed'),
    };
    if (els.downloadsSummary) {
        const bits = [
            groups.active.length ? `${groups.active.length} downloading` : '',
            groups.ready.length ? `${groups.ready.length} ready to save` : '',
            groups.done.length ? `${groups.done.length} on this device` : '',
        ].filter(Boolean);
        els.downloadsSummary.textContent = bits.join(' · ') || 'Nothing queued';
    }
    if (!state.downloads.length) {
        els.downloadsList.innerHTML = '<div class="vd-empty"><b>Nothing downloaded</b>Open a movie and choose Download to keep it on this device. You can queue as many as you like — the NAS prepares them one by one.</div>';
        return;
    }
    const section = (title, rows, hint = '') => (rows.length
        ? `<section class="vd-dl-group"><h3>${esc(title)}</h3>${hint ? `<p class="vd-dl-hint">${esc(hint)}</p>` : ''}
           ${rows.map((d) => downloadRowHtml(d)).join('')}</section>`
        : '');
    els.downloadsList.innerHTML = [
        section('Preparing', groups.active, 'The NAS renders the next film while the previous one saves.'),
        section('Ready to save', groups.ready),
        section('On this device', groups.done),
        section('Failed', groups.failed),
    ].join('');
}

async function syncDownloadState() {
    if (!authed()) return;
    try {
        const list = await syncDownloads();
        state.downloads = list;
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
    await deleteDownload(entry.id);
    state.downloads = downloadsSnapshot();
    updateDownloadsBadge();
    renderDownloads();
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

// ── settings ──────────────────────────────────────────────────────────────

function renderSettings() {
    if (!els.qualityOptions) return;
    const choices = state.downloadQualities.length ? state.downloadQualities : [
        { value: 'original', label: 'Original · no re-encode' },
        { value: 'balanced', label: '1080p' },
        { value: 'fast', label: '720p · small' },
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
    try {
        const data = await apiJson('/api/videos/download-settings');
        state.downloadQuality = data.quality || 'balanced';
        state.downloadQualities = Array.isArray(data.qualities) ? data.qualities : [];
        renderSettings();
    } catch (error) {
        toast(`Settings unavailable: ${apiError(error)}`, 'error');
    }
}

async function saveDownloadQuality(value) {
    try {
        const data = await apiPost('/api/videos/download-settings', { quality: value });
        state.downloadQuality = data.quality || value;
        state.downloadQualities = Array.isArray(data.qualities) ? data.qualities : state.downloadQualities;
        renderSettings();
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
    stopPlayer({ quiet: true });
    els.player.hidden = false;
    els.playerTitle.textContent = displayTitle(item);
    els.playerMode.hidden = true;
    els.playerEnc.hidden = true;
    els.playerError.hidden = true;
    els.quality.value = localStorage.getItem('vd-quality') || 'balanced';

    const player = {
        item, local, video: els.video, hls: null, session: null, base: 0,
        live: false, acodec: audioCodec(), lastSave: 0, wakeLock: null,
        retried: false, hideTimer: null, error: '',
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
        player.duration = item.duration || 0;
        els.video.src = url;
        els.quality.disabled = true;
        els.video.play().catch(() => {});
        els.playerMode.hidden = false;
        els.playerMode.textContent = 'Offline';
        restoreResume(player);
        return;
    }

    els.quality.disabled = false;
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
            ? Number(resume.time) : 0;
        player.base = resumeTime;
        let mode = info.playback_mode || (info.needs_transcode ? 'transcode' : 'direct');
        if (mode === 'direct' && player.audioTracks.length > 1) mode = 'remux';
        player.mode = mode;
        if (mode !== 'direct') {
            await startHls(player, resumeTime, player.audioIdx);
        } else {
            els.video.src = withToken(`/api/videos/stream?path=${encodeURIComponent(item.path)}`);
            if (resumeTime > 0) {
                els.video.addEventListener('loadedmetadata', () => {
                    try { els.video.currentTime = resumeTime; } catch { /* ignore */ }
                }, { once: true });
            }
            els.video.play().catch(() => {});
            els.playerMode.hidden = false;
            els.playerMode.textContent = 'Direct';
        }
        if (resumeTime > 5) toast(`Resumed at ${fmtTime(resumeTime)}`);
    } catch (error) {
        showPlayerError(player, apiError(error));
    }
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

function restoreResume(player) {
    const resume = state.resume[player.item.path];
    const start = resume && resume.time > 5 ? Number(resume.time) : 0;
    if (start > 0) {
        els.video.addEventListener('loadedmetadata', () => {
            try { els.video.currentTime = start; } catch { /* ignore */ }
        }, { once: true });
    }
}

function stopPlayer({ quiet = false } = {}) {
    const player = state.player;
    if (!player) return;
    state.player = null;
    if (!quiet) saveResume(true, player);
    try { els.video.pause(); } catch { /* ignore */ }
    if (player.hls) { try { player.hls.destroy(); } catch { /* ignore */ } }
    if (player.session) {
        const sid = player.session;
        player.session = null;
        apiFetch(`/api/videos/hls/${encodeURIComponent(sid)}/stop`, { method: 'POST' }).catch(() => {});
    }
    releaseWakeLock(player);
    try { els.video.removeAttribute('src'); els.video.load(); } catch { /* ignore */ }
    if (player.hideTimer) clearTimeout(player.hideTimer);
    els.player.hidden = true;
    els.playerError.hidden = true;
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
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
        saveResume(false, player);
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
        if (!player.local) saveResume(true, player);
        updatePlayIcon();
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
        render();
    });
    els.content.addEventListener('click', (event) => {
        const el = event.target.closest('[data-action]');
        if (!el) return;
        const item = findItem(el.dataset.path);
        if (el.dataset.action === 'open' && item) openSheet(item);
    });
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
    });
    els.downloadsList.addEventListener('click', (event) => {
        const el = event.target.closest('[data-action]');
        if (!el) return;
        const action = el.dataset.action;
        if (action === 'delete-download') removeDownload(el.dataset.path, el.dataset.id);
        else if (action === 'cancel-download') cancelOne(el.dataset.id);
        else if (action === 'retry-download') retryOne(el.dataset.id);
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
    ['mousemove', 'touchstart', 'click'].forEach((type) => {
        els.player.addEventListener(type, () => { if (state.player) showUi(state.player); }, { passive: true });
    });
    document.addEventListener('visibilitychange', () => {
        const player = state.player;
        if (player && document.visibilityState === 'visible' && !els.video.paused) acquireWakeLock(player);
        if (document.visibilityState === 'visible') syncDownloadState();
    });
    window.addEventListener('pagehide', () => {
        const player = state.player;
        if (!player) return;
        if (!player.local) saveResume(true, player);
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
        syncResume();
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
    if (!online) els.net.textContent = 'Offline';
}

async function syncResume() {
    if (!authed()) return;
    try {
        const data = await apiJson('/api/videos/resume/all').catch(() => null);
        if (data?.resume) { state.resume = data.resume; render(); }
    } catch { /* ignore */ }
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

    if (!authed()) { showLogin(); return; }
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
