// EthOS Books — device downloads, covers, library snapshot and progress outbox.
//
// Two stores, like /videos:
//
//   • Cache Storage `ethos-books-v1` — the real bytes (audiobook tracks, EPUB,
//     PDF), keys `/__books/<bookId>` and `/__books/<bookId>/t<index>`. Files are
//     streamed into the cache (`res.body.pipeThrough`) so a 1 GB audiobook
//     never sits in phone RAM as one Blob; `sw.js` answers those keys with
//     Range support.
//   • IndexedDB `ethos-books-db` — small metadata: download rows, cover blobs,
//     the last library snapshot (so the app opens offline) and the pending
//     progress writes.
//
// Downloads are queued one at a time; a full device pauses the queue with a
// visible reason instead of failing every file. All feedback goes through the
// shared Notification Center and progress panel — this module never builds its
// own toast.

import { apiFetch } from '../core/api.js';
import { startTask } from '../core/progress.js';
import { notify } from '../core/notification-center.js';
import { logError, logInfo, logWarn } from '../core/logger.js';
import {
    baseName, bookTracks, coverPath, fmtBytes, isAudiobook, localKey, putProgress, rawPath,
} from './api.js';

const DB_NAME = 'ethos-books-db';
const DB_VERSION = 1;
const ROWS = 'rows';
const BLOBS = 'blobs';
const META = 'meta';
export const CACHE = 'ethos-books-v1';
const CONCURRENCY = 1;
const QUOTA_STOP = 0.92;
const PROGRESS_PREFIX = 'progress:';
const FLUSH_DELAY = 2500;

let _db = null;
let _rows = [];
let _index = new Map();
let _queue = [];
let _inflight = 0;
let _paused = false;
let _pauseReason = '';
let _onChange = null;
let _storage = null;
let _covers = new Map();
let _task = null;
let _batchDone = 0;
let _batchTotal = 0;
let _quotaNotified = false;
let _flushTimer = null;
let _flushing = false;

// ── IndexedDB ─────────────────────────────────────────────────────────────

function openDb() {
    if (_db) return Promise.resolve(_db);
    return new Promise((resolve, reject) => {
        if (!('indexedDB' in window)) { reject(new Error('IndexedDB niedostępne')); return; }
        const request = indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(ROWS)) db.createObjectStore(ROWS, { keyPath: 'key' });
            if (!db.objectStoreNames.contains(BLOBS)) db.createObjectStore(BLOBS, { keyPath: 'key' });
            if (!db.objectStoreNames.contains(META)) db.createObjectStore(META, { keyPath: 'k' });
        };
        request.onsuccess = () => { _db = request.result; resolve(_db); };
        request.onerror = () => reject(request.error || new Error('IndexedDB nie otworzyło się'));
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

function rowKey(bookId, trackIndex) {
    return trackIndex == null ? `${bookId}:file` : `${bookId}:t${Number(trackIndex)}`;
}

// ── init ──────────────────────────────────────────────────────────────────

export async function initOffline(onChange) {
    _onChange = typeof onChange === 'function' ? onChange : null;
    let rows = [];
    try { rows = await dbAll(ROWS); } catch (error) { logWarn('Books offline store unavailable', String(error)); }
    _rows = rows.map((row) => {
        const next = { ...row };
        // An interrupted transfer cannot resume byte-for-byte; ask for a retry.
        if (next.status === 'downloading' || next.status === 'queued') {
            next.status = 'failed';
            next.error = 'Przerwane — pobierz ponownie';
        }
        return next;
    }).sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
    for (const row of _rows) {
        if (row.status === 'failed') await dbPut(ROWS, row).catch(() => {});
    }
    reindex();
    await refreshStorage();
    try {
        if (navigator.storage?.persist && !(await navigator.storage.persisted())) {
            await navigator.storage.persist();
        }
    } catch { /* the quota manager may refuse; downloads still try */ }
    notifyChange();
    return _rows;
}

function reindex() {
    _index = new Map(_rows.map((row) => [row.key, row]));
}

function notifyChange() {
    if (!_onChange) return;
    try { _onChange(snapshot()); } catch { /* a render error must not break storage */ }
}

function upsert(row) {
    const position = _rows.findIndex((entry) => entry.key === row.key);
    if (position >= 0) _rows[position] = { ..._rows[position], ...row };
    else _rows.unshift(row);
    reindex();
    notifyChange();
    return _index.get(row.key);
}

export function snapshot() {
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

// ── downloaded lookups ────────────────────────────────────────────────────

export function hasTrack(bookId, trackIndex) {
    return _index.get(rowKey(bookId, trackIndex))?.status === 'done';
}

export function hasFile(bookId) {
    return _index.get(rowKey(bookId, null))?.status === 'done';
}

export function rowFor(bookId, trackIndex = null) {
    return _index.get(rowKey(bookId, trackIndex)) || null;
}

export function tracksDone(book) {
    const tracks = bookTracks(book);
    return tracks.filter((_track, index) => hasTrack(book.id, index)).length;
}

export function isDownloaded(book) {
    if (!book?.id) return false;
    if (isAudiobook(book)) {
        const tracks = bookTracks(book);
        return tracks.length > 0 && tracks.every((_track, index) => hasTrack(book.id, index));
    }
    return hasFile(book.id);
}

export function downloadPercent(book) {
    if (!book?.id) return 0;
    if (isAudiobook(book)) {
        const tracks = bookTracks(book);
        if (!tracks.length) return 0;
        let done = 0;
        let total = 0;
        tracks.forEach((track, index) => {
            total += Number(track.size) || 0;
            const row = rowFor(book.id, index);
            if (row?.status === 'done') done += Number(row.bytes) || Number(track.size) || 0;
            else if (row) done += Number(row.bytes) || 0;
        });
        return total ? Math.min(100, Math.round((done / total) * 100)) : (isDownloaded(book) ? 100 : 0);
    }
    const row = rowFor(book.id, null);
    if (!row) return 0;
    if (row.status === 'done') return 100;
    return row.bytes_total ? Math.min(99, Math.round((row.bytes / row.bytes_total) * 100)) : 0;
}

/** Playback URL for one track: the downloaded copy when it exists. */
export function trackSource(book, index) {
    return hasTrack(book.id, index) ? localKey(book.id, index) : '';
}

// ── covers ────────────────────────────────────────────────────────────────

/** Object URL for a cover (IndexedDB first, then the package proxy). */
export async function coverUrl(book, { fetch = true } = {}) {
    if (!book?.id) return '';
    const cached = _covers.get(book.id);
    if (cached) return cached;
    try {
        const stored = await dbGet(BLOBS, `cover:${book.id}`);
        if (stored?.cover) {
            const url = URL.createObjectURL(stored.cover);
            _covers.set(book.id, url);
            return url;
        }
    } catch { /* cover cache is optional */ }
    if (!fetch || !book.cover) return '';
    try {
        const res = await apiFetch(coverPath(book.id));
        if (!res.ok) return '';
        const blob = await res.blob();
        await dbPut(BLOBS, { key: `cover:${book.id}`, cover: blob }).catch(() => {});
        const url = URL.createObjectURL(blob);
        _covers.set(book.id, url);
        return url;
    } catch (error) {
        logWarn('Books cover unavailable', `${book.id}: ${error}`);
        return '';
    }
}

// ── downloads ─────────────────────────────────────────────────────────────

/** Queue every missing file of a title: one file for EPUB/PDF, every track for audio. */
export function downloadBook(book) {
    const items = [];
    if (isAudiobook(book)) {
        bookTracks(book).forEach((track, index) => {
            if (hasTrack(book.id, index)) return;
            items.push({ book, trackIndex: index, path: track.path, title: baseName(track.path), size: track.size });
        });
    } else if (!hasFile(book.id)) {
        items.push({ book, trackIndex: null, path: book.path, title: baseName(book.path), size: book.size });
    }
    if (!items.length) return false;
    for (const item of items) enqueue(item);
    pump();
    return true;
}

function enqueue(item) {
    const key = rowKey(item.book.id, item.trackIndex);
    const existing = _index.get(key);
    if (existing?.status === 'downloading' || existing?.status === 'queued') return;
    const row = {
        key,
        bookId: item.book.id,
        trackIndex: item.trackIndex,
        kind: item.trackIndex == null && !isAudiobook(item.book) ? 'book' : 'audio',
        book: item.book,
        title: item.title,
        status: 'queued',
        bytes: 0,
        bytes_total: Number(item.size) || 0,
        error: '',
        added_at: existing?.added_at || Date.now(),
    };
    upsert(row);
    dbPut(ROWS, row).catch((error) => logError('Books download row not saved', String(error)));
    if (!_queue.some((entry) => entry.key === key)) _queue.push({ ...item, key });
}

async function pump() {
    if (_paused) return;
    while (_inflight < CONCURRENCY && _queue.length) {
        const item = _queue.shift();
        if (!_index.get(item.key)) continue;      // removed while queued
        _inflight += 1;
        runDownload(item)
            .catch((error) => logError('Books download failed', String(error)))
            .finally(() => { _inflight -= 1; pump(); });
    }
    if (_inflight === 0 && _queue.length === 0) finishBatch();
}

async function quotaExceeded() {
    const storage = await refreshStorage();
    if (!storage?.quota) return false;
    return storage.usage / storage.quota > QUOTA_STOP;
}

function bytesText(done, total) {
    return total ? `${fmtBytes(done)} / ${fmtBytes(total)}` : fmtBytes(done);
}

async function runDownload(item) {
    if (!('caches' in window)) {
        failRow(item.key, 'Pobieranie wymaga HTTPS (secure context)');
        return;
    }
    const row = _index.get(item.key);
    if (!row) return;
    if (await quotaExceeded()) {
        _paused = true;
        _pauseReason = 'quota';
        upsert({ ...row, status: 'waiting', error: 'Brak miejsca na urządzeniu' });
        dbPut(ROWS, _index.get(item.key)).catch(() => {});
        if (!_quotaNotified) {
            _quotaNotified = true;
            notify({
                kind: 'warning',
                title: 'Books offline',
                message: 'Pamięć telefonu prawie pełna — pobieranie wstrzymane. Usuń coś w „Pobrane”.',
            });
        }
        notifyChange();
        return;
    }

    upsert({ ...row, status: 'downloading', error: '' });
    dbPut(ROWS, _index.get(item.key)).catch(() => {});
    ensureTask();
    const key = localKey(item.book.id, item.trackIndex);
    let cache = null;
    try {
        cache = await caches.open(CACHE);
        const res = await apiFetch(rawPath(item.path));
        if (!res.ok) throw new Error(await errorDetail(res));
        const total = Number(res.headers.get('content-length')) || Number(item.size) || 0;
        const type = (res.headers.get('content-type') || 'application/octet-stream').split(';')[0];
        let done = 0;
        let lastReport = 0;
        const report = () => {
            const percent = total ? Math.min(99.5, (done / total) * 100) : 0;
            upsert({ ..._index.get(item.key), bytes: done, bytes_total: total });
            _task?.update({ percent, detail: bytesText(done, total) });
        };
        const headers = { 'Content-Type': type };
        if (total) headers['Content-Length'] = String(total);
        if (res.body && typeof TransformStream !== 'undefined') {
            const counter = new TransformStream({
                transform(chunk, controller) {
                    done += chunk.byteLength;
                    const now = Date.now();
                    if (now - lastReport > 250) { lastReport = now; report(); }
                    controller.enqueue(chunk);
                },
            }, undefined, { highWaterMark: 1 });
            await cache.put(new Request(key), new Response(res.body.pipeThrough(counter), { headers }));
        } else {
            const buffer = await res.arrayBuffer();
            done = buffer.byteLength;
            report();
            await cache.put(new Request(key), new Response(buffer, { headers }));
        }
        const stored = await cache.match(new Request(key));
        if (!stored) throw new Error('Nie udało się zapisać pliku na urządzeniu');
        const size = Number(stored.headers.get('content-length')) || done || Number(item.size) || 0;
        const finished = upsert({
            ..._index.get(item.key),
            status: 'done',
            bytes: size,
            bytes_total: size,
            error: '',
            completed_at: Date.now(),
        });
        await dbPut(ROWS, finished);
        _batchDone += 1;
        taskProgress();
        logInfo('Books download stored', `${key} ${size} B`);
        await refreshStorage();
        // Cache the cover once, so offline cards keep their artwork.
        if (item.trackIndex == null) await coverUrl(item.book).catch(() => '');
    } catch (error) {
        const quota = error?.name === 'QuotaExceededError';
        if (quota) {
            _paused = true;
            _pauseReason = 'quota';
            upsert({ ..._index.get(item.key), status: 'waiting', error: 'Brak miejsca na urządzeniu' });
            notify({
                kind: 'warning',
                title: 'Books offline',
                message: 'Pamięć telefonu prawie pełna — pobieranie wstrzymane.',
            });
        } else {
            failRow(item.key, error?.detail || error?.message || 'Pobieranie nie powiodło się');
        }
        try { await cache?.delete(new Request(key)); } catch { /* nothing stored */ }
        dbPut(ROWS, _index.get(item.key)).catch(() => {});
        logError('Books download failed', `${key}: ${error?.detail || error?.message || error}`);
    }
    notifyChange();
}

async function errorDetail(res) {
    try {
        const payload = await res.clone().json();
        return payload?.detail || payload?.error || `HTTP ${res.status}`;
    } catch {
        return `HTTP ${res.status}`;
    }
}

function failRow(key, message) {
    const row = _index.get(key);
    if (!row) return;
    upsert({ ...row, status: 'failed', error: message });
    dbPut(ROWS, _index.get(key)).catch(() => {});
    notifyChange();
}

function ensureTask() {
    if (_task) return;
    const pending = _rows.filter((row) => ['queued', 'downloading', 'waiting'].includes(row.status)).length;
    _batchDone = _rows.filter((row) => row.status === 'done').length;
    _batchTotal = Math.max(_batchDone + pending, 1);
    _task = startTask({
        appId: 'books',
        title: 'Książki na urządzeniu',
        total: _batchTotal,
        detail: 'Pobieranie',
        testIdPrefix: 'books-download',
    });
}

function taskProgress() {
    if (!_task) return;
    const percent = _batchTotal ? Math.min(99, (_batchDone / _batchTotal) * 100) : null;
    _task.update({ percent, detail: `${_batchDone}/${_batchTotal} na urządzeniu` });
}

function finishBatch() {
    if (!_task) return;
    _task.finish('done', { percent: 100, detail: 'Gotowe — dostępne bez sieci' });
    _task = null;
}

// ── delete / retry ────────────────────────────────────────────────────────

export async function deleteBook(book) {
    const bookId = book?.id;
    if (!bookId) return;
    const keys = _rows.filter((row) => row.bookId === bookId).map((row) => row.key);
    for (const key of keys) await removeRow(key, { silent: true });
    _rows = _rows.filter((row) => row.bookId !== bookId);
    reindex();
    notifyChange();
    await refreshStorage();
}

export async function deleteRow(key) {
    await removeRow(key, { silent: false });
    await refreshStorage();
}

async function removeRow(key, { silent = false } = {}) {
    _queue = _queue.filter((item) => item.key !== key);
    const row = _index.get(key);
    if (row) {
        try {
            const cache = await caches.open(CACHE);
            await cache.delete(new Request(localKey(row.bookId, row.trackIndex)));
        } catch { /* cache may be gone */ }
    }
    const position = _rows.findIndex((entry) => entry.key === key);
    if (position >= 0) _rows.splice(position, 1);
    reindex();
    try { await dbDel(ROWS, key); } catch { /* ignore */ }
    if (!silent) notifyChange();
}

export function retryRow(key) {
    const row = _index.get(key);
    if (!row?.book) return;
    enqueue({
        book: row.book,
        trackIndex: row.trackIndex,
        path: row.trackIndex == null ? row.book.path : bookTracks(row.book)[row.trackIndex]?.path,
        title: row.title,
        size: row.bytes_total,
    });
    resumeDownloads();
    pump();
}

export function resumeDownloads() {
    _paused = false;
    _pauseReason = '';
    _quotaNotified = false;
    for (const row of _rows) {
        if (row.status === 'waiting') upsert({ ...row, status: 'queued' });
    }
    _rows.filter((row) => row.status === 'queued').forEach((row) => {
        if (!_queue.some((item) => item.key === row.key)) {
            const path = row.trackIndex == null
                ? row.book?.path
                : bookTracks(row.book)[row.trackIndex]?.path;
            if (path) _queue.push({ key: row.key, book: row.book, trackIndex: row.trackIndex, path, title: row.title, size: row.bytes_total });
        }
    });
    pump();
}

// ── library snapshot ──────────────────────────────────────────────────────

export async function saveLibrary(state) {
    if (!state) return;
    try { await dbPut(META, { k: 'library', state: { ...state, savedAt: Date.now() } }); } catch { /* optional */ }
}

export async function loadLibrary() {
    try {
        const row = await dbGet(META, 'library');
        return row?.state || null;
    } catch {
        return null;
    }
}

// ── progress outbox ───────────────────────────────────────────────────────

/**
 * Record a reading/playback position. Written locally first (so a locked phone
 * never loses it), then PUT to the package service when the NAS is reachable.
 * The newest write per book wins.
 */
export function queueProgress(bookId, payload) {
    if (!bookId || !payload) return;
    const entry = { k: `${PROGRESS_PREFIX}${bookId}`, bookId, payload, at: Date.now() };
    dbPut(META, entry).catch((error) => logWarn('Books progress not queued', String(error)));
    if (_flushTimer) clearTimeout(_flushTimer);
    _flushTimer = setTimeout(() => { flushProgress(); }, FLUSH_DELAY);
}

export function pendingProgress(bookId) {
    return dbGet(META, `${PROGRESS_PREFIX}${bookId}`).catch(() => null);
}

/** Push every queued position; 4xx entries are dropped, network errors retried. */
export async function flushProgress() {
    if (_flushing) return;
    if (_flushTimer) { clearTimeout(_flushTimer); _flushTimer = null; }
    if (!navigator.onLine) return;
    _flushing = true;
    try {
        const entries = await dbAll(META).catch(() => []);
        for (const entry of entries) {
            if (!entry?.k?.startsWith(PROGRESS_PREFIX)) continue;
            try {
                await putProgress(entry.bookId, entry.payload);
                await dbDel(META, entry.k);
            } catch (error) {
                if (Number(error?.status) >= 400 && Number(error?.status) < 500) {
                    logWarn('Books progress rejected', `${entry.bookId}: ${error?.detail || error?.message}`);
                    await dbDel(META, entry.k).catch(() => {});
                } else {
                    logWarn('Books progress not sent yet', String(error?.message || error));
                    break;
                }
            }
        }
    } finally {
        _flushing = false;
    }
}

// ── helpers used by the views ─────────────────────────────────────────────

export function cleanObjectUrls() {
    for (const url of _covers.values()) {
        try { URL.revokeObjectURL(url); } catch { /* already gone */ }
    }
    _covers = new Map();
}
