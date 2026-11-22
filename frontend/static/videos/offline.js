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
const DB_VERSION = 1;
const STORE = 'downloads';
const POLL_MS = 1500;
const POLL_TIMEOUT_MS = 3 * 3600 * 1000;

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
        };
        request.onsuccess = () => { _db = request.result; resolve(_db); };
        request.onerror = () => reject(request.error || new Error('IndexedDB failed'));
    });
}

function dbPut(row) {
    return openDb().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).put(row);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    }));
}

function dbDelete(id) {
    return openDb().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readwrite');
        tx.objectStore(STORE).delete(id);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
    }));
}

function dbAll() {
    return openDb().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readonly');
        const request = tx.objectStore(STORE).getAll();
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

// ── init ──────────────────────────────────────────────────────────────────

export async function initOffline(onChange) {
    _onChange = typeof onChange === 'function' ? onChange : null;
    let rows = [];
    try { rows = await dbAll(); } catch (error) { logWarn('Offline store unavailable', String(error)); }
    // A download interrupted by a reload cannot be resumed byte-for-byte
    // without the partial blob; mark it so the UI offers Retry.
    _downloads = rows.map((row) => {
        const next = { ...row };
        if (next.status === 'downloading' || next.status === 'rendering') {
            next.status = 'failed';
            next.error = 'Interrupted — download it again';
        }
        if (next.posterBlob && !next.posterUrl) {
            try { next.posterUrl = URL.createObjectURL(next.posterBlob); } catch { /* ignore */ }
        }
        return next;
    }).sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    for (const row of _downloads) {
        if (row.status === 'failed') { await dbPut(withoutRuntime(row)).catch(() => {}); }
    }
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

// ── render polling ────────────────────────────────────────────────────────

async function waitForRender(jobId, task) {
    const started = Date.now();
    let job = null;
    while (Date.now() - started < POLL_TIMEOUT_MS) {
        const res = await apiFetch(`/api/videos/exports/${encodeURIComponent(jobId)}`);
        if (!res.ok) {
            const payload = await res.json().catch(() => ({}));
            throw new Error(payload.detail || `HTTP ${res.status}`);
        }
        job = await res.json();
        if (job.status === 'done') return job;
        if (job.status === 'failed' || job.status === 'cancelled') {
            throw new Error(job.error || 'The NAS could not prepare this download');
        }
        task?.update({ percent: Number(job.percent) || 0, detail: `Preparing on the NAS… ${Math.round(Number(job.percent) || 0)}%` });
        await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    }
    throw new Error('Timed out preparing the download');
}

// ── download ──────────────────────────────────────────────────────────────

/**
 * Prepare + store one movie. Resolves with the IndexedDB row on success.
 *
 * @param {object} item     library item (path, title, tmdb…)
 * @param {string} quality  fast | balanced | high
 */
export async function downloadItem(item, quality = 'fast', { onChange, onDone } = {}) {
    // Cache Storage only exists in a secure context: plain-http LAN installs
    // would otherwise fail deep inside with an opaque error.
    if (!window.isSecureContext || !('caches' in window)) {
        throw new Error('Offline downloads need HTTPS — open this page over https:// or on localhost');
    }
    const previous = _onChange;
    if (onChange) _onChange = onChange;
    const title = item.tmdb?.title || item.title || item.name || String(item.path).split('/').pop();
    let entry = null;
    let task = null;
    try {
        await refreshStorage();
        const startRes = await apiFetch('/api/videos/exports', {
            method: 'POST',
            headers: authHeaders({ 'Content-Type': 'application/json' }),
            body: JSON.stringify({ path: item.path, q: quality, a: 0 }),
        });
        const startBody = await startRes.json().catch(() => ({}));
        if (!startRes.ok) throw new Error(startBody.detail || `HTTP ${startRes.status}`);
        const job = startBody;

        entry = {
            id: job.id,
            path: item.path,
            title,
            quality: job.quality || quality,
            status: job.status === 'done' ? 'downloading' : 'rendering',
            percent: Number(job.percent) || 0,
            bytesDone: 0,
            bytesTotal: Number(job.size) || 0,
            size: Number(job.size) || 0,
            duration: Number(job.duration) || 0,
            download_url: job.download_url || `/api/videos/exports/${encodeURIComponent(job.id)}/download`,
            created_at: Date.now(),
            error: '',
            item: {
                path: item.path, title: item.title, name: item.name, kind: item.kind,
                show: item.show, season: item.season, episode: item.episode,
                quality: item.quality, tmdb: item.tmdb || null,
            },
        };
        upsert(entry);
        await dbPut(withoutRuntime(entry));

        task = startTask({
            appId: 'videos',
            title: `Download · ${title}`,
            detail: job.status === 'done' ? 'Already prepared — downloading…' : 'Preparing on the NAS…',
            testIdPrefix: 'vd-download',
        });

        let ready = job;
        if (job.status !== 'done') {
            ready = await waitForRender(job.id, task);
            entry = upsert({ ...entry, status: 'downloading', size: Number(ready.size) || 0, percent: 100 });
        }

        await downloadToCache(entry, task);
        entry = upsert({ ...entry, status: 'done', finished_at: Date.now() });
        await storePoster(entry, item);
        await storeSubtitle(entry);
        await dbPut(withoutRuntime(_downloads.find((row) => row.id === entry.id) || entry));
        task.finish('done', { percent: 100, detail: 'Ready offline' });
        await refreshStorage();
        if (onDone) {
            try { onDone(_downloads.find((row) => row.id === entry.id) || entry); } catch { /* ignore */ }
        }
        return _downloads.find((row) => row.id === entry.id) || entry;
    } catch (error) {
        const message = error?.detail || error?.message || 'Download failed';
        logError('Download failed', `${item.path}: ${message}`);
        if (entry) {
            upsert({ ...entry, status: 'failed', error: message });
            await dbPut(withoutRuntime(_downloads.find((row) => row.id === entry.id) || entry)).catch(() => {});
        }
        task?.finish('failed', { detail: message });
        toast(`Download failed: ${message}`, 'error');
        throw error;
    } finally {
        _onChange = previous;
    }
}

async function downloadToCache(entry, task) {
    const res = await apiFetch(entry.download_url || `/api/videos/exports/${encodeURIComponent(entry.id)}/download`);
    if (!res.ok) {
        const payload = await res.json().catch(() => ({}));
        throw new Error(payload.detail || `HTTP ${res.status}`);
    }
    const total = Number(res.headers.get('Content-Length')) || entry.size || 0;
    const chunks = [];
    let done = 0;
    if (res.body && res.body.getReader) {
        const reader = res.body.getReader();
        for (;;) {
            const { value, done: finished } = await reader.read();
            if (finished) break;
            if (value) {
                chunks.push(value);
                done += value.length;
                entry = upsert({ ...entry, bytesDone: done, bytesTotal: total || entry.bytesTotal });
                if (total) {
                    const percent = Math.min(99.5, (done / total) * 100);
                    task?.update({
                        percent,
                        detail: `${formatBytes(done)} / ${formatBytes(total)}`,
                        bytesDone: done,
                        bytesTotal: total,
                    });
                }
            }
        }
    } else {
        const buffer = await res.arrayBuffer();
        chunks.push(new Uint8Array(buffer));
        done = buffer.byteLength;
        task?.update({ percent: 99, detail: formatBytes(done), bytesDone: done, bytesTotal: total || done });
    }
    const blob = new Blob(chunks, { type: 'video/mp4' });
    const cache = await caches.open(MEDIA_CACHE);
    await cache.put(new Request(`${MEDIA_PREFIX}${entry.id}`), new Response(blob, {
        headers: {
            'Content-Type': 'video/mp4',
            'Content-Length': String(blob.size),
        },
    }));
    entry.size = blob.size;
    entry.bytesDone = blob.size;
    entry.bytesTotal = blob.size;
    await dbPut(withoutRuntime(upsert(entry)));
    logInfo('Download stored', `${entry.id} ${formatBytes(blob.size)}`);
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

function srtToVtt(srt) {
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

function formatBytes(value) {
    const n = Number(value) || 0;
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
    if (n >= 1e3) return `${Math.round(n / 1e3)} KB`;
    return `${n} B`;
}
