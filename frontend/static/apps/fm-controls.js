// EthOS NAS — File Manager Controls (view mode, hidden files, search)
// Separate file to avoid export conflicts with app.js imports from files.js

// Helper: get focused File Manager window and its state via files.js's exported fmWindowStates
// We avoid importing from core/fm-utils.js because files.js maintains its own state Map.
function _getActiveFmRoot() {
    // Get the first focused 'files' window from openWindows
    let bestWin = null, bestZ = -1;
    for (const [k, v] of (window.__openWindows || window.openWindows || new Map()).entries()) {
        if (v.appId === 'files' && v.zIndex > bestZ) { bestWin = k; bestZ = v.zIndex; }
    }
    if (!bestWin) return null;
    const ow = window.__openWindows || window.openWindows;
    const w = ow ? ow.get(bestWin) : null;
    if (!w) return null;
    return w.element.querySelector('.fm-container') || w.element;
}

function _getWinId(root) {
    return root?.dataset?.fmWin || (root?.closest && root.closest('[data-fm-win]')?.dataset?.fmWin) || null;
}

function _getState(winId) {
    if (!winId) return null;
    // Use files.js's exported fmWindowStates (accessed via import below)
    const states = window.__fmWindowStates;
    if (!states) return null;
    if (!states.has(winId)) {
        let view = 'grid';
        try { view = localStorage.getItem('ethos-fm-view') === 'list' ? 'list' : 'grid'; } catch {}
        states.set(winId, {
            path: '/', viewMode: view, selected: new Set(), clipboard: null,
            clipboardOp: null, history: ['/'], historyIndex: 0, contextMenu: null,
            sortCol: 'name', sortAsc: true, _rootEl: null,
        });
    }
    return states.get(winId);
}

function _getFmStateForRoot(root) {
    if (!root) return null;
    const winId = _getWinId(root);
    return winId ? _getState(winId) : null;
}

export function fmToggleHidden() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    state._showHidden = !state._showHidden;
    try { localStorage.setItem('ethos-fm-hidden', state._showHidden ? '1' : '0'); } catch {}
    const btn = root.querySelector('[data-testid="fm-toggle-hidden"]');
    if (btn) {
        btn.title = `${state._showHidden ? 'Hide' : 'Show'} hidden files`;
        btn.classList.toggle('active', state._showHidden);
    }
    fmLoad(state.path, root);
}

export function fmSetViewMode(mode) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    state.viewMode = mode;
    try { localStorage.setItem('ethos-fm-view', mode); } catch {}
    const gridBtn = root.querySelector('[data-testid="fm-view-grid"]');
    const listBtn = root.querySelector('[data-testid="fm-view-list"]');
    if (gridBtn) gridBtn.classList.toggle('active', mode === 'grid');
    if (listBtn) listBtn.classList.toggle('active', mode === 'list');
    fmLoad(state.path, root);
}

// NOTE: search is handled exclusively by files.js — the inline oninput="fmSearchInput(this)"
// in the toolbar template debounces into files.js's server-side recursive fmSearch()
// (/api/files/search), which re-renders .fm-content with a results table. A second
// client-side name filter used to run here (initSearchInput + local fmSearch) and fired on
// every keystroke alongside it, racing the re-render; it was removed so there is one path.
