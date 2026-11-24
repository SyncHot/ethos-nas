// EthOS Videos — offline downloads for the /videos PWA.
//
// Two steps per movie:
//   1. the NAS renders a compact MP4 (`POST /api/videos/exports`, polled);
//   2. this module streams that MP4 into a dedicated Cache Storage bucket
//      (`ethos-media-v1`, key `/__media/<jobId>`) and keeps small metadata in
//      IndexedDB (title, poster blob, subtitle text).
//
// Playback then uses `/__media/<jobId>`: sw.js answers it from the cache and
// synthesises Range responses, so the existing rule “the worker never touches
// /api/*, ?token= or Range requests” stays intact.

import { apiFetch, getToken } from '../core/api.js';
import { startTask } from '../core/progress.js';
import { notify } from '../core/notification-center.js';
import { logError, logInfo, logWarn } from '../core/logger.js';
import {
    backgroundDownloadSupported, startBackgroundDownload, waitBackgroundDownload,
    backgroundResult, backgroundDownloadState, clearBackgroundResult,
    abortBackgroundDownload, sweepBackgroundResults,
} from '../core/background-download.js';

const MEDIA_CACHE = 'ethos-media-v1';
const MEDIA_PREFIX = '/__media/';
const DB_NAME = 'ethos-videos-db';
const DB_VERSION = 2;
const STORE = 'downloads';
const RESUME_STORE = 'resume';

let _db = null;
let _downloads = [];
let _onChange = null;
let _storage = null;

// A failed attempt is retried automatically (the 2 s server poll turns a
// finished render back into `ready`), but a transfer that never moves a byte
// must not spin forever: two attempts without progress pause the row until the
// user taps Retry or the app is reopened.
const _stalled = new Map();   // id → consecutive attempts without progress
const _paused = new Set();    // id → gave up, do not auto-retry
const _bgWatchers = new Map(); // id → live Background Fetch reattach
const _bgFailed = new Set();   // id → browser-managed attempt failed
// A NAS restart or a dropped link is not a bad file: keep the row and retry
// with a backoff instead of pausing it.
const _retryTimers = new Map();
const _retryTries = new Map();

function noteAttempt(id, progressed) {
    const count = progressed ? 0 : (_stalled.get(id) || 0) + 1;
    _stalled.set(id, count);
    if (count >= 2) _paused.add(id);
    return count;
}

function clearAttempts(id) {
    _stalled.delete(id);
    _paused.delete(id);
}

// ── IndexedDB ─────────────────────────────────────────────────────────────

function openDb() {
    if (_db) return Promise.resolve(_db);
    return new Promise((resolve, reject) => {
        if (!('indexedDB' in window)) { reject(new Error('IndexedDB unavailable')); return; }
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(STORE)) {
                db.createObjectStore(STORE, { keyPath: 'id' });
            }
            if (!db.objectStoreNames.contains(RESUME_STORE)) {
                db.createObjectStore(RESUME_STORE, { keyPath: 'path' });
            }
        };
        request.onsuccess = () => { _db = request.result; resolve(_db); };
        request.onerror = () => reject(request.error || new Error('IndexedDB failed'));
    });
}

function dbPut(row) {
    return dbPutTo(STORE, row);
}

function dbDelete(id) {
    return dbDeleteFrom(STORE, id);
}

function dbAll() {
    return dbAllFrom(STORE);
}

function dbPutTo(store, row) {
    return openDb().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction(store, 'readwrite');
        tx.objectStore(store).put(row);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    }));
}

function dbDeleteFrom(store, key) {
    return openDb().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction(store, 'readwrite');
        tx.objectStore(store).delete(key);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    }));
}

function dbAllFrom(store) {
    return openDb().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction(store, 'readonly');
        const request = tx.objectStore(store).getAll();
        request.onsuccess = () => resolve(request.result || []);
        request.onerror = () => reject(request.error);
    }));
}

// ── public state ──────────────────────────────────────────────────────────

/** Cached `navigator.storage.estimate()` numbers (sync for rendering). */
export function storageInfo() {
    return _storage;
}

export async function refreshStorage() {
    try {
        if (!navigator.storage?.estimate) return null;
        const estimate = await navigator.storage.estimate();
        _storage = { usage: Number(estimate.usage) || 0, quota: Number(estimate.quota) || 0 };
    } catch {
        _storage = null;
    }
    return _storage;
}

/** Sync copy of the download rows (for renders). */
export function downloadsSnapshot() {
    return _downloads.slice();
}

export function localMediaUrl(id) {
    return `${MEDIA_PREFIX}${id}`;
}

export function localPoster(row) {
    return row?.posterUrl || '';
}

export function localSubtitle(row) {
    return row?.subtitle?.text || '';
}

// ── local playback progress (survives an offline restart) ────────────────

export async function saveLocalResume(path, time, duration) {
    if (!path) return;
    try {
        await dbPutTo(RESUME_STORE, {
            path, time: Number(time) || 0, duration: Number(duration) || 0, updated_at: Date.now(),
        });
    } catch { /* private mode / quota — resume is best effort */ }
}

export async function clearLocalResume(path) {
    if (!path) return;
    try { await dbDeleteFrom(RESUME_STORE, path); } catch { /* ignore */ }
}

export async function allLocalResumes() {
    try { return await dbAllFrom(RESUME_STORE); } catch { return []; }
}

// ── init ──────────────────────────────────────────────────────────────────

export async function initOffline(onChange) {
    _onChange = typeof onChange === 'function' ? onChange : null;
    let rows = [];
    try { rows = await dbAll(); } catch (error) { logWarn('Offline store unavailable', String(error)); }
    // Results written by sw.js while the app was closed (Background Fetch
    // keeps running outside the page).
    let bgResults = [];
    try { bgResults = await sweepBackgroundResults('videos'); } catch { bgResults = []; }
    const byTag = new Map(bgResults.map((result) => [result.tag, result]));
    const reattach = [];
    const finished = [];
    const touched = new Set();
    _downloads = [];
    for (const row of rows) {
        const next = { ...row };
        if (next.status === 'downloading' && next.bg_tag) {
            const result = byTag.get(next.bg_tag) || await backgroundResult(next.bg_tag, { waitMs: 2000 });
            if (!result && await backgroundDownloadState(next.bg_tag)) {
                // The browser is still transferring this file: keep the row and
                // follow it again instead of starting a second transfer.
                reattach.push(next);
                if (next.posterBlob && !next.posterUrl) {
                    try { next.posterUrl = URL.createObjectURL(next.posterBlob); } catch { /* ignore */ }
                }
                _downloads.push(next);
                continue;
            }
            touched.add(next.id);
            next.bg_tag = '';
            if (result?.ok) {
                finished.push({ row: next, result });
                next.status = 'done';
                next.percent = 100;
                next.bytesDone = Number(result.bytes) || Number(next.bytesTotal) || 0;
                next.bytesTotal = next.bytesDone;
                next.parts = [];
                next.finished_at = Number(next.finished_at) || Date.now();
                next.error = '';
            } else {
                // The browser gave up while the app was away — queue it again.
                next.status = 'ready';
                next.percent = 0;
                next.bytesDone = 0;
                next.parts = [];
                next.error = '';
            }
        } else if (next.status === 'downloading') {
            // Interrupted transfer: keep `bytesDone`/`parts` so the next
            // attempt resumes with `Range: bytes=<done>-` instead of starting
            // a multi-GB film from zero.
            next.status = 'ready';
            next.error = '';
            touched.add(next.id);
        } else if (next.status === 'waiting') {
            // A previous NAS outage. Rows that opted into retries come back on
            // their own; anything else needs an explicit Retry.
            next.status = next.retry ? 'ready' : 'failed';
            if (!next.retry && !next.error) next.error = 'The NAS no longer has this download — queue it again';
            touched.add(next.id);
        } else if (next.status === 'rendering') {
            // The NAS-side render was cut by the reload and keeps running
            // there — the local byte counters mean nothing yet.
            next.status = 'ready';
            next.bytesDone = 0;
            next.percent = 0;
            next.parts = [];
            next.error = '';
            touched.add(next.id);
        }
        if (next.posterBlob && !next.posterUrl) {
            try { next.posterUrl = URL.createObjectURL(next.posterBlob); } catch { /* ignore */ }
        }
        _downloads.push(next);
    }
    _downloads.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    for (const id of touched) await persistRow(id);
    for (const { row, result } of finished) {
        clearBackgroundResult(result.tag).catch(() => {});
        acknowledgeDownload(row.id);
        storeSubtitle(row);
    }
    await refreshStorage();
    try {
        if (navigator.storage?.persist && !(await navigator.storage.persisted())) {
            await navigator.storage.persist();
        }
    } catch { /* quota manager may refuse */ }
    notifyChange();
    for (const row of reattach) resumeBackgroundRow(row);
    return _downloads;
}

function withoutRuntime(row) {
    const { posterUrl, ...rest } = row;
    return rest;
}

function notifyChange() {
    if (_onChange) {
        try { _onChange(downloadsSnapshot()); } catch { /* ignore */ }
    }
}

function upsert(row) {
    const index = _downloads.findIndex((entry) => entry.id === row.id);
    if (index >= 0) _downloads[index] = { ..._downloads[index], ...row };
    else _downloads.unshift(row);
    notifyChange();
    return index >= 0 ? _downloads[index] : _downloads[0];
}

function authHeaders(extra = {}) {
    const token = getToken();
    const headers = { ...extra };
    if (token) headers.Authorization = `Bearer ${token}`;
    return headers;
}

function toast(message, kind = 'info') {
    try { notify({ kind, message }); } catch { /* host missing */ }
}

function formatBytes(value) {
    const n = Number(value) || 0;
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
    if (n >= 1e3) return `${Math.round(n / 1e3)} KB`;
    return `${n} B`;
}

// ── server <-> device reconciliation ──────────────────────────────────────

const ACTIVE_STATUSES = new Set(['queued', 'rendering', 'ready', 'downloading', 'waiting']);

function rowFromJob(job) {
    return {
        id: job.id,
        path: job.path || '',
        title: job.name || String(job.path || '').split('/').pop() || 'Video',
        quality: job.quality || '',
        qualityLabel: job.quality_label || job.quality || '',
        mode: job.mode || '',
        note: job.note || '',
        status: job.status === 'done' ? 'ready' : job.status,
        percent: Number(job.percent) || 0,
        bytesDone: 0,
        bytesTotal: Number(job.size) || 0,
        size: Number(job.size) || 0,
        duration: Number(job.duration) || 0,
        queuePosition: Number(job.queue_position) || 0,
        download_url: job.download_url || `/api/videos/exports/${encodeURIComponent(job.id)}/download`,
        created_at: (Number(job.created_at) || 0) * 1000,
        finished_at: (Number(job.finished_at) || 0) * 1000,
        error: job.error || '',
        item: { path: job.path, title: job.name, name: job.name },
    };
}

function applyJob(row, job) {
    const snapshot = () => [row.status, row.percent, row.size, row.quality, row.qualityLabel,
        row.mode, row.note, row.queuePosition, row.error, row.bytesTotal].join('|');
    const before = snapshot();
    const previous = row.status;
    row.quality = job.quality || row.quality;
    row.qualityLabel = job.quality_label || row.qualityLabel;
    row.mode = job.mode || row.mode;
    row.note = job.note || row.note;
    row.queuePosition = Number(job.queue_position) || 0;
    row.duration = Number(job.duration) || row.duration || 0;
    if (job.status === 'failed') {
        row.status = 'failed';
        row.error = job.error || 'The NAS could not prepare this download';
    } else if (job.status === 'cancelled' || job.status === 'consumed') {
        // Another device finished it, or the user cancelled it elsewhere.
        row.status = previous === 'done' ? 'done' : 'failed';
        row.error = previous === 'done' ? '' : 'Removed on the server — queue it again';
    } else if (job.status === 'done') {
        row.size = Number(job.size) || row.size || 0;
        row.bytesTotal = row.size;
        if (previous !== 'downloading' && previous !== 'done') {
            row.status = 'ready';
            row.percent = 100;
        }
    } else if (previous !== 'downloading' && previous !== 'done') {
        row.status = job.status;
        row.percent = Number(job.percent) || 0;
    }
    if (row.status === 'done') row.queuePosition = 0;
    return snapshot() !== before;
}

function publicItem(item) {
    return {
        path: item.path, title: item.title, name: item.name, kind: item.kind,
        show: item.show, season: item.season, episode: item.episode,
        quality: item.quality, tmdb: item.tmdb || null,
    };
}

async function persistRow(id) {
    const row = _downloads.find((entry) => entry.id === id);
    if (row) await dbPut(withoutRuntime(row)).catch(() => {});
}

/**
 * Fetch the NAS queue and merge it into the device rows.
 *
 * This is what makes a download survive closing the app: the render keeps
 * running server-side and reappears here with its real stage and position.
 */
export async function syncDownloads() {
    let data;
    try {
        const res = await apiFetch('/api/videos/exports');
        if (!res.ok) return _downloads;
        data = await res.json();
    } catch (error) {
        logWarn('Download sync failed', String(error));
        return _downloads;
    }
    const jobs = Array.isArray(data?.jobs) ? data.jobs : [];
    const byId = new Map(jobs.map((job) => [job.id, job]));
    const touched = new Set();
    const orphaned = new Set();
    for (const row of _downloads) {
        // A paused download stays failed with its own error: reconciling it
        // would flip it back to `ready` every poll and restart the loop.
        if (_paused.has(row.id)) continue;
        // A backoff retry owns the row until its timer fires; a 2 s sync must
        // not restart a transfer the NAS is still rebooting through.
        if (row.retry && row.status === 'waiting') continue;
        const job = byId.get(row.id);
        if (!job) {
            if (row.status !== 'done' && row.status !== 'failed') {
                row.status = 'failed';
                row.error = 'The NAS no longer has this download — queue it again';
                row.parts = [];
                touched.add(row.id);
            }
            if (Array.isArray(row.parts) && row.parts.length) orphaned.add(row.id);
            continue;
        }
        if (applyJob(row, job)) touched.add(row.id);
        if (['failed', 'cancelled', 'consumed'].includes(job.status)
            && Array.isArray(row.parts) && row.parts.length) {
            // The NAS copy is unusable or gone: partial bytes can never resume.
            row.parts = [];
            touched.add(row.id);
            orphaned.add(row.id);
        }
    }
    const known = new Set(_downloads.map((row) => row.id));
    for (const job of jobs) {
        if (known.has(job.id)) continue;
        if (job.status === 'failed' || job.status === 'cancelled' || job.status === 'consumed') continue;
        _downloads.push(rowFromJob(job));
        touched.add(job.id);
    }
    _downloads.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    if (orphaned.size) {
        try {
            const cache = await caches.open(MEDIA_CACHE);
            for (const id of orphaned) await discardParts(cache, id);
        } catch { /* cache may be unavailable */ }
    }
    // A no-op poll must not rewrite IndexedDB or repaint the list every 2 s.
    if (!touched.size) return _downloads;
    notifyChange();
    for (const id of touched) await persistRow(id);
    return _downloads;
}

// ── queue + transfer ──────────────────────────────────────────────────────

let _pumping = false;

/**
 * Queue one movie. Returns as soon as the NAS accepted the job — the render
 * and the transfer continue in the background (`pumpDownloads()`).
 *
 * @param {object} item     library item (path, title, tmdb…)
 * @param {string} [quality]  original | balanced | fast (omitted = account default)
 */
export async function queueDownload(item, quality) {
    // Cache Storage only exists in a secure context: plain-http LAN installs
    // would otherwise fail deep inside with an opaque error.
    if (!window.isSecureContext || !('caches' in window)) {
        throw new Error('Offline downloads need HTTPS — open this page over https:// or on localhost');
    }
    const body = { path: item.path, a: 0 };
    if (quality) body.q = quality;
    const startRes = await apiFetch('/api/videos/exports', {
        method: 'POST',
        headers: authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body),
    });
    const job = await startRes.json().catch(() => ({}));
    if (!startRes.ok) throw new Error(job.detail || `HTTP ${startRes.status}`);
    let row = _downloads.find((entry) => entry.id === job.id);
    if (!row) {
        row = rowFromJob(job);
        row.created_at = Date.now();
    }
    row.item = { ...(row.item || {}), ...publicItem(item) };
    applyJob(row, job);
    upsert(row);
    clearAttempts(row.id);
    await persistRow(row.id);
    // Fetch the poster now: rows drawn while the film renders then reuse one
    // blob URL instead of reloading a thumbnail on every repaint.
    storePoster(row, row.item || item);
    await refreshStorage();
    pumpDownloads();
    return _downloads.find((entry) => entry.id === job.id) || row;
}

/** Fill the device with ready files, one at a time. Safe to call repeatedly. */
export async function pumpDownloads() {
    if (_pumping) return;
    _pumping = true;
    try {
        for (;;) {
            const ready = _downloads
                .filter((row) => row.status === 'ready' && !_paused.has(row.id))
                .sort((a, b) => (a.created_at || 0) - (b.created_at || 0))[0];
            if (!ready) break;
            await transferRow(ready);
        }
    } finally {
        _pumping = false;
    }
}

function canBackgroundTransfer(row) {
    // A previous Background Fetch failure falls back to the in-page transfer
    // (`_stalled` is bumped so the next attempt skips the browser queue), and
    // partial bytes can only be resumed by the page's Range logic.
    return backgroundDownloadSupported()
        && !_bgFailed.has(row.id)
        && !(_stalled.get(row.id) >= 1)
        && !(Array.isArray(row.parts) && row.parts.length)
        && !(Number(row.bytesDone) > 0);
}

function isTransient(error) {
    if (error?.name === 'QuotaExceededError') return false;
    const status = Number(error?.status) || 0;
    if (status >= 400 && status < 500) return false;
    return true;   // network error, timeout, 5xx or a restarting NAS
}

function clearRetry(id) {
    const timer = _retryTimers.get(id);
    if (timer) clearTimeout(timer);
    _retryTimers.delete(id);
}

function scheduleRetry(id) {
    const row = _downloads.find((entry) => entry.id === id);
    if (!row) return;
    const attempt = (_retryTries.get(id) || 0) + 1;
    _retryTries.set(id, attempt);
    const delay = [5000, 15000, 30000, 60000, 120000][Math.min(attempt - 1, 4)];
    clearRetry(id);
    upsert({ ...row, status: 'waiting', retry: true, error: 'The NAS is unavailable — retrying…' });
    persistRow(id).catch(() => {});
    _retryTimers.set(id, setTimeout(() => {
        _retryTimers.delete(id);
        const current = _downloads.find((entry) => entry.id === id);
        if (!current) return;
        if (navigator.onLine === false) { scheduleRetry(id); return; }
        upsert({ ...current, status: 'ready', error: '' });
        persistRow(id).catch(() => {});
        pumpDownloads();
    }, delay));
}

/** Server is back (online event / app visible): retry without waiting. */
export function resumeServerErrors() {
    for (const row of _downloads) {
        if (row.status !== 'waiting' || !row.retry) continue;
        clearRetry(row.id);
        upsert({ ...row, status: 'ready', error: '' });
        persistRow(row.id).catch(() => {});
    }
    pumpDownloads();
}

function downloadUrlFor(row) {
    return row.download_url || `/api/videos/exports/${encodeURIComponent(row.id)}/download`;
}

async function transferRow(row) {
    const title = row.item?.tmdb?.title || row.title || row.path;
    const resumeFrom = Number(row.bytesDone) || 0;
    const total = Number(row.bytesTotal) || Number(row.size) || 0;
    const percent = resumeFrom && total ? Math.min(99, (resumeFrom / total) * 100) : 0;
    upsert({ ...row, status: 'downloading', percent, bytesTotal: total, error: '' });
    await persistRow(row.id);
    const task = startTask({
        appId: 'videos',
        title: `Saving · ${title}`,
        detail: resumeFrom
            ? `Resuming from ${formatBytes(resumeFrom)}…`
            : `Downloading from the NAS… (${row.qualityLabel || row.quality || 'original'})`,
        percent,
        testIdPrefix: 'vd-download',
    });
    try {
        let size = 0;
        let bgTag = null;
        if (canBackgroundTransfer(row)) {
            bgTag = await startBackgroundDownload({
                app: 'videos',
                url: downloadUrlFor(row),
                cache: MEDIA_CACHE,
                key: mediaKey(row.id),
                type: 'video/mp4',
                title: `EthOS Videos — ${title}`,
            });
        }
        if (bgTag) {
            const started = _downloads.find((entry) => entry.id === row.id);
            if (!started) {
                // Deleted while the browser was being asked to take the job.
                await abortBackgroundDownload(bgTag);
                task.finish('cancelled', { detail: 'Cancelled' });
                setTimeout(() => { try { task.dismiss(); } catch { /* ignore */ } }, 1000);
                return;
            }
            upsert({ ...started, bg_tag: bgTag });
            await persistRow(row.id);
            const result = await waitBackgroundDownload(bgTag, ({ downloaded, total: bgTotal }) => {
                const current = _downloads.find((entry) => entry.id === row.id);
                if (!current) return;                       // deleted while away
                const live = bgTotal ? Math.min(99, (downloaded / bgTotal) * 100) : 0;
                upsert({
                    ...current,
                    bg_tag: bgTag,
                    bytesDone: downloaded,
                    bytesTotal: bgTotal || current.bytesTotal || 0,
                    percent: live,
                });
                task.update({ percent: live, bytesDone: downloaded, bytesTotal: bgTotal });
            });
            await clearBackgroundResult(bgTag);
            if (!result.ok) {
                const error = new Error(result.error || 'Background download failed');
                error.background = true;
                throw error;
            }
            const current = _downloads.find((entry) => entry.id === row.id) || row;
            upsert({ ...current, bg_tag: '' });
            await persistRow(row.id);
            size = Number(result.bytes) || Number(current.bytesTotal) || 0;
            if (!size) {
                try {
                    const cache = await caches.open(MEDIA_CACHE);
                    const hit = await cache.match(new Request(mediaKey(row.id)));
                    if (hit) size = (await hit.blob()).size;
                } catch { /* size falls back to zero */ }
            }
        } else {
            size = await downloadToCache(row, task);
        }
        await completeTransfer(row, size, task);
    } catch (error) {
        const message = error?.detail || error?.message || 'Download failed';
        const current = _downloads.find((entry) => entry.id === row.id);
        if (!current) {
            task.finish('cancelled', { detail: 'Cancelled' });
            setTimeout(() => { try { task.dismiss(); } catch { /* ignore */ } }, 1000);
            return;
        }
        if (error?.background) {
            // The browser refused or lost the transfer: retry in-page. NAS
            // restarts surface as network errors there and get a backoff, so
            // a browser-managed failure must not pause the row.
            _bgFailed.add(row.id);
            upsert({
                ...current,
                status: 'ready',
                bg_tag: '',
                percent: 0,
                bytesDone: 0,
                parts: [],
                error: '',
            });
            await persistRow(row.id);
            task.finish('cancelled', { detail: 'Background download unavailable — retrying in the app' });
            setTimeout(() => { try { task.dismiss(); } catch { /* ignore */ } }, 1500);
            logWarn('Background download fell back', `${row.path}: ${message}`);
            return;
        }
        if (isTransient(error)) {
            // NAS restart / network lost: keep the downloaded parts and retry
            // with a backoff; no manual Retry tap is needed.
            upsert({ ...current, status: 'waiting', retry: true, error: 'The NAS is unavailable — retrying…' });
            await persistRow(row.id);
            task.finish('cancelled', { detail: 'NAS unavailable — will retry' });
            setTimeout(() => { try { task.dismiss(); } catch { /* ignore */ } }, 1500);
            scheduleRetry(row.id);
            logWarn('Download paused until the NAS is back', `${row.path}: ${message}`);
            return;
        }
        // Partial bytes that did reach Cache Storage stay on the row, so a
        // retry (this session) and the next app launch both resume.
        const progressed = (Number(current.bytesDone) || 0) > resumeFrom;
        const paused = noteAttempt(row.id, progressed) >= 2;
        upsert({
            ...current,
            status: 'failed',
            error: paused ? `${message} — tap Retry to resume` : message,
        });
        await persistRow(row.id);
        task.finish('failed', { detail: message });
        logError('Download failed', `${row.path}: ${message}`);
        toast(`Download failed: ${message}`, 'error');
    }
}

async function completeTransfer(row, size, task) {
    clearAttempts(row.id);
    clearRetry(row.id);
    _retryTries.delete(row.id);
    _bgFailed.delete(row.id);
    const current = _downloads.find((entry) => entry.id === row.id);
    if (!current) {
        // The user deleted/cancelled the row while the file was streaming.
        await discardLocalMedia(row.id);
        task.finish('cancelled', { detail: 'Cancelled' });
        setTimeout(() => { try { task.dismiss(); } catch { /* ignore */ } }, 1000);
        return;
    }
    const title = row.item?.tmdb?.title || row.title || row.path;
    upsert({
        ...current,
        status: 'done',
        percent: 100,
        size,
        bytesDone: size,
        bytesTotal: size,
        parts: [],
        finished_at: Date.now(),
        error: '',
        bg_tag: '',
        retry: false,
    });
    await persistRow(row.id);
    await storePoster(row, row.item || {});
    await storeSubtitle(row);
    task.finish('done', { percent: 100, detail: 'Ready offline' });
    // The Downloads sheet is the lasting record on a phone; one finished
    // global task row would otherwise linger as “Tasks 1” on the home page.
    setTimeout(() => { try { task.dismiss(); } catch { /* ignore */ } }, 3000);
    logInfo('Download stored', `${row.id} ${formatBytes(size)}`);
    await acknowledgeDownload(row.id);
    await refreshStorage();
    const stillBusy = _downloads.some((entry) => ACTIVE_STATUSES.has(entry.status));
    if (!stillBusy) toast(`${title} is ready offline`, 'success');
}

/** Follow a Background Fetch that survived a reload or an app switch. */
function resumeBackgroundRow(row) {
    if (_bgWatchers.has(row.id) || !row.bg_tag) return;
    const title = row.item?.tmdb?.title || row.title || row.path;
    const run = (async () => {
        const task = startTask({
            appId: 'videos',
            title: `Saving · ${title}`,
            detail: 'Background download',
            percent: Number(row.percent) || 0,
            testIdPrefix: 'vd-download',
        });
        const tag = row.bg_tag;
        const result = await waitBackgroundDownload(tag, ({ downloaded, total }) => {
            const current = _downloads.find((entry) => entry.id === row.id);
            if (!current) return;
            const percent = total ? Math.min(99, (downloaded / total) * 100) : 0;
            upsert({
                ...current,
                bg_tag: tag,
                bytesDone: downloaded,
                bytesTotal: total || current.bytesTotal || 0,
                percent,
            });
            task.update({ percent, bytesDone: downloaded, bytesTotal: total });
        });
        await clearBackgroundResult(tag);
        const current = _downloads.find((entry) => entry.id === row.id);
        if (!current) { task.finish('cancelled', { detail: 'Cancelled' }); return; }
        if (!result.ok) {
            noteAttempt(row.id, false);
            const paused = _paused.has(row.id);
            upsert({
                ...current,
                status: paused ? 'failed' : 'ready',
                bg_tag: '',
                percent: 0,
                bytesDone: 0,
                parts: [],
                error: paused ? `${result.error || 'Background download failed'} — tap Retry` : '',
            });
            await persistRow(row.id);
            task.finish(paused ? 'failed' : 'cancelled', {
                detail: result.error || 'Background download stopped',
            });
            if (!paused) pumpDownloads();
            return;
        }
        const size = Number(result.bytes) || Number(current.bytesTotal) || 0;
        await completeTransfer(current, size, task);
    })().catch((error) => {
        logWarn('Background download reattach failed', `${row.path}: ${error?.message || error}`);
        const current = _downloads.find((entry) => entry.id === row.id);
        if (current) {
            upsert({ ...current, status: 'ready', bg_tag: '', percent: 0, bytesDone: 0, parts: [], error: '' });
            persistRow(row.id).catch(() => {});
            pumpDownloads();
        }
    }).finally(() => { _bgWatchers.delete(row.id); });
    _bgWatchers.set(row.id, run);
}

/** Stop a browser-managed transfer when the user deletes its row. */
function stopBackground(row) {
    const tag = row?.bg_tag;
    if (!tag) return;
    abortBackgroundDownload(tag).catch(() => {});
    clearBackgroundResult(tag).catch(() => {});
}

async function acknowledgeDownload(id) {
    try {
        await apiFetch(`/api/videos/exports/${encodeURIComponent(id)}/consumed`, { method: 'POST' });
    } catch { /* the NAS copy can expire on its own */ }
}

/** Cancel a queued/rendering job (finished rows use `deleteDownload`). */
export async function cancelDownload(id) {
    try {
        await apiFetch(`/api/videos/exports/${encodeURIComponent(id)}`, { method: 'DELETE' });
    } catch (error) {
        logWarn('Could not cancel the download', String(error));
    }
    clearRetry(id);
    _retryTries.delete(id);
    _bgFailed.delete(id);
    const index = _downloads.findIndex((entry) => entry.id === id);
    if (index >= 0) {
        const [row] = _downloads.splice(index, 1);
        stopBackground(row);
        if (row?.posterUrl) { try { URL.revokeObjectURL(row.posterUrl); } catch { /* ignore */ } }
        notifyChange();
    }
    try { await dbDelete(id); } catch { /* ignore */ }
    clearAttempts(id);
    await discardLocalMedia(id);
    await refreshStorage();
}

/** Re-queue a failed/removed download with its previous version. */
export async function retryDownload(id) {
    const row = _downloads.find((entry) => entry.id === id);
    if (!row) return null;
    clearAttempts(id);
    const item = row.item || { path: row.path, title: row.title };
    const next = await queueDownload(item, row.quality || undefined);
    if (row.id !== next.id) {
        const index = _downloads.findIndex((entry) => entry.id === row.id);
        if (index >= 0) _downloads.splice(index, 1);
        try { await dbDelete(row.id); } catch { /* ignore */ }
        // A new job id cannot reuse the old partial bytes.
        await discardLocalMedia(row.id);
        notifyChange();
    }
    return next;
}

// ── resumable transfer ────────────────────────────────────────────────────
//
// A multi-GB film must not restart from zero when the page is reloaded, the
// phone locks or a proxy drops the connection. Every attempt streams into its
// own Cache Storage entry (`/__media/<id>?part=<n>`) and the row records the
// part sizes in IndexedDB, so the next attempt sends `Range: bytes=<done>-`
// and appends. The playable `/__media/<id>` key is written once, by streaming
// the parts into a single response, and only after the last byte arrived.

function mediaKey(id) { return `${MEDIA_PREFIX}${id}`; }
function partKey(id, index) { return `${MEDIA_PREFIX}${id}?part=${index}`; }

function absolutePath(path) {
    try { return new URL(path, location.origin).href; } catch { return path; }
}

/** Recorded parts that are still cached and still contiguous (0..n-1). */
async function usableParts(cache, id, recorded) {
    const list = Array.isArray(recorded) ? recorded : [];
    const out = [];
    for (const entry of list) {
        const index = Number(entry?.index);
        const bytes = Number(entry?.bytes);
        if (index !== out.length || !(bytes > 0)) break;   // no gaps allowed
        let hit = null;
        try { hit = await cache.match(new Request(partKey(id, index))); } catch { hit = null; }
        if (!hit) break;
        out.push({ index, bytes });
    }
    return out;
}

/** Drop every partial entry of one download (cancel, delete, orphaned tail). */
async function discardParts(cache, id) {
    const prefix = absolutePath(`${MEDIA_PREFIX}${id}?part=`);
    let keys = [];
    try { keys = await cache.keys(); } catch { return; }
    for (const req of keys) {
        if (!req.url.startsWith(prefix)) continue;
        try { await cache.delete(req); } catch { /* ignore */ }
    }
}

/** Remove the playable entry and every partial one. */
async function discardLocalMedia(id) {
    try {
        const cache = await caches.open(MEDIA_CACHE);
        await cache.delete(new Request(mediaKey(id)));
        await discardParts(cache, id);
    } catch { /* cache may be unavailable */ }
}

/** Concatenate the parts into the single entry the player reads. */
async function assembleParts(cache, id, parts, total) {
    if (!parts.length) throw new Error('Nothing to assemble');
    if (typeof ReadableStream === 'undefined') throw new Error('This browser cannot store offline video');
    const headers = { 'Content-Type': 'video/mp4' };
    if (total) headers['Content-Length'] = String(total);
    const stream = new ReadableStream({
        async start(controller) {
            for (const part of parts) {
                let hit = null;
                try { hit = await cache.match(new Request(partKey(id, part.index))); } catch { hit = null; }
                if (!hit || !hit.body) continue;
                const reader = hit.body.getReader();
                for (;;) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    controller.enqueue(value);
                }
            }
            controller.close();
        },
    });
    await cache.put(new Request(mediaKey(id)), new Response(stream, { headers }));
}

/** `<total>` from `Content-Range: bytes 100-199/7800000`. */
function contentRangeTotal(header) {
    const match = /^bytes\s+\d+-\d+\/(\d+)$/i.exec(String(header || '').trim());
    return match ? Number(match[1]) || 0 : 0;
}

/**
 * One transfer attempt. Returns the stored size when the file is complete;
 * throws after persisting whatever bytes are durable, so the caller can retry
 * and resume from the new offset.
 */
async function _transferOnce(rawRow, task) {
    // Always work from the live row: the parts list grows as attempts land, and
    // the object captured by the caller goes stale after the first `upsert`.
    const row = _downloads.find((entry) => entry.id === rawRow.id) || rawRow;
    const url = row.download_url || `/api/videos/exports/${encodeURIComponent(row.id)}/download`;
    const cache = await caches.open(MEDIA_CACHE);
    try { await cache.delete(new Request(mediaKey(row.id))); } catch { /* ignore */ }

    const recorded = Array.isArray(row.parts) ? row.parts : [];
    let parts = await usableParts(cache, row.id, recorded);
    if (parts.length !== recorded.length) {
        // The tail was evicted: the recorded sizes no longer add up, so drop
        // whatever survived past the last usable part.
        for (const entry of recorded.slice(parts.length)) {
            try { await cache.delete(new Request(partKey(row.id, Number(entry.index)))); } catch { /* ignore */ }
        }
    }
    let done = parts.reduce((sum, part) => sum + part.bytes, 0);

    let res = await apiFetch(url, done > 0 ? { headers: { Range: `bytes=${done}-` } } : {});
    if (done > 0 && (res.status === 200 || res.status === 416)) {
        // 200: a proxy dropped the Range header — the body is the whole file.
        // 416: the NAS copy changed size; those partial bytes are useless.
        await discardParts(cache, row.id);
        parts = [];
        done = 0;
        if (res.status === 416) res = await apiFetch(url);
    }
    if (!res.ok) {
        const payload = await res.json().catch(() => ({}));
        const error = new Error(payload.detail || `HTTP ${res.status}`);
        error.status = res.status;
        throw error;
    }
    let total = res.status === 206 ? contentRangeTotal(res.headers.get('Content-Range')) : 0;
    if (!total) total = Number(res.headers.get('Content-Length')) || Number(row.size) || 0;
    if (total) total = Math.max(total, done);

    const index = parts.length;
    const key = new Request(partKey(row.id, index));
    let partBytes = 0;
    let lastUpdate = 0;
    const startedAt = Date.now();
    const report = () => {
        const written = done + partBytes;
        const percent = total ? Math.min(99, (written / total) * 100) : 0;
        const elapsed = (Date.now() - startedAt) / 1000;
        const speed = elapsed > 2 && partBytes ? formatBytes(partBytes / elapsed) : '';
        const current = _downloads.find((entry) => entry.id === row.id) || row;
        upsert({ ...current, bytesDone: written, bytesTotal: total, percent });
        task?.update({
            percent,
            // bytesDone/bytesTotal already render as "X / Y" in the shared row;
            // repeating them here printed the same numbers twice, with the
            // server total next to the row's stale estimate.
            detail: speed ? `${speed}/s` : '',
            bytesDone: written,
            bytesTotal: total,
        });
    };
    report();

    try {
        if (res.body && typeof TransformStream !== 'undefined') {
            // Stream straight into Cache Storage: a 4 GB film must not sit in
            // phone RAM as one Blob (the old implementation did exactly that).
            const counter = new TransformStream({
                transform(chunk, controller) {
                    partBytes += chunk.byteLength;
                    const now = Date.now();
                    if (now - lastUpdate > 250) { lastUpdate = now; report(); }
                    controller.enqueue(chunk);
                },
            }, undefined, { highWaterMark: 1 });
            const headers = { 'Content-Type': 'video/mp4' };
            if (total) headers['Content-Length'] = String(total);
            await cache.put(key, new Response(res.body.pipeThrough(counter), { headers }));
        } else {
            const buffer = await res.arrayBuffer();
            partBytes = buffer.byteLength;
            report();
            await cache.put(key, new Response(buffer, {
                headers: { 'Content-Type': 'video/mp4', 'Content-Length': String(done + partBytes) },
            }));
        }
    } catch (error) {
        // Bytes piped into a rejected `cache.put` are not durable: publish the
        // last flushed offset so the retry resumes exactly there.
        const current = _downloads.find((entry) => entry.id === row.id) || row;
        upsert({ ...current, bytesDone: done, bytesTotal: total || Number(current.bytesTotal) || 0, parts });
        await persistRow(row.id);
        throw error;
    }

    if (partBytes > 0) parts = [...parts, { index, bytes: partBytes }];
    const written = done + partBytes;
    if (total && written < total) {
        // The stream closed early (proxy/1 h idle cut) but the bytes are
        // durable — hand them back for the next attempt to resume.
        const current = _downloads.find((entry) => entry.id === row.id) || row;
        upsert({ ...current, bytesDone: written, bytesTotal: total, parts, percent: Math.min(99, (written / total) * 100) });
        await persistRow(row.id);
        throw new Error(`Transfer stopped at ${formatBytes(written)} of ${formatBytes(total)}`);
    }

    task?.update({ percent: 99, detail: 'finalizing…' });
    const size = total || written;
    await assembleParts(cache, row.id, parts, size);
    await discardParts(cache, row.id);
    const current = _downloads.find((entry) => entry.id === row.id) || row;
    upsert({ ...current, bytesDone: size, bytesTotal: size, percent: 100, parts: [] });
    await persistRow(row.id);
    return size;
}

async function downloadToCache(row, task) {
    let lastError = null;
    let best = Number(row.bytesDone) || 0;
    for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
            const size = await _transferOnce(row, task);
            if (size > 0) return size;
        } catch (error) {
            lastError = error;
            const status = Number(error?.status) || 0;
            // 4xx means the job is gone or unreadable — retrying cannot help.
            if (status >= 400 && status < 500) throw error;
        }
        const current = _downloads.find((entry) => entry.id === row.id);
        const written = Number(current?.bytesDone) || 0;
        if (written <= best) break;   // an attempt that moved nothing: stop
        best = written;
    }
    throw lastError || new Error('Download interrupted');
}

async function storePoster(entry, item) {
    try {
        const tm = item.tmdb || {};
        const file = tm.poster_file || tm.backdrop_file;
        const url = file
            ? `/api/videos/metadata/image?file=${encodeURIComponent(file)}`
            : `/api/videos/thumbnail?path=${encodeURIComponent(item.path)}&w=342`;
        const res = await apiFetch(url);
        if (!res.ok) return;
        const blob = await res.blob();
        const row = _downloads.find((item2) => item2.id === entry.id);
        if (!row) return;
        if (row.posterUrl) { try { URL.revokeObjectURL(row.posterUrl); } catch { /* ignore */ } }
        upsert({ ...row, posterBlob: blob, posterUrl: URL.createObjectURL(blob) });
        await dbPut(withoutRuntime(_downloads.find((item2) => item2.id === entry.id)));
    } catch { /* posters are optional */ }
}

async function storeSubtitle(entry) {
    try {
        const res = await apiFetch(`/api/videos/subtitles?path=${encodeURIComponent(entry.path)}`);
        if (!res.ok) return;
        const data = await res.json();
        const sub = (data.subtitles || [])[0];
        if (!sub?.path) return;
        const raw = await apiFetch(`/api/files/raw?path=${encodeURIComponent(sub.path)}`);
        if (!raw.ok) return;
        let text = await raw.text();
        if (!String(sub.name || '').toLowerCase().endsWith('.vtt')) text = srtToVtt(text);
        const row = _downloads.find((item) => item.id === entry.id);
        if (!row) return;
        upsert({ ...row, subtitle: { name: sub.name, text } });
        await dbPut(withoutRuntime(_downloads.find((item) => item.id === entry.id)));
    } catch { /* subtitles are optional */ }
}

export function srtToVtt(srt) {
    const body = String(srt).replace(/\r/g, '').replace(/^\uFEFF/, '');
    const out = ['WEBVTT', ''];
    for (const block of body.split('\n\n')) {
        const lines = block.split('\n');
        if (!lines.length) continue;
        let i = 0;
        if (/^\d+$/.test(lines[0].trim())) i = 1;
        if (!lines[i] || !lines[i].includes('-->')) continue;
        out.push(lines[i].replace(/(\d{2}:\d{2}:\d{2}),(\d{3})/g, '$1.$2'), ...lines.slice(i + 1), '');
    }
    return out.join('\n');
}

// ── delete ────────────────────────────────────────────────────────────────

export async function deleteDownload(id) {
    const row = _downloads.find((entry) => entry.id === id);
    clearRetry(id);
    _retryTries.delete(id);
    _bgFailed.delete(id);
    stopBackground(row);
    await discardLocalMedia(id);
    try { await dbDelete(id); } catch { /* ignore */ }
    clearAttempts(id);
    if (row?.posterUrl) { try { URL.revokeObjectURL(row.posterUrl); } catch { /* ignore */ } }
    const index = _downloads.findIndex((entry) => entry.id === id);
    if (index >= 0) _downloads.splice(index, 1);
    notifyChange();
    await refreshStorage();
    // The NAS copy is shared with the user's other devices and expires with the
    // retention sweep — do not delete it here.
}
