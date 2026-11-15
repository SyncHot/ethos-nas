// EthOS NAS — File Manager Shared Helpers & State

import { openWindows } from './window-manager.js';

/** Per-window file manager state Map (appId → state). */
export const fmWindowStates = new Map();

/** Get the focused files window ID (highest z-index). */
export function _getFocusedWindowId() {
    let bestWin = null, bestZ = -1;
    for (const [k, v] of openWindows.entries()) {
        if (v.appId === 'files' && v.zIndex > bestZ) { bestWin = k; bestZ = v.zIndex; }
    }
    return bestWin || null;
}

/** Get the FM container root element of the focused files window. */
export function _getActiveFmRoot() {
    const winId = _getFocusedWindowId(); if (!winId) return null;
    const w = openWindows.get(winId); if (!w) return null;
    return w.element.querySelector('.fm-container') || w.element;
}

/** Get (or create) state for a window, given its root element. */
export function _getFmStateForRoot(root) {
    const winId = root?.dataset?.fmWin || (root?.closest && root.closest('[data-fm-win]')?.dataset?.fmWin);
    if (!winId) return null;
    if (!fmWindowStates.has(winId)) {
        fmWindowStates.set(winId, {
            path: '/', viewMode: 'grid', selected: new Set(), clipboard: null,
            clipboardOp: null, history: ['/'], historyIndex: 0, contextMenu: null,
            sortCol: 'name', sortAsc: true, _rootEl: null,
        });
    }
    return fmWindowStates.get(winId);
}
