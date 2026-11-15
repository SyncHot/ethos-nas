// EthOS NAS — frontend error reporter (thin hooks over core/logger.js).
//
// Uncaught exceptions, unhandled promise rejections and console.warn/error go
// through the shared batch queue in core/logger.js (docs/logs.md). There is no
// second buffer, endpoint or history here — the Logs app is the only viewer.
// Loaded as a module BEFORE app.js so bootstrap errors are captured too.

import { log } from './logger.js';

function _toText(value) {
    try {
        if (typeof value === 'string') return value;
        if (value instanceof Error) return value.message || String(value);
        return JSON.stringify(value);
    } catch (_) {
        return String(value);
    }
}

function _stackOf(value) {
    try {
        if (value instanceof Error) return value.stack || '';
        if (typeof value === 'object' && value !== null && value.stack) return String(value.stack);
    } catch (_) {
        /* ignore */
    }
    return '';
}

// ── hooks ────────────────────────────────────────────────────────────────────

window.addEventListener('error', (event) => {
    const where = `${event.filename || ''}:${event.lineno || 0}`;
    log('error', event.message || 'Uncaught error', `${where}\n${_stackOf(event.error)}`.trim());
});

window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    log(
        'error',
        `Unhandled promise rejection: ${_toText(reason)}`,
        _stackOf(reason),
    );
});

// Keep the original console behaviour, add reporting.
const _origError = console.error.bind(console);
const _origWarn = console.warn.bind(console);

console.error = (...args) => {
    _origError(...args);
    const detail = args.map(_stackOf).find(Boolean) || '';
    log('error', args.map(_toText).join(' '), detail);
};

console.warn = (...args) => {
    _origWarn(...args);
    const detail = args.map(_stackOf).find(Boolean) || '';
    log('warning', args.map(_toText).join(' '), detail);
};

