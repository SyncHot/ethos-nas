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

const CACHE_VERSION = 'ethos-shell-v4';
const SHELL_URLS = [
    '/',
    '/manifest.webmanifest',
    '/static/icons/icon-192.png',
    '/static/icons/icon-512.png',
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
                const shell = await caches.match('/');
                if (shell) return shell;
            }
            throw err;
        }
    })());
});
