// EthOS NAS — Background Fetch downloads for the standalone phone PWAs.
//
// A page-driven `fetch()` → `cache.put()` transfer dies when Android freezes
// the tab or iOS suspends it. Chromium can hand the transfer to the browser
// instead: `ServiceWorkerRegistration.backgroundFetch.fetch()` keeps running
// after the app is switched away (or closed), shows a system progress
// notification, and fires `backgroundfetchsuccess` in sw.js. The worker writes
// the bytes straight into the target store (Cache Storage for /videos and
// /books, IndexedDB `ethos-music-db` for /music) and records the outcome in a
// small shared database that this module reads on the next app start.
//
// Everything here is best-effort: every caller keeps its in-page transfer as
// the fallback, because iOS Safari and Firefox have no Background Fetch API.
//
// Wire format: the request URL carries `__ethos_bg_*` query parameters (the
// API ignores unknown query params). sw.js reads them to pick the target and
// refuses anything outside its allowlist.

import { getToken } from './api.js';
import { logWarn } from './logger.js';

const RESULTS_DB = 'ethos-bg-downloads';
const RESULTS_VERSION = 1;
const RESULTS_STORE = 'results';
const RESULT_TTL = 7 * 24 * 60 * 60 * 1000;   // stale markers are pruned on sweep
const DEFAULT_POLL_MS = 1000;
const DEFAULT_GRACE_MS = 4000;                // wait for the worker to write the result
const FIRST_BYTE_MS = 3000;                   // browser accepted the job but never started

function sleep(ms) {
    return new Promise((resolve) => { setTimeout(resolve, ms); });
}

function openResults() {
    return new Promise((resolve, reject) => {
        if (!('indexedDB' in window)) { reject(new Error('IndexedDB unavailable')); return; }
        const request = indexedDB.open(RESULTS_DB, RESULTS_VERSION);
        request.onupgradeneeded = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(RESULTS_STORE)) {
                db.createObjectStore(RESULTS_STORE, { keyPath: 'tag' });
            }
        };
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error || new Error('Background results store failed'));
    });
}

function resultsTx(mode, fn) {
    return openResults().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction(RESULTS_STORE, mode);
        const result = fn(tx.objectStore(RESULTS_STORE));
        tx.oncomplete = () => resolve(result?.result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
    }));
}

/** True when this browser can keep a download alive outside the page. */
export function backgroundDownloadSupported() {
    try {
        // Playwright and other automation expose the API but never run the
        // browser-managed transfer, and their request mocks only cover page
        // fetches. Keep e2e deterministic: test the in-page path there.
        if (navigator.webdriver === true) return false;
        return typeof window !== 'undefined'
            && window.isSecureContext === true
            && 'serviceWorker' in navigator
            && 'BackgroundFetchManager' in window
            && 'backgroundFetch' in (window.ServiceWorkerRegistration?.prototype || {});
    } catch {
        return false;
    }
}

/**
 * Hand one URL to the browser's Background Fetch manager.
 *
 * @param {object} opts
 * @param {string} opts.app        videos | books | music (tags + notification copy)
 * @param {string} opts.url        same-origin API/download URL
 * @param {string} [opts.cache]    Cache Storage bucket (videos/books)
 * @param {string} [opts.key]      final cache key (`/__media/<id>`, `/__books/<id>`)
 * @param {string} [opts.idb]      IndexedDB database (music blobs)
 * @param {string} [opts.store]    IndexedDB object store
 * @param {string} [opts.type]     fallback content type for the store record
 * @param {string} [opts.title]    notification title
 * @returns {Promise<string|null>} tag, or null when the browser refused
 */
export async function startBackgroundDownload({
    app, url, cache = '', key = '', idb = '', store = '', type = '', title = '',
} = {}) {
    if (!app || !url || !backgroundDownloadSupported()) return null;
    try {
        const target = new URL(url, location.origin);
        target.searchParams.set('__ethos_bg_app', app);
        target.searchParams.set('__ethos_bg_key', key);
        if (cache) target.searchParams.set('__ethos_bg_cache', cache);
        if (idb) {
            target.searchParams.set('__ethos_bg_idb', idb);
            target.searchParams.set('__ethos_bg_store', store);
        }
        if (type) target.searchParams.set('__ethos_bg_type', type);
        const headers = {};
        const token = getToken();
        if (token) headers.Authorization = `Bearer ${token}`;
        const request = new Request(target.href, { credentials: 'same-origin', headers });
        const registration = await navigator.serviceWorker.ready;
        if (!registration.backgroundFetch) return null;
        const tag = `ethos-${app}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
        await registration.backgroundFetch.fetch(tag, [request], {
            title: title || 'EthOS',
            icons: [{ src: `/static/icons/${app}-192.png`, sizes: '192x192', type: 'image/png' }],
        });
        return tag;
    } catch (error) {
        // Quota, unsupported request shape or a browser that lies about the
        // API: the caller falls back to its in-page transfer.
        logWarn('Background download not started', `${app}: ${error?.message || error}`);
        return null;
    }
}

/** Live counter of a running background fetch (`null` when it is finished). */
export async function backgroundDownloadState(tag) {
    if (!tag || !backgroundDownloadSupported()) return null;
    try {
        const registration = await navigator.serviceWorker.ready;
        const record = await registration.backgroundFetch.get(tag);
        if (!record) return null;
        return {
            downloaded: Number(record.downloaded) || 0,
            total: Number(record.downloadTotal) || 0,
        };
    } catch {
        return null;
    }
}

/** Stored outcome of a finished background fetch (does not consume it). */
export async function backgroundResult(tag, { waitMs = 0 } = {}) {
    if (!tag) return null;
    const deadline = Date.now() + Math.max(0, Number(waitMs) || 0);
    for (;;) {
        try {
            const record = await resultsTx('readonly', (store) => store.get(tag));
            if (record) return record;
        } catch (error) {
            logWarn('Background result not readable', String(error));
            return null;
        }
        if (Date.now() >= deadline) return null;
        await sleep(250);
    }
}

/** Drop one consumed result marker. */
export async function clearBackgroundResult(tag) {
    if (!tag) return;
    try { await resultsTx('readwrite', (store) => store.delete(tag)); } catch { /* best effort */ }
}

/**
 * Results written for one app while the page was away. Markers older than a
 * week are pruned; callers clear the returned ones after applying them.
 */
export async function sweepBackgroundResults(app) {
    let rows = [];
    try { rows = (await resultsTx('readonly', (store) => store.getAll())) || []; } catch { return []; }
    const now = Date.now();
    const own = [];
    for (const row of rows) {
        if (!row || typeof row !== 'object') continue;
        if (now - (Number(row.at) || 0) > RESULT_TTL) {
            clearBackgroundResult(row.tag).catch(() => {});
            continue;
        }
        if (row.app === app) own.push(row);
    }
    return own;
}

/**
 * Wait until a background fetch finishes and return its stored result.
 * The page may reload while the browser keeps transferring: the caller
 * reattaches by tag and this poll simply resumes where it left off.
 */
export async function waitBackgroundDownload(tag, onProgress, {
    pollMs = DEFAULT_POLL_MS, graceMs = DEFAULT_GRACE_MS, firstByteMs = FIRST_BYTE_MS,
} = {}) {
    if (!tag || !backgroundDownloadSupported()) return { ok: false, error: 'Background downloads unavailable' };
    let registration;
    try { registration = await navigator.serviceWorker.ready; } catch {
        return { ok: false, error: 'Service worker unavailable' };
    }
    const startedAt = Date.now();
    let firstByteAt = 0;
    for (;;) {
        let record = null;
        try { record = await registration.backgroundFetch.get(tag); } catch { record = null; }
        if (record) {
            const downloaded = Number(record.downloaded) || 0;
            const total = Number(record.downloadTotal) || 0;
            if (downloaded > 0 && !firstByteAt) firstByteAt = Date.now();
            try { onProgress?.({ downloaded, total }); } catch { /* render errors are local */ }
            // A browser may accept the registration and never start it (no
            // notification service, quota, killed job). Falling back to the
            // page beats waiting forever; real transfers move within seconds.
            if (!firstByteAt && Date.now() - startedAt > firstByteMs) {
                try { await record.abort(); } catch { /* already gone */ }
                return { ok: false, error: 'Background download did not start' };
            }
            await sleep(pollMs);
            continue;
        }
        // Finished or dropped: the worker writes (or already wrote) the marker.
        const stored = await backgroundResult(tag, { waitMs: graceMs });
        return stored || { ok: false, error: 'Background download ended without a result' };
    }
}

/** Ask the browser to stop a background fetch (user cancelled/deleted). */
export async function abortBackgroundDownload(tag) {
    if (!tag || !backgroundDownloadSupported()) return;
    try {
        const registration = await navigator.serviceWorker.ready;
        const record = await registration.backgroundFetch.get(tag);
        if (record) await record.abort();
    } catch (error) {
        logWarn('Background download not aborted', String(error));
    }
    await clearBackgroundResult(tag);
}
