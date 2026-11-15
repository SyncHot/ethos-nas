// EthOS NAS — one logger for every app (docs/logs.md).
//
// Apps (and native packages through `ctx.log`) report diagnostics here. The
// queue batches into `POST /api/logs/ingest`; the backend derives `app` from the
// package id (or marks browser events as `browser`) and stores them next to the
// Python records in the unified store.
//
// Rules:
//   • never throw — reporting must not break the page;
//   • no import of api.js (this module is loaded before bootstrap; a cycle would
//     break the login overlay) — read the token from localStorage directly;
//   • no console output — the Logs app is the place to read this.

const FLUSH_INTERVAL_MS = 2000;
const MAX_PENDING = 10;
const MAX_QUEUE = 200;
const LEVELS = { debug: 10, info: 20, warning: 30, error: 40 };

let _pending = [];
let _timer = null;
let _flushing = false;
let _context = { app: 'core' };

/** Attribute subsequent browser events to an app window (`files`, `logs`, …). */
export function setLogContext(context = {}) {
    _context = { ..._context, ...context };
}

export function getLogContext() {
    return { ..._context };
}

function _threshold() {
    try {
        const raw = (localStorage.getItem('ethosLogLevel') || 'warning').toLowerCase();
        return LEVELS[raw] || LEVELS.warning;
    } catch (_) {
        return LEVELS.warning;
    }
}

function _token() {
    try { return localStorage.getItem('token') || ''; } catch (_) { return ''; }
}

/**
 * Queue one event.
 * `opts.packageId` marks a native-package message; the server verifies the
 * package and stores it as `pkg:<id>`.
 */
export function log(level, message, detail = '', opts = {}) {
    try {
        const name = LEVELS[level] ? level : 'info';
        if (LEVELS[name] < _threshold()) return;
        const payload = {
            level: name,
            message: String(message ?? '').slice(0, 500),
            detail: String(detail ?? '').slice(0, 4000),
        };
        if (opts.packageId) {
            payload.package_id = String(opts.packageId).slice(0, 60);
            payload.source = (opts.source || 'pkg-ui').slice(0, 120);
        } else {
            payload.source = (opts.source || `browser:${_context.app || 'core'}`).slice(0, 120);
        }
        _pending.push(payload);
        if (_pending.length > MAX_QUEUE) _pending.splice(0, _pending.length - MAX_QUEUE);
        if (_pending.length >= MAX_PENDING) _flush();
        else if (!_timer) _timer = setTimeout(() => { _flush(); }, FLUSH_INTERVAL_MS);
    } catch (_) {
        /* never break the page */
    }
}

export const logDebug = (message, detail = '', opts = {}) => log('debug', message, detail, opts);
export const logInfo = (message, detail = '', opts = {}) => log('info', message, detail, opts);
export const logWarn = (message, detail = '', opts = {}) => log('warning', message, detail, opts);
export const logError = (message, detail = '', opts = {}) => log('error', message, detail, opts);

async function _flush(useBeacon = false) {
    if (_timer) { clearTimeout(_timer); _timer = null; }
    if (!_pending.length) return;
    if (_flushing && !useBeacon) return;

    const batch = _pending.splice(0, _pending.length);
    if (useBeacon && typeof navigator.sendBeacon === 'function') {
        try {
            const token = _token();
            const url = '/api/logs/ingest' + (token ? `?token=${encodeURIComponent(token)}` : '');
            const blob = new Blob([JSON.stringify(batch)], { type: 'application/json' });
            if (navigator.sendBeacon(url, blob)) return;
        } catch (_) {
            /* fall through to fetch */
        }
    }

    _flushing = true;
    try {
        const headers = { 'Content-Type': 'application/json' };
        const token = _token();
        if (token) headers.Authorization = `Bearer ${token}`;
        // Raw fetch: a 401 here must NOT trigger the global session-expiry
        // reload (apiFetch would). Before login there is no token — events are
        // simply dropped, which is fine.
        await fetch('/api/logs/ingest', {
            method: 'POST',
            headers,
            body: JSON.stringify(batch),
            keepalive: true,
        });
    } catch (_) {
        /* offline / not logged in — drop the batch */
    } finally {
        _flushing = false;
        if (_pending.length) _timer = setTimeout(() => { _flush(); }, FLUSH_INTERVAL_MS);
    }
}

/** Force a flush (tests and explicit "report now" paths). */
export function flushLogs() {
    return _flush(false);
}

// Nothing should be lost when the tab goes away.
window.addEventListener('pagehide', () => {
    try { _flush(true); } catch (_) { /* ignore */ }
});
