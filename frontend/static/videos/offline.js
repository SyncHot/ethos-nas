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
    // A transfer interrupted by a reload cannot be resumed byte-for-byte, but
    // the NAS-side render keeps running. Reconcile with the server instead of
    // declaring the download lost — `syncDownloads()` decides the real state.
    _downloads = rows.map((row) => {
        const next = { ...row };
        if (next.status === 'downloading' || next.status === 'rendering') {
            next.status = 'ready';
            next.bytesDone = 0;
            next.percent = 0;
            next.error = '';
        }
        if (next.posterBlob && !next.posterUrl) {
            try { next.posterUrl = URL.createObjectURL(next.posterBlob); } catch { /* ignore */ }
        }
        return next;
    }).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    await refreshStorage();
    try {
        if (navigator.storage?.persist && !(await navigator.storage.persisted())) {
            await navigator.storage.persist();
        }
    } catch { /* quota manager may refuse */ }
    notifyChange();
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

const ACTIVE_STATUSES = new Set(['queued', 'rendering', 'ready', 'downloading']);

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
    for (const row of _downloads) {
        const job = byId.get(row.id);
        if (!job) {
            if (row.status !== 'done' && row.status !== 'failed') {
                row.status = 'failed';
                row.error = 'The NAS no longer has this download — queue it again';
                touched.add(row.id);
            }
            continue;
        }
        if (applyJob(row, job)) touched.add(row.id);
    }
    const known = new Set(_downloads.map((row) => row.id));
    for (const job of jobs) {
        if (known.has(job.id)) continue;
        if (job.status === 'failed' || job.status === 'cancelled' || job.status === 'consumed') continue;
        _downloads.push(rowFromJob(job));
        touched.add(job.id);
    }
    _downloads.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
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
                .filter((row) => row.status === 'ready')
                .sort((a, b) => (a.created_at || 0) - (b.created_at || 0))[0];
            if (!ready) break;
            await transferRow(ready);
        }
    } finally {
        _pumping = false;
    }
}

async function transferRow(row) {
    const title = row.item?.tmdb?.title || row.title || row.path;
    upsert({ ...row, status: 'downloading', percent: 0, bytesDone: 0, bytesTotal: row.size || 0, error: '' });
    await persistRow(row.id);
    const task = startTask({
        appId: 'videos',
        title: `Saving · ${title}`,
        detail: `Downloading from the NAS… (${row.qualityLabel || row.quality || 'original'})`,
        testIdPrefix: 'vd-download',
    });
    try {
        const size = await downloadToCache(row, task);
        const current = _downloads.find((entry) => entry.id === row.id);
        if (!current) {
            // The user deleted/cancelled the row while the file was streaming.
            try { const cache = await caches.open(MEDIA_CACHE); await cache.delete(new Request(`${MEDIA_PREFIX}${row.id}`)); } catch { /* ignore */ }
            task.finish('cancelled', { detail: 'Cancelled' });
            setTimeout(() => { try { task.dismiss(); } catch { /* ignore */ } }, 1000);
            return;
        }
        upsert({
            ...current,
            status: 'done',
            percent: 100,
            size,
            bytesDone: size,
            bytesTotal: size,
            finished_at: Date.now(),
            error: '',
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
    } catch (error) {
        const message = error?.detail || error?.message || 'Download failed';
        const current = _downloads.find((entry) => entry.id === row.id);
        if (!current) {
            task.finish('cancelled', { detail: 'Cancelled' });
            setTimeout(() => { try { task.dismiss(); } catch { /* ignore */ } }, 1000);
            return;
        }
        upsert({ ...current, status: 'failed', error: message });
        await persistRow(row.id);
        task.finish('failed', { detail: message });
        logError('Download failed', `${row.path}: ${message}`);
        toast(`Download failed: ${message}`, 'error');
    }
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
    const index = _downloads.findIndex((entry) => entry.id === id);
    if (index >= 0) {
        const [row] = _downloads.splice(index, 1);
        if (row?.posterUrl) { try { URL.revokeObjectURL(row.posterUrl); } catch { /* ignore */ } }
        notifyChange();
    }
    try { await dbDelete(id); } catch { /* ignore */ }
    try {
        const cache = await caches.open(MEDIA_CACHE);
        await cache.delete(new Request(`${MEDIA_PREFIX}${id}`));
    } catch { /* cache may be unavailable */ }
    await refreshStorage();
}

/** Re-queue a failed/removed download with its previous version. */
export async function retryDownload(id) {
    const row = _downloads.find((entry) => entry.id === id);
    if (!row) return null;
    const item = row.item || { path: row.path, title: row.title };
    const next = await queueDownload(item, row.quality || undefined);
    if (row.id !== next.id) {
        const index = _downloads.findIndex((entry) => entry.id === row.id);
        if (index >= 0) _downloads.splice(index, 1);
        try { await dbDelete(row.id); } catch { /* ignore */ }
        notifyChange();
    }
    return next;
}

async function downloadToCache(row, task) {
    const url = row.download_url || `/api/videos/exports/${encodeURIComponent(row.id)}/download`;
    const res = await apiFetch(url);
    if (!res.ok) {
        const payload = await res.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${res.status}`);
    }
    const total = Number(res.headers.get('Content-Length')) || Number(row.size) || 0;
    const cache = await caches.open(MEDIA_CACHE);
    const key = new Request(`${MEDIA_PREFIX}${row.id}`);
    let done = 0;
    let lastUpdate = 0;
    const report = () => {
        const percent = total ? Math.min(99.5, (done / total) * 100) : 0;
        upsert({ ...(_downloads.find((entry) => entry.id === row.id) || row), bytesDone: done, bytesTotal: total, percent });
        task?.update({
            percent,
            detail: `${formatBytes(done)}${total ? ` / ${formatBytes(total)}` : ''}`,
            bytesDone: done,
            bytesTotal: total,
        });
    };

    try {
        if (res.body && typeof TransformStream !== 'undefined') {
            // Stream straight into Cache Storage: a 4 GB film must not sit in
            // phone RAM as one Blob (the old implementation did exactly that).
            const counter = new TransformStream({
                transform(chunk, controller) {
                    done += chunk.byteLength;
                    const now = Date.now();
                    if (now - lastUpdate > 250) { lastUpdate = now; report(); }
                    controller.enqueue(chunk);
                },
            }, undefined, { highWaterMark: 1 });
            const headers = { 'Content-Type': 'video/mp4' };
            if (total) headers['Content-Length'] = String(total);
            const response = new Response(res.body.pipeThrough(counter), { headers });
            await cache.put(key, response);
        } else {
            const buffer = await res.arrayBuffer();
            done = buffer.byteLength;
            report();
            await cache.put(key, new Response(buffer, {
                headers: { 'Content-Type': 'video/mp4', 'Content-Length': String(done) },
            }));
        }
    } catch (error) {
        try { await cache.delete(key); } catch { /* ignore */ }
        throw error;
    }
    if (!done) {
        // Some builds keep the transformed byte count internal; measure the entry.
        try {
            const stored = await cache.match(key);
            done = Number(stored?.headers.get('Content-Length')) || Number(row.size) || 0;
        } catch { /* ignore */ }
    }
    report();
    return done;
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
    try {
        const cache = await caches.open(MEDIA_CACHE);
        await cache.delete(new Request(`${MEDIA_PREFIX}${id}`));
    } catch { /* cache may be unavailable */ }
    try { await dbDelete(id); } catch { /* ignore */ }
    if (row?.posterUrl) { try { URL.revokeObjectURL(row.posterUrl); } catch { /* ignore */ } }
    const index = _downloads.findIndex((entry) => entry.id === id);
    if (index >= 0) _downloads.splice(index, 1);
    notifyChange();
    await refreshStorage();
    // The NAS copy is shared with the user's other devices and expires with the
    // retention sweep — do not delete it here.
}
