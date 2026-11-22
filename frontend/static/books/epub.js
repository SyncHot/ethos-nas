// EthOS Books — EPUB reader for the /books PWA.
//
// Mirrors the package reader: epub.js renders the book into an iframe, the
// package's per-account settings (theme, font, size, margins, line height,
// flow) are injected into every chapter, and the position is stored as the
// same CFI the desktop uses — so a book read on the phone continues on the
// desktop at the right page.
//
// The file comes from Cache Storage (`/__books/<id>`) when it was downloaded,
// otherwise from `/api/files/raw`. epub.js and JSZip are served from
// `/static/vendor/` (same pinned versions as the package, see THIRD-PARTY.md).

import { apiFetch } from '../core/api.js';
import { localKey, rawPath } from './api.js';
import { CACHE, loadLocations, saveLocations } from './offline.js';

const VENDORS = [
    '/static/vendor/jszip.min.js',
    '/static/vendor/epub.min.js',
];
let _loaded = null;

function loadScript(src) {
    return new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = src;
        script.async = false;
        script.onload = () => resolve(true);
        script.onerror = () => reject(new Error(`Nie udało się wczytać ${src}`));
        document.head.appendChild(script);
    });
}

function loadVendors() {
    if (!_loaded) {
        _loaded = VENDORS.reduce((chain, src) => chain.then(() => loadScript(src)), Promise.resolve())
            .then(() => !!window.ePub)
            .catch(() => false);
    }
    return _loaded;
}

const READ_THEMES = {
    light: { bg: '#ffffff', text: '#1f2430', muted: '#5b6472', link: '#2563eb' },
    sepia: { bg: '#f6ecd9', text: '#3d2f1f', muted: '#7a6752', link: '#8a5a1e' },
    dark: { bg: '#0f1720', text: '#e2e8f0', muted: '#94a3b8', link: '#7dd3fc' },
};

export function readerThemeName(settings) {
    if (settings?.theme && settings.theme !== 'system') return settings.theme;
    return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
}

export function readerTheme(settings) {
    return READ_THEMES[readerThemeName(settings)] || READ_THEMES.light;
}

function epubStyles(settings) {
    const theme = readerTheme(settings);
    const pad = 4 * (Number(settings.margin) || 2);
    const family = settings.font_family === 'sans'
        ? 'system-ui, -apple-system, "Segoe UI", sans-serif'
        : 'Georgia, "Iowan Old Style", serif';
    return `
html, body { background: ${theme.bg} !important; color: ${theme.text} !important; }
body {
    padding: 0 ${pad}% !important;
    max-width: 46em;
    margin: 0 auto !important;
    font-family: ${family} !important;
    font-size: ${Number(settings.font_size) || 100}% !important;
    line-height: ${Number(settings.line_height) || 1.6} !important;
    text-align: left;
}
p, li, div, span, blockquote, td { color: ${theme.text}; }
h1, h2, h3, h4 { color: ${theme.text}; line-height: 1.25; }
a { color: ${theme.link}; }
img, svg { max-width: 100% !important; height: auto !important; }
::selection { background: rgba(37, 99, 235, 0.28); }`;
}

function applyTheme(contents, settings) {
    const doc = contents?.document;
    if (!doc || !doc.head) return;
    let style = doc.getElementById('ethos-books-theme');
    if (!style) {
        style = doc.createElement('style');
        style.id = 'ethos-books-theme';
        doc.head.appendChild(style);
    }
    style.textContent = epubStyles(settings);
    if (doc.documentElement) doc.documentElement.style.background = readerTheme(settings).bg;
}

/** Book bytes: the downloaded copy first, then the NAS. */
export async function bookBuffer(book) {
    const key = localKey(book.id, null);
    try {
        if ('caches' in window) {
            const cache = await caches.open(CACHE);
            const hit = await cache.match(new Request(key));
            if (hit) return { buffer: await hit.arrayBuffer(), local: true };
        }
    } catch { /* cache unavailable — stream from the NAS */ }
    const res = await apiFetch(rawPath(book.path));
    if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
            const payload = await res.clone().json();
            detail = payload?.detail || payload?.error || detail;
        } catch { /* non-JSON */ }
        const error = new Error(detail);
        error.detail = detail;
        throw error;
    }
    return { buffer: await res.arrayBuffer(), local: false };
}

/**
 * Mount the reader into the shared overlay.
 * @returns {{destroy: Function}}
 */
export function mountEpub({ book, body, foot, actions, settings, progress, onProgress, note }) {
    const state = {
        destroyed: false, epub: null, rendition: null, locations: null,
        lastPercent: Number(progress?.percent) || 0, lastCfi: '', settings,
    };
    body.innerHTML = '<div class="bk-epub-host" data-epub-host></div>';
    const host = body.querySelector('[data-epub-host]');
    foot.innerHTML = '<span data-reader-position>Wczytywanie…</span>';
    const positionLabel = foot.querySelector('[data-reader-position]');
    if (actions) {
        actions.innerHTML = '<button class="bk-btn small" type="button" data-toc data-testid="books-reader-toc">Spis treści</button>';
    }

    const destroy = () => {
        state.destroyed = true;
        try { state.epub?.destroy(); } catch { /* already gone */ }
        state.epub = null;
        state.rendition = null;
    };

    /** Re-apply typography after the shared reader settings changed. */
    const refresh = (next) => {
        if (next) state.settings = next;
        try {
            (state.rendition?.getContents?.() || []).forEach((contents) => applyTheme(contents, state.settings));
            state.rendition?.themes?.fontSize(`${Number(state.settings.font_size) || 100}%`);
        } catch { /* reader not ready yet */ }
    };

    (async () => {
        const ok = await loadVendors();
        if (state.destroyed) return;
        if (!ok || !window.ePub) {
            note?.('Nie udało się wczytać czytnika EPUB. Odśwież stronę.', 'error');
            body.innerHTML = '<div class="bk-empty"><span class="big">📕</span>Nie udało się wczytać czytnika EPUB.</div>';
            return;
        }
        const { buffer, local } = await bookBuffer(book);
        if (state.destroyed) return;
        state.epub = window.ePub(buffer);
        const rendition = state.epub.renderTo(host, {
            width: '100%',
            height: '100%',
            flow: settings?.flow === 'scrolled' ? 'scrolled-doc' : 'paginated',
            spread: 'none',
            allowScriptedContent: false,
        });
        state.rendition = rendition;
        rendition.hooks.content.register((contents) => {
            applyTheme(contents, state.settings);
            // Swipe inside the iframe: horizontal drag turns the page. Tap
            // zones stay for single-handed reading; both work in paginated
            // and scrolled mode (a mostly-vertical drag is ignored).
            const doc = contents?.document;
            if (!doc || doc.__bkSwipe) return;
            try { doc.__bkSwipe = true; } catch { return; }
            let startX = 0;
            let startY = 0;
            let tracking = false;
            doc.addEventListener('touchstart', (event) => {
                if (event.touches?.length !== 1) { tracking = false; return; }
                tracking = true;
                startX = event.touches[0].clientX;
                startY = event.touches[0].clientY;
            }, { passive: true });
            doc.addEventListener('touchend', (event) => {
                if (!tracking) return;
                tracking = false;
                const touch = event.changedTouches?.[0];
                if (!touch) return;
                const dx = touch.clientX - startX;
                const dy = touch.clientY - startY;
                if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 1.5) return;
                if (dx < 0) rendition.next();
                else rendition.prev();
            }, { passive: true });
        });

        const report = (value, cfi) => {
            if (positionLabel) positionLabel.textContent = `${Math.round(value * 100)}%${local ? ' · offline' : ''}`;
            onProgress?.({
                percent: Math.max(0, Math.min(1, value)),
                locator: cfi || '',
                position_s: 0,
                finished: value >= 0.995,
            });
        };

        rendition.on('relocated', (location) => {
            const start = location?.start || {};
            if (start.cfi) state.lastCfi = start.cfi;
            let value = Number(start.percentage);
            if (!Number.isFinite(value) && state.locations && start.cfi) {
                try { value = state.locations.percentageFromCfi(start.cfi); } catch { /* ignore */ }
            }
            if (!Number.isFinite(value)) value = state.lastPercent;
            state.lastPercent = Math.max(0, Math.min(1, value));
            report(state.lastPercent, start.cfi || '');
        });

        // Percentage needs the locations index. Reuse the one saved for this
        // book (reopening a 600-page novel is instant) and generate it in the
        // background otherwise, then report the real percentage for the page
        // already on screen.
        (async () => {
            const cached = await loadLocations(book.id).catch(() => null);
            if (state.destroyed) return;
            if (cached) {
                try {
                    state.epub.locations.load(cached);
                    state.locations = state.epub.locations;
                } catch { /* regenerate below */ }
            }
            if (!state.locations) {
                await new Promise((resolve) => { Promise.resolve(state.epub.ready).then(resolve, resolve); });
                if (state.destroyed) return;
                const generated = await state.epub.locations.generate(1600).catch(() => null);
                if (!generated || state.destroyed) return;
                state.locations = generated;
                try {
                    const saved = state.epub.locations.save();
                    const list = typeof saved === 'string' ? JSON.parse(saved) : saved;
                    await saveLocations(book.id, Array.isArray(list) ? list : []);
                } catch { /* cache is optional */ }
            }
            if (!state.lastCfi) return;
            let value;
            try { value = state.locations.percentageFromCfi(state.lastCfi); } catch { /* ignore */ }
            if (Number.isFinite(value)) {
                state.lastPercent = Math.max(0, Math.min(1, value));
                report(state.lastPercent, state.lastCfi);
            }
        })().catch(() => { /* percentage falls back to page counts */ });

        rendition.on('keyup', (event) => {
            if (event?.key === 'ArrowLeft') rendition.prev();
            if (event?.key === 'ArrowRight') rendition.next();
        });

        // Tap zones (paginated mode only — scrolled mode needs the full width
        // for scrolling). They sit above the iframe, so text selection is lost
        // on phones, which is the expected reading gesture there.
        if (settings?.flow !== 'scrolled') {
            const left = document.createElement('button');
            left.type = 'button';
            left.className = 'bk-tapzone left';
            left.setAttribute('aria-label', 'Poprzednia strona');
            left.addEventListener('click', () => rendition.prev());
            const right = document.createElement('button');
            right.type = 'button';
            right.className = 'bk-tapzone right';
            right.setAttribute('aria-label', 'Następna strona');
            right.addEventListener('click', () => rendition.next());
            body.append(left, right);
        }

        try { book.ready; } catch { /* ignore */ }
        await rendition.display(progress?.locator || undefined);
        if (state.destroyed) return;
        // Re-apply once the first section exists (margins/typography).
        try {
            (rendition.getContents?.() || []).forEach((contents) => applyTheme(contents, settings));
        } catch { /* theme is best effort */ }

        actions?.querySelector('[data-toc]')?.addEventListener('click', async () => {
            const existing = body.querySelector('.bk-toc');
            if (existing) { existing.remove(); return; }
            let toc = [];
            try {
                const navigation = await state.epub.loaded.navigation;
                toc = navigation?.toc || [];
            } catch { /* no navigation document */ }
            const panel = document.createElement('div');
            panel.className = 'bk-toc';
            panel.dataset.testid = 'books-reader-toc-panel';
            const flatten = (items, depth = 0) => items.flatMap((item) => {
                const sub = Array.isArray(item.subitems) ? item.subitems : [];
                return [{ item, depth }, ...flatten(sub, depth + 1)];
            });
            const rows = flatten(toc);
            panel.innerHTML = rows.length
                ? rows.map(({ item, depth }) => `<button type="button" class="${depth ? 'sub' : ''}" data-href="${String(item.href || '').replace(/"/g, '&quot;')}">${String(item.label || '').replace(/[<>&]/g, (ch) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[ch]))}</button>`).join('')
                : '<div class="bk-empty">Ta książka nie ma spisu treści.</div>';
            panel.addEventListener('click', (event) => {
                const target = event.target.closest('[data-href]');
                if (!target) return;
                rendition.display(target.dataset.href);
                panel.remove();
            });
            body.appendChild(panel);
        });
    })().catch((error) => {
        if (state.destroyed) return;
        const message = String(error?.detail || error?.message || error);
        note?.(`Nie udało się otworzyć książki: ${message}`, 'error');
        body.innerHTML = `<div class="bk-empty"><span class="big">📕</span>Nie udało się otworzyć tego EPUB-a.<br>${message.replace(/[<>&]/g, '')}</div>`;
    });

    return { destroy, refresh };
}
