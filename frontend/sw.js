/* EthOS NAS — service worker (TODO.md P3-14).
 *
 * Deliberately conservative: this app ships unhashed ES modules and relies on
 * `Cache-Control: no-cache` revalidation (see `frontend_cache_middleware`), so a
 * cache-first worker would happily serve last week's code. Strategy here is
 * NETWORK-FIRST for everything it touches:
 *
 *   • online  — the network answer wins (same bytes as without the worker),
 *               and the response is stored for the offline case;
 *   • offline — the cached copy answers; navigations fall back to the shell so
 *               the user sees EthOS instead of a browser error page.
 *
 * Never touched: non-GET requests, other origins (Tailwind/xterm CDNs), `/api/*`,
 * websockets, `Range` requests (media streaming), and any URL carrying a token
 * in the query string.
 *
 * Offline downloads are the one deliberate exception: `/__media/<id>` is served
 * from the dedicated `ethos-media-v1` cache (written by /videos), including
 * synthesised 206 responses for Range requests. `/__books/<id>` does the same
 * for the Books PWA (audiobooks, EPUB, PDF) from `ethos-books-v1`. Those caches
 * are never pruned by a shell update.
 *
 * Bump CACHE_VERSION when the shell URLs change; old caches are pruned on
 * activate.
 */

const CACHE_VERSION = 'ethos-shell-v10';
const MEDIA_CACHE = 'ethos-media-v1';
const MEDIA_PREFIX = '/__media/';
const BOOKS_CACHE = 'ethos-books-v1';
const BOOKS_PREFIX = '/__books/';

// Background Fetch results. The page hands a download to the browser
// (core/background-download.js); on `backgroundfetchsuccess` this worker writes
// the bytes into the target store and records the outcome here, so an app that
// was closed (or killed) can reconcile its rows on the next start.
const BG_RESULTS_DB = 'ethos-bg-downloads';
const BG_RESULTS_STORE = 'results';
const BG_RESULT_CAP = 200;
const BG_CACHES = new Set([MEDIA_CACHE, BOOKS_CACHE]);
const BG_IDB = new Map([['ethos-music-db', new Set(['blobs'])]]);
const BG_PATHS = { videos: '/videos', books: '/books', music: '/music' };
const SHELL_URLS = [
    '/',
    '/manifest.webmanifest',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
    // Sticky Notes PWA: the standalone capture page and its shell. The module
    // graph below `/static/notes/` is cached on first visit by the fetch
    // handler, so an installed PWA opens offline after one online load.
    '/notes',
    '/notes/',
    '/manifest-notes.webmanifest',
    '/static/css/tokens.css',
    '/static/css/notes.css',
    '/static/notes/notes.js',
    '/static/icons/notes-192.png',
    '/static/icons/notes-512.png',
    '/static/icons/notes-maskable-512.png',
    '/static/icons/notes-apple-touch-icon.png',
    // Videos PWA (offline library + downloads).
    '/videos',
    '/videos/',
    '/manifest-videos.webmanifest',
    '/static/css/videos.css',
    '/static/videos/videos.js',
    '/static/videos/offline.js',
    '/static/vendor/hls.min.js',
    '/static/icons/videos-192.png',
    '/static/icons/videos-512.png',
    '/static/icons/videos-maskable-512.png',
    '/static/icons/videos-apple-touch-icon.png',
    // Music PWA (the phone player; the APK channel is retired). Its own module
    // graph below /static/music/ is cached on first visit by the fetch handler.
    '/music',
    '/music/',
    '/manifest-music.webmanifest',
    '/static/css/music.css',
    '/static/music/api.js',
    '/static/music/offline.js',
    '/static/music/player.js',
    '/static/music/music.js',
    // Core modules the standalone music page imports. The desktop shell is
    // never loaded there, so without these an offline start serves the page but
    // no script runs (the module graph would only be cached after a controlled
    // visit, and the first visit happens before the worker takes over).
    '/static/core/api.js',
    '/static/core/logger.js',
    '/static/core/error-reporter.js',
    '/static/core/background-download.js',
    '/static/core/notification-center.js',
    '/static/core/progress.js',
    '/static/css/progress.css',
    '/static/core/utils.js',
    '/static/core/icons.js',
    '/static/core/poller.js',
    '/static/core/ws.js',
    '/static/core/window-manager.js',
    '/static/core/modal.js',
    '/static/core/app-icons.js',
    '/static/icons/music-192.png',
    '/static/icons/music-512.png',
    '/static/icons/music-maskable-512.png',
    '/static/icons/music-apple-touch-icon.png',
    // Books PWA (audiobooks in the background, EPUB/PDF, offline downloads).
    // The readers are vendored in the core so an installed app can open a
    // downloaded book with no network at all.
    '/books',
    '/books/',
    '/manifest-books.webmanifest',
    '/static/css/books.css',
    '/static/books/api.js',
    '/static/books/offline.js',
    '/static/books/player.js',
    '/static/books/epub.js',
    '/static/books/pdf.js',
    '/static/books/books.js',
    '/static/vendor/jszip.min.js',
    '/static/vendor/epub.min.js',
    '/static/vendor/pdf.min.js',
    '/static/vendor/pdf.worker.min.js',
    '/static/icons/books-192.png',
    '/static/icons/books-512.png',
    '/static/icons/books-maskable-512.png',
    '/static/icons/books-apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE_VERSION);
        // Individual adds so one failure (e.g. offline at install time) does not
        // abort the whole precache.
        await Promise.all(SHELL_URLS.map(async (url) => {
            try { await cache.add(new Request(url, { cache: 'reload' })); } catch (_) { /* optional */ }
        }));
        await self.skipWaiting();
    })());
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const names = await caches.keys();
        // Keep the offline-download buckets: wiping them on every shell update
        // would silently delete the user's downloaded films and books.
        const keep = new Set([CACHE_VERSION, MEDIA_CACHE, BOOKS_CACHE]);
        await Promise.all(names.filter((n) => !keep.has(n)).map((n) => caches.delete(n)));
        await self.clients.claim();
    })());
});

function isCacheable(request, url) {
    if (request.method !== 'GET') return false;
    if (url.origin !== self.location.origin) return false;
    if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/ws')) return false;
    if (url.searchParams.has('token')) return false;   // media/download links
    if (request.headers.has('range')) return false;    // partial content
    return true;
}

self.addEventListener('fetch', (event) => {
    const request = event.request;
    const url = new URL(request.url);
    if (url.origin === self.location.origin && url.pathname.startsWith(MEDIA_PREFIX)
            && (request.method === 'GET' || request.method === 'HEAD')) {
        event.respondWith(serveMedia(request, url, MEDIA_CACHE, 'video/mp4'));
        return;
    }
    if (url.origin === self.location.origin && url.pathname.startsWith(BOOKS_PREFIX)
            && (request.method === 'GET' || request.method === 'HEAD')) {
        event.respondWith(serveMedia(request, url, BOOKS_CACHE, 'application/octet-stream'));
        return;
    }
    if (!isCacheable(request, url)) return;             // let the browser handle it

    event.respondWith((async () => {
        try {
            const response = await fetch(request);
            // Only store complete, successful, same-origin responses.
            if (response && response.ok && response.type === 'basic') {
                const copy = response.clone();
                caches.open(CACHE_VERSION).then((cache) => cache.put(request, copy)).catch(() => {});
            }
            return response;
        } catch (err) {
            const cached = await caches.match(request, { ignoreSearch: false });
            if (cached) return cached;
            if (request.mode === 'navigate') {
                // Each standalone PWA keeps its own shell when the NAS is
                // unreachable; every other navigation falls back to the desktop.
                const path = url.pathname;
                const fallback = (path === '/notes' || path === '/notes/') ? path
                    : (path === '/videos' || path === '/videos/') ? path
                        : (path === '/music' || path === '/music/') ? path
                            : (path === '/books' || path === '/books/') ? path
                                : '/';
                const shell = await caches.match(fallback, { ignoreSearch: true });
                if (shell) return shell;
            }
            throw err;
        }
    })());
});

// ── Offline media (/__media/<exportId>, /__books/<bookId>[/t<n>]) ──────────
// Written by /static/videos/offline.js and /static/books/offline.js. <video>
// and <audio> ask with `Range`, and a plain 200 makes some browsers refuse to
// seek, so the worker synthesises 206 slices from the cached Blob. A missing
// entry answers 404 (download again).
async function serveMedia(request, url, cacheName, fallbackType) {
    const cache = await caches.open(cacheName);
    const cached = await cache.match(url.pathname);
    if (!cached) return new Response('Not found', { status: 404 });
    const type = (cached.headers.get('content-type') || fallbackType).split(';')[0];
    if (request.method === 'HEAD') {
        const headers = new Headers(cached.headers);
        headers.set('Accept-Ranges', 'bytes');
        return new Response(null, { status: 200, headers });
    }
    const range = request.headers.get('range');
    if (!range) {
        const headers = new Headers(cached.headers);
        headers.set('Accept-Ranges', 'bytes');
        return new Response(cached.body, { status: 200, headers });
    }
    let blob;
    try { blob = await cached.blob(); } catch { return cached; }
    const size = blob.size;
    const match = /bytes=(\d*)-(\d*)/.exec(range);
    let start = match && match[1] ? parseInt(match[1], 10) : 0;
    let end = match && match[2] ? parseInt(match[2], 10) : size - 1;
    if (!Number.isFinite(start) || start < 0) start = 0;
    if (!Number.isFinite(end) || end >= size) end = size - 1;
    if (start > end || start >= size) {
        return new Response(null, {
            status: 416,
            headers: { 'Content-Range': `bytes */${size}` },
        });
    }
    const slice = blob.slice(start, end + 1);
    return new Response(slice, {
        status: 206,
        statusText: 'Partial Content',
        headers: {
            'Content-Type': type,
            'Content-Range': `bytes ${start}-${end}/${size}`,
            'Content-Length': String(slice.size),
            'Accept-Ranges': 'bytes',
        },
    });
}

// ── Background Fetch (background downloads for /videos, /books, /music) ──────
// The page registers `backgroundFetch.fetch(tag, [request])` with
// `__ethos_bg_*` query params. Chromium runs the transfer while the app is in
// the background or closed; the success event copies the response into the
// allowlisted target and stores a result marker for the page.

let _bgDbPromise = null;

function bgDb() {
    if (!_bgDbPromise) {
        _bgDbPromise = new Promise((resolve, reject) => {
            const request = indexedDB.open(BG_RESULTS_DB, 1);
            request.onupgradeneeded = () => {
                const db = request.result;
                if (!db.objectStoreNames.contains(BG_RESULTS_STORE)) {
                    db.createObjectStore(BG_RESULTS_STORE, { keyPath: 'tag' });
                }
            };
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error || new Error('Background results store failed'));
        }).catch((error) => { _bgDbPromise = null; throw error; });
    }
    return _bgDbPromise;
}

function bgPutResult(record) {
    return bgDb().then((db) => new Promise((resolve, reject) => {
        const tx = db.transaction(BG_RESULTS_STORE, 'readwrite');
        const store = tx.objectStore(BG_RESULTS_STORE);
        store.put(record);
        tx.oncomplete = () => resolve();
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
        // Keep the marker table bounded: the app consumes rows, but a wiped
        // PWA could leave markers behind forever.
        const count = store.count();
        count.onsuccess = () => {
            if (count.result <= BG_RESULT_CAP) return;
            const all = store.getAllKeys();
            all.onsuccess = () => {
                const extra = count.result - BG_RESULT_CAP;
                for (const key of all.result.slice(0, extra)) store.delete(key);
            };
        };
    }));
}

function bgIdbWrite(dbName, storeName, value) {
    return new Promise((resolve, reject) => {
        const request = indexedDB.open(dbName);
        request.onupgradeneeded = () => {
            const db = request.result;
            if (!db.objectStoreNames.contains(storeName)) {
                db.createObjectStore(storeName, { keyPath: 'key' });
            }
        };
        request.onsuccess = () => {
            const db = request.result;
            try {
                const tx = db.transaction(storeName, 'readwrite');
                tx.objectStore(storeName).put(value);
                tx.oncomplete = () => resolve();
                tx.onerror = () => reject(tx.error);
                tx.onabort = () => reject(tx.error);
            } catch (error) {
                reject(error);
            }
        };
        request.onerror = () => reject(request.error);
    });
}

function bgKeyAllowed(cacheName, key) {
    if (cacheName === MEDIA_CACHE) return key.startsWith(MEDIA_PREFIX);
    if (cacheName === BOOKS_CACHE) return key.startsWith(BOOKS_PREFIX);
    return false;
}

async function bgStoreResponse(url, response) {
    const cacheName = url.searchParams.get('__ethos_bg_cache') || '';
    const key = url.searchParams.get('__ethos_bg_key') || '';
    const idbName = url.searchParams.get('__ethos_bg_idb') || '';
    const storeName = url.searchParams.get('__ethos_bg_store') || '';
    const type = (response.headers.get('content-type')
        || url.searchParams.get('__ethos_bg_type') || '').split(';')[0];
    const declared = Number(response.headers.get('content-length')) || 0;
    if (cacheName && key && BG_CACHES.has(cacheName) && bgKeyAllowed(cacheName, key)) {
        const cache = await caches.open(cacheName);
        await cache.put(new Request(new URL(key, self.location.origin).href), response);
        return { bytes: declared, type };
    }
    if (idbName && storeName && key && BG_IDB.get(idbName)?.has(storeName)) {
        const blob = await response.blob();
        await bgIdbWrite(idbName, storeName, { key, audio: blob, thumb: null, type: type || 'audio/mpeg' });
        return { bytes: blob.size, type };
    }
    throw new Error('Unknown background download target');
}

function bgAppFromTag(tag) {
    const match = /^ethos-(videos|books|music)-/.exec(String(tag || ''));
    return match ? match[1] : '';
}

self.addEventListener('backgroundfetchsuccess', (event) => {
    event.waitUntil((async () => {
        const tag = event.registration.id;
        let app = bgAppFromTag(tag);
        try {
            const records = await event.registration.matchAll();
            for (const record of records) {
                const url = new URL(record.request.url);
                app = url.searchParams.get('__ethos_bg_app') || app;
                const response = await record.responseReady;
                if (!response || !response.ok) {
                    throw new Error(`HTTP ${response ? response.status : 'no response'}`);
                }
                const stored = await bgStoreResponse(url, response);
                await bgPutResult({
                    tag,
                    app,
                    ok: true,
                    key: url.searchParams.get('__ethos_bg_key') || '',
                    cache: url.searchParams.get('__ethos_bg_cache') || '',
                    db: url.searchParams.get('__ethos_bg_idb') || '',
                    store: url.searchParams.get('__ethos_bg_store') || '',
                    type: stored.type,
                    bytes: stored.bytes,
                    at: Date.now(),
                });
            }
            try { await event.updateUI({ title: 'Pobrano — zapisano na urządzeniu' }); } catch { /* older Chrome */ }
        } catch (error) {
            await bgPutResult({
                tag, app, ok: false, error: String(error?.message || error), at: Date.now(),
            }).catch(() => {});
        }
    })());
});

self.addEventListener('backgroundfetchfail', (event) => {
    const tag = event.registration.id;
    event.waitUntil(bgPutResult({
        tag, app: bgAppFromTag(tag), ok: false, error: 'Background download failed', at: Date.now(),
    }).catch(() => {}));
});

self.addEventListener('backgroundfetchabort', (event) => {
    const tag = event.registration.id;
    event.waitUntil(bgPutResult({
        tag, app: bgAppFromTag(tag), ok: false, error: 'Background download aborted', at: Date.now(),
    }).catch(() => {}));
});

self.addEventListener('backgroundfetchclick', (event) => {
    const app = bgAppFromTag(event.registration.id);
    const target = BG_PATHS[app] || '/';
    event.waitUntil((async () => {
        const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        const client = windows.find((entry) => {
            try { return new URL(entry.url).pathname.startsWith(target); } catch { return false; }
        });
        if (client) { await client.focus(); return; }
        if (self.clients.openWindow) await self.clients.openWindow(target);
    })());
});

// ── Web Push (Sticky Notes reminders) ───────────────────────────────────────
// The package service sends `{title, body, tag, url, note_id, …}`. Show one
// notification per reminder tag (a re-fired reminder replaces the old card) and
// send the click to the notes PWA, never to the desktop shell.
self.addEventListener('push', (event) => {
    let payload = {};
    try {
        payload = event.data ? event.data.json() : {};
    } catch {
        try { payload = { body: event.data ? event.data.text() : '' }; } catch { payload = {}; }
    }
    const title = String(payload.title || 'Sticky Notes');
    const body = String(payload.body || 'Reminder');
    const rawUrl = typeof payload.url === 'string' ? payload.url : '/notes';
    const url = rawUrl.startsWith('/') ? rawUrl : '/notes';
    const tag = String(payload.tag || 'sticky-notes');
    event.waitUntil(self.registration.showNotification(title, {
        body,
        tag,
        renotify: true,
        icon: '/static/icons/notes-192.png',
        badge: '/static/icons/notes-192.png',
        data: { url, noteId: payload.note_id || null },
    }));
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();
    const raw = event.notification.data && event.notification.data.url
        ? String(event.notification.data.url)
        : '/notes';
    const target = new URL(raw.startsWith('/') ? raw : '/notes', self.location.origin).href;
    event.waitUntil((async () => {
        const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        const notesClient = windows.find((client) => {
            try { return new URL(client.url).pathname.startsWith('/notes'); } catch { return false; }
        });
        if (notesClient) {
            await notesClient.focus();
            if ('navigate' in notesClient) await notesClient.navigate(target).catch(() => {});
            return;
        }
        if (self.clients.openWindow) await self.clients.openWindow(target);
    })());
});
