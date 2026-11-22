// EthOS NAS — cross-app links and one-shot context hand-off.
//
// Apps used to park data on `window.*` before opening another window
// (`window.__fmShareCtx`, `window.__fmPendingPath`, `window.fmNavigateTo`),
// which put state in the global namespace and made the contract invisible.
// Core-to-core links go through this module instead:
//
//   setContext('shared-links', { path });   // before opening
//   const ctx = consumeContext('shared-links');  // once, inside the app
//
// `consumeContext` is one-shot: the payload is deleted when read, so a window
// that is reopened later does not receive a stale path.
//
// Package apps (Gallery, Editor, Photos) still read their documented
// `window.__fm*Ctx` contract — those globals are owned by the package API and
// are intentionally left in place until the packages move to `app-links`.
//
// No dynamic text is rendered here; callers escape their own values.

import { openWindows } from './window-manager.js';

const _contexts = new Map();

/** Store a one-shot payload for `appId`. */
export function setContext(appId, payload) {
    if (!appId) return;
    if (payload == null) _contexts.delete(appId);
    else _contexts.set(appId, payload);
}

/** Read and clear the payload for `appId` (returns null when absent). */
export function consumeContext(appId) {
    const value = _contexts.get(appId) || null;
    if (value) _contexts.delete(appId);
    return value;
}

/** Inspect without consuming — only for tests/diagnostics. */
export function peekContext(appId) {
    return _contexts.get(appId) || null;
}

/** Open an app at a section: the app reads `section` from its context. */
export function openAppAt(appId, section) {
    if (!appId) return;
    if (section) setContext(appId, { section });
    if (typeof window.openApp === 'function') window.openApp(appId);
}

/**
 * Show `path` in Files: navigate an already-open window immediately (its
 * router is live), otherwise hand the path to the next window that renders.
 */
export function openPathInFiles(path) {
    if (!path) return;
    const hasWindow = [...openWindows.values()].some((win) => win.appId === 'files');
    if (hasWindow && typeof window.fmNavigateTo === 'function') {
        window.fmNavigateTo(path);
        return;
    }
    setContext('files', { path });
    if (typeof window.openApp === 'function') window.openApp('files');
}
