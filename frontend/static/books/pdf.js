// EthOS Books — PDF reader for the /books PWA.
//
// pdf.js renders one page at a time into a canvas sized for the phone's device
// pixel ratio, with page navigation, a page slider/input and zoom. The resume
// position is the package's `locator` = page number (string), so the desktop
// reader continues on the same page.
//
// The PDF comes from Cache Storage (`/__books/<id>`) when downloaded, otherwise
// from `/api/files/raw`. The worker is served from `/static/vendor/`, so no
// token or blob wrangling is needed (static assets are public).

import { bookBuffer } from './epub.js';

const VENDOR = '/static/vendor/pdf.min.js';
const WORKER = '/static/vendor/pdf.worker.min.js';
let _loaded = null;

function loadVendor() {
    if (!_loaded) {
        _loaded = new Promise((resolve) => {
            if (window.pdfjsLib) { resolve(true); return; }
            const script = document.createElement('script');
            script.src = VENDOR;
            script.async = false;
            script.onload = () => {
                try {
                    if (window.pdfjsLib) window.pdfjsLib.GlobalWorkerOptions.workerSrc = WORKER;
                } catch { /* worker is optional; rendering degrades to the main thread */ }
                resolve(!!window.pdfjsLib);
            };
            script.onerror = () => resolve(false);
            document.head.appendChild(script);
        });
    }
    return _loaded;
}

/**
 * Mount the PDF reader into the shared overlay.
 * @returns {{destroy: Function}}
 */
export function mountPdf({ book, body, foot, settings, progress, onProgress, note }) {
    const state = { destroyed: false, doc: null, page: Math.max(1, Number(progress?.locator) || 1), zoom: 1, rendering: false };
    body.innerHTML = '<div class="bk-pdf-scroll" data-pdf-scroll></div>';
    const scroll = body.querySelector('[data-pdf-scroll]');
    foot.innerHTML = '<span data-reader-position>Wczytywanie…</span>';

    const destroy = () => {
        state.destroyed = true;
        try { state.doc?.destroy(); } catch { /* already gone */ }
        state.doc = null;
    };

    (async () => {
        const ok = await loadVendor();
        if (state.destroyed) return;
        if (!ok || !window.pdfjsLib) {
            note?.('Nie udało się wczytać czytnika PDF. Odśwież stronę.', 'error');
            body.innerHTML = '<div class="bk-empty"><span class="big">📄</span>Nie udało się wczytać czytnika PDF.</div>';
            return;
        }
        const { buffer, local } = await bookBuffer(book);
        if (state.destroyed) return;
        const doc = await window.pdfjsLib.getDocument({ data: buffer }).promise;
        if (state.destroyed) return;
        state.doc = doc;
        state.page = Math.max(1, Math.min(doc.numPages, state.page));

        foot.innerHTML = `
  <button class="bk-btn small" type="button" data-page-prev data-testid="books-pdf-prev" aria-label="Poprzednia strona">◀</button>
  <input class="bk-input" type="number" min="1" max="${doc.numPages}" value="${state.page}" data-page-input aria-label="Strona" data-testid="books-pdf-page">
  <span data-page-total>/ ${doc.numPages}</span>
  <span class="time" data-reader-position></span>
  <button class="bk-btn small" type="button" data-page-next data-testid="books-pdf-next" aria-label="Następna strona">▶</button>
  <span style="flex:1"></span>
  <button class="bk-btn small" type="button" data-zoom-out aria-label="Pomniejsz">−</button>
  <button class="bk-btn small" type="button" data-zoom-in aria-label="Powiększ">+</button>`;
        const position = foot.querySelector('[data-reader-position]');

    const render = async () => {
        if (state.rendering || state.destroyed) return;
        state.rendering = true;
        try {
            const page = await doc.getPage(state.page);
            if (state.destroyed) return;
            const base = page.getViewport({ scale: 1 });
            const available = Math.max(280, (scroll.clientWidth || 360) - 20);
            const scale = Math.max(0.3, Math.min(3, (available / base.width) * state.zoom));
            const viewport = page.getViewport({ scale });
            const dpr = Math.min(2, window.devicePixelRatio || 1);
            const canvas = document.createElement('canvas');
            canvas.width = Math.round(viewport.width * dpr);
            canvas.height = Math.round(viewport.height * dpr);
            canvas.style.width = `${Math.round(viewport.width)}px`;
            canvas.style.height = `${Math.round(viewport.height)}px`;
            canvas.dataset.testid = 'books-pdf-canvas';
            const context = canvas.getContext('2d');
            scroll.replaceChildren(canvas);
            await page.render({
                canvasContext: context,
                viewport,
                transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null,
            }).promise;
            if (state.destroyed) return;
            const input = foot.querySelector('[data-page-input]');
            if (input) input.value = String(state.page);
            if (position) position.textContent = local ? 'offline' : '';
            onProgress?.({
                percent: state.page / doc.numPages,
                locator: String(state.page),
                position_s: 0,
                finished: state.page >= doc.numPages,
            });
        } catch (error) {
            note?.(`Nie udało się wyświetlić strony: ${error?.message || error}`, 'error');
        } finally {
            state.rendering = false;
        }
    };

    foot.addEventListener('click', (event) => {
        if (event.target.closest('[data-page-prev]') && state.page > 1) { state.page -= 1; render(); }
        else if (event.target.closest('[data-page-next]') && state.page < doc.numPages) { state.page += 1; render(); }
        else if (event.target.closest('[data-zoom-in]')) { state.zoom = Math.min(3, state.zoom + 0.2); render(); }
        else if (event.target.closest('[data-zoom-out]')) { state.zoom = Math.max(0.5, state.zoom - 0.2); render(); }
    });
    foot.querySelector('[data-page-input]')?.addEventListener('change', (event) => {
        const value = Number(event.target.value) || 1;
        state.page = Math.max(1, Math.min(doc.numPages, value));
        render();
    });
    await render();
    })().catch((error) => {
        if (state.destroyed) return;
        const message = String(error?.detail || error?.message || error);
        note?.(`Nie udało się otworzyć PDF-a: ${message}`, 'error');
        body.innerHTML = `<div class="bk-empty"><span class="big">📄</span>Nie udało się otworzyć tego PDF-a.<br>${message.replace(/[<>&]/g, '')}</div>`;
    });

    return { destroy };
}
