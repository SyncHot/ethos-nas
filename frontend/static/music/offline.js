// EthOS Music — device downloads for the /music PWA.
//
// Behaviour matches the Android app's `OfflineStore`: every track that belongs
// to any playlist is kept on the device, and when it leaves all playlists its
// audio is deleted. Podcast episodes are downloaded on demand and never
// auto-deleted.
//
// Storage: audio lives as a Blob in IndexedDB (store `blobs`), metadata in
// `rows` and small key/value state (library snapshot, episode positions) in
// `meta`. Blobs (not Cache Storage) keep `<audio>` seeking free of any service
// worker Range handler and make deletion explicit.

import { apiFetch } from '../core/api.js';
import { startTask } from '../core/progress.js';
import { notify } from '../core/notification-center.js';
import { logError, logInfo, logWarn } from '../core/logger.js';
import { artUrl, downloadSrc, tkey } from './api.js';

const DB_NAME = 'ethos-music-db';
const DB_VERSION = 1;
const ROWS = 'rows';
const BLOBS = 'blobs';
const META = 'meta';

const PARALLEL_KEY = 'ethos.music.parallel';
const PARALLEL_CHOICES = [1, 2, 4, 6];
const DEFAULT_PARALLEL = 4;      // NAS resolves and proxies in parallel; YouTube tolerates this
const PROGRESS_MS = 250;         // repaint budget per row while bytes stream in
const QUOTA_STOP = 0.92;
const STATE_KEY = 'library';
const AUTO_KEY = 'ethos.music.autooffline';
const POSITION_PREFIX = 'pos:';

let _db = null;
let _rows = [];
let _index = new Map();
let _queue = [];
let _inflight = 0;
let _paused = false;
let _pauseReason = '';
let _onChange = null;
let _storage = null;
let _urls = new Map();
let _task = null;
let _batchDone = 0;
let _batchTotal = 0;
let _quotaNotified = false;
let _lastProgress = new Map();
const _cancelled = new Set();
const _controllers = new Map();
const _runIds = new Map();

// ── IndexedDB ─────────────────────────────────────────────────────────────

function openDb() {
    if (_db) return Promise.resolve(_db);
    return new Promise((resolve, reject) => {
        if (!('indexedDB' in window)) { reject(new Error('IndexedDB unavailable')); return; }
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(ROWS)) db.createObjectStore(ROWS, { keyPath: 'key' });
            if (!db.objectStoreNames.contains(BLOBS)) db.createObjectStore(BLOBS, { keyPath: 'key' });
            if (!db.objectStoreNames.contains(META)) db.createObjectStore(META, { keyPath: 'k' });
        };
        request.onsuccess = () => { _db = request.result; resolve(_db); };
        request.onerror = () => reject(request.error || new Error('IndexedDB failed'));
    });
}

function dbTx(store, mode, fn) {
    return openDb().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction(store, mode);
        const result = fn(tx.objectStore(store));
        tx.oncomplete = () => resolve(result?.result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
    }));
}

const dbGet = (store, key) => dbTx(store, 'readonly', (s) => s.get(key));
const dbPut = (store, value) => dbTx(store, 'readwrite', (s) => s.put(value));
const dbDel = (store, key) => dbTx(store, 'readwrite', (s) => s.delete(key));
const dbAll = (store) => dbTx(store, 'readonly', (s) => s.getAll());

// ── init / state ──────────────────────────────────────────────────────────

export async function initOffline(onChange) {
    _onChange = typeof onChange === 'function' ? onChange : null;
    let rows = [];
    try { rows = await dbAll(ROWS); } catch (error) { logWarn('Music offline store unavailable', String(error)); }
    _rows = rows.map((row) => {
        const next = { ...row };
        // A download interrupted by a reload has no partial file to resume from;
        // put it back in the queue so the next start picks it up again.
        if (next.status === 'downloading') {
            next.status = 'queued';
            next.error = '';
        }
        return next;
    }).sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
    for (const row of _rows) {
        if (row.status === 'failed') await dbPut(ROWS, row).catch(() => {});
    }
    reindex();
    // Rows queued before the reload have no in-memory queue item yet.
    for (const row of _rows) {
        if (row.status === 'queued' && row.track) {
            _queue.push({ key: row.key, track: row.track, kind: row.kind });
        }
    }
    await refreshStorage();
    try {
        if (navigator.storage?.persist && !(await navigator.storage.persisted())) {
            await navigator.storage.persist();
        }
    } catch { /* the quota manager may refuse */ }
    notifyChange();
    if (_queue.length) pump();
    return _rows;
}

function reindex() {
    _index = new Map(_rows.map((row) => [row.key, row]));
}

function notifyChange() {
    if (_onChange) {
        try { _onChange(downloadsSnapshot()); } catch { /* ignore */ }
    }
}

function upsert(row) {
    const index = _rows.findIndex((entry) => entry.key === row.key);
    if (index >= 0) _rows[index] = { ..._rows[index], ...row };
    else _rows.unshift(row);
    reindex();
    notifyChange();
    return _index.get(row.key);
}

export function downloadsSnapshot() {
    return _rows.slice();
}

export function totalBytes() {
    return _rows.reduce((sum, row) => sum + (Number(row.bytes) || 0), 0);
}

export async function refreshStorage() {
    try {
        if (!navigator.storage?.estimate) { _storage = null; return null; }
        const estimate = await navigator.storage.estimate();
        _storage = { usage: Number(estimate.usage) || 0, quota: Number(estimate.quota) || 0 };
    } catch {
        _storage = null;
    }
    return _storage;
}

export function storageInfo() {
    return _storage;
}

export function isPaused() {
    return _paused;
}

export function pauseReason() {
    return _pauseReason;
}

// ── local playback ────────────────────────────────────────────────────────

export function hasLocal(trackOrKey) {
    const key = typeof trackOrKey === 'string' ? trackOrKey : tkey(trackOrKey);
    return _index.get(key)?.status === 'done';
}

export function rowFor(trackOrKey) {
    const key = typeof trackOrKey === 'string' ? trackOrKey : tkey(trackOrKey);
    return _index.get(key) || null;
}

/** Object URL for a downloaded track (null when it is not on the device). */
export async function localAudio(trackOrKey) {
    const key = typeof trackOrKey === 'string' ? trackOrKey : tkey(trackOrKey);
    const row = _index.get(key);
    if (!row || row.status !== 'done') return null;
    const cached = _urls.get(key);
    if (cached) return cached;
    try {
        const blob = await dbGet(BLOBS, key);
        if (!blob?.audio) return null;
        const url = URL.createObjectURL(blob.audio);
        _urls.set(key, url);
        return url;
    } catch (error) {
        logWarn('Music offline blob missing', `${key}: ${error}`);
        return null;
    }
}

/** Object URL for a stored cover (falls back to the network in views). */
export async function localThumb(trackOrKey) {
    const key = typeof trackOrKey === 'string' ? trackOrKey : tkey(trackOrKey);
    const cacheKey = `thumb:${key}`;
    if (_urls.has(cacheKey)) return _urls.get(cacheKey);
    try {
        const blob = await dbGet(BLOBS, key);
        if (!blob?.thumb) return null;
        const url = URL.createObjectURL(blob.thumb);
        _urls.set(cacheKey, url);
        return url;
    } catch {
        return null;
    }
}

// ── auto-download setting ─────────────────────────────────────────────────

export function isAutoDownload() {
    try { return localStorage.getItem(AUTO_KEY) !== '0'; } catch { return true; }
}

export function setAutoDownload(on) {
    try { localStorage.setItem(AUTO_KEY, on ? '1' : '0'); } catch { /* private mode */ }
}

/** How many tracks are fetched at the same time (user setting, default 4). */
export function downloadParallelism() {
    try {
        const value = Number(localStorage.getItem(PARALLEL_KEY));
        return PARALLEL_CHOICES.includes(value) ? value : DEFAULT_PARALLEL;
    } catch {
        return DEFAULT_PARALLEL;
    }
}

export function setDownloadParallelism(value) {
    const next = Number(value);
    if (!PARALLEL_CHOICES.includes(next)) return downloadParallelism();
    try { localStorage.setItem(PARALLEL_KEY, String(next)); } catch { /* private mode */ }
    // Raising the limit takes effect immediately; lowering it lets running
    // tracks finish instead of cutting them off.
    pump();
    return next;
}

// ── playlist mirroring (Android parity) ───────────────────────────────────

/**
 * Bring the device in line with the playlists: download every track that is
 * missing and drop audio for tracks no longer in any playlist.
 *
 * The cleanup is skipped for an empty set — an empty document is usually a
 * failed load, and deleting the user's music over it would be irreversible.
 */
export function syncPlaylists(playlists) {
    if (!isAutoDownload()) return;
    const desired = new Map();
    (playlists || []).forEach((playlist) => {
        (playlist.tracks || []).forEach((track) => {
            if (track && (track.id || track.file_url)) desired.set(tkey(track), track);
        });
    });
    if (!desired.size) return;

    for (const row of _rows.slice()) {
        // Manually pinned downloads (a single track pulled from search) are not
        // playlists' business: the mirror must never delete them.
        if (row.kind === 'track' && !row.pinned && !desired.has(row.key)) {
            removeRow(row.key, { silent: true });
        }
    }
    desired.forEach((track, key) => {
        const row = _index.get(key);
        if (row && ['done', 'queued', 'downloading', 'waiting'].includes(row.status)) return;
        enqueue({ key, track, kind: 'track' });
    });
    pump();
}

/** Explicit download of one track (row menu). Kept even outside a playlist. */
export function downloadTrack(track) {
    const key = tkey(track);
    if (!key.trim() || !(track.id || track.file_url)) return false;
    enqueue({ key, track, kind: 'track' }, { pinned: true });
    pump();
    return true;
}

/** Explicit download of one podcast episode. */
export function downloadEpisode(track) {
    const key = tkey(track);
    if (!key.trim() || !(track.file_url || track.id)) return false;
    enqueue({ key, track: { ...track, is_episode: true }, kind: 'episode' }, { pinned: true });
    pump();
    return true;
}

function enqueue(item, { pinned = false } = {}) {
    const existing = _index.get(item.key);
    // A fresh request overrides a pending cancellation of the same key.
    _cancelled.delete(item.key);
    const row = {
        key: item.key,
        kind: item.kind,
        track: item.track,
        status: 'queued',
        bytes: existing?.bytes || 0,
        pinned: pinned || !!existing?.pinned,
        error: '',
        added_at: existing?.added_at || Date.now(),
    };
    upsert(row);
    dbPut(ROWS, row).catch((error) => logError('Music download row not saved', String(error)));
    if (!_queue.some((entry) => entry.key === item.key)) _queue.push(item);
}

async function pump() {
    if (_paused) return;
    const parallel = downloadParallelism();
    while (_inflight < parallel && _queue.length) {
        const item = _queue.shift();
        if (!_index.get(item.key)) continue;            // deleted while queued
        _inflight += 1;
        runDownload(item)
            .catch((error) => logError('Music download failed', String(error)))
            .finally(() => { _inflight -= 1; pump(); });
    }
    if (_inflight === 0 && _queue.length === 0) finishBatch();
}

async function quotaExceeded() {
    const storage = await refreshStorage();
    if (!storage?.quota) return false;
    return storage.usage / storage.quota > QUOTA_STOP;
}

async function runDownload({ key, track, kind }) {
    const row = _index.get(key);
    if (!row || _cancelled.has(key)) return;
    const src = downloadSrc(track);
    if (!src) {
        markFailed(key, 'Brak adresu utworu');
        return;
    }
    if (await quotaExceeded()) {
        _paused = true;
        _pauseReason = 'quota';
        upsert({ ...row, status: 'waiting', error: 'Brak miejsca na urządzeniu' });
        dbPut(ROWS, _index.get(key)).catch(() => {});
        if (!_quotaNotified) {
            _quotaNotified = true;
            notify({
                kind: 'warning',
                title: 'Muzyka offline',
                message: 'Pamięć urządzenia prawie pełna — pobieranie wstrzymane. Usuń coś w „Pobrane”.',
            });
        }
        notifyChange();
        return;
    }

    upsert({ ...row, status: 'downloading', error: '' });
    dbPut(ROWS, _index.get(key)).catch(() => {});
    ensureTask();
    _lastProgress.delete(key);
    const controller = new AbortController();
    const runId = Symbol(key);
    _controllers.set(key, controller);
    _runIds.set(key, runId);
    try {
        const { blob, type } = await fetchBlob(src, (done, total) => {
            if (_cancelled.has(key)) return;
            // A 4 MB track yields ~64 chunks; painting the badges on every one
            // of them keeps the main thread busy for no benefit.
            const now = Date.now();
            if (now - (_lastProgress.get(key) || 0) < PROGRESS_MS) return;
            _lastProgress.set(key, now);
            upsert({ ..._index.get(key), bytes: done, bytes_total: total || row.bytes_total || 0 });
        }, controller.signal);
        if (_cancelled.has(key)) return;          // deleted while the bytes streamed in
        // Audio first, cover later: the slot must be free for the next track
        // as soon as the bytes are stored, not after a thumbnail round-trip.
        await dbPut(BLOBS, { key, audio: blob, thumb: null, type });
        if (_cancelled.has(key)) return;          // deleted during the IDB write
        const done = upsert({
            ..._index.get(key),
            status: 'done',
            bytes: blob.size,
            bytes_total: blob.size,
            error: '',
            completed_at: Date.now(),
        });
        await dbPut(ROWS, done);
        _batchDone += 1;
        taskProgress();
        logInfo('Music download stored', `${key} ${blob.size} B`);
        storeCover(key, track).catch(() => {});
        await refreshStorage();
    } catch (error) {
        if (error?.name === 'AbortError' || _cancelled.has(key)) {
            logInfo('Music download cancelled', key);
            return;
        }
        const quota = error?.name === 'QuotaExceededError';
        if (quota) {
            _paused = true;
            _pauseReason = 'quota';
            upsert({ ..._index.get(key), status: 'waiting', error: 'Brak miejsca na urządzeniu' });
            notify({
                kind: 'warning',
                title: 'Muzyka offline',
                message: 'Pamięć urządzenia prawie pełna — pobieranie wstrzymane.',
            });
        } else {
            markFailed(key, error?.detail || error?.message || 'Pobieranie nie powiodło się');
        }
        dbPut(ROWS, _index.get(key)).catch(() => {});
        logError('Music download failed', `${key}: ${error?.detail || error?.message || error}`);
    } finally {
        if (_runIds.get(key) === runId) {
            _runIds.delete(key);
            _controllers.delete(key);
        }
    }
    notifyChange();
}

function markFailed(key, message) {
    const row = _index.get(key);
    if (!row) return;
    upsert({ ...row, status: 'failed', error: message });
    dbPut(ROWS, _index.get(key)).catch(() => {});
    notifyChange();
}

/** Covers are cosmetic: fetched after the track is usable, never in a slot. */
async function storeCover(key, track) {
    const art = artUrl(track);
    if (!art) return;
    try {
        const res = art.startsWith('/') ? await apiFetch(art) : await fetch(art, { mode: 'cors' });
        if (!res.ok) return;
        const thumb = await res.blob();
        const record = await dbGet(BLOBS, key);
        if (!record) return;                        // deleted meanwhile
        await dbPut(BLOBS, { ...record, thumb });
        const row = _index.get(key);
        if (row) upsert({ ...row, has_thumb: true });
    } catch { /* covers are optional */ }
}

async function fetchBlob(src, onProgress, signal) {
    const res = await apiFetch(src, { signal });
    if (!res.ok) {
        let detail = '';
        try {
            const payload = await res.clone().json();
            detail = payload?.detail || payload?.error || '';
        } catch { /* non-JSON error body */ }
        throw new Error(detail || `HTTP ${res.status}`);
    }
    const type = (res.headers.get('content-type') || 'audio/mpeg').split(';')[0];
    const total = Number(res.headers.get('content-length')) || 0;
    const chunks = [];
    let done = 0;
    if (res.body?.getReader) {
        const reader = res.body.getReader();
        for (;;) {
            const { value, done: finished } = await reader.read();
            if (finished) break;
            if (value) {
                chunks.push(value);
                done += value.length;
                onProgress(done, total);
            }
        }
    } else {
        const buffer = await res.arrayBuffer();
        chunks.push(new Uint8Array(buffer));
        done = buffer.byteLength;
        onProgress(done, total || done);
    }
    return { blob: new Blob(chunks, { type }), type };
}

function ensureTask() {
    if (_task) return;
    const pending = _rows.filter((row) => ['queued', 'downloading', 'waiting'].includes(row.status)).length;
    _batchDone = _rows.filter((row) => row.status === 'done').length;
    _batchTotal = Math.max(_batchDone + pending, 1);
    _task = startTask({
        appId: 'music',
        title: 'Muzyka na urządzeniu',
        total: _batchTotal,
        detail: 'Pobieranie',
        testIdPrefix: 'music-download',
    });
}

function taskProgress() {
    if (!_task) return;
    const percent = _batchTotal ? Math.min(99, (_batchDone / _batchTotal) * 100) : null;
    _task.update({ percent, detail: `${_batchDone}/${_batchTotal} na urządzeniu` });
}

function finishBatch() {
    if (!_task) return;
    _task.finish('done', { percent: 100, detail: 'Gotowe — gra bez sieci' });
    _task = null;
}

// ── delete / retry ────────────────────────────────────────────────────────

export async function deleteDownload(trackOrKey) {
    const key = typeof trackOrKey === 'string' ? trackOrKey : tkey(trackOrKey);
    await removeRow(key);
}

async function removeRow(key, { silent = false } = {}) {
    // Stop a transfer that is still running; without this the finished fetch
    // would re-insert the row a moment later.
    _cancelled.add(key);
    const controller = _controllers.get(key);
    if (controller) {
        try { controller.abort(); } catch { /* already finished */ }
    }
    _queue = _queue.filter((item) => item.key !== key);
    const index = _rows.findIndex((row) => row.key === key);
    if (index < 0) return;
    _rows.splice(index, 1);
    reindex();
    try { await dbDel(ROWS, key); } catch { /* ignore */ }
    try { await dbDel(BLOBS, key); } catch { /* ignore */ }
    for (const cacheKey of [key, `thumb:${key}`]) {
        const url = _urls.get(cacheKey);
        if (url) { try { URL.revokeObjectURL(url); } catch { /* ignore */ } _urls.delete(cacheKey); }
    }
    if (!silent) {
        notifyChange();
        await refreshStorage();
    }
}

export function retryDownload(trackOrKey) {
    const key = typeof trackOrKey === 'string' ? trackOrKey : tkey(trackOrKey);
    const row = _index.get(key);
    if (!row?.track) return;
    enqueue({ key, track: row.track, kind: row.kind });
    resumeDownloads();
    pump();
}

export async function clearAll() {
    for (const row of _rows.slice()) await removeRow(row.key, { silent: true });
    _rows = [];
    reindex();
    notifyChange();
    await refreshStorage();
}

export function resumeDownloads() {
    _paused = false;
    _pauseReason = '';
    _quotaNotified = false;
    for (const row of _rows) {
        if (row.status === 'waiting') upsert({ ...row, status: 'queued' });
    }
    _rows.filter((row) => row.status === 'queued').forEach((row) => {
        if (row.track && !_queue.some((item) => item.key === row.key)) {
            _queue.push({ key: row.key, track: row.track, kind: row.kind });
        }
    });
    pump();
}

// ── library snapshot + episode positions ──────────────────────────────────

export async function saveLibraryState(state) {
    try { await dbPut(META, { k: STATE_KEY, state, savedAt: Date.now() }); } catch { /* optional */ }
}

export async function loadLibraryState() {
    try {
        const row = await dbGet(META, STATE_KEY);
        return row?.state || null;
    } catch {
        return null;
    }
}

export async function savePosition(trackOrKey, seconds) {
    const key = typeof trackOrKey === 'string' ? trackOrKey : tkey(trackOrKey);
    try { await dbPut(META, { k: POSITION_PREFIX + key, pos: Number(seconds) || 0, at: Date.now() }); } catch { /* optional */ }
}

export async function loadPosition(trackOrKey) {
    const key = typeof trackOrKey === 'string' ? trackOrKey : tkey(trackOrKey);
    try {
        const row = await dbGet(META, POSITION_PREFIX + key);
        return Number(row?.pos) || 0;
    } catch {
        return 0;
    }
}
