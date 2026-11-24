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
import {
    backgroundDownloadSupported, startBackgroundDownload, waitBackgroundDownload,
    backgroundResult, backgroundDownloadState, clearBackgroundResult,
    abortBackgroundDownload, sweepBackgroundResults,
} from '../core/background-download.js';
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
// Background Fetch bookkeeping: tags by key, live reattached transfers, and
// keys whose browser-managed attempt failed (the next attempt transfers in-page).
const _bgTags = new Map();
const _bgWatchers = new Map();
const _bgFailed = new Set();
// A NAS restart kills in-flight transfers with a network error or 5xx. Those
// are not failures of the track: keep it queued and retry with a backoff until
// the server answers again.
const _retryTimers = new Map();
const _retryTries = new Map();

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
    // Results written by sw.js while the app was closed (Background Fetch
    // keeps running outside the page).
    let bgResults = [];
    try { bgResults = await sweepBackgroundResults('music'); } catch { bgResults = []; }
    const byTag = new Map(bgResults.map((result) => [result.tag, result]));
    const finished = [];
    const reattach = [];
    const touched = new Set();
    _rows = [];
    for (const row of rows) {
        const next = { ...row };
        if (next.status === 'downloading' && next.bg_tag) {
            const result = byTag.get(next.bg_tag) || await backgroundResult(next.bg_tag, { waitMs: 2000 });
            if (!result && await backgroundDownloadState(next.bg_tag)) {
                reattach.push(next);
                _rows.push(next);
                continue;
            }
            const tag = next.bg_tag;
            next.bg_tag = '';
            touched.add(next.key);
            const stored = result?.ok ? await dbGet(BLOBS, next.key).catch(() => null) : null;
            if (stored?.audio) {
                next.status = 'done';
                next.bytes = stored.audio.size;
                next.bytes_total = stored.audio.size;
                next.parts = [];
                next.error = '';
                next.completed_at = Number(next.completed_at) || Date.now();
                finished.push({ row: next, tag });
            } else {
                // The browser gave up (or its blob did not land) — fetch again.
                _bgFailed.add(next.key);
                next.status = 'queued';
                next.error = '';
            }
        } else if (next.status === 'downloading') {
            // A download interrupted by a reload has no partial file to resume
            // from; put it back in the queue so the next start picks it up again.
            next.status = 'queued';
            next.error = '';
            touched.add(next.key);
        }
        _rows.push(next);
    }
    _rows.sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
    reindex();
    for (const key of touched) {
        const row = _index.get(key);
        if (row) await dbPut(ROWS, row).catch(() => {});
    }
    for (const { row, tag } of finished) {
        clearBackgroundResult(tag).catch(() => {});
        if (row.track) storeCover(row.key, row.track).catch(() => {});
    }
    // Rows queued before the reload have no in-memory queue item yet.
    for (const row of _rows) {
        if (row.status === 'waiting' && row.retry && row.track) {
            upsert({ ...row, status: 'queued', error: '' });
            dbPut(ROWS, _index.get(row.key)).catch(() => {});
        }
        const current = _index.get(row.key);
        if (current && current.status === 'queued' && current.track
                && !_queue.some((item) => item.key === current.key)) {
            _queue.push({ key: current.key, track: current.track, kind: current.kind });
        }
    }
    await refreshStorage();
    try {
        if (navigator.storage?.persist && !(await navigator.storage.persisted())) {
            await navigator.storage.persist();
        }
    } catch { /* the quota manager may refuse */ }
    notifyChange();
    for (const row of reattach) resumeBackgroundRow(row);
    if (_queue.length && navigator.onLine !== false) pump();
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

/** True when at least one finished track is stored on the device. */
export function hasAnyDownloads() {
    return _rows.some((row) => row.status === 'done');
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
        // Partial chunks of an interrupted attempt stay valid: a retry resumes
        // from the last stored byte instead of fetching the track again.
        parts: existing?.parts || [],
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

function canBackgroundTransfer(row) {
    // Partial parts can only be resumed by Range requests in the page, and a
    // browser-managed attempt that already failed sends the retry in-page.
    return backgroundDownloadSupported()
        && !_bgFailed.has(row.key)
        && !(Array.isArray(row.parts) && row.parts.length)
        && !(Number(row.bytes) > 0);
}

function isTransient(error) {
    const status = Number(error?.status) || 0;
    if (status >= 400 && status < 500) return false;
    return true;   // network error, timeout, 5xx or a restarting NAS
}

function clearRetry(key) {
    const timer = _retryTimers.get(key);
    if (timer) clearTimeout(timer);
    _retryTimers.delete(key);
}

function scheduleRetry(key) {
    const row = _index.get(key);
    if (!row || _cancelled.has(key)) return;
    const attempt = (_retryTries.get(key) || 0) + 1;
    _retryTries.set(key, attempt);
    const delay = [5000, 15000, 30000, 60000, 120000][Math.min(attempt - 1, 4)];
    clearRetry(key);
    upsert({ ...row, status: 'waiting', retry: true, error: 'Serwer niedostępny — ponawiam…' });
    dbPut(ROWS, _index.get(key)).catch(() => {});
    _retryTimers.set(key, setTimeout(() => {
        _retryTimers.delete(key);
        const current = _index.get(key);
        if (!current || _cancelled.has(key)) return;
        if (navigator.onLine === false) { scheduleRetry(key); return; }
        upsert({ ...current, status: 'queued', error: '', retry: true });
        dbPut(ROWS, _index.get(key)).catch(() => {});
        _queue.push({ key, track: current.track, kind: current.kind });
        pump();
    }, delay));
}

/** Server is back (online event / app visible): retry without waiting. */
export function resumeServerErrors() {
    for (const row of _rows) {
        if (row.status !== 'waiting' || !row.retry) continue;
        clearRetry(row.key);
        upsert({ ...row, status: 'queued', error: '' });
        dbPut(ROWS, _index.get(row.key)).catch(() => {});
        if (row.track && !_queue.some((item) => item.key === row.key)) {
            _queue.push({ key: row.key, track: row.track, kind: row.kind });
        }
    }
    pump();
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
        if (_cancelled.has(key)) return;             // deleted while the check ran
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
    if (_cancelled.has(key)) return;              // deleted while the space check ran

    upsert({ ...row, status: 'downloading', error: '' });
    dbPut(ROWS, _index.get(key)).catch(() => {});
    ensureTask();
    _lastProgress.delete(key);
    const controller = new AbortController();
    const runId = Symbol(key);
    _controllers.set(key, controller);
    _runIds.set(key, runId);
    try {
        let blob = null;
        let type = 'audio/mpeg';
        let fromBackground = false;
        let bgTag = null;
        if (canBackgroundTransfer(row)) {
            bgTag = await startBackgroundDownload({
                app: 'music',
                url: src,
                idb: DB_NAME,
                store: BLOBS,
                key,
                type,
                title: `EthOS Music — ${track.title || 'Utwór'}`,
            });
        }
        if (bgTag) {
            _bgTags.set(key, bgTag);
            const started = _index.get(key);
            if (!started) {
                await abortBackgroundDownload(bgTag);
                _bgTags.delete(key);
                return;
            }
            upsert({ ...started, bg_tag: bgTag });
            await dbPut(ROWS, _index.get(key)).catch(() => {});
            const result = await waitBackgroundDownload(bgTag, ({ downloaded, total }) => {
                if (_cancelled.has(key)) return;
                const now = Date.now();
                if (now - (_lastProgress.get(key) || 0) < PROGRESS_MS) return;
                _lastProgress.set(key, now);
                upsert({
                    ..._index.get(key),
                    bytes: downloaded,
                    bytes_total: total || _index.get(key)?.bytes_total || 0,
                });
            });
            await clearBackgroundResult(bgTag);
            _bgTags.delete(key);
            bgTag = null;
            const current = _index.get(key);
            if (current) {
                upsert({ ...current, bg_tag: '' });
                await dbPut(ROWS, _index.get(key)).catch(() => {});
            }
            if (_cancelled.has(key)) return;
            if (result.ok) {
                // sw.js stored the audio record itself; only metadata is left.
                const stored = await dbGet(BLOBS, key).catch(() => null);
                if (stored?.audio) {
                    blob = stored.audio;
                    type = stored.type || type;
                    fromBackground = true;
                } else {
                    // Marker said ok but no blob landed — transfer in the page.
                    _bgFailed.add(key);
                }
            } else {
                // The browser gave up (quota, killed job): retry in the page.
                _bgFailed.add(key);
                logWarn('Music background download fell back', `${key}: ${result.error || ''}`);
            }
        }
        if (!fromBackground) {
            const fetched = await downloadAudio(key, src, controller.signal, (done, total) => {
                if (_cancelled.has(key)) return;
                // A 4 MB track yields ~64 chunks; painting the badges on every
                // one of them keeps the main thread busy for no benefit.
                const now = Date.now();
                if (now - (_lastProgress.get(key) || 0) < PROGRESS_MS) return;
                _lastProgress.set(key, now);
                upsert({ ..._index.get(key), bytes: done, bytes_total: total || _index.get(key)?.bytes_total || 0 });
            });
            blob = fetched.blob;
            type = fetched.type;
        }
        if (_cancelled.has(key)) return;          // deleted while the bytes streamed in
        // Audio first, cover later: the slot must be free for the next track
        // as soon as the bytes are stored, not after a thumbnail round-trip.
        if (!fromBackground) await dbPut(BLOBS, { key, audio: blob, thumb: null, type });
        if (_cancelled.has(key)) return;          // deleted during the IDB write
        clearRetry(key);
        _retryTries.delete(key);
        const done = upsert({
            ..._index.get(key),
            status: 'done',
            bytes: blob.size,
            bytes_total: blob.size,
            parts: [],
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
            });        } else if (isTransient(error)) {
            scheduleRetry(key);        } else {
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
    clearRetry(key);
    _retryTries.delete(key);
    upsert({ ...row, status: 'failed', retry: false, error: message });
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

// ── resumable transfer ────────────────────────────────────────────────────
//
// Partial audio is stored as one IndexedDB record per attempt
// (`<key>#part:<n>`) and the row keeps the part sizes, so a reload, a dropped
// connection or an expired YouTube URL continues with `Range: bytes=<done>-`
// instead of fetching the track again. Tracks are a few MB, so the parts are
// concatenated with `new Blob([...])` (references, no copy).

const PART_SUFFIX = '#part:';

function partKey(key, index) { return `${key}${PART_SUFFIX}${index}`; }

/** `<total>` from `Content-Range: bytes 100-199/4000000`. */
function contentRangeTotal(header) {
    const match = /^bytes\s+\d+-\d+\/(\d+)$/i.exec(String(header || '').trim());
    return match ? Number(match[1]) || 0 : 0;
}

/** Recorded parts that are still in IndexedDB and still contiguous (0..n-1). */
async function usableParts(key, recorded) {
    const list = Array.isArray(recorded) ? recorded : [];
    const out = [];
    for (const entry of list) {
        const index = Number(entry?.index);
        const bytes = Number(entry?.bytes);
        if (index !== out.length || !(bytes > 0)) break;   // no gaps allowed
        let record = null;
        try { record = await dbGet(BLOBS, partKey(key, index)); } catch { record = null; }
        if (!record?.audio) break;
        out.push({ index, bytes });
    }
    return out;
}

async function discardParts(key, parts) {
    for (const part of (Array.isArray(parts) ? parts : [])) {
        try { await dbDel(BLOBS, partKey(key, Number(part.index))); } catch { /* ignore */ }
    }
}

/** Concatenate the stored parts plus the freshly fetched tail into one blob. */
async function assembleAudio(key, parts, tail, type) {
    const blobs = [];
    for (const part of parts) {
        let record = null;
        try { record = await dbGet(BLOBS, partKey(key, part.index)); } catch { record = null; }
        if (record?.audio) blobs.push(record.audio);
    }
    if (tail) blobs.push(tail);
    return new Blob(blobs, { type });
}

/** Remember the bytes that did arrive, so the retry resumes exactly there. */
async function keepPartial(key, parts, blob, bytes, total, type) {
    const index = parts.length;
    await dbPut(BLOBS, { key: partKey(key, index), audio: blob, thumb: null, type });
    const next = [...parts, { index, bytes }];
    const row = _index.get(key);
    if (row) {
        upsert({
            ...row,
            parts: next,
            bytes: next.reduce((sum, part) => sum + part.bytes, 0),
            bytes_total: total || row.bytes_total || 0,
        });
        await dbPut(ROWS, _index.get(key));
    }
    return next;
}

/**
 * Download one track, resuming from the bytes already on disk. Up to three
 * attempts while progress is being made; a server that ignores `Range`
 * (or answers `416`) restarts the track cleanly.
 */
async function downloadAudio(key, src, signal, onProgress) {
    let parts = await usableParts(key, _index.get(key)?.parts);
    let lastError = null;
    let best = parts.reduce((sum, part) => sum + part.bytes, 0);
    for (let attempt = 0; attempt < 3; attempt += 1) {
        if (_cancelled.has(key)) throw new DOMException('Aborted', 'AbortError');
        const offset = parts.reduce((sum, part) => sum + part.bytes, 0);
        let result;
        try {
            result = await fetchBlob(src, onProgress, signal, offset);
        } catch (error) {
            if (error?.name === 'AbortError' || _cancelled.has(key)) throw error;
            if (offset > 0 && Number(error?.status) === 416) {
                // The upstream (or the resolved stream URL) changed size.
                await discardParts(key, parts);
                parts = [];
                lastError = error;
                continue;
            }
            if (error?.partial && error.partialBytes > 0) {
                try {
                    parts = await keepPartial(key, parts, error.partial, error.partialBytes,
                        error.total, error.type || 'audio/mpeg');
                } catch (storeError) {
                    logWarn('Music partial not saved', `${key}: ${storeError}`);
                }
            }
            lastError = error;
            const moved = parts.reduce((sum, part) => sum + part.bytes, 0);
            if (moved <= best) throw error;    // nothing new arrived — give up
            best = moved;
            continue;
        }
        if (offset > 0 && !result.ranged) {
            // A proxy dropped the Range header: this body is the whole file.
            await discardParts(key, parts);
            parts = [];
        }
        const written = offset > 0 && result.ranged ? offset + result.blob.size : result.blob.size;
        if (result.total && written < result.total) {
            // Stream closed early but the bytes are durable — retry and resume.
            parts = await keepPartial(key, parts, result.blob, result.blob.size,
                result.total, result.type);
            lastError = new Error('Przerwane pobieranie — wznawiam');
            best = parts.reduce((sum, part) => sum + part.bytes, 0);
            continue;
        }
        const blob = await assembleAudio(key, parts, result.blob, result.type);
        await discardParts(key, parts);
        return { blob, type: result.type, total: result.total };
    }
    throw lastError || new Error('Pobieranie nie powiodło się');
}

/**
 * Fetch audio from `offset` onwards. Both proxies (`/api/music/stream` and
 * `/api/music/episode-audio`) forward `Range`, so a 206 body starts exactly
 * where the stored parts end.
 */
async function fetchBlob(src, onProgress, signal, offset = 0) {
    const res = await apiFetch(src, {
        signal,
        ...(offset > 0 ? { headers: { Range: `bytes=${offset}-` } } : {}),
    });
    if (!res.ok) {
        let detail = '';
        try {
            const payload = await res.clone().json();
            detail = payload?.detail || payload?.error || '';
        } catch { /* non-JSON error body */ }
        const error = new Error(detail || `HTTP ${res.status}`);
        error.status = res.status;
        throw error;
    }
    const type = (res.headers.get('content-type') || 'audio/mpeg').split(';')[0];
    const ranged = res.status === 206;
    let total = ranged ? contentRangeTotal(res.headers.get('content-range')) : 0;
    if (!total) {
        const length = Number(res.headers.get('content-length')) || 0;
        if (length) total = ranged ? length + offset : length;
    }
    const chunks = [];
    let done = 0;
    try {
        if (res.body?.getReader) {
            const reader = res.body.getReader();
            for (;;) {
                const { value, done: finished } = await reader.read();
                if (finished) break;
                if (value) {
                    chunks.push(value);
                    done += value.length;
                    onProgress(offset + done, total);
                }
            }
        } else {
            const buffer = await res.arrayBuffer();
            chunks.push(new Uint8Array(buffer));
            done = buffer.byteLength;
            onProgress(offset + done, total || offset + done);
        }
    } catch (error) {
        // Bytes already received are worth keeping: the retry resumes from here.
        try {
            if (chunks.length) {
                error.partial = new Blob(chunks, { type });
                error.partialBytes = done;
            }
        } catch { /* ignore */ }
        error.type = type;
        error.total = total;
        throw error;
    }
    return { blob: new Blob(chunks, { type }), type, total, ranged };
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

function markBackgroundFailed(key) {
    const row = _index.get(key);
    if (!row) return;
    _bgFailed.add(key);
    upsert({ ...row, status: 'waiting', bg_tag: '', retry: true, error: 'Przerwane — ponawiam…' });
    dbPut(ROWS, _index.get(key)).catch(() => {});
    notifyChange();
    scheduleRetry(key);
}

/** Follow a Background Fetch that survived a reload or an app switch. */
function resumeBackgroundRow(row) {
    if (_bgWatchers.has(row.key) || !row.bg_tag) return;
    const tag = row.bg_tag;
    const run = (async () => {
        const result = await waitBackgroundDownload(tag, ({ downloaded, total }) => {
            const current = _index.get(row.key);
            if (!current) return;
            upsert({ ...current, bytes: downloaded, bytes_total: total || current.bytes_total || 0 });
            if (_task) {
                const percent = total ? Math.min(99, (downloaded / total) * 100) : 0;
                _task.update({ percent, detail: total ? `${Math.round(percent)}%` : '' });
            }
        });
        await clearBackgroundResult(tag);
        const current = _index.get(row.key);
        if (!current) return;
        if (!result.ok) {
            _bgFailed.add(row.key);
            markBackgroundFailed(row.key);
            return;
        }
        const stored = await dbGet(BLOBS, row.key).catch(() => null);
        if (!stored?.audio) {
            _bgFailed.add(row.key);
            markBackgroundFailed(row.key);
            return;
        }
        const done = upsert({
            ..._index.get(row.key),
            status: 'done',
            bytes: stored.audio.size,
            bytes_total: stored.audio.size,
            parts: [],
            error: '',
            bg_tag: '',
            completed_at: Date.now(),
        });
        await dbPut(ROWS, done);
        _batchDone += 1;
        taskProgress();
        logInfo('Music download stored', `${row.key} ${stored.audio.size} B`);
        storeCover(row.key, current.track).catch(() => {});
        await refreshStorage();
        notifyChange();
    })().catch((error) => {
        logWarn('Music background download reattach failed', `${row.key}: ${error?.message || error}`);
        _bgFailed.add(row.key);
        markBackgroundFailed(row.key);
    }).finally(() => { _bgWatchers.delete(row.key); });
    _bgWatchers.set(row.key, run);
}

// ── delete / retry ────────────────────────────────────────────────────────

export async function deleteDownload(trackOrKey) {
    const key = typeof trackOrKey === 'string' ? trackOrKey : tkey(trackOrKey);
    await removeRow(key);
}

async function removeRow(key, { silent = false } = {}) {
    // Stop a transfer that is still running; without this the finished fetch
    // would re-insert the row a moment later. A Background Fetch keeps running
    // in the browser, so it must be aborted explicitly as well.
    _cancelled.add(key);
    clearRetry(key);
    _retryTries.delete(key);
    const controller = _controllers.get(key);
    if (controller) {
        try { controller.abort(); } catch { /* already finished */ }
    }
    const bgTag = _bgTags.get(key) || _index.get(key)?.bg_tag;
    if (bgTag) {
        _bgTags.delete(key);
        abortBackgroundDownload(bgTag).catch(() => {});
        clearBackgroundResult(bgTag).catch(() => {});
    }
    _bgFailed.delete(key);
    _queue = _queue.filter((item) => item.key !== key);
    const index = _rows.findIndex((row) => row.key === key);
    if (index < 0) return;
    const [removed] = _rows.splice(index, 1);
    reindex();
    try { await dbDel(ROWS, key); } catch { /* ignore */ }
    try { await discardParts(key, removed?.parts); } catch { /* ignore */ }
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
    clearRetry(key);
    _retryTries.delete(key);
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
        if (row.status === 'waiting') {
            clearRetry(row.key);
            upsert({ ...row, status: 'queued', retry: false, error: '' });
        }
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
