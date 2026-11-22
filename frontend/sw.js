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
 * Bump CACHE_VERSION when the shell URLs change; old caches are pruned on
 * activate.
 */

const CACHE_VERSION = 'ethos-shell-v5';
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
        await Promise.all(names.filter((n) => n !== CACHE_VERSION).map((n) => caches.delete(n)));
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
                // The notes PWA keeps its own shell when the NAS is unreachable;
                // every other navigation falls back to the desktop shell.
                const fallback = (url.pathname === '/notes' || url.pathname === '/notes/')
                    ? url.pathname
                    : '/';
                const shell = await caches.match(fallback, { ignoreSearch: true });
                if (shell) return shell;
            }
            throw err;
        }
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
