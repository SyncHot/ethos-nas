// EthOS NAS — Desktop Trash shortcut
//
// The desktop "Trash" icon is a shortcut into Files' Trash view, not an
// app window of its own. This module owns everything that is Trash-specific:
// the item-count badge, opening/emptying the trash without an open Files
// window, and dropping Files items onto the icon.
//
// Files keeps its own (much larger) trash UI in apps/files.js; we reuse
// its exported window functions via the existing `window.fm*` bindings rather
// than duplicating the confirm / DELETE / toast flow here.

import { apiFetch, getToken } from './api.js';
import { addNotification } from './notification-center.js';
import { openWindows, focusWindow, restoreWindowWin } from './window-manager.js';

// Must match FM_DND_MIME in frontend/static/apps/files.js — that drag payload is
// the JSON array [{name, path, is_dir}] used for cross-window file moves.
const FM_DND_MIME = 'application/x-ethos-fm-items';
const TRASH_ICON_SELECTOR = '[data-testid="desktop-icon-trash"]';
const BADGE_ID = 'desktopTrashBadge';
const POLL_MS = 60000;

let _pollTimer = null;
let _refreshInFlight = null;
let _debounceTimer = null;
let _dropBusy = false;

function _trashIconEl() {
    return document.querySelector(TRASH_ICON_SELECTOR);
}

function _iconIdList(n) {
    return n === 1 ? '1 item' : `${n} items`;
}

function _setBadge(total) {
    const count = Number(total) || 0;
    const badge = document.getElementById(BADGE_ID);
    if (badge) {
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.hidden = count <= 0;
        badge.setAttribute('aria-label', `${_iconIdList(count)} in Trash`);
    }
    const icon = _trashIconEl();
    if (icon) icon.title = count > 0 ? `Trash — ${_iconIdList(count)}` : 'Trash (empty)';
}

function _scheduleRefresh(delayMs = 250) {
    clearTimeout(_debounceTimer);
    _debounceTimer = setTimeout(() => { refreshTrashBadge(); }, delayMs);
}

/**
 * Refresh the desktop Trash badge from GET /api/files/trash.
 * Coalesces concurrent calls; silently hides the badge when signed out/offline.
 * @returns {Promise<number>} the item count (0 on any failure)
 */
export function refreshTrashBadge() {
    if (_refreshInFlight) return _refreshInFlight;
    _refreshInFlight = (async () => {
        if (!getToken()) { _setBadge(0); return 0; }
        try {
            // limit=1 keeps the response tiny — only `total` is needed.
            const resp = await apiFetch('/api/files/trash?limit=1');
            if (!resp.ok) { _setBadge(0); return 0; }
            const data = await resp.json();
            const total = Number(data && data.total) || 0;
            _setBadge(total);
            return total;
        } catch (_) {
            // Offline / server restart: keep a usable icon, don't show a stale count.
            _setBadge(0);
            return 0;
        } finally {
            _refreshInFlight = null;
        }
    })();
    return _refreshInFlight;
}

/**
 * Open the Trash view, reusing an existing Files window when possible.
 * Files is a multi-window app, so calling openApp('files') blindly would
 * spawn a new window on every click.
 */
export async function openTrashFromDesktop() {
    let target = null;
    for (const w of openWindows.values()) {
        if (w.appId !== 'files' || !w.element || w.element.classList.contains('closing')) continue;
        if (!target || (w.zIndex || 0) > (target.zIndex || 0)) target = w;
    }
    if (target) {
        if (target.element.classList.contains('minimized')) restoreWindowWin(target.element);
        else focusWindow(target.element);
    } else if (typeof window.openApp === 'function') {
        window.openApp('files'); // wrapper in app.js → initFileManager
    }
    try {
        // fmOpenTrash() needs a live Files window; openApp() above creates
        // its DOM synchronously, so this is safe right after.
        await window.fmOpenTrash?.();
    } catch (err) {
        addNotification(err?.detail || err?.message || 'Could not open Trash', 'error');
    }
    refreshTrashBadge();
}

/**
 * Empty the trash from the desktop icon. Delegates to Files so the
 * confirmation dialog, DELETE request and success toast stay in one place.
 */
export async function emptyTrashFromDesktop() {
    try {
        if (typeof window.fmEmptyTrash !== 'function') {
            addNotification('Files is unavailable', 'error');
            return;
        }
        await window.fmEmptyTrash();
    } catch (err) {
        addNotification(err?.detail || err?.message || 'Could not empty Trash', 'error');
    } finally {
        refreshTrashBadge();
    }
}

function _clearDropHint(icon) {
    if (icon) icon.classList.remove('trash-drop-target');
}

async function _moveItemsToTrash(raw) {
    let items;
    try { items = JSON.parse(raw || '[]'); } catch { return; }
    if (!Array.isArray(items) || items.length === 0) return;
    if (_dropBusy) return;
    _dropBusy = true;
    try {
        const paths = items.map(it => it.path).filter(Boolean);
        if (!paths.length) return;
        // Delegate to Files: the confirmation, the single op-tracked
        // request and the per-item failure reasons live there (same pattern as
        // emptyTrashFromDesktop). One request instead of N.
        if (typeof window.fmTrashPaths === 'function') {
            await window.fmTrashPaths(paths);
        } else {
            const resp = await apiFetch('/api/files/trash/batch', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paths }),
            });
            if (!resp.ok) {
                addNotification('Could not move items to Trash', 'error');
                return;
            }
        }
        document.dispatchEvent(new CustomEvent('ethos:trash-changed'));
        refreshTrashBadge();
    } catch (err) {
        addNotification(err?.detail || err?.message || 'Could not move items to Trash', 'error');
    } finally {
        _dropBusy = false;
    }
}

function _attachDropHandlers(container) {
    const isFileDrag = (e) => {
        const types = e.dataTransfer ? Array.from(e.dataTransfer.types || []) : [];
        return types.includes(FM_DND_MIME);
    };
    const iconFromEvent = (e) => (e.target && e.target.closest ? e.target.closest(TRASH_ICON_SELECTOR) : null);

    // Capture phase: claim the drop before the document-wide desktop drop zone
    // (which exists for pinning apps dragged out of the App Drawer).
    container.addEventListener('dragover', (e) => {
        if (!isFileDrag(e)) return;
        const icon = iconFromEvent(e);
        if (!icon) return;
        e.preventDefault();
        e.stopPropagation();
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
        icon.classList.add('trash-drop-target');
    }, true);

    container.addEventListener('dragleave', (e) => {
        if (!isFileDrag(e)) return;
        _clearDropHint(iconFromEvent(e));
    }, true);

    container.addEventListener('drop', (e) => {
        if (!isFileDrag(e)) return;
        const icon = iconFromEvent(e);
        _clearDropHint(icon);
        if (!icon) return; // files dropped on empty desktop: ignore
        e.preventDefault();
        e.stopPropagation();
        _moveItemsToTrash(e.dataTransfer ? e.dataTransfer.getData(FM_DND_MIME) : '');
    }, true);

    // dragend fires on the source even when the drop is cancelled.
    document.addEventListener('dragend', () => {
        document.querySelectorAll('.trash-drop-target').forEach(el => el.classList.remove('trash-drop-target'));
    }, true);
}

/**
 * Wire the desktop Trash shortcut: drop target, badge polling and refresh
 * triggers. Idempotent — safe to call once during bootstrap.
 */
export function initDesktopTrash() {
    const container = document.getElementById('desktopIcons');
    if (container && !container._trashDropBound) {
        _attachDropHandlers(container);
        container._trashDropBound = true;
    }
    if (!document._trashBadgeBound) {
        document._trashBadgeBound = true;
        // Files moved to / restored from trash (also by another client) — the
        // Files WS handler dispatches this before its no-window return.
        document.addEventListener('ethos:trash-changed', () => _scheduleRefresh());
        document.addEventListener('visibilitychange', () => {
            if (!document.hidden) refreshTrashBadge();
        });
    }
    if (!_pollTimer) {
        _pollTimer = setInterval(() => {
            if (!document.hidden) refreshTrashBadge();
        }, POLL_MS);
    }
    refreshTrashBadge();
}
