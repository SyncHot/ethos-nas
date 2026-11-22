// EthOS NAS — File Manager App (multi-window Nautilus-style with unified components)
import { escapeAttr, escapeHtml, formatBytes, getFileIcon, hexToRgb, jsAttr, safeJsStr } from '../core/utils.js';
import { onAppClose, openWindows } from '../core/window-manager.js';
import { apiFetch, apiJsonFetch, isAdmin, readErrorDetail } from '../core/api.js';
import { connectWs } from '../core/ws.js';
import { getUploadStatus, showUploadPanel, startUpload } from '../core/upload-engine.js';
import { startTask } from '../core/progress.js';
import { addNotification, toggleNotificationDrawer } from '../core/notification-center.js';
import { pickFolder } from '../core/file-picker.js';
import { showAlert, showConfirm as showModalConfirm, showFormModal, showPrompt } from '../core/modal.js';
import { openShareDialog } from '../core/share-dialog.js';
import { fetchIncoming } from '../core/sharing-ui.js';

export const fmWindowStates = new Map();

// The Code Editor is a Packages package now, so nothing is imported here.
// `app.js` owns resolution: it calls the installed package's provider
// (`window.__ethosEditorOpen`) and otherwise opens the package app icon.
const openEditorForPath = (path) => {
    if (typeof window.openEditorForPath === 'function') { window.openEditorForPath(path); return; }
    if (path) window.__fmEditorCtx = { path };
    window.openApp?.('editor');
};

// ── Persisted UI preferences (view mode / sort order / hidden files) ──────────
// Users expect a file manager to remember how they like it; previously every
// new window reset to grid + name-asc and forgot the hidden-files toggle.
function _fmPref(key, fallback) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : v; } catch { return fallback; }
}
export function _fmSavePref(key, value) { try { localStorage.setItem(key, value); } catch { /* storage disabled */ } }
export function _fmLoadSortPref() {
    try {
        const v = JSON.parse(_fmPref('ethos-fm-sort', 'null'));
        if (v && typeof v.col === 'string') return { col: v.col, asc: v.asc !== false };
    } catch { /* corrupt preference — fall back to defaults */ }
    return { col: 'name', asc: true };
}
export function _fmSaveSortPref(state) {
    if (state) _fmSavePref('ethos-fm-sort', JSON.stringify({ col: state.sortCol || 'name', asc: state.sortAsc !== false }));
}

/** True when the signed-in JWT carries `role: admin` (admin-only trash overview). */
function _fmIsAdmin() {
    try {
        const token = localStorage.getItem('token') || '';
        const part = token.split('.')[1] || '';
        if (!part) return false;
        let b64 = part.replace(/-/g, '+').replace(/_/g, '/');
        while (b64.length % 4) b64 += '=';
        const payload = JSON.parse(atob(b64));
        return String(payload.role || '') === 'admin';
    } catch (_) {
        return false;
    }
}

function _getFmState(winId) {
    if (!fmWindowStates.has(winId)) {
        const view = _fmPref('ethos-fm-view', 'grid');
        const sort = _fmLoadSortPref();
        fmWindowStates.set(winId, {
            path: '/', viewMode: view === 'list' ? 'list' : 'grid', selected: new Set(), clipboard: null,
            clipboardOp: null, history: ['/'], historyIndex: 0, contextMenu: null,
            sortCol: sort.col, sortAsc: sort.asc, _rootEl: null,
            _showHidden: _fmPref('ethos-fm-hidden', '0') === '1',
            _trashScope: 'mine',
        });
    }
    return fmWindowStates.get(winId);
}

function _getFocusedWindowId() {
    let bestWin = null, bestZ = -1;
    for (const [k, v] of openWindows.entries()) {
        if (v.appId === 'files' && v.zIndex > bestZ) { bestWin = k; bestZ = v.zIndex; }
    }
    return bestWin || null;
}

function _getActiveFmRoot() {
    const winId = _getFocusedWindowId(); if (!winId) return null;
    const w = openWindows.get(winId); if (!w) return null;
    return w.element.querySelector('.fm-container') || w.element;
}

const svgIcon = (path, size) => `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;

function _getFmStateForRoot(root) {
    const winId = root?.dataset?.fmWin || (root?.closest && root.closest('[data-fm-win]')?.dataset?.fmWin);
    return winId ? _getFmState(winId) : null;
}

// ── Sidebar config ──
const SIDEBAR_FAVORITES = [
    { path: '/', label: 'Root', iconPath: '<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>', iconColor: '#5b9aff' },
    { path: '/home', label: 'Home', iconPath: '<path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><polyline points="9,22 9,12 15,12 15,22"/>', iconColor: '#22c55e' },
];

// ── Bookmarks / Favorites (persisted per-user via /api/files/favorites) ──────
let _fmFavorites = [];        // [{ path, name, exists }]
let _fmFavSet = new Set();    // set of bookmarked paths for O(1) lookup
let _fmFavsLoaded = false;
let _pendingFmPath = null;    // path to open in a newly created FM window
let _fmShared = [];           // received path grants [{id, owner, resource, label}]
let _fmSharedLoaded = false;

// ── Shared clipboard (all Files windows) ──────────────────────────
// Standard desktop semantics: Ctrl+C / Ctrl+X in one window can be pasted into
// any other Files window (previously the clipboard lived inside a single
// window's state, so cross-window copy was impossible without drag & drop).
let _fmClipboard = { op: null, paths: [] };
function _fmClipboardEmpty() { return !_fmClipboard.op || _fmClipboard.paths.length === 0; }
function _fmClearClipboard() { _fmClipboard = { op: null, paths: [] }; }


const FM_DENSITY_KEY = 'ethos-fm-density';
const FM_STAR_PATH = '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>';

function _fmDensity() { try { return localStorage.getItem(FM_DENSITY_KEY) || 'small'; } catch { return 'small'; } }
export function fmSetDensity(mode) {
    if (!['small', 'medium', 'large'].includes(mode)) mode = 'small';
    try { localStorage.setItem(FM_DENSITY_KEY, mode); } catch {}
    document.querySelectorAll('.fm-content').forEach(el => {
        el.classList.remove('fm-size-small', 'fm-size-medium', 'fm-size-large');
        if (mode !== 'medium') el.classList.add('fm-size-' + mode);
    });
    const labels = { small: 'compact', medium: 'cozy', large: 'comfortable' };
    document.querySelectorAll('[data-testid="fm-density-btn"]').forEach(btn => { btn.title = `Density: ${labels[mode]} (click to change)`; });
}
export function fmToggleDensity() {
    const order = ['small', 'medium', 'large'];
    fmSetDensity(order[(order.indexOf(_fmDensity()) + 1) % order.length]);
}

// ── Cinematic video preview (grid): enlarge video thumbnails up to 507×285 (16:9) ──
const FM_VIDEO_XL_KEY = 'ethos-fm-video-xl';
function _fmVideoXL() { try { return localStorage.getItem(FM_VIDEO_XL_KEY) === '1'; } catch { return false; } }
export function fmSetVideoXL(on) {
    on = !!on;
    try { localStorage.setItem(FM_VIDEO_XL_KEY, on ? '1' : '0'); } catch {}
    document.querySelectorAll('.fm-content').forEach(el => el.classList.toggle('fm-video-xl', on));
    _syncVideoXlButtons();
    // Re-render open grid views so video thumbnails request the right resolution (320 vs 640)
    for (const st of fmWindowStates.values()) {
        const root = st._rootEl;
        if (!root || st.viewMode !== 'grid' || st._inTrash || st._searchTerm) continue;
        _silentRefetch(root);
    }
}
export function fmToggleVideoXL() { fmSetVideoXL(!_fmVideoXL()); }
function _syncVideoXlButtons() {
    const on = _fmVideoXL();
    document.querySelectorAll('[data-testid="fm-video-xl-btn"]').forEach(btn => {
        btn.classList.toggle('active', on);
        btn.title = `Video preview: ${on ? 'cinematic (up to 507×285)' : 'compact'} — click to change`;
    });
}

function _fmIsFavorite(p) { return _fmFavSet.has(p); }

export async function fmLoadFavorites() {
    try {
        const resp = await apiFetch('/api/files/favorites');
        if (!resp.ok) return;
        const data = await resp.json();
        _fmFavorites = data.favorites || [];
        _fmFavSet = new Set(_fmFavorites.map(f => f.path));
        _fmFavsLoaded = true;
        _renderAllFmFavorites();
    } catch { /* non-fatal — bookmarks are optional */ }
}

function _fmFavoritesHtml(activePath) {
    if (!_fmFavsLoaded) return '<div class="fm-sidebar-item fm-empty">Loading…</div>';
    if (_fmFavorites.length === 0) return '<div class="fm-sidebar-item fm-empty">Right-click a folder → Add to Favorites</div>';
    return _fmFavorites.map(f => {
        const active = f.path === activePath ? 'active' : '';
        // The API prunes deleted folders; the only `exists:false` entries left
        // are favorites on removable media whose volume is currently offline.
        const missing = f.exists === false ? 'fm-fav-missing' : '';
        const offline = f.exists === false ? ' (not mounted)' : '';
        return `<div class="fm-sidebar-item ${active} ${missing}" data-path="${escapeAttr(f.path)}" onclick="fmNavigateTo('${safeJsStr(f.path)}')" title="${escapeAttr(f.path)}${offline}"><span class="fm-sidebar-icon" style="background:rgba(245,181,63,0.14);color:#f5b53f;">${svgIcon(FM_STAR_PATH, 16)}</span><span>${escapeHtml(f.name)}</span></div>`;
    }).join('');
}

function _fmSharedHtml(activePath) {
    if (!_fmSharedLoaded) return '<div class="fm-sidebar-item fm-empty">Loading…</div>';
    if (!_fmShared.length) return '<div class="fm-sidebar-item fm-empty">Nothing shared with you</div>';
    const rows = _fmShared.slice(0, 20).map((grant) => {
        const path = grant.resource || '';
        const active = activePath === path || (activePath || '').startsWith(path + '/') ? 'active' : '';
        const label = grant.label || path.split('/').filter(Boolean).pop() || path;
        return `<div class="fm-sidebar-item ${active}" data-path="${escapeAttr(path)}" onclick="fmNavigateTo('${jsAttr(path)}')" title="${escapeAttr(grant.owner + ' · ' + path)}"><span class="fm-sidebar-icon" style="background:rgba(45,212,191,0.14);color:#2dd4bf;">${svgIcon('<circle cx="9" cy="8" r="3"/><path d="M3.5 20c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/><circle cx="17.5" cy="9" r="2.5"/><path d="M14.8 14.6c2.9-.4 5.2 1.6 5.2 4.4"/>', 16)}</span><span>${escapeHtml(label)}</span></div>`;
    }).join('');
    return rows;
}

async function fmLoadShared() {
    try {
        _fmShared = await fetchIncoming('path');
    } catch (err) {
        _fmShared = [];
    } finally {
        _fmSharedLoaded = true;
        document.querySelectorAll('[data-fm-win]').forEach((root) => { fmRenderSidebar(root); });
    }
}

function _renderAllFmFavorites() {
    document.querySelectorAll('.fm-container').forEach(root => {
        const el = root.querySelector('.fm-sidebar-favs');
        if (!el) return;
        const state = _getFmStateForRoot(root);
        el.innerHTML = _fmFavoritesHtml(state ? state.path : '/');
    });
}

// Update star badges on already-rendered grid/list items without a full reload
function _refreshFmStarBadges(root) {
    if (!root) return;
    root.querySelectorAll('.fm-file-item, .fm-list-row').forEach(el => {
        const p = el.getAttribute('data-path');
        if (!p) return;
        const fav = _fmIsFavorite(p);
        let star = el.querySelector('.fm-item-star');
        if (fav && !star) {
            star = document.createElement('span');
            star.className = 'fm-item-star';
            star.textContent = '★';
            (el.querySelector('.fm-list-ic') || el).appendChild(star);
        } else if (!fav && star) {
            star.remove();
        }
    });
}

export async function fmToggleFavorite(path) {
    const starred = !_fmIsFavorite(path);
    try {
        const resp = await apiFetch('/api/files/favorites', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, starred }) });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        addNotification(starred ? `Added to Favorites: ${path}` : `Removed from Favorites: ${path}`, 'success');
    } catch (e) { addNotification(e.message, 'error'); return; }
    await fmLoadFavorites();
    const root = _getActiveFmRoot();
    if (root) {
        _refreshFmStarBadges(root);
        const st = _getFmStateForRoot(root);
        const starBtn = root.querySelector('[data-testid="fm-star-btn"]');
        if (starBtn && st && path === st.path) starBtn.classList.toggle('active', starred);
    }
}

export function fmToggleCurrentFolderFavorite() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    fmToggleFavorite(state.path);
}

export function fmOpenInNewWindow(path) {
    _pendingFmPath = path || '/';
    window.openApp('files');
}

function _fmShortDate(ts) { return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) : ''; }
function _fmFullDate(ts) { const d = new Date(ts * 1000); return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }) + ', ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }); }

// Empty-folder state — a dead end without a call to action, so offer the two
// things a user actually wants here. Hidden in read-only folders.
const _FM_EMPTY_ICON = '<svg width="76" height="76" viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" '
    + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M6 17a3 3 0 0 1 3-3h13l4 5h25a3 3 0 0 1 3 3v27a3 3 0 0 1-3 3H9a3 3 0 0 1-3-3z"/>'
    + '<path d="M6 26h52" opacity=".55"/></svg>';
const _FM_EMPTY_STATE_HTML = '<div class="fm-empty-state">' +
    `<div class="fm-empty-ic">${_FM_EMPTY_ICON}</div>` +
    '<div class="fm-empty-title">This folder is empty</div>' +
    '<div class="fm-empty-sub">Drop files here to upload</div>' +
    '<div style="display:flex;gap:8px;margin-top:6px;">' +
    '<button class="fm-act-btn fm-empty-create" onclick="fmNewFolder()">＋ New folder</button>' +
    '<button class="fm-act-btn fm-empty-create" onclick="this.closest(\'.fm-container\').querySelector(\'input.fm-file-input\')?.click()">⬆ Upload files</button>' +
    '</div></div>';

// ── File Manager initialization ──────────────────────────────────────────────

export function initFileManager(body) {
    const winEl = body.closest('.app-window'); let winId = null;
    for (const [id, w] of openWindows) { if (w.element === winEl) { winId = id; break; } }
    winId = winId || 'unknown';
    if (!fmWindowStates.has(winId)) _getFmState(winId);
    const state = fmWindowStates.get(winId);

    body.innerHTML = `
        <div class="fm-container" data-fm-win="${escapeAttr(winId)}">
            <aside class="fm-sidebar">
                <div class="fm-side-scroll" role="navigation" aria-label="Places" tabindex="0">
                    <div class="fm-section-header">Quick Access</div><div class="fm-sidebar-favorites"></div>
                    <div class="fm-section-divider"></div>
                    <div class="fm-section-header">Favorites</div><div class="fm-sidebar-favs" data-testid="fm-sidebar-favorites"></div>
                    <div class="fm-section-divider"></div>
                    <div class="fm-section-header">Shared with me</div><div class="fm-sidebar-shared" data-testid="fm-sidebar-shared"></div>
                    <div class="fm-section-divider"></div>
                    <div class="fm-section-header fm-trash-drop" data-fm-trash="1" style="cursor:pointer;" onclick="fmOpenTrash()" title="Open Trash — drag files here to move them to Trash">🗑 Trash</div><div class="fm-sidebar-trash" style="min-height:4px;"></div>
                    <div class="fm-section-divider"></div>
                    <div class="fm-section-header">Removable</div><div class="fm-sidebar-usb" style="min-height:8px;"></div>
                    <div class="fm-section-divider"></div>
                    <div class="fm-section-header">Mounts</div><div class="fm-sidebar-devices"></div>
                </div>
                <div class="fm-side-storage" data-testid="fm-side-storage" title="Volume usage">
                    <div class="fm-side-storage-bar"><div class="fm-side-storage-fill" style="width:0%"></div></div>
                    <div class="fm-side-storage-text">—</div>
                </div>
            </aside>
            <main class="fm-main">
                <div class="fm-toolbar">
                    <div class="fm-toolbar-nav">
                        <button class="fm-nav-btn" onclick="toggleSidebar()" title="Toggle sidebar">${svgIcon('<line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>', 18)}</button>
                    <div style="width:1px;height:24px;background:var(--fm-border);margin:0 4px;"></div>
                    <button class="fm-nav-btn" data-testid="fm-nav-back" onclick="fmNavigateBack()" title="Back (Alt+←)" aria-label="Back">${svgIcon('<polyline points="15,4 9,12 15,20"/>', 18)}</button>
                    <button class="fm-nav-btn" data-testid="fm-nav-forward" onclick="fmNavigateForward()" title="Forward (Alt+→)" aria-label="Forward">${svgIcon('<polyline points="9,4 15,12 9,20"/>', 18)}</button>
                    <div class="fm-pathbar" data-testid="fm-pathbar" onclick="fmPathbarClick(event)" title="Ctrl+L — type a path">
                        <div class="fm-path-crumbs fm-breadcrumb" role="navigation" aria-label="Current folder"></div>
                        <input class="fm-path-input" spellcheck="false" autocomplete="off" aria-label="Path" placeholder="/path/to/folder" onkeydown="fmPathInputKey(event)" onblur="fmPathInputBlur(event)">
                        <button type="button" class="fm-path-edit" data-testid="fm-path-edit" title="Edit path (Ctrl+L)" aria-label="Edit path" onclick="event.stopPropagation();fmStartPathEdit()">${svgIcon('<path d="M17 3a2.85 2.83 0 114 4L7.5 20.5 2 22l1.5-5.5z"/>', 13)}</button>
                    </div>
                        <div class="fm-search-box" role="search">
                            <span style="display:flex;color:var(--fm-secondary);">${svgIcon('<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/>', 14)}</span>
                            <input type="text" id="fm-search-input-${winId}" class="fm-search-input" placeholder="Search files..." onkeydown="if(event.key==='Enter')fmSearch();if(event.key==='Escape'){this.value='';fmSearch()}" oninput="fmSearchInput(this)">
                            <button type="button" class="fm-search-clear" data-testid="fm-search-clear" style="display:none" title="Clear search" onclick="const i=document.getElementById('fm-search-input-${winId}');i.value='';fmSearch();i.focus()">&times;</button>
                        </div>
                    </div>
                    <div class="fm-toolbar-actions">
                        <button class="fm-nav-btn" data-testid="fm-upload-btn" onclick="this.closest('.fm-container').querySelector('input.fm-file-input')?.click()" title="Upload files">
                            ${svgIcon('<path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2 2v-4"/><polyline points="17,8 12,3 7,8"/><line x1="12" y1="3" x2="12" y2="15"/>', 16)}
                        </button>
                        <input type="file" class="fm-file-input" style="display:none" multiple onchange="fmUploadFiles(this.files)">
                        <button class="fm-nav-btn" data-testid="fm-new-folder-btn" onclick="fmNewFolder()" title="New folder">${svgIcon('<path d="M12 5v14M5 12h14"/>', 16)}</button>
                        <div style="width:1px;height:20px;background:var(--fm-border);margin:0 3px;"></div>
                        <button class="fm-nav-btn" data-testid="fm-copy-btn" onclick="fmCopySelected()" title="Copy selected items (Ctrl+C)">
                            ${svgIcon('<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>', 16)}
                        </button>
                        <button class="fm-nav-btn" data-testid="fm-cut-btn" onclick="fmCutSelected()" title="Cut selected items (Ctrl+X)">
                            ${svgIcon('<path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/>', 16)}
                        </button>
                        <button class="fm-nav-btn" data-testid="fm-paste-btn" onclick="fmPaste()" title="Paste from clipboard (Ctrl+V)">
                            ${svgIcon('<path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>', 16)}
                        </button>
                        <button class="fm-nav-btn" data-testid="fm-zip-btn" onclick="fmZipSelected()" title="Download selected as ZIP archive">
                            ${svgIcon('<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/>', 16)}
                        </button>
                        <button class="fm-nav-btn" data-testid="fm-refresh-btn" onclick="fmRefresh()" title="Refresh file list (F5)">${svgIcon('<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>', 16)}</button>
                        <div class="fm-toolbar-spacer"></div>
                        <button class="fm-nav-btn ${state._showHidden ? 'active' : ''}" data-testid="fm-toggle-hidden" onclick="fmToggleHidden()" title="${state._showHidden ? 'Hide' : 'Show'} hidden files">
                            ${svgIcon('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1.5" fill="currentColor"/><circle cx="4" cy="12" r="1.5" fill="currentColor"/>', 16)}
                        </button>
                        <button class="fm-nav-btn" data-testid="fm-density-btn" onclick="fmToggleDensity()" title="Density: compact (click to change)">
                            ${svgIcon('<rect x="3" y="3" width="18" height="4"/><rect x="3" y="10" width="18" height="4"/><rect x="3" y="17" width="18" height="4"/>', 16)}
                        </button>
                        <button class="fm-nav-btn ${_fmVideoXL() ? 'active' : ''}" data-testid="fm-video-xl-btn" onclick="fmToggleVideoXL()" title="${_fmVideoXL() ? 'Video preview: cinematic (up to 507×285)' : 'Video preview: compact'} — click to change">
                            ${svgIcon('<rect x="2" y="5" width="14" height="14" rx="2"/><polygon points="16,9 22,5 22,19 16,15"/>', 16)}
                        </button>
                        <div style="width:1px;height:20px;background:var(--fm-border);margin:0 3px;"></div>
                        <div class="fm-segmented" role="group" aria-label="View mode">
                            <button class="fm-seg-btn ${state.viewMode === 'grid' ? 'active' : ''}" data-testid="fm-view-grid" onclick="fmSetViewMode('grid')" title="Grid view">
                                ${svgIcon('<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>', 15)}
                            </button>
                            <button class="fm-seg-btn ${state.viewMode === 'list' ? 'active' : ''}" data-testid="fm-view-list" onclick="fmSetViewMode('list')" title="List view">
                                ${svgIcon('<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/>', 15)}
                            </button>
                        </div>
                        <button class="fm-nav-btn" data-testid="fm-sort-btn" onclick="fmToggleSortMenu(event)" title="Sort by: Name — ascending">
                            ${svgIcon('<path d="m3 16 4 4 4-4"/><path d="M7 20V4"/><path d="m21 8-4-4-4 4"/><path d="M17 4v16"/>', 15)}
                        </button>
                        <button class="fm-nav-btn" data-testid="fm-star-btn" onclick="fmToggleCurrentFolderFavorite()" title="Star / unstar this folder (Ctrl+D)">
                            ${svgIcon('<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>', 16)}
                        </button>
                    </div>
                </div>
                <div class="fm-grid fm-content" aria-label="Folder contents"
                     role="group" tabindex="0"></div>
                <div class="fm-drop-zone" data-testid="fm-drop-zone">
                    <div class="fm-drop-zone-content">
                        <span class="fm-drop-icon">📁</span>
                        <span class="fm-drop-text">Drop files anywhere to upload</span>
                        <span class="fm-drop-hint">or use the Upload button above</span>
                    </div>
                </div>
                <footer class="fm-statusbar" role="status" aria-live="polite"><span class="fm-status-left"><span data-testid="fm-statusbar-ro" style="display:none;color:#f59e0b;">🔒 read-only · </span><span data-testid="fm-statusbar-upload" class="fm-status-upload" style="display:none;" title="Upload status — click to open the upload panel"></span><span data-testid="fm-statusbar-selected" style="display:none;"></span><span data-testid="fm-statusbar-items">—</span></span><span class="fm-status-right"><span data-testid="fm-statusbar-free"></span></span></footer>
            </main></div>`;
    const container = body.querySelector('.fm-container');
    if (container && fmWindowStates.has(winId)) Object.assign(fmWindowStates.get(winId), { _rootEl: container });
    // Upload status chip: uploads are app-wide (one progress panel), so every
    // File Manager window mirrors the same summary; clicking it re-opens the panel.
    const uploadChip = container.querySelector('[data-testid="fm-statusbar-upload"]');
    if (uploadChip) {
        uploadChip.addEventListener('click', () => showUploadPanel());
        _fmRenderUploadChip(container, getUploadStatus());
    }
    // Click on empty area clears the selection
    const contentEl = container.querySelector('.fm-content');
    if (contentEl) {
        contentEl.addEventListener('click', (e) => {
            const st = _getFmStateForRoot(container);
            // A marquee drag ends with a click on the empty background — keep the
            // items it just selected instead of clearing them again.
            if (st && st._marqueeJustDragged) { st._marqueeJustDragged = false; return; }
            if (e.target === contentEl || e.target.classList.contains('fm-grid') || e.target.classList.contains('fm-list-table')) {
                if (st && st.selected.size > 0) { st.selected.clear(); st._anchorName = null; fmRenderGridItems(); }
            }
        });
        // Right-click on empty space → New Folder / New File / Paste / Refresh menu
        contentEl.addEventListener('contextmenu', (e) => fmShowItemContextMenuOrBackground(e, container));
    }
    // Apply persisted density preference (default: compact)
    const _density = _fmDensity();
    if (_density === 'small') contentEl.classList.add('fm-size-small');
    else if (_density === 'large') contentEl.classList.add('fm-size-large');
    if (_fmVideoXL()) contentEl.classList.add('fm-video-xl');

    // Open a specific path if requested (e.g. "Open in New Window"), otherwise
    // reopen the folder this browser last looked at — a file manager that forgets
    // where you were is a daily papercut.
    if (window.__fmPendingPath) { _pendingFmPath = window.__fmPendingPath; delete window.__fmPendingPath; }
    if (_pendingFmPath) { const p = _pendingFmPath; _pendingFmPath = null; state.path = p; state.history = [p]; state.historyIndex = 0; }
    else if (!state._openedOnce) {
        const last = _fmPref('ethos-fm-last-path', null);
        if (last && last !== state.path) {
            state.path = last; state.history = [last]; state.historyIndex = 0; state._restoredPath = true;
        }
    }
    state._openedOnce = true;
    _fmSyncNavButtons(container, state);

    // Initialize drag-and-drop upload (T-FM3)
    _initFmDragDrop(container);
    _initFmItemDnd(container);
    _initFmMarquee(container);
    fmLoadFavorites();
    fmLoadShared();
    // Load into THIS window explicitly. A bare fmLoad() resolves the "active"
    // window by z-index, and a freshly opened second window was not always the
    // topmost one yet — the listing (and its state) then landed in the other
    // File Manager window and the new one stayed blank.
    fmLoad(undefined, container);
}

// ── T-FM3: Drag & Drop Upload ────────────────────────────────────────────────

let _fmDragCounter = 0;
const FM_DND_MIME = 'application/x-ethos-fm-items';
let _fmDragCtx = null; // { root } — window where an internal item drag started

function _isInternalFmDrag(e) {
    const types = e.dataTransfer ? Array.from(e.dataTransfer.types || []) : [];
    return types.includes(FM_DND_MIME);
}

function _fmClearDndHighlights() {
    document.querySelectorAll('.fm-drop-target').forEach(el => el.classList.remove('fm-drop-target'));
    document.querySelectorAll('.fm-content.fm-dnd-active').forEach(el => el.classList.remove('fm-dnd-active'));
}

function _fmItemElFromEvent(e, container) {
    const t = e.target;
    if (!t || !t.closest) return null;
    const el = t.closest('.fm-file-item, .fm-list-row');
    return (el && container.contains(el)) ? el : null;
}

function _fmIsDirItem(el) {
    return el.getAttribute('data-isdir') === 'true' || el.getAttribute('data-isDir') === 'true';
}

function _initFmDragDrop(container) {
    if (!container) return;
    const dropZone = container.querySelector('[data-testid="fm-drop-zone"]');
    if (!dropZone) return;

    container.addEventListener('dragenter', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (_isInternalFmDrag(e)) return;
        _fmDragCounter++;
        dropZone.classList.add('fm-drop-active');
    });

    container.addEventListener('dragleave', (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (_isInternalFmDrag(e)) return;
        _fmDragCounter--;
        if (_fmDragCounter <= 0) {
            _fmDragCounter = 0;
            dropZone.classList.remove('fm-drop-active');
        }
    });

    container.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.stopPropagation();
    });

    container.addEventListener('drop', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        if (_isInternalFmDrag(e)) return;
        _fmDragCounter = 0;
        dropZone.classList.remove('fm-drop-active');
        const files = Array.from(e.dataTransfer?.files || []);
        if (files.length > 0) {
            // Use existing fmUploadFiles for consistency
            await fmUploadFiles(files);
        }
    });
}

// ── Internal drag & drop: move items between folders / windows ───────────────

function _initFmItemDnd(container) {
    if (!container) return;
    const contentEl = container.querySelector('.fm-content');
    let lastTargetEl = null;

    const setDropTarget = (el) => {
        if (lastTargetEl === el) return;
        if (lastTargetEl) lastTargetEl.classList.remove('fm-drop-target');
        lastTargetEl = el;
        if (el) el.classList.add('fm-drop-target');
    };

    const resolveDest = (e, state, dropContainer) => {
        let dest = null;
        const itemEl = _fmItemElFromEvent(e, dropContainer);
        if (itemEl && itemEl.getAttribute('data-name') !== '..' && _fmIsDirItem(itemEl)) {
            dest = itemEl.getAttribute('data-path');
        } else {
            const t = e.target;
            const trash = (t && t.closest) ? t.closest('[data-fm-trash]') : null;
            if (trash) return '__trash__';   // drop on the sidebar Trash → move to trash
            const sb = (t && t.closest) ? t.closest('.fm-sidebar-item[data-path]') : null;
            if (sb) dest = sb.dataset.path;
        }
        return (dest && dest.startsWith('/')) ? dest : state.path;
    };

    container.addEventListener('dragstart', (e) => {
        const itemEl = _fmItemElFromEvent(e, container);
        if (!itemEl) return;
        const name = itemEl.getAttribute('data-name');
        if (!name || name === '..') { e.preventDefault(); return; }
        const state = _getFmStateForRoot(container);
        if (!state) return;
        // Grabbing an unselected item selects just it (Nautilus behavior)
        if (!state.selected.has(name)) {
            state.selected.clear();
            state.selected.add(name);
            state._anchorName = name;
            fmRenderGridItems(container);
        }
        const metaByName = new Map();
        container.querySelectorAll('.fm-file-item[data-name], .fm-list-row[data-name]').forEach(el => {
            const n = el.getAttribute('data-name');
            if (n && n !== '..') metaByName.set(n, { path: el.getAttribute('data-path'), is_dir: _fmIsDirItem(el) });
        });
        const items = [...state.selected].map(n => {
            const m = metaByName.get(n);
            return { name: n, path: (m && m.path) || _fmJoin(state.path, n), is_dir: !!(m && m.is_dir) };
        }).filter(i => i.path && i.path.startsWith('/'));
        if (items.length === 0) { e.preventDefault(); return; }
        e.dataTransfer.effectAllowed = 'move';
        try {
            e.dataTransfer.setData(FM_DND_MIME, JSON.stringify(items));
            e.dataTransfer.setData('text/plain', items.map(i => i.name).join('\n'));
        } catch {}
        _fmDragCtx = { root: container };
        requestAnimationFrame(() => {
            container.querySelectorAll('.fm-file-item.selected, .fm-list-row.selected').forEach(el => el.classList.add('fm-dragging'));
        });
    });

    container.addEventListener('dragend', () => {
        setDropTarget(null);
        if (contentEl) contentEl.classList.remove('fm-dnd-active');
        _fmClearDndHighlights();
        if (_fmDragCtx && _fmDragCtx.root === container) _fmDragCtx = null;
    });

    container.addEventListener('dragover', (e) => {
        if (!_isInternalFmDrag(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        const itemEl = _fmItemElFromEvent(e, container);
        let target = null;
        const t = e.target;
        const trashEl = (t && t.closest) ? t.closest('[data-fm-trash]') : null;
        if (trashEl) {
            target = trashEl;                       // drop onto Trash → recycle item
        } else if (itemEl && itemEl.getAttribute('data-name') !== '..' && _fmIsDirItem(itemEl)) {
            target = itemEl; // drop INTO this folder
        } else {
            const sb = (t && t.closest) ? t.closest('.fm-sidebar-item[data-path]') : null;
            if (sb) target = sb;
        }
        setDropTarget(target);
        if (contentEl) contentEl.classList.add('fm-dnd-active');
    });

    container.addEventListener('dragleave', (e) => {
        if (!_isInternalFmDrag(e)) return;
        if (e.relatedTarget && container.contains(e.relatedTarget)) return;
        setDropTarget(null);
        if (contentEl) contentEl.classList.remove('fm-dnd-active');
    });

    container.addEventListener('drop', async (e) => {
        if (!_isInternalFmDrag(e)) return;
        e.preventDefault();
        e.stopPropagation();
        // The drop may land in a DIFFERENT fm window than the drag started in
        // (cross-window move). Resolve the container from the event target.
        const dropEl = (e.target && e.target.closest) ? e.target.closest('[data-fm-win]') : null;
        const dropContainer = dropEl || container;
        const raw = e.dataTransfer.getData(FM_DND_MIME);
        setDropTarget(null);
        if (contentEl) contentEl.classList.remove('fm-dnd-active');
        _fmClearDndHighlights();
        let items;
        try { items = JSON.parse(raw || '[]'); } catch { return; }
        if (!Array.isArray(items) || items.length === 0) return;

        const state = _getFmStateForRoot(dropContainer);
        if (!state) return;
        const dest = resolveDest(e, state, dropContainer);

        // Dropped onto the sidebar Trash → soft-delete everything dragged.
        if (dest === '__trash__') {
            const srcRoot = (_fmDragCtx && _fmDragCtx.root) || container;
            const srcState = _getFmStateForRoot(srcRoot);
            if (srcState) srcState.selected.clear();
            _fmClearDndHighlights();
            if (_fmDragCtx && _fmDragCtx.root === container) _fmDragCtx = null;
            // One op-tracked request for the whole drop: single progress card,
            // per-item failure reasons, one confirmation.
            await fmTrashPaths(items.map(it => it.path), srcRoot);
            return;
        }

        // Reject dropping a folder into itself or its own subtree
        for (const it of items) {
            if (it.is_dir && (dest === it.path || dest.startsWith(it.path + '/'))) {
                addNotification('Cannot move a folder into itself', 'error');
                return;
            }
        }
        // Skip items already directly in the destination
        const toMove = items.filter(it => _fmJoin(dest, it.name) !== it.path);
        if (toMove.length === 0) { addNotification('Items are already in this folder', 'info'); return; }

        try {
            const resp = await apiFetch('/api/files/move', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sources: toMove.map(i => i.path), dest_dir: dest }),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            if (data.moved === 0 && data.failed > 0) { addNotification('Move failed', 'error'); return; }
            // Refresh source window (clear selection) and destination window
            const srcRoot = (_fmDragCtx && _fmDragCtx.root) || container;
            const srcState = _getFmStateForRoot(srcRoot);
            if (srcState) srcState.selected.clear();
            const rootsToRefresh = new Set([srcRoot, container]);
            for (const r of rootsToRefresh) { if (r) fmLoad(undefined, r); }
        } catch (err) {
            addNotification('Move failed: ' + err.message, 'error');
        } finally {
            _fmClearDndHighlights();
            if (_fmDragCtx && _fmDragCtx.root === container) _fmDragCtx = null;
        }
    });
}

// ── Rubber-band (marquee) selection ──────────────────────────────────────────
// Dragging on empty space selects every item the rectangle touches — the
// interaction every desktop file manager offers and the fastest way to grab a
// group of files without Ctrl-clicking each one.
let _fmMarqueeEndedAt = 0;
// A marquee drag ends with a click on whatever is under the pointer. Swallow it
// in the capture phase so it cannot re-select a single item / clear the result.
document.addEventListener('click', (e) => {
    if (Date.now() - _fmMarqueeEndedAt < 350) { e.stopPropagation(); e.preventDefault(); }
}, true);

function _initFmMarquee(container) {
    if (!container) return;
    const contentEl = container.querySelector('.fm-content');
    if (!contentEl) return;

    let startX = 0, startY = 0, box = null, active = false;
    let baseSelection = new Set(), snapshots = [], state = null;
    const root = container;

    const reset = () => {
        if (box) { box.remove(); box = null; }
        document.body.style.userSelect = '';
        active = false;
        snapshots = [];
    };

    contentEl.addEventListener('pointerdown', (e) => {
        if (e.pointerType !== 'mouse' || e.button !== 0) return;      // touch scrolls instead
        if (e.ctrlKey || e.metaKey) return;                           // Ctrl-click toggles selection
        if (e.target.closest('.fm-file-item, .fm-list-row, .fm-search-result, .fm-trash-row, .fm-list-table thead, .fm-trash-toolbar, .fm-empty-state')) return;
        const bounds = contentEl.getBoundingClientRect();
        if (e.clientX > bounds.left + contentEl.clientWidth) return;  // ignore scrollbar drags
        state = _getFmStateForRoot(container);
        if (!state) return;
        startX = e.clientX; startY = e.clientY;
        baseSelection = e.shiftKey ? new Set(state.selected) : new Set();
        snapshots = [];
        active = true;

        const onMove = (ev) => {
            if (!active) return;
            const dx = ev.clientX - startX, dy = ev.clientY - startY;
            if (!box && Math.hypot(dx, dy) < 4) return;  // plain click, not a drag
            if (!box) {
                box = document.createElement('div');
                box.className = 'fm-marquee';
                document.body.appendChild(box);
                // Snapshot item rectangles once the gesture really starts.
                contentEl.querySelectorAll('.fm-file-item[data-name], .fm-list-row[data-name]').forEach(el => {
                    const name = el.getAttribute('data-name');
                    if (!name || name === '..') return;
                    snapshots.push({ el, name, rect: el.getBoundingClientRect() });
                });
                document.body.style.userSelect = 'none';
            }
            const left = Math.min(startX, ev.clientX), top = Math.min(startY, ev.clientY);
            const right = Math.max(startX, ev.clientX), bottom = Math.max(startY, ev.clientY);
            box.style.cssText = `left:${left}px;top:${top}px;width:${right - left}px;height:${bottom - top}px`;
            const next = new Set(baseSelection);
            for (const s of snapshots) {
                const r = s.rect;
                const hit = !(r.right < left || r.left > right || r.bottom < top || r.top > bottom);
                if (hit) next.add(s.name);
                s.el.classList.toggle('selected', next.has(s.name));
            }
            state.selected = next;
            _fmUpdateSelectionStatus(root, state);
            _fmSyncActionButtons(root, state);
        };

        const onUp = () => {
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
            window.removeEventListener('pointercancel', onUp);
            if (box) {
                state._anchorName = [...state.selected].pop() || null;
                // The pointerup is followed by a click on the empty background —
                // suppress the click-to-clear so the new selection survives.
                state._marqueeJustDragged = true;
                _fmMarqueeEndedAt = Date.now();
                setTimeout(() => { state._marqueeJustDragged = false; }, 400);
            }
            reset();
        };
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp);
        window.addEventListener('pointercancel', onUp);
    });
}

export function toggleSidebar() { const root = _getActiveFmRoot(); if (!root) return; const sb = root.querySelector('.fm-sidebar'); if (sb) sb.classList.toggle('collapsed'); }

async function fmRenderSidebar(root) {
    const activeRoot = root || _getActiveFmRoot(); if (!activeRoot) return;
    const favsEl = activeRoot.querySelector('.fm-sidebar-favorites'), usbEl = activeRoot.querySelector('.fm-sidebar-usb'), devsEl = activeRoot.querySelector('.fm-sidebar-devices');
    if (!favsEl) return; const state = _getFmStateForRoot(activeRoot); if (!state) return;

    // Dynamic bookmarks (persisted per-user via /api/files/favorites)
    const bookmarksEl = activeRoot.querySelector('.fm-sidebar-favs');
    if (bookmarksEl) bookmarksEl.innerHTML = _fmFavoritesHtml(state.path);

    // Received shares (view-only) — click navigates into the granted path.
    const sharedEl = activeRoot.querySelector('.fm-sidebar-shared');
    if (sharedEl) sharedEl.innerHTML = _fmSharedHtml(state.path);

    favsEl.innerHTML = SIDEBAR_FAVORITES.map(f => {
        const act = f.path === state.path ? 'active' : '';
        return `<div class="fm-sidebar-item ${act}" data-path="${escapeAttr(f.path)}" onclick="fmNavigateTo('${jsAttr(f.path)}')"><span class="fm-sidebar-icon" style="background:rgba(${hexToRgb(f.iconColor)},0.12);color:${f.iconColor};">${svgIcon(f.iconPath, 16)}</span><span>${f.label}</span></div>`;
    }).join('');

    try { const r = await apiFetch('/api/storage/usb'); if (r.ok) { const d = await r.json(); let usbHtml = ''; if (d.usb_devices && d.usb_devices.length > 0) for (const ud of d.usb_devices) { const np = ud.mountpoint || null; if (!np) continue; const a = state.path === np || state.path.startsWith(np + '/') ? 'active' : ''; usbHtml += `<div class="fm-sidebar-item ${a}" data-path="${escapeAttr(np)}" onclick="fmNavigateTo('${jsAttr(np)}')" title="${escapeAttr(ud.label || ud.model)} — ${escapeAttr(ud.size)}"><span class="fm-sidebar-icon" style="background:rgba(245,158,11,0.15);color:#f59e0b;">${svgIcon('<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/>', 16)}</span><span>${escapeHtml(ud.label || ud.model || `${ud.name} (${ud.size})`)}</span></div>`; } if (usbEl) usbEl.innerHTML = usbHtml || '<div class="fm-sidebar-item fm-empty">No USB devices</div>'; } } catch{}

    try { const r2 = await apiFetch('/api/files/search-roots'); if (r2.ok) { const d2 = await r2.json(); let devHtml = ''; for (const m of (d2.roots || [])) { if (!m.path.startsWith('/mnt/') && !m.path.startsWith('/media/')) continue; const a = state.path === m.path || state.path.startsWith(m.path + '/') ? 'active' : ''; const label = (m.label.split(' (')[0] || m.path); devHtml += `<div class="fm-sidebar-item ${a}" data-path="${escapeAttr(m.path)}" onclick="fmNavigateTo('${jsAttr(m.path)}')" title="${escapeAttr(m.path)}"><span class="fm-sidebar-icon" style="background:rgba(59,130,246,0.12);color:#3b82f6;">${svgIcon('<path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>', 16)}</span><span>${escapeHtml(label)}</span></div>`; } if (devsEl) devsEl.innerHTML = devHtml || '<div class="fm-sidebar-item fm-empty">No mounts</div>'; } } catch{}
}

// ── Keyboard item navigation & type-ahead ────────────────────────────────────
// Arrow keys move a single selection through the listing (Shift extends it) and
// typing a name prefix jumps to the first match — both standard file-manager
// behaviours that were missing (only Enter / Escape were handled before).
let _fmTypeBuf = '';
let _fmTypeTimer = null;

function _fmNavigableItems(root) {
    return [...root.querySelectorAll('.fm-content .fm-file-item[data-name], .fm-content .fm-list-row[data-name]')]
        .filter(el => { const n = el.getAttribute('data-name'); return n && n !== '..'; });
}

function _fmGridColumnCount(items) {
    if (!items.length) return 1;
    const top = items[0].getBoundingClientRect().top;
    let n = 0;
    for (const el of items) {
        if (Math.abs(el.getBoundingClientRect().top - top) < 2) n++;
        else break;
    }
    return Math.max(1, n);
}

function _fmSelectOnlyElement(root, state, el, additive) {
    const name = el.getAttribute('data-name');
    if (!name) return;
    if (additive) state.selected.add(name);
    else { state.selected.clear(); state.selected.add(name); }
    state._anchorName = name;
    fmRenderGridItems(root);
    try { el.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch { /* jsdom / old browser */ }
}

// ── Keyboard Shortcuts ──

/** Focus the toolbar search box ( "/" shortcut ). */
function _fmFocusSearch(root) {
    const input = root && root.querySelector('.fm-search-input');
    if (input) { input.focus(); input.select(); }
}

function fmHandleKeyboard(e) {
    const tag = e.target.tagName; if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    // A modal dialog owns the keyboard while it is open (Delete/Esc must not
    // reach the listing behind it).
    if (e.defaultPrevented) return;
    if (document.querySelector('.ethos-modal-overlay, .unified-confirm-overlay')) return;
    // This is a document-level handler — only act when focus is inside a Files window.
    // Never steal shortcuts from other apps' windows (e.g. Terminal's Ctrl+C/Ctrl+V),
    // and never from the desktop: a click on empty wallpaper left targetWin null and
    // Shift+Delete then permanently deleted files in the last focused Files window.
    const targetWin = e.target.closest('.app-window');
    if (!targetWin) return;
    {
        const w = openWindows.get(targetWin.id);
        if (!w || w.appId !== 'files') return;
    }
    // Enter/Space on a focused toolbar button should activate the button, not the file manager
    if ((e.key === 'Enter' || e.key === ' ') && tag === 'BUTTON') return;
    // Don't process FM keyboard shortcuts when video player is open
    if (_videoPlayer.open) return;
    let afw = null; let afwZ = -1; for (const [k, v] of openWindows.entries()) {
        if (v.appId === 'files' && !v.element.classList.contains('minimized') && v.zIndex > afwZ) { afw = v; afwZ = v.zIndex; }
    }
    if (!afw) return;
    const winEl = afw.element;

    // ── Arrow-key item navigation + type-ahead ──────────────────────────────
    // Only in a real directory listing (not Trash / search results) and when no
    // modifier is held (Alt+Left/Right stays browser-style history navigation).
    const navRoot = winEl.querySelector('.fm-container');
    const navState = navRoot ? _getFmStateForRoot(navRoot) : null;
    if (navState && !navState._inTrash && !navState._searchTerm && !e.altKey && !e.ctrlKey && !e.metaKey) {
        const items = _fmNavigableItems(navRoot);
        if (items.length > 0) {
            const names = items.map(el => el.getAttribute('data-name'));
            const cur = names.findIndex(n => navState.selected.has(n));

            if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
                if (navState.viewMode === 'grid' || e.key === 'ArrowUp' || e.key === 'ArrowDown') {
                    e.preventDefault();
                    const cols = _fmGridColumnCount(items);
                    let delta = 0;
                    if (e.key === 'ArrowDown') delta = navState.viewMode === 'list' ? 1 : cols;
                    else if (e.key === 'ArrowUp') delta = navState.viewMode === 'list' ? -1 : -cols;
                    else if (e.key === 'ArrowLeft') delta = -1;
                    else if (e.key === 'ArrowRight') delta = 1;
                    const next = cur < 0
                        ? (delta < 0 ? items.length - 1 : 0)
                        : Math.max(0, Math.min(items.length - 1, cur + delta));
                    _fmSelectOnlyElement(navRoot, navState, items[next], e.shiftKey);
                    return;
                }
            }
            if (e.key === 'Home' || e.key === 'End') {
                e.preventDefault();
                _fmSelectOnlyElement(navRoot, navState, e.key === 'Home' ? items[0] : items[items.length - 1], e.shiftKey);
                return;
            }
            // Type-ahead: a printable character without modifiers jumps to the
            // first item whose name starts with the typed prefix. "/" and "?"
            // are reserved for the search box and the shortcut sheet.
            if (e.key.length === 1 && e.key !== ' ' && e.key !== '/' && e.key !== '?') {
                _fmTypeBuf += e.key.toLowerCase();
                clearTimeout(_fmTypeTimer);
                _fmTypeTimer = setTimeout(() => { _fmTypeBuf = ''; }, 800);
                const match = items.find(el => (el.getAttribute('data-name') || '').toLowerCase().startsWith(_fmTypeBuf));
                if (match) { e.preventDefault(); _fmSelectOnlyElement(navRoot, navState, match, false); }
                return;
            }
        }
    }

    // Normalise single-character keys: with Shift held, `e.key` is uppercase
    // ("N" for Ctrl+Shift+N) and would never match the lowercase switch cases.
    const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    switch (key) {
        case 'Backspace': if (!e.ctrlKey && !e.metaKey) { e.preventDefault(); fmGoUp(); } break;
        case 'Delete':
            e.preventDefault();
            if (e.shiftKey) fmHardDeleteSelected();  // Shift+Del = permanent
            else fmDeleteSelected();                 // Del = move to trash (reversible)
            break;
        case 'F2': e.preventDefault(); fmRenameSelected(); break;  // F2 triggers inline rename
        case 'F5': e.preventDefault(); fmRefresh(); break;         // F5 refreshes the file list
        case 'n': if ((e.ctrlKey || e.metaKey)) { e.preventDefault(); if (e.shiftKey) fmNewFolder(); else fmNewFile(); } break;
        case 'l': if (e.ctrlKey || e.metaKey) { e.preventDefault(); fmStartPathEdit(); } break;  // editable path
        case '/': if (!e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); _fmFocusSearch(winEl.querySelector('.fm-container')); } break;
        case '?': e.preventDefault(); fmShowShortcuts(); break;          // shortcut cheat sheet
        case 'F1': e.preventDefault(); fmShowShortcuts(); break;
        case ' ': e.preventDefault(); fmToggleQuickLook(); break;         // Space = Quick Look
        case 'c': if (e.ctrlKey || e.metaKey) { e.preventDefault(); fmCopySelected(); } break;
        case 'x': if (e.ctrlKey || e.metaKey) { e.preventDefault(); fmCutSelected(); } break;
        case 'v': if (e.ctrlKey || e.metaKey) { e.preventDefault(); fmPaste(); } break;
        case 'a': if ((e.ctrlKey || e.metaKey) && !e.shiftKey) { e.preventDefault(); fmSelectAll(); } break;
        case 'd': if (e.ctrlKey || e.metaKey) { e.preventDefault(); fmToggleCurrentFolderFavorite(); } break;
        case 'ArrowLeft': if (e.altKey) { e.preventDefault(); fmNavigateBack(); } break;
        case 'ArrowRight': if (e.altKey) { e.preventDefault(); fmNavigateForward(); } break;
        case 'Enter': {
            // Open the selected item — keyboard parity with double-click
            const root = winEl.querySelector('.fm-container');
            const st = _getFmStateForRoot(root);
            if (st && st.selected.size === 1) {
                e.preventDefault();
                const name = [...st.selected][0];
                if (name === '..') { fmGoUp(); break; }
                const el = root.querySelector(`.fm-file-item[data-name="${CSS.escape(name)}"], .fm-list-row[data-name="${CSS.escape(name)}"]`);
                const isDir = el ? _fmIsDirItem(el) : false;
                fmOpenItem(name, isDir);
            }
            break;
        }
        case 'Escape': {
            _fmTypeBuf = '';  // reset the type-ahead prefix
            // Clear selection in the focused window (don't close the window)
            const root = winEl.querySelector('.fm-container');
            const st = _getFmStateForRoot(root);
            if (st) {
                // Only steal Escape if the window is actually focused/visible
                if (st.selected.size > 0) {
                    e.preventDefault();
                    st.selected.clear();
                    st._anchorName = null;
                    fmRenderGridItems();
                }
            }
            break;
        }
    }
}

// ── Load & render directory ──────────────────────────────────────────────────
export async function fmLoad(path, root) {
    const activeRoot = root || _getActiveFmRoot(); if (!activeRoot) return;
    const state = _getFmStateForRoot(activeRoot); if (path !== undefined) state.path = path;
    state._inTrash = false;  // loading a real directory always leaves trash view
    state._trashScope = 'mine';  // …and the admin all-users view is per-visit
    state._searchTerm = null;  // …and exits the search results view
    // Sequence token: a slower, older listing must never overwrite a newer one
    // (rapid A → B navigation used to render A last when A's response arrived late).
    const seq = (state._loadSeq = (state._loadSeq || 0) + 1);
    { const si = activeRoot.querySelector(`#fm-search-input-${activeRoot.dataset?.fmWin}`); if (si && si.value) si.value = ''; const sc = activeRoot.querySelector('.fm-search-clear'); if (sc) sc.style.display = 'none'; }
    const contentEl = activeRoot.querySelector('.fm-content'), breadcrumbEl = activeRoot.querySelector('.fm-breadcrumb');
    if (!contentEl) return;
    // Transient states (skeleton/error/empty) are not grid layouts — drop the grid class so they center full-width
    contentEl.classList.remove('fm-grid');
    contentEl.innerHTML = _fmSkeletonHtml();
    try {
        const resp = await apiFetch(`/api/files?path=${encodeURIComponent(state.path)}`);
        if (!resp.ok) {
            // Surface the server's explanation (permission, not a directory…) instead of "HTTP 404".
            let detail = `HTTP ${resp.status}`;
            try { detail = (await resp.json()).detail || detail; } catch { /* non-JSON error body */ }
            throw new Error(detail);
        }
        const data = await resp.json();
        if (state._loadSeq !== seq) return;  // superseded by a newer navigation
        state._canWrite = data.can_write !== false;
        if (breadcrumbEl && state && !activeRoot.querySelector('.fm-pathbar.editing')) {
            breadcrumbEl.innerHTML = fmBuildBreadcrumb(state.path, activeRoot);
            // Only nudge the trail when it really overflows (CSS ellipsis covers the rest).
            requestAnimationFrame(() => { if (breadcrumbEl.scrollWidth > breadcrumbEl.clientWidth) breadcrumbEl.scrollLeft = breadcrumbEl.scrollWidth; });
        }
        if (data.error && (!data.files || !data.files.length)) { contentEl.innerHTML = `<div class="fm-empty-state" style="color:#ef4444;"><div class="fm-empty-ic">⚠️</div><p>${escapeHtml(data.error)}</p></div>`; return; }
        let files = data.files || []; const cp = data.path || state.path;
        // A different directory always starts with an empty selection. Keeping
        // names that happen to exist in both places meant Delete could hit a
        // file the user never touched (folder/backup.tar → parent/backup.tar).
        const sameDir = (state._loadedPath === cp);
        state._loadedPath = cp;
        if (!sameDir) {
            state.selected.clear();
            state._anchorName = null;
        } else if (state.selected.size > 0) {
            // Same-path refresh (F5 / after paste): only drop entries that vanished.
            const names = new Set(files.map(f => f.name));
            for (const n of [...state.selected]) if (!names.has(n)) state.selected.delete(n);
            if (state._anchorName && !names.has(state._anchorName)) state._anchorName = null;
        }
        // Filter hidden files (dotfiles) unless _showHidden is true
        if (!state._showHidden) files = files.filter(f => !f.name.startsWith('.'));
        if (state.viewMode === 'list') fmRenderListView(contentEl, files, cp, state); else fmRenderGridView(contentEl, files, cp, state);
        // Keep the toolbar star button in sync with bookmarks state
        const starBtn = activeRoot.querySelector('[data-testid="fm-star-btn"]');
        if (starBtn) { const fav = _fmIsFavorite(state.path); starBtn.classList.toggle('active', fav); }
        _fmApplyCanWrite(activeRoot, state);
        // Remember this folder for the next time the app is opened; it is a valid
        // path now that the listing arrived.
        _fmSavePref('ethos-fm-last-path', state.path);
        state._restoredPath = false;
        _fmSyncNavButtons(activeRoot, state);
        await fmRenderSidebar(activeRoot);
        // Update status bar with item count, selected count, and free space
        const itemsEl = activeRoot.querySelector('[data-testid="fm-statusbar-items"]');
        const freeEl = activeRoot.querySelector('[data-testid="fm-statusbar-free"]');
        if (itemsEl) {
            const dirs = files.filter(f => f.is_dir).length;
            const fcount = files.length - dirs;
            let txt = `${files.length} item${files.length === 1 ? '' : 's'}`;
            if (dirs > 0 && fcount > 0) txt += ` (${dirs} folders, ${fcount} files)`;
            else if (files.length > 0 && dirs === 0) txt += ' — files only';
            itemsEl.textContent = txt;
        }
        _fmUpdateSelectionStatus(activeRoot, state);
        // Fetch free disk space for the status bar + sidebar usage footer
        if (freeEl) {
            apiFetch('/api/system/status').then(r => r.ok ? r.json() : null).then(d => {
                if (d && d.disk && d.disk.free != null) {
                    freeEl.textContent = d.disk.total ? `${formatBytes(d.disk.free)} free of ${formatBytes(d.disk.total)}` : `free: ${formatBytes(d.disk.free)}`;
                    _fmRenderStorageFooter(activeRoot, d.disk);
                }
            }).catch(() => { freeEl.textContent = ''; });
        }
    } catch(e) {
        if (state._loadSeq !== seq) return;
        // A remembered folder can be gone (deleted, unmounted) — fall back to Home once
        // instead of dead-ending on an error screen.
        if (state._restoredPath) { state._restoredPath = false; state.path = '/home'; state.history = ['/home']; state.historyIndex = 0; fmLoad('/home', activeRoot); return; }
        contentEl.innerHTML = `<div class="fm-empty-state" style="color:#ef4444;"><div class="fm-empty-ic">❌</div><p>${escapeHtml(e.message)}</p>`
            + '<div style="display:flex;gap:8px;margin-top:8px;">'
            + '<button class="fm-act-btn" onclick="fmRefresh()">↻ Retry</button>'
            + '<button class="fm-act-btn" onclick="fmNavigateTo(\'/home\')">Go to Home</button>'
            + '</div></div>';
    }
}

/**
 * Reflect the directory's writability (`can_write` from GET /api/files) in the
 * toolbar: mutating actions are disabled and the status bar shows "read-only",
 * so the UI no longer offers operations that are guaranteed to fail with 403.
 */
function _fmApplyCanWrite(root, state) {
    if (!root || !state) return;
    const ro = state._canWrite === false;
    const roEl = root.querySelector('[data-testid="fm-statusbar-ro"]');
    if (roEl) roEl.style.display = ro ? '' : 'none';
    root.querySelectorAll('.fm-empty-create').forEach(b => { b.style.display = ro ? 'none' : ''; });
    _fmSyncActionButtons(root, state);
}

/**
 * Selection-aware toolbar state. Copy / Cut / ZIP need a selection, Paste needs
 * a non-empty clipboard, every mutating action needs a writable folder. Without
 * this the buttons look clickable and only answer with a toast ("No items
 * selected") — a dead-end the UI can prevent.
 */
function _fmSyncActionButtons(root, state) {
    if (!root || !state) return;
    const hasSel = state.selected.size > 0;
    const selCount = state.selected.size;
    const clipCount = _fmClipboard.paths.length;
    const hasClip = !!_fmClipboard.op && clipCount > 0;
    const ro = state._canWrite === false;
    const setBtn = (id, disabled, title) => {
        const b = root.querySelector(`[data-testid="${id}"]`);
        if (!b) return;
        b.disabled = !!disabled;
        if (title) b.title = title;
    };
    setBtn('fm-upload-btn', ro, ro ? 'Read-only folder — uploads are not allowed' : 'Upload files');
    setBtn('fm-new-folder-btn', ro, ro ? 'Read-only folder — cannot create folders' : 'New folder (Ctrl+Shift+N)');
    setBtn('fm-copy-btn', !hasSel, hasSel ? `Copy ${selCount} item${selCount === 1 ? '' : 's'} (Ctrl+C)` : 'Select items to copy');
    setBtn('fm-cut-btn', !hasSel, hasSel ? `Cut ${selCount} item${selCount === 1 ? '' : 's'} (Ctrl+X)` : 'Select items to cut');
    setBtn('fm-paste-btn', ro || !hasClip, hasClip ? `Paste ${clipCount} item${clipCount === 1 ? '' : 's'} (Ctrl+V)` : 'Clipboard is empty');
    setBtn('fm-zip-btn', !hasSel, hasSel ? `Download ${selCount} item${selCount === 1 ? '' : 's'} as ZIP` : 'Select items to download as ZIP');
}

function _fmSelectionStats(state) {
    let bytes = 0, files = 0, dirs = 0;
    const meta = state && state._fileMeta;
    if (meta) {
        for (const name of state.selected) {
            const f = meta.get(name);
            if (!f) continue;
            if (f.is_dir) dirs++;
            else { files++; bytes += f.size || 0; }
        }
    }
    return { bytes, files, dirs, count: state ? state.selected.size : 0 };
}

/** Status-bar "N selected · 12.4 MB" indicator. */
function _fmUpdateSelectionStatus(root, state) {
    if (!root) return;
    const selectedCountEl = root.querySelector('[data-testid="fm-statusbar-selected"]');
    if (!selectedCountEl) return;
    if (!state || state.selected.size === 0) {
        selectedCountEl.textContent = '';
        selectedCountEl.style.display = 'none';
        return;
    }
    const { bytes, files, dirs } = _fmSelectionStats(state);
    const parts = [];
    if (files > 0) parts.push(formatBytes(bytes));
    if (dirs > 0) parts.push(`${dirs} folder${dirs === 1 ? '' : 's'}`);
    const detail = parts.length ? ` · ${parts.join(' · ')}` : '';
    selectedCountEl.textContent = `${state.selected.size} selected${detail} / `;
    selectedCountEl.style.display = '';
}

/**
 * Status-bar upload indicator: "⬆ Uploading 2 · 45%" / "⚠ 1 upload failed" /
 * "✓ 3 uploaded". Fed by the upload engine's ethos:uploads-changed event.
 * The percentage is rounded to 5% steps because the footer is a live region —
 * per-chunk updates would flood screen readers.
 */
function _fmRenderUploadChip(root, detail) {
    if (!root) return;
    const chip = root.querySelector('[data-testid="fm-statusbar-upload"]');
    if (!chip) return;

    const d = detail || getUploadStatus();
    let text = '';
    let kind = '';
    if (d.active > 0) {
        const pct = Math.floor((d.percent || 0) / 5) * 5;
        text = `⬆ Uploading ${d.active} · ${pct}%`;
        kind = 'is-active';
    } else if (d.queued > 0) {
        text = `⏳ ${d.queued} waiting`;
        kind = 'is-active';
    } else if (d.failed > 0) {
        text = `⚠ ${d.failed} upload${d.failed === 1 ? '' : 's'} failed`;
        kind = 'is-failed';
    } else if (d.completed > 0) {
        text = `✓ ${d.completed} uploaded`;
        kind = 'is-done';
    }

    if (!text) {
        chip.textContent = '';
        chip.style.display = 'none';
        return;
    }
    if (chip.textContent !== text) chip.textContent = text;
    chip.className = `fm-status-upload ${kind}`.trim();
    chip.style.display = '';
    chip.title = (d.active > 0 || d.queued > 0)
        ? 'Upload in progress — click to open the upload panel'
        : (d.failed > 0 ? 'Upload failed — click for details' : 'Uploads finished — click to review');
}

/** Volume usage footer in the sidebar (reuses the status request). */
function _fmRenderStorageFooter(root, disk) {
    if (!root || !disk) return;
    const el = root.querySelector('[data-testid="fm-side-storage"]');
    if (!el) return;
    const total = Number(disk.total) || 0;
    const free = Number(disk.free) || 0;
    const used = Math.max(0, total - free);
    const pct = total > 0 ? Math.min(100, (used / total) * 100) : 0;
    const fill = el.querySelector('.fm-side-storage-fill');
    const text = el.querySelector('.fm-side-storage-text');
    if (fill) {
        fill.style.width = `${pct.toFixed(1)}%`;
        fill.style.background = pct >= 90 ? '#e5484d' : (pct >= 75 ? '#f59e0b' : '');
    }
    if (text) text.textContent = total > 0
        ? `${formatBytes(used)} used of ${formatBytes(total)}`
        : `${formatBytes(free)} free`;
}

/** Shimmering placeholder tiles shown while a directory listing is loading. */
function _fmSkeletonHtml() {
    const tile = '<div class="fm-skeleton-tile"><div class="fm-skeleton-ic"></div><div class="fm-skeleton-line"></div></div>';
    return `<div class="fm-skeleton" aria-hidden="true">${tile.repeat(12)}</div>`;
}

/** Enable/disable Back / Forward to match this window's history. */
function _fmSyncNavButtons(root, state) {
    if (!root || !state) return;
    const back = root.querySelector('[data-testid="fm-nav-back"]');
    const fwd = root.querySelector('[data-testid="fm-nav-forward"]');
    if (back) back.disabled = !(state.historyIndex > 0);
    if (fwd) fwd.disabled = !(state.historyIndex < (state.history?.length || 1) - 1);
}

//: Above this many segments the middle of the trail collapses into "…".
const FM_CRUMB_MAX = 4;
const FM_CRUMB_SEP = '<span class="fm-crumb-sep" aria-hidden="true">›</span>';

function _fmCrumb(name, path, cls = '') {
    return `<span class="fm-crumb ${cls}" data-path="${escapeAttr(path)}" title="${escapeAttr(path)}" onclick="event.stopPropagation();fmNavigateTo('${safeJsStr(path)}')">${escapeHtml(name)}</span>`;
}

export function fmBuildBreadcrumb(path, root) {
    if (!path || path === '/') return '<span class="fm-crumb current" data-path="/" title="/">/</span>';
    const crumbs = []; let acc = '';
    path.split('/').filter(Boolean).forEach((part, idx, arr) => {
        acc += '/' + part;
        crumbs.push({ name: part, path: acc, last: idx === arr.length - 1 });
    });
    let html = _fmCrumb('/', '/');
    if (crumbs.length > FM_CRUMB_MAX) {
        // Collapse the middle: long paths used to be clipped mid-word by the
        // path bar (the trail just scrolled sideways).
        const hidden = crumbs.slice(0, crumbs.length - 2);
        const head = crumbs[crumbs.length - 2], tail = crumbs[crumbs.length - 1];
        html += FM_CRUMB_SEP
            + `<span class="fm-crumb fm-crumb-more" data-crumbs="${escapeAttr(JSON.stringify(hidden))}" title="${hidden.length} more folder${hidden.length === 1 ? '' : 's'} — click to jump" aria-label="${hidden.length} more folders" onclick="event.stopPropagation();fmShowCrumbMenu(event)">…</span>`
            + FM_CRUMB_SEP + _fmCrumb(head.name, head.path)
            + FM_CRUMB_SEP + _fmCrumb(tail.name, tail.path, 'current');
        return html;
    }
    html += crumbs.map(c => FM_CRUMB_SEP + _fmCrumb(c.name, c.path, c.last ? 'current' : '')).join('');
    return html;
}

/** "…" in the breadcrumb expands the collapsed middle segments. */
export function fmShowCrumbMenu(e) {
    const el = e.currentTarget || e.target;
    let hidden = [];
    try { hidden = JSON.parse(el.getAttribute('data-crumbs') || '[]'); } catch { /* corrupt payload — ignore */ }
    if (!hidden.length) return;
    const icon = '<path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>';
    _fmBuildContextMenu(e.clientX, e.clientY, hidden.map(c => ({
        testid: 'fm-crumb-' + c.name, label: c.name, icon,
        action: () => fmNavigateTo(c.path),
    })));
}

/** Clicking the empty part of the path bar starts editing (Explorer-style). */
export function fmPathbarClick(e) {
    if (e.target.closest && e.target.closest('.fm-crumb, .fm-path-edit, .fm-path-input')) return;
    fmStartPathEdit();
}

export function fmStartPathEdit(winId) {
    const root = winId ? document.querySelector(`.fm-container[data-fm-win="${CSS.escape(winId)}"]`) : _getActiveFmRoot();
    if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    const bar = root.querySelector('.fm-pathbar');
    const input = bar && bar.querySelector('.fm-path-input');
    if (!bar || !input) return;
    bar.classList.add('editing');
    input.value = state.path;
    input.focus();
    input.select();
}

/** Enter = navigate, Esc / click-away = cancel. */
export function fmPathInputKey(e) {
    e.stopPropagation();  // never let the global file-manager shortcuts see this
    const input = e.target;
    const bar = input.closest('.fm-pathbar');
    if (!bar) return;
    if (e.key === 'Enter') {
        e.preventDefault();
        const root = bar.closest('.fm-container');
        const st = _getFmStateForRoot(root);
        let target = (input.value || '').trim();
        if (!target) { bar.classList.remove('editing'); return; }
        // Accept "home/marcin", "~/docs" and absolute paths alike.
        if (!target.startsWith('/') && !target.startsWith('~')) target = '/' + target;
        bar.classList.remove('editing');
        if (st && target === st.path) return;
        fmNavigateTo(target);
    } else if (e.key === 'Escape') {
        e.preventDefault();
        bar.classList.remove('editing');
        const root = bar.closest('.fm-container');
        const st = _getFmStateForRoot(root);
        const crumbs = bar.querySelector('.fm-breadcrumb');
        if (crumbs && st) crumbs.innerHTML = fmBuildBreadcrumb(st.path, root);
    }
}

export function fmPathInputBlur(e) {
    const bar = e && e.target ? e.target.closest('.fm-pathbar') : null;
    if (!bar) return;
    // Defer: a click on a crumb or the Enter handler must win over the blur.
    setTimeout(() => { if (!bar.contains(document.activeElement)) bar.classList.remove('editing'); }, 120);
}

function fmRenderGridView(container, files, path, state) {
    // An empty folder is a dead end — show the call to action instead of a lone
    // ".." tile (the breadcrumb / Backspace still navigate up).
    if (files.length === 0) {
        container.classList.remove('fm-grid');
        container.removeAttribute('role');
        container.removeAttribute('aria-multiselectable');
        if (state) state._imagePaths = [];
        container.innerHTML = _FM_EMPTY_STATE_HTML;
        _fmSyncSortUI(container.closest('.fm-container'));
        return;
    }
    // Grid layout only when actually rendering tiles; transient states are centered full-width
    container.classList.add('fm-grid');
    container.setAttribute('role', 'listbox');
    container.setAttribute('aria-multiselectable', 'true');
    if (state) state._fileMeta = new Map(files.map(f => [f.name, f]));
    if (state) files = _fmSortFiles(files, state);
    _fmSyncSortUI(container.closest('.fm-container'));
    const _imgExts = ['.png','.jpg','.jpeg','.gif','.webp','.bmp','.svg'];
    const _videoExts = ['.mp4','.mkv','.mov','.avi','.webm','.flv','.m4v','.wmv','.ts','.mts'];
    // Request thumbnails at ~2x the rendered icon size for the active density.
    const thumbSize = ({ small: 96, medium: 128, large: 192 })[_fmDensity()] || 96;
    if (state) {
        state._imagePaths = [];
        files.forEach(f => {
            if (!f.is_dir && f.path) {
                const ext = '.' + f.path.split('/').filter(Boolean).pop().toLowerCase().split('.').pop();
                if (_imgExts.includes(ext)) state._imagePaths.push(f.path);
            }
        });
    }
    function _getExt(p) {
        const parts = p.split('/').filter(Boolean);
        const name = parts.pop() || '';
        const dot = name.lastIndexOf('.');
        return dot > 0 ? name.slice(dot).toLowerCase() : '';
    }
    let html = ''; if (path !== '/' && path !== '') { const pp = path.substring(0, path.lastIndexOf('/')) || '/'; html += `<div class="fm-file-item fm-parent-item" data-name=".." data-path="${escapeAttr(pp)}" onclick="event.stopPropagation();fmNavigateTo('${safeJsStr(pp)}')">${getFileIcon('..', 'dir')}<div class="fm-file-name">..</div><div class="fm-file-meta">Parent folder</div></div>`; }
    files.forEach(f => {
        const sel = state.selected.has(f.name);
        const ext = _getExt(f.path || f.name);
        const isImg = !f.is_dir && _imgExts.includes(ext);
        const isVid = !f.is_dir && _videoExts.includes(ext);
        const tk = encodeURIComponent(localStorage.getItem('token') || '');
        const vidSize = _fmVideoXL() ? 640 : 320;  // cinematic mode fetches a sharper frame (backend caps at 640)
        const star = _fmIsFavorite(f.path) ? '<span class="fm-item-star" title="Favorite">★</span>' : '';
        let iconHtml = '';
        if (isImg) {
            iconHtml = `<div class="fm-file-thumb-wrap"><img class="fm-grid-thumb" draggable="false" data-src="/api/files/thumbnail?path=${encodeURIComponent(f.path||'')}&size=${thumbSize}&token=${tk}" alt=""></div>`;
        } else if (isVid) {
            iconHtml = `<div class="fm-file-thumb-wrap fm-video-thumb"><img class="fm-grid-thumb" draggable="false" data-vid-src="/api/files/video-thumbnail?path=${encodeURIComponent(f.path||'')}&size=${vidSize}&token=${tk}" alt=""><div class="fm-video-play-badge">▶</div></div>`;
        } else {
            iconHtml = `<div class="fm-file-icon">${getFileIcon(f.name, f.is_dir ? 'dir' : 'file')}</div>`;
        }
        const meta = f.is_dir ? 'Folder' : `${formatBytes(f.size || 0)} · ${_fmShortDate(f.modified)}`;
        const tip = f.is_dir ? `${f.name} — Folder` : `${f.name} — ${formatBytes(f.size || 0)}${f.modified ? ' · ' + _fmFullDate(f.modified) : ''}`;
        html += `<div class="fm-file-item ${isVid ? 'fm-video-item ' : ''}${sel ? 'selected' : ''}" draggable="true" role="option" aria-selected="${sel}" title="${escapeAttr(tip)}" data-name="${escapeAttr(f.name)}" data-isdir="${f.is_dir}" data-path="${escapeAttr(f.path||'')}" oncontextmenu="event.preventDefault();event.stopPropagation();fmShowItemContextMenu(event,'${jsAttr(f.path||'')}','${safeJsStr(f.name)}',${f.is_dir})" onclick="event.stopPropagation();fmSelectItem('${safeJsStr(f.name)}', event)" ondblclick="event.stopPropagation();fmOpenItem('${safeJsStr(f.name)}', ${f.is_dir})">${iconHtml}${star}<div class="fm-file-name">${escapeHtml(f.name)}</div><div class="fm-file-meta">${meta}</div></div>`;
    });
    container.innerHTML = html || _FM_EMPTY_STATE_HTML;
    _fmObserveThumbs(container);
}

// ── Thumbnail loading: lazy + capped concurrency ──────────────────
// A folder with thousands of images used to fire one thumbnail request per tile,
// saturating the browser's 6-connections-per-host pool so ordinary API calls
// (save, rename, this very listing) queued behind them and the UI froze. Tiles
// now carry `data-src` / `data-vid-src`; an IntersectionObserver enqueues only
// what is near the viewport and at most _FM_THUMB_MAX requests are in flight.
//
// Images load straight into <img>. Video frames are produced by a bounded
// background worker that answers 202 + Retry-After until the frame is ready —
// an <img> tag cannot see that status (it only fires `error`), which is why
// most video tiles stayed blank. Videos therefore load through fetch(), poll
// while the backend queues/renders, and keep a small blob-URL cache so folder
// switches and cinematic/compact toggles are instant.
const _FM_THUMB_MAX = 4;
const _FM_VID_RETRY_MAX = 10;
const _FM_VID_CACHE_MAX = 300;
const _thumbQueue = [];
const _fmVideoThumbCache = new Map();   // request URL → blob: URL
let _thumbActive = 0;
let _thumbObserver = null;

function _fmPumpThumbs() {
    // While a film is playing, thumbnail work must not compete for CPU/disk
    // (8K decodes on a loaded NAS are exactly what makes the next film stall).
    // Pending tiles keep their data-* attributes and resume on player close.
    if (_videoPlayer.open) return;
    while (_thumbActive < _FM_THUMB_MAX && _thumbQueue.length) {
        const img = _thumbQueue.shift();
        if (!img || !img.isConnected || !img.dataset) continue;
        if (img.dataset.vidSrc) {
            const url = img.dataset.vidSrc;
            delete img.dataset.vidSrc;
            _thumbActive++;
            _fmLoadVideoThumb(img, url).finally(() => {
                _thumbActive = Math.max(0, _thumbActive - 1);
                _fmPumpThumbs();
            });
            continue;
        }
        if (!img.dataset.src) continue;
        const src = img.dataset.src;
        delete img.dataset.src;
        _thumbActive++;
        const done = () => { _thumbActive = Math.max(0, _thumbActive - 1); _fmPumpThumbs(); };
        img.addEventListener('load', done, { once: true });
        img.addEventListener('error', () => {
            // Broken source (or the server denied it) — keep the tile tidy.
            const wrap = img.closest('.fm-file-thumb-wrap');
            if (wrap) wrap.classList.add('fm-thumb-missing');
            done();
        }, { once: true });
        img.src = src;
    }
}

function _fmRememberVideoThumb(url, blobUrl) {
    if (_fmVideoThumbCache.size >= _FM_VID_CACHE_MAX) {
        const oldest = _fmVideoThumbCache.keys().next().value;
        const old = _fmVideoThumbCache.get(oldest);
        _fmVideoThumbCache.delete(oldest);
        try { URL.revokeObjectURL(old); } catch { /* already revoked */ }
    }
    _fmVideoThumbCache.set(url, blobUrl);
}

async function _fmLoadVideoThumb(img, url) {
    // Playback started before this tile was pumped — keep it queued.
    if (_videoPlayer.open) { if (img.isConnected) img.dataset.vidSrc = url; return; }
    const cached = _fmVideoThumbCache.get(url);
    if (cached) { if (img.isConnected) img.src = cached; return; }
    for (let attempt = 0; attempt <= _FM_VID_RETRY_MAX && img.isConnected; attempt++) {
        if (_videoPlayer.open) {
            // Stop polling during playback; the tile retries when the player closes.
            if (img.isConnected) img.dataset.vidSrc = url;
            return;
        }
        let resp = null;
        try { resp = await apiFetch(url); } catch { resp = null; }
        if (resp && resp.ok) {
            const blob = await resp.blob().catch(() => null);
            if (!blob || !blob.size) break;
            const blobUrl = URL.createObjectURL(blob);
            // Keep the generic fallback out of the cache so a fixed file can
            // render again on the next visit without a page reload.
            if (blob.type !== 'image/svg+xml') _fmRememberVideoThumb(url, blobUrl);
            if (img.isConnected) img.src = blobUrl;
            return;
        }
        if (resp && resp.status !== 202 && resp.status !== 429) break;  // real failure
        if (resp) { try { await resp.body?.cancel(); } catch { /* body already consumed */ } }
        // The server renders in the background; short polls keep the UI responsive.
        const secs = resp ? parseInt(resp.headers.get('Retry-After') || '2', 10) : 2;
        const waitMs = Math.min(5000, (Number.isFinite(secs) && secs > 0 ? secs : 2) * 1000);
        await new Promise(r => setTimeout(r, waitMs));
    }
    if (img.isConnected) {
        const wrap = img.closest('.fm-file-thumb-wrap');
        if (wrap) wrap.classList.add('fm-thumb-missing');
    }
}

function _fmObserveThumbs(container) {
    const imgs = container.querySelectorAll('img[data-src], img[data-vid-src]');
    if (!imgs.length) return;
    if (!('IntersectionObserver' in window)) {
        imgs.forEach(i => _thumbQueue.push(i));
        _fmPumpThumbs();
        return;
    }
    if (!_thumbObserver) {
        _thumbObserver = new IntersectionObserver((entries) => {
            for (const e of entries) {
                if (!e.isIntersecting) continue;
                _thumbObserver.unobserve(e.target);
                _thumbQueue.push(e.target);
            }
            _fmPumpThumbs();
        }, { rootMargin: '400px 0px' });
    }
    imgs.forEach(i => _thumbObserver.observe(i));
}

function _getFileKind(file) {
    if (file.is_dir) return 'Folder';
    if (file.file_type === 'image') return 'Image';
    if (file.file_type === 'video') return 'Video';
    if (file.file_type === 'audio') return 'Audio';
    if (file.file_type === 'document') return 'Document';
    if (file.file_type === 'text') return 'Text';
    if (file.file_type === 'archive') return 'Archive';
    if (file.file_type === 'code') return 'Code';
    const ext = file.name ? '.' + file.name.toLowerCase().split('.').pop() : '';
    if (ext === '.pdf') return 'PDF';
    if (ext === '.sh' || ext === '.bash') return 'Script';
    return 'File';
}

function fmRenderListView(container, files, path, state) {
    container.classList.remove('fm-grid');
    container.removeAttribute('role');  // a <table> carries its own semantics
    container.removeAttribute('aria-multiselectable');
    if (state) state._fileMeta = new Map(files.map(f => [f.name, f]));
    _fmSyncSortUI(container.closest('.fm-container'));  // table must not be a grid item (width:100% collapses otherwise)
    if (files.length === 0) { container.innerHTML = _FM_EMPTY_STATE_HTML; return; }
    let html = `<table class="fm-list-table"><thead><tr>
        <th class="fm-col-name" onclick="fmSortBy('name')" style="cursor:pointer;">Name${state.sortCol === 'name' ? (state.sortAsc ? ' ↑' : ' ↓') : ''}</th>
        <th class="fm-col-size" onclick="fmSortBy('size')" style="cursor:pointer;">Size${state.sortCol === 'size' ? (state.sortAsc ? ' ↑' : ' ↓') : ''}</th>
        <th class="fm-col-kind" onclick="fmSortBy('kind')" style="cursor:pointer;">Kind${state.sortCol === 'kind' ? (state.sortAsc ? ' ↑' : ' ↓') : ''}</th>
        <th class="fm-col-date" onclick="fmSortBy('date')" style="cursor:pointer;">Modified${state.sortCol === 'date' ? (state.sortAsc ? ' ↑' : ' ↓') : ''}</th>
    </tr></thead><tbody>`;
    if (path !== '/' && path !== '') { const pp = path.substring(0, path.lastIndexOf('/')) || '/'; html += `<tr onclick="event.stopPropagation();fmNavigateTo('${safeJsStr(pp)}')"><td class="fm-col-name"><span><span class="fm-list-ic">${getFileIcon('..', 'dir')}</span><span style="color: var(--text-2);">..</span></span></td><td class="fm-col-size">—</td><td class="fm-col-kind" style="color: var(--text-3);">Parent folder</td><td class="fm-col-date" style="color: var(--text-3);">—</td></tr>`; }
    const sorted = _fmSortFiles(files, state);
    for (const f of sorted) { const sel = state.selected.has(f.name); const star = _fmIsFavorite(f.path) ? '<span class="fm-item-star" title="Favorite">★</span>' : ''; const tip = f.is_dir ? `${f.name} — Folder` : `${f.name} — ${formatBytes(f.size || 0)}${f.modified ? ' · ' + _fmFullDate(f.modified) : ''}`; html += `<tr class="fm-list-row ${sel ? 'selected' : ''}" draggable="true" title="${escapeAttr(tip)}" data-name="${escapeAttr(f.name)}" data-isdir="${f.is_dir}" data-path="${escapeAttr(f.path||'')}" oncontextmenu="event.preventDefault();event.stopPropagation();fmShowItemContextMenu(event,'${jsAttr(f.path||'')}','${safeJsStr(f.name)}',${f.is_dir})" onclick="event.stopPropagation();fmSelectItem('${safeJsStr(f.name)}', event)" ondblclick="event.stopPropagation();fmOpenItem('${safeJsStr(f.name)}', ${f.is_dir})"><td class="fm-col-name"><span><span class="fm-list-ic">${getFileIcon(f.name, f.is_dir ? 'dir' : 'file')}${star}</span><span class="fm-file-name-inline" style="color:${sel ? 'var(--fm-accent)' : ''};">${escapeHtml(f.name)}</span></span></td><td class="fm-col-size" style="font-family:monospace;color: var(--text-2);">${f.is_dir ? '—' : formatBytes(f.size || 0)}</td><td class="fm-col-kind" style="color: var(--text-3);font-size:11px;">${_getFileKind(f)}</td><td class="fm-col-date" style="color: var(--text-2);">${f.modified ? _fmFullDate(f.modified) : '—'}</td></tr>`; }
    html += '</tbody></table>'; container.innerHTML = html || _FM_EMPTY_STATE_HTML;
}

window.fmSortBy = function(col) {
    const root = _getActiveFmRoot();
    if (!root) return;
    const state = _getFmStateForRoot(root);
    if (!state) return;
    if (state.sortCol === col) state.sortAsc = !state.sortAsc;
    else { state.sortCol = col; state.sortAsc = true; }
    _fmSaveSortPref(state);
    fmLoad(undefined, root);
};

const _FM_SORT_LABELS = { name: 'Name', size: 'Size', kind: 'Kind', date: 'Date Modified' };

function _fmSyncSortUI(root) {
    if (!root || !root.querySelector) return;
    const st = _getFmStateForRoot(root); if (!st) return;
    const col = st.sortCol || 'name';
    const asc = st.sortAsc !== false;
    const btn = root.querySelector('[data-testid="fm-sort-btn"]');
    if (btn) btn.title = `Sort by: ${_FM_SORT_LABELS[col] || col} — ${asc ? 'ascending' : 'descending'} (click to change)`;
}

// ── Sort popup menu (macOS Finder style) ─────────────────────────────
let _fmOpenSortMenu = null;

function _fmCloseSortMenu() {
    if (!_fmOpenSortMenu) return;
    const { el, cleanup } = _fmOpenSortMenu;
    _fmOpenSortMenu = null;
    try { el.remove(); } catch {}
    if (cleanup) cleanup();
}

window.fmToggleSortMenu = function(e) {
    e.stopPropagation();
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (_fmOpenSortMenu && _fmOpenSortMenu.root === root) { _fmCloseSortMenu(); return; }
    _fmCloseSortMenu();
    _closeContextMenu();  // never show both menus at once
    const btn = e.currentTarget;
    const col = state.sortCol || 'name';
    const asc = state.sortAsc !== false;
    const check = svgIcon('<polyline points="20,6 9,17 4,12"/>', 13);
    const menu = document.createElement('div');
    menu.className = 'fm-context-menu fm-sort-menu';
    menu.setAttribute('data-testid', 'fm-sort-menu');
    menu.innerHTML = Object.keys(_FM_SORT_LABELS).map(v =>
        `<button class="fm-ctx-item" data-col="${v}"><span class="fm-ctx-ic">${col === v ? check : ''}</span><span>${_FM_SORT_LABELS[v]}</span></button>`).join('') +
        '<div class="fm-ctx-sep"></div>' +
        `<button class="fm-ctx-item" data-dir="asc"><span class="fm-ctx-ic">${asc ? check : ''}</span><span>Ascending</span></button>` +
        `<button class="fm-ctx-item" data-dir="desc"><span class="fm-ctx-ic">${!asc ? check : ''}</span><span>Descending</span></button>`;
    document.body.appendChild(menu);
    const r = btn.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + 'px';
    let top = r.bottom + 6;
    if (top + menu.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - menu.offsetHeight - 6);
    menu.style.top = top + 'px';
    const onClickOut = (ev) => { if (!menu.contains(ev.target) && !btn.contains(ev.target)) _fmCloseSortMenu(); };
    const onKey = (ev) => { if (ev.key === 'Escape') _fmCloseSortMenu(); };
    document.addEventListener('mousedown', onClickOut, true);
    document.addEventListener('keydown', onKey, true);
    menu.querySelectorAll('[data-col]').forEach(b => b.addEventListener('click', () => {
        state.sortCol = b.dataset.col;  // keep current direction (Finder behavior)
        _fmSaveSortPref(state);
        fmLoad(undefined, root); _fmSyncSortUI(root); _fmCloseSortMenu();
    }));
    menu.querySelectorAll('[data-dir]').forEach(b => b.addEventListener('click', () => {
        state.sortAsc = b.dataset.dir === 'asc';
        _fmSaveSortPref(state);
        fmLoad(undefined, root); _fmSyncSortUI(root); _fmCloseSortMenu();
    }));
    _fmOpenSortMenu = { el: menu, root, cleanup: () => { document.removeEventListener('mousedown', onClickOut, true); document.removeEventListener('keydown', onKey, true); } };
};

function _fmSortFiles(files, state) {
    const col = (state && state.sortCol) || 'name';
    const dir = (state && state.sortAsc === false) ? -1 : 1;
    return [...files].sort((a, b) => {
        // Folders always group above files (matches the backend listing and
        // every mainstream file manager), regardless of the sort column.
        if (!!a.is_dir !== !!b.is_dir) return a.is_dir ? -1 : 1;
        let va, vb;
        if (col === 'size') { va = a.size || 0; vb = b.size || 0; }
        else if (col === 'date') { va = new Date(a.modified || 0).getTime(); vb = new Date(b.modified || 0).getTime(); }
        else if (col === 'kind') { va = _getFileKind(a) + '\u0001' + a.name.toLowerCase(); vb = _getFileKind(b) + '\u0001' + b.name.toLowerCase(); return dir * (va < vb ? -1 : va > vb ? 1 : 0); }
        else { va = a.name.toLowerCase(); vb = b.name.toLowerCase(); }
        if (typeof va === 'number') return dir * (va - vb) || (a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1);
        return dir * (va < vb ? -1 : va > vb ? 1 : 0);
    });
}

// ── File operations (refactored to use proper backend endpoints) ─────────────

export function fmSelectItem(name, e) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (!name || name === '..') return;  // '..' is never selectable
    const ctrl = e && (e.ctrlKey || e.metaKey);
    const shift = e && e.shiftKey;
    if (shift && state._anchorName) {
        // Range select between anchor and clicked item (visual order), keeps other ctrl-selected items
        const names = [...root.querySelectorAll('.fm-content .fm-file-item, .fm-content .fm-list-row')]
            .map(el => el.dataset?.name || el.getAttribute('data-name')).filter(Boolean);
        const a = names.indexOf(state._anchorName), b = names.indexOf(name);
        if (a !== -1 && b !== -1) {
            const [lo, hi] = a < b ? [a, b] : [b, a];
            const range = names.slice(lo, hi + 1).filter(n => n && n !== '..');
            if (ctrl) range.forEach(n => state.selected.add(n));
            else state.selected = new Set(range);
        }
    } else if (ctrl) {
        if (state.selected.has(name)) state.selected.delete(name);
        else state.selected.add(name);
        state._anchorName = name;
    } else {
        // A plain click always replaces the selection — the old `size > 1` guard
        // left the previous item selected when exactly one row was selected, so
        // e.g. Copy grabbed an extra (invisible) file.
        state.selected.clear();
        state.selected.add(name);
        state._anchorName = name;
    }
    fmRenderGridItems();
}

function _getImageIndex(fullPath) {
    const state = _getFocusedWindowId() ? _getFmState(_getFocusedWindowId()) : null;
    if (state?._imagePaths && state._imagePaths.length > 0) {
        return state._imagePaths.indexOf(fullPath);
    }
    return 0;
}

function _getCurrentFileList() {
    const root = _getActiveFmRoot(); if (!root) return [];
    const items = root.querySelectorAll('.fm-file-item');
    const list = [];
    items.forEach(el => {
        const path = el.getAttribute('data-path') || '';
        const name = el.getAttribute('data-name') || '';
        let isDirVal = false;
        // Use getAttribute for case-insensitive matching of data-isdir/data-isDir
        const dAttr = el.getAttribute('data-isdir') ?? el.getAttribute('data-isDir');
        if (dAttr === 'true' || dAttr === true) isDirVal = true;
        if (!isDirVal && path) {
            const ext = '.' + path.split('/').filter(Boolean).pop().toLowerCase().split('.').pop();
            const imageExts = ['.png','.jpg','.jpeg','.gif','.webp','.bmp','.svg'];
            if (imageExts.includes(ext)) list.push({ name, path });
        }
    });
    return list;
}
// ── File format detection (T-ED): shared by double-click dispatch, the
// context menu and the preview so more formats open/preview/edit consistently.
const _IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg']);
const _AUDIO_EXTS = new Set(['.mp3', '.ogg', '.oga', '.wav', '.flac', '.aac', '.m4a', '.opus', '.wma']);
const _VIDEO_EXTS = new Set(['.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv', '.m4v', '.wmv', '.ts', '.mts']);
// Mirrors ARCHIVE_EXTS in backend/services/files_service.py — tests/test_archive_extract.py
// fails when the two lists drift apart.
const _ARCHIVE_EXTS = new Set([
    // ZIP and ZIP-based containers
    '.zip', '.zipx', '.jar', '.war', '.ear', '.whl', '.pyz', '.cbz', '.xpi', '.crx', '.vsix',
    '.nupkg', '.apk',
    // tar, any compression
    '.tar', '.tgz', '.tbz', '.txz', '.tzst', '.tlz', '.tlz4', '.tlzma', '.taz',
    // bare single-stream compressors
    '.gz', '.bz2', '.xz', '.lzma', '.lz', '.z', '.zst', '.lz4',
    // external-extractor formats
    '.7z', '.rar', '.cab', '.iso', '.ar', '.deb', '.cpio', '.rpm',
]);
const _TAR_MULTI_SUFFIXES = [
    '.tar.gz', '.tar.bz2', '.tar.xz', '.tar.zst', '.tar.lz4', '.tar.lz', '.tar.lzma', '.tar.z',
];
// Multi-volume sets: archive.part01.rar, archive.r00, archive.7z.001, …
const _SPLIT_PATTERNS = [
    /^(.+?)\.part0*\d{1,4}\.(?:rar|zip|7z)$/i,
    /^(.+?)\.r\d{2}$/i,
    /^(.+?)\.z\d{2}$/i,
    /^(.+?)\.\d{3}$/i,
];
function _archiveBaseName(path) {
    const name = String(path || '').replace(/\\/g, '/').split('/').filter(Boolean).pop() || '';
    const lower = name.toLowerCase();
    for (const suffix of _TAR_MULTI_SUFFIXES) {
        if (lower.endsWith(suffix)) return name.slice(0, -suffix.length) || name;
    }
    for (const pattern of _SPLIT_PATTERNS) {
        const match = name.match(pattern);
        if (match) return match[1].replace(/\.+$/, '') || name;
    }
    return name.replace(/\.[^.]+$/, '') || name;
}
function _isSplitVolumePath(path) {
    const name = String(path || '').replace(/\\/g, '/').split('/').filter(Boolean).pop() || '';
    return _SPLIT_PATTERNS.some((pattern) => pattern.test(name));
}
function _isArchivePath(p) {
    const lower = (p || '').toLowerCase();
    if (_TAR_MULTI_SUFFIXES.some(sfx => lower.endsWith(sfx))) return true;
    if (_ARCHIVE_EXTS.has('.' + (lower.split('.').pop() || ''))) return true;
    return _isSplitVolumePath(p);
}

// Extended plain-text/code list — languages, markup and config formats.
const _TEXT_EXTS = new Set([
    // core text & web
    '.txt', '.md', '.log', '.rst', '.html', '.htm', '.css', '.scss', '.json', '.xml', '.csv',
    // scripting & compiled languages
    '.py', '.js', '.ts', '.jsx', '.tsx', '.vue', '.svelte', '.sh', '.bash', '.zsh', '.fish',
    '.rb', '.go', '.rs', '.c', '.cpp', '.h', '.hpp', '.java', '.php', '.swift', '.kt', '.kts',
    '.scala', '.groovy', '.pl', '.pm', '.lua', '.r', '.m', '.mm', '.dart', '.ex', '.exs',
    '.erl', '.hs', '.ml', '.clj', '.cs', '.fs', '.vb', '.ps1', '.bat', '.cmd', '.asm',
    // data, config & infra formats
    '.yaml', '.yml', '.toml', '.env', '.conf', '.cfg', '.ini', '.sql', '.properties',
    '.gradle', '.cmake', '.tf', '.hcl', '.proto', '.graphql', '.gql', '.prisma',
    // markup, docs & misc
    '.tex', '.bib', '.org', '.adoc', '.sol', '.f90', '.for', '.ada', '.adb', '.cob',
    '.pas', '.dpr', '.jsp', '.asp', '.cgi', '.s',
]);

// Special file names that are plain text regardless of extension.
const _TEXT_FILENAMES = new Set([
    'makefile', 'dockerfile', 'cmakelists.txt', '.gitignore', '.htaccess', '.editorconfig',
    '.envrc', '.npmrc', '.yarnrc', 'license', 'licence', 'readme', 'changelog', 'authors',
]);

export function _fmIsTextFile(name) {
    if (!name) return false;
    const lower = name.toLowerCase();
    if (_TEXT_FILENAMES.has(lower)) return true;
    const ext = '.' + lower.split('.').pop();
    return _TEXT_EXTS.has(ext);
}

// Double-click to open: folder → navigate, file → preview / video / gallery / download
export function fmOpenItem(name, isDir) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (!name || name === '..') { if (isDir) fmGoUp(); return; }
    if (isDir) { fmNavigateTo(_fmJoin(state.path, name)); return; }
    // File — dispatch by type
    let fullPath = _fmJoin(state.path, name);
    const item = root.querySelector(`.fm-file-item[data-path="${CSS.escape(fullPath)}"]`);
    if (item?.getAttribute('data-path')) fullPath = item.getAttribute('data-path');
    const ext = '.' + (fullPath.split('/').filter(Boolean).pop() || '').toLowerCase().split('.').pop();
    if (_IMAGE_EXTS.has(ext)) {
        const imgFiles = [];
        root.querySelectorAll('.fm-file-item, .fm-list-row').forEach(el => {
            const p = el.dataset.path || '';
            const n = el.dataset.name || '';
            const dAttr = el.getAttribute('data-isdir') ?? el.getAttribute('data-isDir');
            if (dAttr === 'true' || dAttr === true || !p) return;
            const ex = '.' + p.split('/').filter(Boolean).pop().toLowerCase().split('.').pop();
            if (_IMAGE_EXTS.has(ex)) imgFiles.push({ name: n, path: p, is_dir: false });
        });
        window.__fmGalleryCtx = { dir: state.path, files: imgFiles, index: _getImageIndex(fullPath) };
        openApp('gallery');
    } else if (_VIDEO_EXTS.has(ext)) {
        fmOpenVideoPlayer(fullPath);
    } else if (_AUDIO_EXTS.has(ext) || ext === '.pdf') {
        // Audio and PDF open in the side panel instead of a dead-end toast.
        fmShowPreview(fullPath, false);
    } else if (_fmIsTextFile(name)) {
        fmShowPreview(fullPath, false);
    } else if (_isArchivePath(fullPath)) {
        _extractArchive(fullPath);
    } else {
        addNotification(`Opening ${name}...`, 'info');
    }
}
export function fmGoUp() { const root = _getActiveFmRoot(); if (!root) return; const state = _getFmStateForRoot(root); if (state && state.path !== '/') { const parent = state.path.substring(0, state.path.lastIndexOf('/')) || '/'; fmNavigateTo(parent); } }
// Surface the server's error detail (e.g. the permission explanation) instead of a bare HTTP status.
async function _fmReportError(r) { let d = `HTTP ${r.status}`; try { d = (await r.json()).detail || d; } catch {} addNotification(d, 'error'); }
export async function fmNewFolder() { const root = _getActiveFmRoot(); if (!root) return; const name = await showPrompt('New Folder', 'Enter folder name:'); if (name) { apiFetch('/api/files/mkdir', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ path: _fmJoin(_getFmStateForRoot(root).path, name) }) }).then(r => r.ok ? fmLoad() : _fmReportError(r)).catch(e => addNotification(e.message, 'error')); } }
export async function fmNewFile() { const root = _getActiveFmRoot(); if (!root) return; const name = await showPrompt('New File', 'Enter file name:'); if (name) { apiFetch('/api/files/touch', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ path: _fmJoin(_getFmStateForRoot(root).path, name) }) }).then(r => r.ok ? fmLoad() : _fmReportError(r)).catch(e => addNotification(e.message, 'error')); } }

// Soft delete (move to trash) — one op-tracked request, confirmation for every delete.

/**
 * Resolve every selected name to its absolute path, preferring the row's
 * authoritative `data-path`. Resolving by name alone picked the first match in
 * the search results — two files with the same name in different folders made
 * Delete hit the wrong one.
 */
function _fmResolveSelectedPaths(state, root) {
    const out = [];
    for (const name of state.selected) {
        if (!name || name === '..') continue;
        let path = null;
        if (root) {
            const el = root.querySelector(`[data-name="${CSS.escape(name)}"][data-path]`);
            path = el ? el.getAttribute('data-path') : null;
        }
        out.push(path || _fmJoin(state.path, name));
    }
    return out;
}

/** Refresh the window a deletion came from without leaving its view. */
function _fmRefreshAfterDelete(root, state) {
    if (!root || !state) return;
    if (state._inTrash) { fmOpenTrash(); return; }
    if (state._searchTerm) { fmSearch(); return; }  // stay in the search results
    state.selected.clear();
    _fmUpdateSelectionStatus(root, state);
    _silentRefetch(root);
    fmLoadFavorites();  // a deleted folder must also leave the Favorites sidebar
}

/**
 * Move items to Trash through the background op endpoint.
 *
 * Used by the Delete key / context menu, drag-to-Trash and the desktop Trash
 * icon. One request per batch → one progress card and one summary toast with
 * the server's reason for every item that could not be moved.
 */
export async function fmTrashPaths(paths, root = null) {
    const list = (Array.isArray(paths) ? paths : []).filter(Boolean);
    if (!list.length) return null;
    const targetRoot = root || _getActiveFmRoot();
    const state = targetRoot ? _getFmStateForRoot(targetRoot) : null;
    if (state && state._canWrite === false) {
        addNotification('Read-only folder — items cannot be deleted here', 'error');
        return null;
    }
    if (state && state._deleteBusy) {
        addNotification('A delete operation is already running', 'warning');
        return null;
    }
    const what = list.length === 1 ? `"${_fmBasename(list[0])}"` : `${list.length} items`;
    const ok = await showModalConfirm('Move to Trash',
        `Move ${what} to Trash?${list.length === 1 ? ' A folder is moved with all its contents.' : ' Folders are moved with all their contents.'}`);
    if (!ok) return null;
    if (state) state._deleteBusy = true;
    try {
        const resp = await apiFetch('/api/files/trash/batch', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ paths: list }),
        });
        if (!resp.ok) {
            addNotification(await readErrorDetail(resp, `HTTP ${resp.status}`), 'error');
            return null;
        }
        const { op_id: opId } = await resp.json();
        if (!opId) { addNotification('Could not start the Trash operation', 'error'); return null; }
        const result = await _trackFileOp(opId, list.length, 'trash', { deferMs: 600 });
        if (!result) return null;
        _fmReportOpResult(result, 'Move to Trash', 'moved to Trash');
        if (targetRoot) _fmRefreshAfterDelete(targetRoot, state);
        else document.dispatchEvent(new CustomEvent('ethos:trash-changed'));
        return result;
    } catch (e) {
        addNotification(e.message, 'error');
        return null;
    } finally {
        if (state) state._deleteBusy = false;
    }
}

/** Move the current selection (or explicit paths) to Trash. */
export function fmDeleteSelected(paths) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    const targets = Array.isArray(paths) && paths.length ? paths : _fmResolveSelectedPaths(state, root);
    if (!targets.length) return addNotification('No items selected', 'info');
    return fmTrashPaths(targets, root);
}

// Copy selected items — puts them on the shared clipboard. The actual copy
// happens on Paste (standard file-manager semantics). No duplicate in place.
export function fmCopySelected() { const root = _getActiveFmRoot(); if (!root) return; const state = _getFmStateForRoot(root); if (!state || state.selected.size === 0) return addNotification('No items selected', 'info');
    const paths = _fmResolveSelectedPaths(state, root);
    _fmClipboard = { op: 'copy', paths };
    _fmSyncActionButtons(root, state);
    addNotification(`${paths.length} item(s) copied`, 'success');
}

// Cut selected items — same as copy but marks for move operation on paste
export function fmCutSelected() { const root = _getActiveFmRoot(); if (!root) return; const state = _getFmStateForRoot(root); if (!state || state.selected.size === 0) return addNotification('No items selected', 'info');
    // Store clipboard for paste operation (cut/move mode)
    const paths = _fmResolveSelectedPaths(state, root);
    _fmClipboard = { op: 'cut', paths };
    _fmSyncActionButtons(root, state);
    addNotification(`${paths.length} item(s) cut`, 'success');  // Visual feedback only — actual move happens on paste
}

// Paste from the shared clipboard — dispatches to /api/files/copy or /api/files/move
export function fmPaste() { const root = _getActiveFmRoot(); if (!root) return; const state = _getFmStateForRoot(root); if (!state || _fmClipboardEmpty()) return addNotification('Clipboard is empty', 'info');
    if (state._pasteBusy) return addNotification('A copy/move operation is already in progress', 'warning');
    const op = _fmClipboard.op;
    const opLabel = op === 'cut' ? 'Move' : 'Copy';
    const sources = [..._fmClipboard.paths];
    const destDir = state.path;
    (async () => { try {
        // Start the background op, then track it via /api/files/ops/{op_id}
        state._pasteBusy = true;
        const endpoint = op === 'cut' ? '/api/files/move' : '/api/files/copy';
        const resp = await apiFetch(endpoint, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({sources, dest_dir: destDir})
        });
        if (!resp.ok) { const d = await resp.json().catch(() => ({})); addNotification(d.detail || `HTTP ${resp.status}`, 'error'); return; }
        const { op_id } = await resp.json();
        const result = await _trackFileOp(op_id, sources.length, op === 'cut' ? 'move' : 'copy');
        if (!result) return;  // tracking failed — clipboard kept for retry
        addNotification(result.failed > 0
            ? `${opLabel} finished with errors: ${result.succeeded} ok, ${result.failed} failed`
            : `${result.succeeded} item(s) pasted`, result.failed > 0 ? 'error' : 'success');
        fmLoad(destDir, root);  // Refresh to show new state
        _fmClearClipboard();    // Clear clipboard after successful paste
        _fmSyncActionButtons(root, state);
    } catch(e) { addNotification(e.message, 'error'); } finally { state._pasteBusy = false; } })();
}

/**
 * Poll a background file op (copy/move/delete/trash/purge) until it finishes,
 * driving a row in the shared task center (core/progress.js) — the same panel
 * uploads and package jobs use.
 *
 * `deferMs` creates the row only when the operation is not instantly done, so a
 * single small file does not flash a progress row.
 */
const FM_OP_VERBS = {
    copy: { running: 'Copying', done: 'Copied' },
    move: { running: 'Moving', done: 'Moved' },
    hard_delete: { running: 'Deleting', done: 'Deleted' },
    trash: { running: 'Moving to Trash', done: 'Moved to Trash' },
    purge: { running: 'Deleting from Trash', done: 'Deleted from Trash' },
};

async function _trackFileOp(opId, itemCount, action = 'copy', { deferMs = 0 } = {}) {
    const verbs = FM_OP_VERBS[action] || { running: 'Working', done: 'Done' };
    let task = null;
    const ensureTask = () => {
        if (!task) {
            task = startTask({
                appId: 'files',
                title: `${verbs.running} ${itemCount} item${itemCount === 1 ? '' : 's'}…`,
                total: itemCount,
                testIdPrefix: 'fm-op',
            });
        }
        return task;
    };
    let timer = deferMs > 0 ? setTimeout(ensureTask, deferMs) : null;
    if (deferMs <= 0) ensureTask();
    try {
        let delayMs = 400;
        for (;;) {
            await new Promise(r => setTimeout(r, delayMs));
            delayMs = 1000;
            const resp = await apiFetch(`/api/files/ops/${opId}`);
            if (!resp.ok) throw new Error(resp.status === 404 ? 'Operation no longer available (server restarted?)' : `HTTP ${resp.status}`);
            const snap = await resp.json();
            if (snap.status !== 'running') {
                if (timer) { clearTimeout(timer); timer = null; }
                const firstFailed = (snap.details || []).find(d => d.status !== 'ok');
                ensureTask().finish(snap.failed > 0 ? 'failed' : 'done', {
                    percent: 100,
                    detail: snap.failed > 0
                        ? `${snap.failed} failed — ${_fmDeleteReason(firstFailed)}`
                        : `${verbs.done} ${snap.succeeded} item${snap.succeeded === 1 ? '' : 's'}`,
                });
                return snap;
            }
            if (task) {
                task.update({
                    percent: snap.percent,
                    index: snap.index,
                    total: snap.total,
                    detail: snap.current || verbs.running,
                    bytesDone: snap.bytes_done,
                    bytesTotal: snap.bytes_total,
                });
            }
        }
    } catch (e) {
        if (timer) { clearTimeout(timer); timer = null; }
        addNotification(e.message, 'error');
        ensureTask().finish('failed', { detail: e.message });
        return null;
    } finally {
        if (timer) clearTimeout(timer);
    }
}

/** Human-readable reason for one failed delete/trash entry. */
function _fmDeleteReason(detail) {
    if (detail && detail.reason) return detail.reason;
    const status = String((detail && detail.status) || '');
    if (status === 'permission_denied') return 'permission denied (the ethos-nas service user cannot remove it)';
    if (status === 'not_found') return 'no longer exists';
    if (status.startsWith('error: ')) return status.slice(7);
    return status || 'failed';
}

/** One summary toast for a finished delete/trash/purge op, with reasons. */
function _fmReportOpResult(result, title, doneVerb) {
    const failed = (result.details || []).filter(d => d.status !== 'ok');
    if (!failed.length) {
        addNotification(`${result.succeeded} item${result.succeeded === 1 ? '' : 's'} ${doneVerb}`, 'success');
        return;
    }
    const reasons = failed.slice(0, 3).map(d => `${d.name}: ${_fmDeleteReason(d)}`).join('; ');
    const more = failed.length > 3 ? ` (+${failed.length - 3} more)` : '';
    addNotification(`${title}: ${result.succeeded} ok, ${failed.length} failed — ${reasons}${more}`, 'error');
}

// Download selected items as ZIP archive — calls GET /api/files/zip with a JSON path list.
// `explicitPaths` lets the folder context menu ask for exactly one folder without
// depending on the current selection.
export function fmZipSelected(explicitPaths) {
    const explicit = Array.isArray(explicitPaths) ? explicitPaths.filter(Boolean) : [];
    const root = _getActiveFmRoot();
    const state = root ? _getFmStateForRoot(root) : null;

    if (!explicit.length && (!state || state.selected.size === 0)) {
        return addNotification('No items selected for ZIP', 'info');
    }
    // Build a proper JSON array of paths — the endpoint parses it with json.loads,
    // so per-element encoding alone breaks on names containing quotes.
    // Resolve through the rendered rows: in the search view the items are not
    // inside state.path (a name-only lookup could target a different file).
    const paths = explicit.length ? explicit : _fmResolveSelectedPaths(state, root);
    if (!paths.length) return addNotification('No items selected for ZIP', 'info');
    // The archive is built before any response is sent, so large folders show
    // nothing for a while. Say so instead of leaving the user with a blank tab.
    addNotification(paths.length === 1
        ? 'Preparing ZIP — large folders can take a while. The download starts when the archive is ready.'
        : `Preparing ZIP with ${paths.length} items — the download starts when the archive is ready.`, 'info');
    // Include JWT token in query so direct browser navigation (new tab) passes auth middleware
    const _token = localStorage.getItem('token') || '';
    window.open(`/api/files/zip?path=${encodeURIComponent(JSON.stringify(paths))}&token=${encodeURIComponent(_token)}`, '_blank');  // Open zip in new tab — user can save it
}

// Hard delete selected items — confirmation for every delete, op-tracked.
export async function fmHardDeleteSelected(paths) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (state._canWrite === false) return addNotification('Read-only folder — items cannot be deleted here', 'error');
    if (state._deleteBusy) return addNotification('A delete operation is already running', 'warning');
    const targets = Array.isArray(paths) && paths.length ? paths : _fmResolveSelectedPaths(state, root);
    if (!targets.length) return addNotification('No items selected', 'info');

    // Summarize large selections so the confirm dialog stays compact
    const names = targets.map(_fmBasename);
    const where = state._searchTerm ? `matching "${state._searchTerm}"` : `in ${state.path}`;
    const items = names.length <= 8 ? `${names.join(', ')} ${where}` : `${names.length} item(s) ${where}`;
    const ok = await showModalConfirm('Permanently Delete',
        `Permanently delete ${items}? This cannot be undone — folders are removed with all their contents.`);
    if (!ok) return;

    state._deleteBusy = true;
    try {
        const resp = await apiFetch('/api/files/delete', {
            method: 'DELETE',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ paths: targets })
        });
        if (!resp.ok) {
            addNotification(await readErrorDetail(resp, `HTTP ${resp.status}`), 'error');
            return;
        }
        const { op_id: opId } = await resp.json();
        const result = await _trackFileOp(opId, targets.length, 'hard_delete', { deferMs: 400 });
        if (!result) return;  // tracking failed — user already notified
        _fmReportOpResult(result, 'Permanent delete', 'permanently deleted');
        _fmRefreshAfterDelete(root, state);
    } catch (e) {
        addNotification(e.message, 'error');
    } finally {
        state._deleteBusy = false;
    }
}

function _fmBasename(p) { return p.split('/').filter(Boolean).pop() || ''; }
function _fmJoin(dir, name) { return (dir === '/' ? '' : dir) + '/' + name; }
// Resolve a selected item's full path. Search results live outside state.path,
// so read the authoritative path from the rendered row (any view).
function _fmResolveSelectedPath(state, root, name) {
    if (name && name !== '..' && root) {
        const el = root.querySelector(`[data-name="${CSS.escape(name)}"][data-path]`);
        const p = el ? el.getAttribute('data-path') : null;
        if (p) return p;
    }
    return _fmJoin(state.path, name);
}
function _fmBaseNoExt(n) { const dot = n.lastIndexOf('.'); return dot > 0 ? n.substring(0, dot) : n; }
function _fmExt(n) { const i = n.lastIndexOf('.'); return i > 0 ? n.substring(i) : ''; }
async function _fmListDir(path) { try { const r = await apiFetch('/api/files?path=' + encodeURIComponent(path)); if (!r.ok) return []; const d = await r.json(); return (d.files || []).map(f => ({ name: f.name, path: f.path })); } catch { return []; } }
export function fmSelectAll() { const root = _getActiveFmRoot(); if (!root) return; const state = _getFmStateForRoot(root); if (state) { const items = root.querySelectorAll('.fm-content .fm-file-item, .fm-content .fm-list-row'); for (const item of items) { const n = item.dataset?.name || item.getAttribute('data-name'); if (n && n !== '..') state.selected.add(n); } fmRenderGridItems(); } }

// ── Inline Rename (F2 key) ────────────────────────────────────────────────────
export function fmRenameSelected() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (state.selected.size !== 1) { addNotification('Select exactly one item to rename', 'info'); return; }
    const name = [...state.selected][0];
    // Find the file item element
    const item = root.querySelector(`.fm-file-item[data-name="${CSS.escape(name)}"], .fm-list-row[data-name="${CSS.escape(name)}"]`);
    if (!item) return;
    // Get the name display element
    const nameEl = item.querySelector('.fm-file-name, td:first-child span:last-child');
    if (!nameEl || item.classList.contains('fm-renaming')) return;
    const fullPath = item.getAttribute('data-path') || state.path + '/' + name;
    item.classList.add('fm-renaming');
    const origText = nameEl.textContent;
    const input = document.createElement('input');
    input.type = 'text';
    input.value = origText;
    input.className = 'fm-inline-rename-input';
    input.style.cssText = 'background:var(--surface-1);border:1px solid var(--accent);border-radius:4px;color: var(--text-1);font-size:13px;padding:2px 6px;width:80%;outline:none;';
    // Replace name content with input
    nameEl.textContent = '';
    nameEl.appendChild(input);
    input.focus();
    input.select();
    // Preselect the base name only (keep the extension) — Nautilus / Finder behaviour.
    const _dot = origText.lastIndexOf('.');
    try { input.setSelectionRange(0, _dot > 0 ? _dot : origText.length); } catch { /* not focusable */ }

    let settled = false;
    const cleanup = () => { input.removeEventListener('keydown', onKey); input.removeEventListener('blur', onBlur); };
    const finishRename = async (save) => {
        // One-shot: Enter / Escape is often followed by a blur, and the old code
        // submitted a second rename request that failed with "Path not found".
        if (settled) return;
        settled = true;
        cleanup();
        item.classList.remove('fm-renaming');
        const newName = input.value.trim();
        if (!save || !newName || newName === origText) { nameEl.textContent = origText; return; }
        try {
            const path = item.getAttribute('data-path') || state.path + '/' + origText;
            const resp = await apiFetch('/api/files/rename', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path, new_name: newName})
            });
            if (resp.ok) {
                addNotification('Renamed to ' + newName, 'success');
                fmLoad(undefined, root);
            } else {
                const d = await resp.json().catch(() => ({}));
                addNotification(d.detail || `HTTP ${resp.status}`, 'error');
                nameEl.textContent = origText;
            }
        } catch (e) {
            addNotification(e.message, 'error');
            nameEl.textContent = origText;
        }
    };
    const onKey = (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); finishRename(true); }
        else if (ev.key === 'Escape') { ev.preventDefault(); input.value = origText; finishRename(false); }
    };
    // Clicking elsewhere commits the rename (like a desktop file manager).
    const onBlur = () => setTimeout(() => finishRename(true), 150);
    input.addEventListener('keydown', onKey);
    input.addEventListener('blur', onBlur);
}

// ── Context Menu ─────────────────────────────────────────────────────────────

// ── Context menu engine (item menu + empty-space menu) ───────────────────────
// A menu used to stay on screen until one of its entries was clicked; it now
// closes on outside click, Escape and window blur like a native menu.
let _fmMenuEl = null;
let _fmMenuCleanup = null;

function _closeContextMenu() {
    if (_fmMenuEl) { try { _fmMenuEl.remove(); } catch { /* already detached */ } _fmMenuEl = null; }
    if (_fmMenuCleanup) { try { _fmMenuCleanup(); } catch { /* listeners already gone */ } _fmMenuCleanup = null; }
}

/**
 * Render a context menu at (x, y).
 *
 * @param {number} x viewport X
 * @param {number} y viewport Y
 * @param {Array<object|null>} items entries `{label, icon, action, testid?, key?, danger?, disabled?}`,
 *        `null` for a separator
 */
function _fmBuildContextMenu(x, y, items) {
    _closeContextMenu();
    _fmCloseSortMenu();
    const menu = document.createElement('div');
    menu.className = 'fm-context-menu';
    menu.setAttribute('data-testid', 'fm-ctx-menu');
    for (const item of items) {
        if (item === null) {
            const s = document.createElement('div');
            s.className = 'fm-ctx-sep';
            menu.appendChild(s);
            continue;
        }
        const btn = document.createElement('button');
        btn.className = 'fm-ctx-item' + (item.danger ? ' fm-ctx-danger' : '');
        if (item.testid) btn.setAttribute('data-testid', item.testid);
        if (item.disabled) {
            btn.disabled = true;
            btn.style.opacity = '0.45';
            btn.style.cursor = 'default';
        } else {
            btn.onclick = (ev) => { ev.stopPropagation(); _closeContextMenu(); item.action(); };
        }
        btn.innerHTML = `<span class="fm-ctx-ic">${svgIcon(item.icon, 15)}</span><span>${escapeHtml(item.label)}</span>`
            + (item.key ? `<span class="fm-ctx-key">${escapeHtml(item.key)}</span>` : '');
        menu.appendChild(btn);
    }
    document.body.appendChild(menu);
    // Clamp to the viewport using the measured size (menus differ in height).
    const w = menu.offsetWidth || 220, h = menu.offsetHeight || 200;
    menu.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 8)) + 'px';
    menu.style.top = Math.max(8, Math.min(y, window.innerHeight - h - 8)) + 'px';
    const onDown = (ev) => { if (!menu.contains(ev.target)) _closeContextMenu(); };
    const onKey = (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); _closeContextMenu(); } };
    const onBlur = () => _closeContextMenu();
    setTimeout(() => {
        document.addEventListener('mousedown', onDown, true);
        document.addEventListener('keydown', onKey, true);
        window.addEventListener('blur', onBlur);
    }, 0);
    _fmMenuEl = menu;
    _fmMenuCleanup = () => {
        document.removeEventListener('mousedown', onDown, true);
        document.removeEventListener('keydown', onKey, true);
        window.removeEventListener('blur', onBlur);
    };
}

/** Right-click on empty space: create / paste / select / refresh. */
export function fmShowBackgroundContextMenu(e, container) {
    e.preventDefault();
    e.stopPropagation();
    const root = container || (e.target && e.target.closest ? e.target.closest('[data-fm-win]') : null);
    const state = _getFmStateForRoot(root);
    if (!state) return;

    if (state._inTrash) {
        _fmBuildContextMenu(e.clientX, e.clientY, [
            { testid: 'fm-bg-trash-refresh', label: 'Refresh', key: 'F5', icon: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>', action: () => fmOpenTrash() },
            null,
            { testid: 'fm-bg-trash-empty', label: 'Empty Trash…', danger: true, icon: '<polyline points="3,6 5,6 21,6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>', action: () => fmEmptyTrash() },
        ]);
        return;
    }

    const writable = state._canWrite !== false;
    const clipCount = _fmClipboard.paths.length;
    _fmBuildContextMenu(e.clientX, e.clientY, [
        { testid: 'fm-bg-new-folder', label: 'New Folder…', key: 'Ctrl+Shift+N', disabled: !writable, icon: '<path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/>', action: () => fmNewFolder() },
        { testid: 'fm-bg-new-file', label: 'New Empty File…', key: 'Ctrl+N', disabled: !writable, icon: '<path d="M13 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V9z"/><polyline points="13,2 13,9 20,9"/>', action: () => fmNewFile() },
        null,
        { testid: 'fm-bg-paste', label: clipCount ? `Paste ${clipCount} item${clipCount > 1 ? 's' : ''}` : 'Paste', key: 'Ctrl+V', disabled: !writable || clipCount === 0, icon: '<path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>', action: () => fmPaste() },
        null,
        { testid: 'fm-bg-select-all', label: 'Select All', key: 'Ctrl+A', icon: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 12l3 3 5-6"/>', action: () => fmSelectAll() },
        { testid: 'fm-bg-refresh', label: 'Refresh', key: 'F5', icon: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>', action: () => fmRefresh() },
        { testid: 'fm-bg-upload', label: 'Upload Files…', disabled: !writable, icon: '<path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="17,8 12,3 7,8"/><line x1="12" y1="3" x2="12" y2="15"/>', action: () => root.querySelector('input.fm-file-input')?.click() },
    ]);
}

function fmShowItemContextMenuOrBackground(e, container) {
    // Let item-level inline handlers deal with rows; this is the empty-space case.
    const itemEl = _fmItemElFromEvent(e, container);
    if (itemEl) return;
    fmShowBackgroundContextMenu(e, container);
}

export function fmShowItemContextMenu(e, path, name, isDir) {
    e.preventDefault();
    e.stopPropagation();
    _closeContextMenu(); // close any existing menu first

    const state = _getFmStateForRoot(_getActiveFmRoot());
    if (!state) return;

    // Standard file-manager behavior: keep the current multi-selection when the
    // right-clicked item is part of it (e.g. after a shift-select); otherwise select just this item.
    if (name !== '..' && !state.selected.has(name)) {
        state.selected = new Set([name]);
        state._anchorName = name;
        fmRenderGridItems();
    }
    const selCount = Math.max(1, state.selected.size);
    const selLabel = (singular, plural) => selCount > 1 ? plural : singular;

    const items = [];
    const sep = () => items.push(null);

    // Open / Navigate (directories only)
    if (isDir && name !== '..') {
        items.push({ testid: 'fm-ctx-open', label: 'Open', icon: '<path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15,3 21,3 21,9"/><line x1="10" y1="14" x2="21" y2="3"/>', action: () => fmNavigateTo(path) });
        items.push({ testid: 'fm-ctx-open-gallery', label: 'Open in Gallery', icon: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/>', action: () => { window.__fmGalleryCtx = { folder: path }; openApp('gallery'); } });
        items.push({ testid: 'fm-ctx-new-window', label: 'Open in New Window', icon: '<rect x="3" y="3" width="18" height="14" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/>', action: () => fmOpenInNewWindow(path) });
        sep();
    }

    // Cut / Copy / Paste — the clipboard is shared by all Files windows
    const hasClip = !_fmClipboardEmpty();
    if (hasClip) {
        items.push({ testid: 'fm-ctx-paste', label: `Paste here (${_fmClipboard.paths.length} item${_fmClipboard.paths.length > 1 ? 's' : ''})`, disabled: state._canWrite === false, icon: '<path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/>', action: () => { fmPaste(); } });
        sep();
    }

    if (!isDir || name !== '..') {
        items.push({ testid: 'fm-ctx-cut', label: selLabel('Cut', `Cut ${selCount} Items`), icon: '<path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/>', action: () => { _closeContextMenu(); fmCutSelected(); } });
        items.push({ testid: 'fm-ctx-copy', label: selLabel('Copy', `Copy ${selCount} Items`), icon: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/>', action: () => { _closeContextMenu(); fmCopySelected(); } });
    }

    items.push({ testid: 'fm-ctx-rename', label: 'Rename\u2026', icon: '<path d="M17 3a2.85 2.83 0 114 4L7.5 20.5 2 22l1.5-5.5z"/>', disabled: state._canWrite === false || selCount !== 1, action: () => _renameItem(name, path) });

    if (isDir && name !== '..') {
        items.push({ testid: 'fm-ctx-open-project', label: 'Open as Project\u2026', icon: '<path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>', action: () => { window.__fmProjectCtx = { path }; window.openApp('editor'); } });
        // Folders have no single-file download endpoint — the NAS builds a ZIP
        // server-side. Without this entry the only way was: select the tile and
        // press the toolbar ZIP button, which users rarely discover.
        items.push({ testid: 'fm-ctx-download-zip', label: 'Download Folder (.zip)', icon: '<path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7,10 12,15 17,10"/><line x1="12" y1="15" x2="12" y2="3"/>', action: () => { _closeContextMenu(); fmZipSelected([path]); } });
        sep();
    }

    if (!isDir && name !== '..') {
        sep();
        // Open / Edit options (T-ED) — more ways to open a file by format.
        const _ext = '.' + (path.split('/').filter(Boolean).pop() || '').toLowerCase().split('.').pop();
        items.push({ testid: 'fm-ctx-edit', label: 'Edit\u2026', icon: '<path d="M17 3a2.85 2.83 0 114 4L7.5 20.5 2 22l1.5-5.5z"/>', action: () => openEditorForPath(path) });
        items.push({ testid: 'fm-ctx-preview', label: 'Preview', icon: '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>', action: () => fmShowPreview(path, false) });
        if (_IMAGE_EXTS.has(_ext)) {
            items.push({ testid: 'fm-ctx-view-photos', label: 'View in Photos', icon: '<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/>', action: () => { window.__fmGalleryCtx = { dir: state.path, files: [{ name, path, is_dir: false }], index: 0 }; window.openApp('gallery'); } });
        }
        if (_VIDEO_EXTS.has(_ext)) {
            items.push({ testid: 'fm-ctx-play-video', label: 'Play Video', icon: '<polygon points="5,3 19,12 5,21 5,3"/>', action: () => fmOpenVideoPlayer(path) });
        }
        if (_isArchivePath(path)) {
            const archives = _selectedArchivePaths(path);
            const suffix = archives.length > 1 ? ` ${archives.length} Archives` : '';
            const extractIcon = '<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/>';
            items.push({ testid: 'fm-ctx-extract', label: `Extract${suffix} Here`, icon: extractIcon, action: () => _extractArchives(archives, {}) });
            items.push({ testid: 'fm-ctx-extract-to', label: `Extract${suffix} to…`, icon: '<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M12 11v6"/><polyline points="9,14 12,17 15,14"/>', action: () => _extractToDialog(archives) });
        }
        sep();
        const _token = localStorage.getItem('token') || '';
        items.push({ testid: 'fm-ctx-download', label: 'Download', icon: '<path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7,10 12,15 17,10"/><line x1="12" y1="15" x2="12" y2="3"/>', action: () => _downloadFile(path, _token) });
    }

    sep();
    items.push({ testid: 'fm-ctx-favorite', label: _fmIsFavorite(path) ? 'Remove from Favorites' : 'Add to Favorites', icon: '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>', action: () => fmToggleFavorite(path) });
    items.push({ testid: 'fm-ctx-copy-path', label: 'Copy Path', icon: '<path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/>', action: () => _copyPathToClipboard(path) });
    items.push({ testid: 'fm-ctx-share-link', label: 'Share link…', icon: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/>', action: () => { window.__fmShareCtx = { path }; openApp('shared-links'); } });
    items.push({ testid: 'fm-ctx-share-users', label: 'Share with users…', icon: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>', action: () => { openShareDialog({ kind: 'path', resource: path }); } });
    items.push({ testid: 'fm-ctx-properties', label: 'Properties', icon: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>', action: () => _showProperties(path, isDir) });
    sep();
    // Single-item actions use the exact path the menu was opened on — resolving
    // by name could hit a different file when search results share a name.
    const exactPath = (name !== '..' && state.selected.size === 1) ? [path] : null;
    items.push({ testid: 'fm-ctx-trash', label: selLabel(isDir ? 'Move Folder to Trash' : 'Move File to Trash', `Move ${selCount} Items to Trash`), disabled: state._canWrite === false, icon: '<polyline points="3,6 5,6 21,6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>', action: () => { _closeContextMenu(); fmDeleteSelected(exactPath); } });
    items.push({ testid: 'fm-ctx-delete', label: selLabel(isDir ? 'Delete Folder Permanently' : 'Delete File Permanently', `Delete ${selCount} Items Permanently`), danger: true, disabled: state._canWrite === false, icon: '<polyline points="3,6 5,6 21,6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>', action: () => { _closeContextMenu(); fmHardDeleteSelected(exactPath); } });

    _fmBuildContextMenu(e.clientX, e.clientY, items);
}

async function _renameItem(name, path) {
    const root = _getActiveFmRoot();
    if (!root) return;
    const state = _getFmStateForRoot(root);
    if (!state) return;
    
    const newName = await showPrompt('Rename', 'New name:', name);
    if (newName && newName !== name) {
        try {
            const resp = await apiFetch('/api/files/rename', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({path, new_name: newName})
            });
            if (resp.ok) {
                addNotification('Renamed successfully', 'success');
                fmLoad(undefined, root);   // refresh the window the rename started in
            } else {
                const d = await resp.json();
                addNotification(d.detail || `HTTP ${resp.status}`, 'error');
            }
        } catch (e) {
            addNotification(e.message, 'error');
        }
    }
}

async function _downloadFile(path, token) {
    try {
        const url = `/api/files/download?path=${encodeURIComponent(path)}&token=${encodeURIComponent(token)}`;
        // Use a hidden anchor with direct URL — browser handles full response (Content-Disposition + size) natively
        const a = document.createElement('a');
        a.href = url;
        a.download = path.split('/').filter(Boolean).pop() || 'download';
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        setTimeout(() => { a.remove(); }, 2000);
    } catch (e) {
        addNotification(`Download failed: ${e.message}`, 'error');
    }
}

// ── Archive extraction ──────────────────────────────────────────────────────
// One POST per archive (POST /api/files/extract). The quick path ("Extract
// here", double-click) keeps creating a folder next to the archive; the dialog
// path ("Extract to…") chooses destination/folder/password. All archives of one
// action share a single progress row, and other windows refresh through the
// file-events WebSocket.

function _extractArchive(path) {
    return _extractArchives([path], {});
}

/** Archive paths in the current selection (falls back to the right-clicked file). */
function _selectedArchivePaths(clickedPath) {
    const root = _getActiveFmRoot();
    const state = root ? _getFmStateForRoot(root) : null;
    let paths = [];
    if (state && root) {
        try { paths = _fmResolveSelectedPaths(state, root) || []; } catch { paths = []; }
    }
    const archives = paths.filter((p) => _isArchivePath(p));
    if (clickedPath && !archives.includes(clickedPath)) archives.unshift(clickedPath);
    return [...new Set(archives)];
}

async function _extractArchives(paths, { destDir = '', subfolder = null, overwrite = false, password = '' } = {}) {
    const list = [...new Set((paths || []).filter((p) => p && _isArchivePath(p)))];
    if (!list.length) { addNotification('Not an extractable archive', 'warning'); return; }
    const root = _getActiveFmRoot();
    const multi = list.length > 1;
    const singleName = list[0].split('/').filter(Boolean).pop() || list[0];
    const task = startTask({
        appId: 'files',
        title: multi ? `Extracting ${list.length} archives…` : `Extracting ${singleName}…`,
        testIdPrefix: 'fm-op',
    });
    let ok = 0;
    let failed = 0;
    let files = 0;
    let kept = 0;
    let lastDest = '';
    let lastError = '';
    const errors = [];

    for (const archive of list) {
        const name = archive.split('/').filter(Boolean).pop() || archive;
        task.update({ detail: multi ? name : '' });
        const body = { path: archive, overwrite: !!overwrite };
        if (destDir) body.dest_dir = destDir;
        if (subfolder !== null && subfolder !== undefined) body.subfolder = subfolder;
        if (password) body.password = password;
        try {
            const resp = await apiFetch('/api/files/extract', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (resp.ok) {
                ok += 1;
                files += Number(data.extracted_count || 0);
                kept += Number(data.skipped_existing || 0);
                lastDest = data.dest_dir || lastDest;
                continue;
            }
            if (resp.status === 409 && data.missing_requirement) {
                task.finish('failed', { detail: data.detail || 'Missing extractor' });
                if (await _offerArchiveToolsInstall(data)) {
                    await _extractArchives(list, { destDir, subfolder, overwrite, password });
                }
                return;
            }
            failed += 1;
            lastError = String(data.detail || `HTTP ${resp.status}`);
            errors.push(lastError);
        } catch (e) {
            failed += 1;
            lastError = e?.message || String(e);
            errors.push(lastError);
        }
    }

    const notes = [];
    if (files) notes.push(`${files} file${files === 1 ? '' : 's'}`);
    if (kept) notes.push(`${kept} existing kept`);
    if (failed) notes.push(`${failed} failed`);
    if (errors.length) {
        addNotification(`Extract failed: ${errors[0]}`, 'error');
    } else {
        addNotification(
            multi
                ? `Extracted ${ok} archives${lastDest ? ` to ${lastDest}` : ''}${notes.length ? ` (${notes.join(', ')})` : ''}`
                : `Extracted to ${lastDest}${notes.length ? ` (${notes.join(', ')})` : ''}`,
            'success',
        );
    }
    task.finish(failed ? 'failed' : 'done', {
        percent: 100,
        detail: lastError || (notes.join(', ') || 'Done'),
    });
    if (root) _silentRefetch(root);
}

/** 409 from the API → offer a one-click admin install of the missing extractor. */
async function _offerArchiveToolsInstall(data) {
    const requirement = data?.missing_requirement;
    if (!requirement) return false;
    if (!isAdmin()) {
        addNotification(`${data.detail || 'Archive tools are not installed'} ${data.hint || ''}`.trim(), 'error');
        return false;
    }
    const confirmed = await showModalConfirm(
        'Archive tools required',
        `${data.detail || 'This archive needs an extractor that is not installed.'}\n\nInstall the archive tools now? (${data.hint || requirement})`,
    );
    if (!confirmed) return false;
    addNotification('Installing archive tools…', 'info');
    try {
        const resp = await apiFetch(`/api/system/deps/${encodeURIComponent(requirement)}/install`, { method: 'POST' });
        const out = await resp.json().catch(() => ({}));
        if (resp.ok) {
            addNotification('Archive tools installed', 'success');
            return true;
        }
        addNotification(out.detail || `HTTP ${resp.status}`, 'error');
    } catch (e) {
        addNotification(e?.message || String(e), 'error');
    }
    return false;
}

/** "Extract to…" dialog: destination picker, folder name, password, overwrite. */
async function _extractToDialog(paths) {
    const list = [...new Set((paths || []).filter(Boolean))];
    if (!list.length) return;
    const single = list.length === 1;
    const first = list[0];
    const parent = first.slice(0, first.lastIndexOf('/')) || '/';
    const stem = _archiveBaseName(first);
    let remembered = '';
    try { remembered = localStorage.getItem('ethos-fm-extract-dest') || ''; } catch { /* storage disabled */ }

    const values = await showFormModal({
        title: single ? `Extract ${stem}` : `Extract ${list.length} archives`,
        confirmLabel: 'Extract',
        width: '520px',
        bodyHtml: `
            <div class="ui-fields">
                <div class="ui-field full"><label for="fmExtractDest">Destination folder</label>
                    <div class="ui-row">
                        <input id="fmExtractDest" name="dest" class="ui-input" data-testid="fm-extract-dest"
                               value="${escapeAttr(remembered || parent)}" autocomplete="off" spellcheck="false">
                        <button type="button" class="ui-btn" data-testid="fm-extract-browse">Browse…</button>
                    </div>
                </div>
                <div class="ui-field full"><label for="fmExtractSubfolder">Folder name</label>
                    <input id="fmExtractSubfolder" name="subfolder" class="ui-input" data-testid="fm-extract-subfolder"
                           value="${escapeAttr(single ? stem : '')}" autocomplete="off" spellcheck="false"${single ? '' : ' disabled'}>
                    <p class="ui-muted">${single
                        ? 'Leave empty to extract straight into the destination folder.'
                        : 'Each archive gets its own folder in the destination.'}</p>
                </div>
                <div class="ui-field"><label for="fmExtractPassword">Password (optional)</label>
                    <input id="fmExtractPassword" name="password" type="password" class="ui-input"
                           data-testid="fm-extract-password" autocomplete="new-password">
                </div>
                <div class="ui-field"><label class="ui-check">
                    <input type="checkbox" name="overwrite" data-testid="fm-extract-overwrite"> Overwrite existing files
                </label></div>
            </div>`,
        validate: (v) => {
            const dest = String(v.dest || '').trim();
            if (!dest.startsWith('/')) return 'Destination must be an absolute path';
            if (single && /[\\/]/.test(String(v.subfolder || ''))) return 'Folder name must not contain slashes';
            return null;
        },
        onReady: (body) => {
            const browse = body.querySelector('[data-testid="fm-extract-browse"]');
            const destInput = body.querySelector('[data-testid="fm-extract-dest"]');
            if (browse && destInput) {
                browse.onclick = async () => {
                    const picked = await pickFolder({
                        title: 'Extract to folder',
                        startPath: destInput.value.trim() || parent,
                    });
                    if (picked) destInput.value = picked;
                };
            }
        },
    });
    if (!values) return;
    const dest = String(values.dest || '').trim();
    try { localStorage.setItem('ethos-fm-extract-dest', dest); } catch { /* storage disabled */ }
    await _extractArchives(list, {
        destDir: dest,
        subfolder: single ? String(values.subfolder ?? '').trim() : null,
        overwrite: values.overwrite === true,
        password: values.password || '',
    });
}

async function _copyPathToClipboard(path) {
    try {
        await navigator.clipboard.writeText(path);
        addNotification('Path copied to clipboard', 'success');
    } catch {
        // Fallback for older browsers or non-HTTPS contexts
        const ta = document.createElement('textarea');
        ta.value = path;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        try {
            document.execCommand('copy');
            addNotification('Path copied to clipboard', 'success');
        } catch {
            addNotification('Failed to copy path', 'error');
        }
        ta.remove();
    }
}

async function _showProperties(path, isDir) {
    // Close context menu first
    _closeContextMenu();
    
    const root = _getActiveFmRoot();
    if (!root) return;
    const winEl = root.closest('.app-window');
    if (!winEl) return;

    let fileData = null;
    try {
        // Authoritative single-item metadata. Fetching the parent listing (the old
        // behaviour) failed for items outside the current directory (search view)
        // and reported the listing's item count in the Size row for folders.
        const resp = await apiFetch(`/api/files/stat?path=${encodeURIComponent(path)}`);
        if (resp.ok) fileData = await resp.json();
    } catch (e) { console.debug('[EthOS] Properties lookup failed:', e.message); }

    let size = '—';
    let modified = '—';
    let permissions = '';
    let owner = '';
    const type = isDir ? 'Directory' : 'File';
    
    if (fileData) {
        if (fileData.is_dir) {
            if (fileData.item_count != null) size = `${fileData.item_count} item${fileData.item_count === 1 ? '' : 's'}`;
        } else {
            size = formatBytes(fileData.size || 0);
        }
        modified = fileData.modified ? new Date(fileData.modified * 1000).toLocaleString() : '—';
        permissions = fileData.permissions || '';
        if (fileData.owner) owner = fileData.group ? `${fileData.owner}:${fileData.group}` : fileData.owner;
    }

    const name = path.split('/').filter(Boolean).pop() || path;
    const dialog = document.createElement('div');
    dialog.className = 'fm-props-dialog';
    dialog.setAttribute('data-testid', 'fm-props-dialog');

    let rowsHtml = `<span class="fm-props-label">Path</span><span class="fm-props-value mono">${escapeHtml(path)}</span>`;
    if (size !== '\u2014') rowsHtml += `<span class="fm-props-label">Size</span><span class="fm-props-value">${escapeHtml(size)}</span>`;
    if (modified !== '\u2014') rowsHtml += `<span class="fm-props-label">Modified</span><span class="fm-props-value">${escapeHtml(modified)}</span>`;
    if (owner) rowsHtml += `<span class="fm-props-label">Owner</span><span class="fm-props-value mono">${escapeHtml(owner)}</span>`;
    if (permissions) rowsHtml += `<span class="fm-props-label">Permissions</span><span class="fm-props-value mono">${escapeHtml(permissions)}</span>`;
    if (fileData && fileData.can_write === false) rowsHtml += `<span class="fm-props-label">Access</span><span class="fm-props-value" style="color:#f59e0b;">read-only</span>`;

    dialog.innerHTML = `
        <div class="fm-props-header">
            <div class="fm-props-icon">${getFileIcon(name, isDir ? 'dir' : 'file')}</div>
            <div style="flex:1;min-width:0;">
                <div class="fm-props-name">${escapeHtml(name)}</div>
                <div class="fm-props-type">${type}</div>
            </div>
            <button class="fm-props-close" data-testid="fm-props-close">&times;</button>
        </div>
        <div class="fm-props-body">${rowsHtml}</div>`;

    winEl.appendChild(dialog);

    // Close handlers
    dialog.querySelector('.fm-props-close').onclick = () => dialog.remove();
    const closeOutside = (ev) => {
        if (!dialog.contains(ev.target)) { dialog.remove(); document.removeEventListener('mousedown', closeOutside); }
    };
    setTimeout(() => document.addEventListener('mousedown', closeOutside), 0);
}

// Also expose globally for inline onclick handlers in HTML
window.fmShowItemContextMenu = fmShowItemContextMenu;
window._renameItem = _renameItem;
window.fmSelectItem = fmSelectItem;
window.fmOpenPreview = fmShowPreview;
window.closePreview = fmClosePreview;
window.fmZipSelected = fmZipSelected;   // toolbar button uses an inline onclick

// ── Render selection visuals only ──
function fmRenderGridItems(explicitRoot) { const root = explicitRoot || _getActiveFmRoot(); if (!root) return; const contentEl = root.querySelector('.fm-content'); if (!contentEl) return; const state = _getFmStateForRoot(root); if (!state) return; const items = contentEl.querySelectorAll('.fm-file-item, .fm-list-row, .fm-search-result'); items.forEach(item => { const n = item.dataset?.name || item.getAttribute('data-name'); if (!n || n === '..') return; const sel = state.selected.has(n); item.classList.toggle('selected', sel); if (item.hasAttribute('role')) item.setAttribute('aria-selected', String(sel)); });
    // Keep the status bar and toolbar in sync with the selection.
    _fmUpdateSelectionStatus(root, state);
    _fmSyncActionButtons(root, state);
}

// ── Preview Panel (image, audio, PDF & text/code) ────────────────────────────

const _previewState = { open: false, currentPath: null };

function _isPreviewableFile(fileType, name) {
    if (!fileType && !name) return false;
    const ext = name ? '.' + name.toLowerCase().split('.').pop() : '';
    if (_IMAGE_EXTS.has(ext) || _AUDIO_EXTS.has(ext) || ext === '.pdf') return true;
    if (_fmIsTextFile(name) || fileType === 'code' || fileType === 'text') return true;
    return false;
}

function fmShowPreview(path, isDir) {
    if (_previewState.open && _previewState.currentPath === path) { fmClosePreview(); return; }
    // Target the specific FM window via the active root's parent app-window
    const root = _getActiveFmRoot();
    const winEl = root ? root.closest('.app-window') : document.querySelector('.app-window');
    if (!winEl) return;
    
    // Remove any existing preview
    const existing = winEl.querySelector('.fm-preview-overlay');
    if (existing) existing.remove();

    const fileName = path.split('/').filter(Boolean).pop() || path;
    const ext = isDir ? '' : ('.' + fileName.toLowerCase().split('.').pop());
    const canEdit = !isDir && _fmIsTextFile(fileName);
    const token = localStorage.getItem('token') || '';

    const overlay = document.createElement('div');
    overlay.className = 'fm-preview-overlay active';
    overlay.setAttribute('data-testid', 'fm-preview-panel');
    overlay.innerHTML = `
        <div class="fm-preview-backdrop"></div>
        <div class="fm-preview-panel">
            <div class="fm-preview-header">
                <span class="fm-preview-title" data-testid="fm-preview-name">${isDir ? 'Folder' : escapeHtml(fileName)}</span>
                <div class="fm-preview-actions">
                    ${canEdit ? '<button class="fm-preview-action" data-testid="fm-preview-edit" title="Open in Code Editor">✎</button>' : ''}
                    <button class="fm-preview-action" data-testid="fm-preview-download" title="Download">⬇</button>
                    <button class="fm-preview-close" data-testid="fm-preview-close" title="Close">&times;</button>
                </div>
            </div>
            <div class="fm-preview-body"><div style="color: var(--text-3);">Loading…</div></div>
        </div>`;

    winEl.appendChild(overlay);
    
    // Trigger slide-in animation
    requestAnimationFrame(() => {
        overlay.querySelector('.fm-preview-panel').classList.add('open');
    });

    const body = overlay.querySelector('.fm-preview-body');
    const closeBtn = overlay.querySelector('.fm-preview-close');
    const backdrop = overlay.querySelector('.fm-preview-backdrop');
    
    // Close handlers
    const cleanup = () => { fmClosePreview(); };
    closeBtn.onclick = cleanup;
    backdrop.onclick = cleanup;
    const editBtn = overlay.querySelector('[data-testid="fm-preview-edit"]');
    if (editBtn) editBtn.onclick = () => { fmClosePreview(); openEditorForPath(path); };
    const dlBtn = overlay.querySelector('[data-testid="fm-preview-download"]');
    if (dlBtn) dlBtn.onclick = () => { _downloadFile(path, token); };

    // Escape key closes preview
    function onEsc(e) { if (e.key === 'Escape') { e.preventDefault(); fmClosePreview(); document.removeEventListener('keydown', onEsc); } }
    setTimeout(() => document.addEventListener('keydown', onEsc), 0);

    _previewState.open = true;
    _previewState.currentPath = path;
    
    // Load content based on file type
    if (!ext) { body.innerHTML = '<div style="color: var(--text-3);">Empty folder</div>'; return; }

    const rawUrl = `/api/files/raw?path=${encodeURIComponent(path)}&token=${encodeURIComponent(token)}`;
    if (_IMAGE_EXTS.has(ext)) {
        // Image preview — use raw endpoint for direct image rendering
        body.innerHTML = `<img src="${rawUrl}" alt="${escapeAttr(fileName)}" loading="lazy">`;
    } else if (_AUDIO_EXTS.has(ext)) {
        // Audio preview — the browser plays it inline, no download detour needed
        body.innerHTML = `<div class="fm-preview-audio"><div class="fm-preview-audio-art">🎵</div>` +
            `<audio controls autoplay src="${rawUrl}" data-testid="fm-preview-audio"></audio>` +
            `<div class="fm-preview-audio-name">${escapeHtml(fileName)}</div></div>`;
    } else if (ext === '.pdf') {
        body.innerHTML = `<iframe class="fm-preview-pdf" data-testid="fm-preview-pdf" src="${rawUrl}" title="${escapeAttr(fileName)}"></iframe>`;
    } else {
        // Text/code preview — fetch raw content and render
        apiFetch(`/api/files/raw?path=${encodeURIComponent(path)}`)
            .then(r => r.text())
            .then(text => {
                body.innerHTML = `<pre class="fm-preview-text">${escapeHtml(text.substring(0, 50000))}</pre>`;
            })
            .catch(() => { body.innerHTML = '<div style="color: var(--text-3);">Could not load file content</div>'; });
    }
}

function fmClosePreview() {
    // Target the specific FM window via the active root's parent app-window
    const root = _getActiveFmRoot();
    const winEl = root ? root.closest('.app-window') : document.querySelector('.app-window');
    if (!winEl) return;
    const overlay = winEl.querySelector('.fm-preview-overlay');
    if (overlay) {
        overlay.classList.remove('active');
        const panel = overlay.querySelector('.fm-preview-panel');
        if (panel) panel.classList.remove('open');
        setTimeout(() => { overlay.remove(); }, 250);
    }
    _previewState.open = false;
    _previewState.currentPath = null;
}

/**
 * Space — Quick Look. Previews the single selected file in place (text, image,
 * audio, PDF) or opens the video player, without leaving the listing.
 */
export function fmToggleQuickLook() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (_previewState.open) { fmClosePreview(); return; }
    if (_videoPlayer.open) { fmCloseVideoPlayer(); return; }
    if (state._inTrash || state._searchTerm) return;
    if (state.selected.size !== 1) { addNotification('Select one file, then press Space to preview', 'info'); return; }
    const name = [...state.selected][0];
    const meta = state._fileMeta && state._fileMeta.get(name);
    if (meta && meta.is_dir) { addNotification('Press Enter to open the folder', 'info'); return; }
    const path = (meta && meta.path) || _fmResolveSelectedPath(state, root, name);
    const ext = '.' + (path.split('/').filter(Boolean).pop() || '').toLowerCase().split('.').pop();
    if (_VIDEO_EXTS.has(ext)) { fmOpenVideoPlayer(path); return; }
    if (_isPreviewableFile(meta && meta.file_type, name)) { fmShowPreview(path, false); return; }
    addNotification('No preview available for this file type', 'info');
}

// ── Shortcut cheat sheet ( "?" / F1 ) ───────────────────────────────────────

const _FM_SHORTCUT_GROUPS = [
    ['Navigation', [
        ['Ctrl+L', 'Edit path'], ['Alt+← / Alt+→', 'Back / Forward'],
        ['Backspace', 'Go to parent folder'], ['F5', 'Refresh'],
        ['↑ ↓ ← →', 'Move selection'], ['Home / End', 'First / last item'],
        ['Type-ahead', 'Jump to a name'],
    ]],
    ['Files', [
        ['Enter', 'Open'], ['Space', 'Quick Look'], ['F2', 'Rename'],
        ['Ctrl+C / Ctrl+X / Ctrl+V', 'Copy / Cut / Paste'],
        ['Ctrl+A', 'Select all'], ['Delete', 'Move to Trash'],
        ['Shift+Delete', 'Delete permanently'], ['Ctrl+Shift+N', 'New folder'],
        ['Ctrl+N', 'New empty file'], ['Ctrl+D', 'Star / unstar folder'],
    ]],
    ['Other', [
        ['/', 'Search'], ['Esc', 'Clear selection / close'], ['?', 'This cheat sheet'],
    ]],
];

export function fmShowShortcuts() {
    const existing = document.querySelector('.fm-shortcuts-overlay');
    if (existing) { existing.remove(); return; }
    const groups = _FM_SHORTCUT_GROUPS.map(([title, rows]) => `
        <div class="fm-shortcuts-group">
            <div class="fm-shortcuts-group-title">${escapeHtml(title)}</div>
            ${rows.map(([k, d]) => `<div class="fm-shortcuts-row"><kbd>${escapeHtml(k)}</kbd><span>${escapeHtml(d)}</span></div>`).join('')}
        </div>`).join('');
    const el = document.createElement('div');
    el.className = 'fm-shortcuts-overlay';
    el.setAttribute('data-testid', 'fm-shortcuts');
    el.innerHTML = `<div class="fm-shortcuts-dialog" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts">
        <div class="fm-shortcuts-head"><span>⌨ Keyboard shortcuts</span><button class="fm-props-close" data-testid="fm-shortcuts-close" aria-label="Close">&times;</button></div>
        <div class="fm-shortcuts-body">${groups}</div>
    </div>`;
    document.body.appendChild(el);
    const close = () => { el.remove(); document.removeEventListener('keydown', onKey, true); };
    const onKey = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); close(); } };
    document.addEventListener('keydown', onKey, true);
    el.addEventListener('mousedown', (ev) => { if (ev.target === el) close(); });
    el.querySelector('[data-testid="fm-shortcuts-close"]').onclick = close;
    el.querySelector('[data-testid="fm-shortcuts-close"]').focus();
}

// ── Video Player ─────────────────────────────────────────────────
const _videoPlayer = { open: false, files: [], index: 0, path: '' };

// The overlay is a fixed layer on <body> (`.app-window` has will-change:
// transform, so `position: fixed` inside a window would be window-relative).
// Closing the owning window therefore does NOT remove it — the hidden <video>
// would keep streaming and decoding in the background and slow down every film
// opened afterwards. Watch the window layer and tear the player down when its
// owning window disappears.
let _videoRootObserver = null;

function _watchVideoOwnerWindow(root) {
    if (_videoRootObserver) { _videoRootObserver.disconnect(); _videoRootObserver = null; }
    if (!root) return;
    const layer = document.getElementById('modalOverlay') || document.body;
    _videoRootObserver = new MutationObserver(() => {
        if (!_videoPlayer.open || root.isConnected) return;
        _videoPlayer._needsRefresh = false;   // the window is gone — nothing to refresh
        fmCloseVideoPlayer();
    });
    _videoRootObserver.observe(layer, { childList: true });
}

export function fmOpenVideoPlayer(path) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (!root.closest('.app-window')) return;

    // Collect video files from current directory
    const _videoExts = ['.mp4','.mkv','.mov','.avi','.webm','.flv','.m4v','.wmv','.ts','.mts'];
    const vids = [];
    root.querySelectorAll('.fm-file-item, .fm-list-row').forEach(el => {
        const p = el.getAttribute('data-path') || '';
        const n = el.getAttribute('data-name') || '';
        const dAttr = el.getAttribute('data-isdir') ?? el.getAttribute('data-isDir');
        if ((dAttr === 'true' || dAttr === true) || !p) return;
        const ex = '.' + p.split('/').filter(Boolean).pop().toLowerCase().split('.').pop();
        if (_videoExts.includes(ex)) vids.push({ name: n, path: p });
    });
    if (!vids.length) { addNotification('No video files in this directory', 'info'); return; }

    const idx = vids.findIndex(v => v.path === path);
    _videoPlayer.open = true;
    _videoPlayer.files = vids;
    _videoPlayer.index = idx >= 0 ? idx : 0;
    _videoPlayer.path = state.path;
    _videoPlayer.root = root;   // refresh this window on close, not the focused one

    // Only one player may exist. Tear down any stray overlay left behind by a
    // closed window: an orphan keeps downloading/decoding its film invisibly.
    document.querySelectorAll('.fm-video-overlay').forEach(el => {
        const v = el.querySelector('video');
        if (v) {
            try { v.pause(); } catch { /* already stopped */ }
            v.removeAttribute('src');
            try { v.load(); } catch { /* detached element */ }
        }
        el.remove();
    });
    _watchVideoOwnerWindow(root);

    const overlay = document.createElement('div');
    overlay.className = 'fm-video-overlay';
    overlay.innerHTML = `
        <div class="fm-video-backdrop" style="position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9000;"></div>
        <div class="fm-video-container" style="position:fixed;inset:0;z-index:9001;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px 60px;">
            <div class="fm-video-header" style="display:flex;align-items:center;justify-content:space-between;width:100%;max-width:960px;margin-bottom:12px;">
                <span class="fm-video-filename" data-testid="video-player-name" style="color: var(--text-1);font-size:15px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></span>
                <div style="display:flex;gap:8px;align-items:center;">
                    <span data-testid="video-player-counter" style="color: var(--text-2);font-size:13px;"></span>
                    <label data-testid="video-player-left-half" style="display:flex;align-items:center;gap:4px;font-size:12px;color: var(--text-2);cursor:pointer;padding:4px 6px;border-radius:6px;user-select:none;" title="For side-by-side (VR goggles) videos: use only the left half of the frame as the cover, so it doesn't look doubled">
                        <input type="checkbox" id="fm-video-left-half" checked style="accent-color: var(--accent, #3b82f6);"> VR&nbsp;left
                    </label>
                    <button class="btn-ghost" data-testid="video-player-capture" onclick="fmVideoCapture()" style="font-size:12px;padding:4px 12px;" title="Set the current frame as this video's cover/thumbnail (T)">📸 Set Cover</button>
                    <button class="btn-ghost" data-testid="video-player-delete" onclick="fmVideoDelete()" style="font-size:12px;padding:4px 12px;color:#ef4444;" title="Permanently delete file">🗑 Delete</button>
                    <button class="btn-ghost" data-testid="video-player-close" onclick="fmCloseVideoPlayer()" style="font-size:18px;padding:2px 10px;color: var(--text-2);">✕</button>
                </div>
            </div>
            <div style="display:flex;align-items:center;gap:12px;width:100%;max-width:960px;">
                <button class="btn-ghost" data-testid="video-player-prev" onclick="fmVideoPrev()" style="font-size:24px;padding:8px 12px;color: var(--text-2);" title="Previous (Left arrow)">◀</button>
                <div style="flex:1;aspect-ratio:16/9;background:#000;border-radius:8px;overflow:hidden;">
                    <video id="fm-video-element" controls autoplay playsinline preload="metadata" style="width:100%;height:100%;"></video>
                </div>
                <button class="btn-ghost" data-testid="video-player-next" onclick="fmVideoNext()" style="font-size:24px;padding:8px 12px;color: var(--text-2);" title="Next (Right arrow)">▶</button>
            </div>
        </div>`;

    document.body.appendChild(overlay);
    _videoLoadCurrent();

    // Keyboard handlers
    document.addEventListener('keydown', _videoKeyHandler);
}

function _videoLoadCurrent() {
    if (!_videoPlayer.open || !_videoPlayer.files.length) return;
    const v = _videoPlayer.files[_videoPlayer.index];
    if (!v || !v.path) return;

    const vidEl = document.getElementById('fm-video-element');
    const nameEl = document.querySelector('[data-testid="video-player-name"]');
    const counterEl = document.querySelector('[data-testid="video-player-counter"]');
    const prevBtn = document.querySelector('[data-testid="video-player-prev"]');
    const nextBtn = document.querySelector('[data-testid="video-player-next"]');

    if (!vidEl) return;

    // Validate path: ensure it doesn't contain the filename twice (directory + file name confusion)
    let cleanPath = v.path;
    const parts = cleanPath.split('/').filter(Boolean);
    const lastName = parts.pop() || '';
    if (parts.length > 0 && parts[parts.length - 1] === lastName) {
        // Path ends with duplicated filename — use state.path + filename instead
        cleanPath = (_videoPlayer.path + '/' + lastName).replace(/\/+/g, '/');
    }
    parts.push(lastName); // put back for name extraction below

    // Release the previous stream *before* opening the next one: a lingering
    // 8K range request keeps the NAS disk busy and makes the next film start
    // slowly (the classic "after three films the next one is slow" symptom).
    _videoPlayer._closing = true;
    try { vidEl.pause(); } catch { /* already stopped */ }
    vidEl.removeAttribute('src');
    vidEl.load();
    _videoPlayer._closing = false;

    const token = encodeURIComponent(localStorage.getItem('token') || '');
    vidEl.src = `/api/files/raw?path=${encodeURIComponent(cleanPath)}&token=${token}`;
    vidEl.load();
    vidEl.play().catch(() => {});

    // Handle video load errors gracefully — advance to next or show message, max 3 retries
    _videoPlayer._errorCount = _videoPlayer._errorCount || 0;
    vidEl.onerror = function() {
        // Clearing `src` on close/delete fires `error` too — that is a deliberate
        // teardown, not a broken file, so never report it.
        if (!_videoPlayer.open || _videoPlayer._closing || !vidEl.getAttribute('src')) return;
        _videoPlayer._errorCount++;
        if (_videoPlayer._errorCount > 3) {
            addNotification('Too many video errors, closing player', 'error');
            fmCloseVideoPlayer();
            return;
        }
        addNotification(`Video error — skipping to next`, 'error');
        if (_videoPlayer.index < _videoPlayer.files.length - 1) {
            setTimeout(() => fmVideoNext(), 1000);
        } else {
            fmCloseVideoPlayer();
        }
    };
    // Clear error count on successful load
    vidEl.oncanplay = function() { _videoPlayer._errorCount = 0; };

    if (nameEl) nameEl.textContent = v.name;
    if (counterEl) counterEl.textContent = `${_videoPlayer.index + 1} / ${_videoPlayer.files.length}`;
    if (prevBtn) prevBtn.style.visibility = _videoPlayer.index > 0 ? 'visible' : 'hidden';
    if (nextBtn) nextBtn.style.visibility = _videoPlayer.index < _videoPlayer.files.length - 1 ? 'visible' : 'hidden';
}

function _videoKeyHandler(e) {
    if (!_videoPlayer.open) { document.removeEventListener('keydown', _videoKeyHandler); return; }
    // Don't intercept when typing in input
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    switch (e.key) {
        case 'ArrowLeft': e.preventDefault(); fmVideoPrev(); break;
        case 'ArrowRight': e.preventDefault(); fmVideoNext(); break;
        case 'Escape': e.preventDefault(); fmCloseVideoPlayer(); break;
        case 'Delete': e.preventDefault(); fmVideoDelete(); break;
        case 't': case 'T': e.preventDefault(); fmVideoCapture(); break;
    }
}

export function fmVideoPrev() {
    if (!_videoPlayer.open || _videoPlayer.index <= 0) return;
    _videoPlayer.index--;
    _videoLoadCurrent();
}

export function fmVideoNext() {
    if (!_videoPlayer.open || _videoPlayer.index >= _videoPlayer.files.length - 1) return;
    _videoPlayer.index++;
    _videoLoadCurrent();
}

export async function fmVideoDelete() {
    if (!_videoPlayer.open || !_videoPlayer.files.length) return;
    const v = _videoPlayer.files[_videoPlayer.index];
    if (!v || !await showModalConfirm('Permanently Delete', `Permanently delete "${v.name}"? This cannot be undone.`)) return;

    // Release the video file handle FIRST so server can delete the file cleanly
    const vidEl = document.getElementById('fm-video-element');
    _videoPlayer._closing = true;
    if (vidEl) {
        try { vidEl.pause(); } catch { /* already stopped */ }
        vidEl.removeAttribute('src');
        vidEl.load();
    }
    _videoPlayer._closing = false;

    (async () => {
        try {
            const resp = await apiFetch('/api/files/delete', {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paths: [v.path] })
            });
            if (!resp.ok) { const d = await resp.json().catch(() => ({})); addNotification(d.detail || 'Delete failed', 'error'); return; }
            const { op_id } = await resp.json();
            const result = await _trackFileOp(op_id, 1, 'hard_delete');
            if (!result) return;
            if (result.failed > 0) { addNotification(`Failed to delete "${v.name}"`, 'error'); return; }
            addNotification(`"${v.name}" permanently deleted`, 'success');
        } catch(e) { addNotification(e.message, 'error'); return; }
        // Remove from list, advance to next/prev, DON'T reload grid (spinner would appear over player)
        _videoPlayer.files.splice(_videoPlayer.index, 1);
        if (_videoPlayer.files.length === 0) { fmCloseVideoPlayer(); return; }
        if (_videoPlayer.index >= _videoPlayer.files.length) _videoPlayer.index = _videoPlayer.files.length - 1;
        _videoLoadCurrent();
        _videoPlayer._needsRefresh = true; // mark for refresh on close
    })();
}

let _videoCapturing = false;

export async function fmVideoCapture() {
    if (!_videoPlayer.open || !_videoPlayer.files.length || _videoCapturing) return;
    const v = _videoPlayer.files[_videoPlayer.index];
    if (!v || !v.path) return;
    let t = 0;
    try { const vidEl = document.getElementById('fm-video-element'); if (vidEl && isFinite(vidEl.currentTime)) t = vidEl.currentTime; } catch {}
    let leftHalf = false;
    try { const cb = document.getElementById('fm-video-left-half'); if (cb) leftHalf = !!cb.checked; } catch {}

    _videoCapturing = true;
    addNotification(`Setting cover from frame at ${t.toFixed(1)}s…${leftHalf ? ' (VR left half)' : ''}`, 'info');
    try {
        const resp = await apiFetch('/api/files/video-cover', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: v.path, t, left_half: leftHalf })
        });
        if (!resp.ok) {
            let detail = `HTTP ${resp.status}`;
            try { const d = await resp.json(); if (d.detail) detail = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail); } catch {}
            addNotification(`Cover update failed: ${detail}`, 'error');
            return;
        }
        addNotification('Cover updated', 'success');
        _videoPlayer._needsRefresh = true;
    } catch (e) {
        addNotification(e.message, 'error');
    } finally {
        _videoCapturing = false;
    }
}

export function fmCloseVideoPlayer() {
    if (_videoRootObserver) { _videoRootObserver.disconnect(); _videoRootObserver = null; }
    // IMPORTANT: Pause and clear video FIRST before removing from DOM
    const vidEl = document.getElementById('fm-video-element');
    _videoPlayer._closing = true;  // silence the teardown `error` event
    if (vidEl) {
        try { vidEl.pause(); } catch { /* already stopped */ }
        vidEl.removeAttribute('src');
        vidEl.load();
    }
    document.querySelectorAll('.fm-video-overlay').forEach(el => el.remove());
    const needsRefresh = _videoPlayer._needsRefresh;
    const ownerRoot = _videoPlayer.root;
    _videoPlayer.open = false;
    _videoPlayer.files = [];
    _videoPlayer.index = 0;
    _videoPlayer.root = null;
    _videoPlayer._needsRefresh = false;
    _videoPlayer._errorCount = 0;
    _videoPlayer._closing = false;
    document.removeEventListener('keydown', _videoKeyHandler);
    // Resume thumbnails paused for playback (and pick up tiles rendered while
    // the player was open).
    document.querySelectorAll('.fm-container').forEach(c => _fmObserveThumbs(c));
    _fmPumpThumbs();
    // Refresh the file listing only after fully closing (no spinner interference).
    // Use the window the player was opened from — with two File Managers open the
    // "focused" window may be a different one.
    if (needsRefresh && ownerRoot && ownerRoot.isConnected) fmLoad(undefined, ownerRoot);
    else if (needsRefresh) fmLoad();
}

window.fmOpenVideoPlayer = fmOpenVideoPlayer;
window.fmCloseVideoPlayer = fmCloseVideoPlayer;
window.fmVideoPrev = fmVideoPrev;
window.fmVideoNext = fmVideoNext;
window.fmVideoDelete = fmVideoDelete;
window.fmVideoCapture = fmVideoCapture;

// Last line of defence: closing the Files app must never leave a hidden player
// streaming. (`_notifyClosed` fires only for the last window of the app; the
// root observer covers closing the owner while another Files window stays open.)
onAppClose('files', () => {
    if (!_videoPlayer.open) return;
    if (_videoPlayer.root && _videoPlayer.root.isConnected) return;
    _videoPlayer._needsRefresh = false;
    fmCloseVideoPlayer();
});

// ── Navigation helpers ──
export function fmNavigateTo(path) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    // From trash/search views, re-navigating to the current path must reload the directory (not no-op)
    if (path === state.path && !state._inTrash && !state._searchTerm) return;
    // Push onto per-window history (truncate forward branch)
    state.history = state.history.slice(0, state.historyIndex + 1);
    state.history.push(path);
    state.historyIndex = state.history.length - 1;
    (async () => { try { await fmLoad(path, root); } catch(e) { addNotification(`Cannot open ${path}: ${e.message}`, 'error'); } })();
}
function fmNavigateBack() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (state.historyIndex > 0) {
        state.historyIndex--;
        fmLoad(state.history[state.historyIndex], root);
    }
}
function fmNavigateForward() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (state.historyIndex < state.history.length - 1) {
        state.historyIndex++;
        fmLoad(state.history[state.historyIndex], root);
    }
}
window.fmNavigateBack = fmNavigateBack;
window.fmNavigateForward = fmNavigateForward;
export function fmRefresh() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (state._inTrash) { fmOpenTrash(); return; }   // re-fetch trash listing
    if (state._searchTerm) { fmSearch(); return; }   // re-run the active search
    fmLoad(state.path, root);                        // reload current directory
}
window.fmRefresh = fmRefresh;
export function fmSearchInput(el) {
    clearTimeout(window.__fmSearchTimer);
    window.__fmSearchTimer = setTimeout(() => fmSearch(), 250);
}
window.fmSearchInput = fmSearchInput;

/**
 * Recursive search from the current directory (backend /api/files/search).
 * Renders a results view; an empty term restores the normal directory listing.
 */
export async function fmSearch() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    const searchInputId = `fm-search-input-${root.dataset.fmWin}`;
    const inputEl = document.getElementById(searchInputId);
    const searchTerm = (inputEl?.value || '').trim();
    const clearBtn = root.querySelector('.fm-search-clear');
    if (clearBtn) clearBtn.style.display = searchTerm ? '' : 'none';

    if (!searchTerm) {
        state._searchTerm = null;
        fmLoad(state.path, root);  // restore directory view
        return;
    }

    const contentEl = root.querySelector('.fm-content');
    const breadcrumbEl = root.querySelector('.fm-breadcrumb');
    if (!contentEl) return;
    state._searchTerm = searchTerm;
    state.selected.clear();
    state._anchorName = null;
    contentEl.classList.remove('fm-grid');
    if (breadcrumbEl) breadcrumbEl.innerHTML = `<span class="fm-crumb current" style="color:var(--fm-accent);">🔍 "${escapeHtml(searchTerm)}" in ${escapeHtml(state.path)}</span>`;
    contentEl.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;"><div class="spinner"></div></div>';

    try {
        const resp = await apiFetch(`/api/files/search?q=${encodeURIComponent(searchTerm)}&path=${encodeURIComponent(state.path)}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        // Stale-response guard: user may have cleared or navigated while waiting
        if (state._searchTerm !== searchTerm) return;
        const results = data.results || [];

        const itemsEl = root.querySelector('[data-testid="fm-statusbar-items"]');
        if (itemsEl) itemsEl.textContent = `${results.length} match${results.length === 1 ? '' : 'es'}${data.truncated ? ' (showing first 500)' : ''}`;

        if (results.length === 0) {
            contentEl.innerHTML = `<div class="fm-empty-state"><div class="fm-empty-ic">🔍</div><div class="fm-empty-title">No matches for "${escapeHtml(searchTerm)}"</div></div>`;
            return;
        }

        let html = '<table class="fm-list-table"><thead><tr>' +
            `<th style="cursor:default;">Name</th><th style="cursor:default;width:auto;">Location</th><th style="cursor:default;text-align:right !important;">Size</th>` +
            '</tr></thead><tbody>';
        for (const r of results) {
            const parent = r.path.substring(0, Math.max(r.path.lastIndexOf('/'), 1)) || '/';
            html += `<tr class="fm-search-result" data-name="${escapeAttr(r.name)}" data-isdir="${r.is_dir}" data-path="${escapeAttr(r.path)}" onclick="event.stopPropagation();fmSearchOpenResult('${safeJsStr(parent)}')" ondblclick="fmSearchOpenFile('${safeJsStr(r.path)}',${r.is_dir})" oncontextmenu="event.preventDefault();event.stopPropagation();fmShowItemContextMenu(event,'${jsAttr(r.path)}','${safeJsStr(r.name)}',${r.is_dir})" title="Open containing folder">` +
                `<td><span>${r.is_dir ? getFileIcon(r.name, 'dir') : getFileIcon(r.name, 'file')}<span class="fm-file-name-inline">${escapeHtml(r.name)}</span></span></td>` +
                `<td style="color:var(--fm-secondary);font-size:12px;">${escapeHtml(parent)}</td>` +
                `<td style="text-align:right;font-family:monospace;color: var(--text-2);">${r.is_dir ? '—' : formatBytes(r.size || 0)}</td></tr>`;
        }
        html += '</tbody></table>';
        contentEl.innerHTML = html;
    } catch (e) {
        if (state._searchTerm !== searchTerm) return;
        contentEl.innerHTML = `<div class="fm-empty-state"><div class="fm-empty-ic">❌</div><p>${escapeHtml(e.message)}</p></div>`;
    }
}

export function fmSearchOpenResult(parentPath) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    const inputEl = document.getElementById(`fm-search-input-${root.dataset.fmWin}`);
    if (inputEl) inputEl.value = '';
    state._searchTerm = null;
    fmNavigateTo(parentPath);  // fmLoad exits search view and loads the folder
}
window.fmSearchOpenResult = fmSearchOpenResult;

// Double-click on a search result: open the item itself (dir → enter, image → gallery,
// text/code → preview panel). Other types go to the containing folder where full actions exist.
export function fmSearchOpenFile(path, isDir) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    const inputEl = document.getElementById(`fm-search-input-${root.dataset.fmWin}`);
    if (inputEl) inputEl.value = '';
    state._searchTerm = null;

    if (isDir) { fmNavigateTo(path); return; }
    const name = path.split('/').filter(Boolean).pop() || '';
    const parent = path.substring(0, Math.max(path.lastIndexOf('/'), 1)) || '/';
    const ext = '.' + (name.toLowerCase().split('.').pop() || '');
    if (_IMAGE_EXTS.has(ext)) {
        window.__fmGalleryCtx = { dir: parent, files: [{ name, path, is_dir: false }], index: 0 };
        openApp('gallery');
    } else if (_AUDIO_EXTS.has(ext) || ext === '.pdf') {
        fmShowPreview(path, false);
    } else if (_fmIsTextFile(name)) {
        fmShowPreview(path, false);
    } else {
        fmNavigateTo(parent);
    }
}
window.fmSearchOpenFile = fmSearchOpenFile;

// ── Trash View (per-user recycle bin; admins may view every account) ──────────
export async function fmOpenTrash(scope) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    const contentEl = root.querySelector('.fm-content');
    const breadcrumbEl = root.querySelector('.fm-breadcrumb');
    if (!contentEl) return;
    // Scope is sticky per window so restore/delete refreshes keep the view the
    // admin selected; non-admins are always pinned to their own trash.
    const isAdmin = _fmIsAdmin();
    const wanted = (scope === 'all' || scope === 'mine') ? scope : (state._trashScope || 'mine');
    state._trashScope = (isAdmin && wanted === 'all') ? 'all' : 'mine';
    const showAll = state._trashScope === 'all';
    contentEl.classList.remove('fm-grid');  // trash renders a table / centered states, not grid items
    state._inTrash = true;
    // The trash is a different namespace. Keeping the folder selection meant
    // Delete here resolved an old name against the old folder and trashed a
    // file the user never selected.
    state.selected.clear();
    state._anchorName = null;
    state._searchTerm = '';
    state._trashSelected = new Set();
    _fmSyncActionButtons(root, state);
    const seq = (state._loadSeq = (state._loadSeq || 0) + 1);
    if (breadcrumbEl) breadcrumbEl.innerHTML = showAll
        ? '<span class="fm-crumb" style="color:var(--accent);">🗑 Trash — all users</span>'
        : '<span class="fm-crumb" style="color:var(--accent);">🗑 Trash</span>';
    contentEl.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%;"><div class="spinner"></div></div>';
    try {
        const resp = await apiFetch(showAll ? '/api/files/trash?all=1' : '/api/files/trash');
        if (!resp.ok) throw new Error(await readErrorDetail(resp, 'Could not load Trash'));
        const data = await resp.json();
        if (state._loadSeq !== seq) return;   // user navigated away meanwhile
        const items = data.items || [];
        const itemsEl = root.querySelector('[data-testid="fm-statusbar-items"]');
        if (itemsEl) itemsEl.textContent = `${items.length} item${items.length === 1 ? '' : 's'} in trash`;
        const scopeToggle = isAdmin
            ? `<button class="fm-act-btn" onclick="fmTrashToggleScope()" data-testid="fm-trash-scope-toggle">${showAll ? '👤 Show only mine' : '👥 Show all users'}</button>`
            : '';
        if (items.length === 0) {
            // Keep the admin scope toggle reachable even when this view is empty
            // (otherwise an empty personal bin could hide other users' items).
            contentEl.innerHTML = `<div class="fm-trash-toolbar">${scopeToggle}<span class="fm-trash-info">Trash is empty</span></div><div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color: var(--text-3);font-size:14px;min-height:160px;"><div style="font-size:40px;margin-bottom:12px;">🗑</div>Trash is empty</div>`;
            return;
        }
        let html = `<div class="fm-trash-toolbar">
            <button class="fm-act-btn fm-act-danger" onclick="fmEmptyTrash(${showAll})" data-testid="fm-trash-empty">${showAll ? '🗑 Empty All Trash' : '🗑 Empty Trash'}</button>
            <button class="fm-act-btn" data-testid="fm-trash-delete-selected" disabled>🗑 Delete selected</button>
            ${scopeToggle}
            <span class="fm-trash-info">${items.length} item${items.length === 1 ? '' : 's'} ${showAll ? 'across all users' : 'in your trash'}</span>
        </div>
        <table class="fm-list-table"><thead><tr>
            <th style="width:34px;"><input type="checkbox" data-testid="fm-trash-select-all" aria-label="Select all trash items"></th>
            <th style="cursor:default;">Name</th>
            ${showAll ? '<th style="cursor:default;width:140px;">Owner</th>' : ''}
            <th style="cursor:default;width:auto;">Original Path</th>
            <th style="text-align:right !important;cursor:default;width:210px;">Actions</th>
        </tr></thead><tbody>`;
        for (const item of items) {
            const safeName = escapeAttr(item.name);
            const owner = String(item.owner || '');
            const ownerArg = showAll ? `, '${jsAttr(owner)}'` : '';
            const ownerCell = showAll
                ? `<td style="color:var(--fm-secondary);font-size:12px;">${escapeHtml(owner || '—')}</td>`
                : '';
            html += `<tr class="fm-trash-row" data-name="${safeName}"${showAll ? ` data-owner="${escapeAttr(owner)}"` : ''}>
                <td style="width:34px;"><input type="checkbox" data-trash-select="${safeName}" data-owner="${escapeAttr(owner)}" aria-label="Select ${safeName}"></td>
                <td><span>${item.is_dir ? svgIcon('<path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/>', 16) : svgIcon('<path d="M13 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V9z"/><polyline points="13,2 13,9 20,9"/>', 16)}<span class="fm-file-name-inline">${escapeHtml(item.name)}</span></span></td>
                ${ownerCell}
                <td style="color:var(--fm-secondary);font-size:12px;">${escapeHtml(item.original_path || '—')}</td>
                <td style="text-align:right;white-space:nowrap;">
                    <button class="btn-ghost" data-testid="fm-trash-restore-${escapeAttr(item.name)}" onclick="fmTrashRestore('${jsAttr(item.name)}'${ownerArg})" style="font-size:11px;padding:3px 8px;color:#22c55e;">↩ Restore</button>
                    <button class="btn-ghost" data-testid="fm-trash-delete-${escapeAttr(item.name)}" onclick="fmTrashPermanentDelete('${jsAttr(item.name)}'${ownerArg})" style="font-size:11px;padding:3px 8px;color:#ef4444;">🗑 Delete</button>
                </td>
            </tr>`;
        }
        html += '</tbody></table>';
        contentEl.innerHTML = html;
        _fmWireTrashSelection(contentEl);
        root.querySelectorAll('.fm-sidebar-item').forEach(el => el.classList.remove('active'));
    } catch (e) {
        contentEl.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:#ef4444;min-height:150px;"><div style="font-size:32px;margin-bottom:8px;">❌</div><p>${escapeHtml(e.message)}</p></div>`;
    }
}

/** Admin-only: switch the Trash view between "my items" and "all users". */
export function fmTrashToggleScope() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    fmOpenTrash(state._trashScope === 'all' ? 'mine' : 'all');
}

/** Wire the Trash table checkboxes (no inline handlers → no new globals). */
function _fmWireTrashSelection(contentEl) {
    const all = contentEl.querySelector('[data-testid="fm-trash-select-all"]');
    const boxes = [...contentEl.querySelectorAll('[data-trash-select]')];
    const button = contentEl.querySelector('[data-testid="fm-trash-delete-selected"]');
    const refresh = () => {
        const checked = boxes.filter(cb => cb.checked).length;
        if (button) {
            button.disabled = checked === 0;
            button.textContent = checked ? `🗑 Delete selected (${checked})` : '🗑 Delete selected';
        }
        if (all) {
            all.checked = checked > 0 && checked === boxes.length;
            all.indeterminate = checked > 0 && checked < boxes.length;
        }
    };
    boxes.forEach(cb => cb.addEventListener('change', refresh));
    if (all) all.addEventListener('change', () => {
        boxes.forEach(cb => { cb.checked = all.checked; });
        refresh();
    });
    if (button) button.addEventListener('click', () => { fmTrashDeleteSelected(); });
    refresh();
}

/** Permanently delete checked Trash rows (one op, one progress card). */
async function fmTrashDeleteSelected() {
    const root = _getActiveFmRoot(); if (!root) return;
    const boxes = [...root.querySelectorAll('[data-trash-select]:checked')];
    if (!boxes.length) return addNotification('No trash items selected', 'info');
    const items = boxes.map(cb => ({
        name: cb.getAttribute('data-trash-select'),
        owner: cb.getAttribute('data-owner') || null,
    }));
    const label = items.length === 1 ? `"${items[0].name}"` : `${items.length} items`;
    if (!await showModalConfirm('Permanently Delete',
        `Permanently delete ${label} from Trash? This cannot be undone.`)) return;
    await _fmPurgeTrashItems(items, 'Delete from Trash');
}

/** Shared op-tracked permanent delete for Trash entries. */
async function _fmPurgeTrashItems(items, title) {
    const root = _getActiveFmRoot();
    const state = root ? _getFmStateForRoot(root) : null;
    if (state && state._deleteBusy) {
        addNotification('A delete operation is already running', 'warning');
        return null;
    }
    if (state) state._deleteBusy = true;
    try {
        const resp = await apiFetch('/api/files/trash/items', {
            method: 'DELETE', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ items }),
        });
        if (!resp.ok) {
            addNotification(await readErrorDetail(resp, `HTTP ${resp.status}`), 'error');
            return null;
        }
        const { op_id: opId } = await resp.json();
        const result = await _trackFileOp(opId, items.length, 'purge', { deferMs: 400 });
        if (!result) return null;
        _fmReportOpResult(result, title, 'deleted from Trash');
        document.dispatchEvent(new CustomEvent('ethos:trash-changed'));
        fmOpenTrash();
        return result;
    } catch (e) {
        addNotification(e.message, 'error');
        return null;
    } finally {
        if (state) state._deleteBusy = false;
    }
}

async function fmLoadDir() {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    state._inTrash = false;
    if (state.path) fmLoad(state.path, root);
}

export async function fmTrashRestore(name, owner) {
    const where = owner ? ` from ${owner}'s trash` : '';
    if (!await showModalConfirm('Restore', `Restore "${name}"${where} to its original location?`)) return;
    const url = owner
        ? `/api/files/trash/restore?owner=${encodeURIComponent(owner)}`
        : '/api/files/trash/restore';
    try {
        const resp = await apiFetch(url, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ name })
        });
        if (!resp.ok) { addNotification(await readErrorDetail(resp, 'Restore failed'), 'error'); return; }
        const d = await resp.json().catch(() => ({}));
        addNotification(d.fallback_home ? `"${name}" restored to ${d.restored_to} (original location missing)` : `"${name}" restored`, 'success');
        document.dispatchEvent(new CustomEvent('ethos:trash-changed'));
        fmOpenTrash();
    } catch(e) { addNotification(e.message, 'error'); }
}

async function fmTrashPermanentDelete(name, owner) {
    if (!await showModalConfirm('Permanently Delete', `Permanently delete "${name}"? This cannot be undone.`)) return;
    await _fmPurgeTrashItems([{ name, owner: owner || null }], 'Permanently Delete');
}

export async function fmEmptyTrash(allUsers = false) {
    const title = allUsers ? 'Empty All Trash' : 'Empty Trash';
    const msg = allUsers
        ? "Permanently delete ALL items in every user's trash? This cannot be undone."
        : 'Permanently delete ALL items in your trash? This cannot be undone.';
    if (!await showModalConfirm(title, msg)) return;
    const root = _getActiveFmRoot();
    const state = root ? _getFmStateForRoot(root) : null;
    if (state && state._deleteBusy) return addNotification('A delete operation is already running', 'warning');
    if (state) state._deleteBusy = true;
    try {
        const resp = await apiFetch(allUsers ? '/api/files/trash/all?all=1' : '/api/files/trash/all', { method: 'DELETE' });
        if (!resp.ok) {
            addNotification(await readErrorDetail(resp, 'Empty trash failed'), 'error');
            return;
        }
        const { op_id: opId, total } = await resp.json();
        if (!opId) {
            addNotification('Trash is already empty', 'info');
            document.dispatchEvent(new CustomEvent('ethos:trash-changed'));
            fmOpenTrash();
            return;
        }
        const result = await _trackFileOp(opId, total || 1, 'purge', { deferMs: 400 });
        if (!result) return;
        _fmReportOpResult(result, title, 'deleted from Trash');
        // Permanent deletion does not broadcast a directory mutation — refresh
        // the desktop Trash badge locally.
        document.dispatchEvent(new CustomEvent('ethos:trash-changed'));
        fmOpenTrash();
    } catch(e) { addNotification(e.message, 'error'); }
    finally { if (state) state._deleteBusy = false; }
}

window.fmOpenTrash = fmOpenTrash;
window.fmTrashToggleScope = fmTrashToggleScope;
window.fmTrashRestore = fmTrashRestore;
window.fmTrashPermanentDelete = fmTrashPermanentDelete;
window.fmEmptyTrash = fmEmptyTrash;
// Desktop Trash drop delegates here (same confirmation + op tracking as the app).
window.fmTrashPaths = fmTrashPaths;
window.fmToggleFavorite = fmToggleFavorite;
window.fmToggleCurrentFolderFavorite = fmToggleCurrentFolderFavorite;
window.fmLoadFavorites = fmLoadFavorites;
window.fmSetDensity = fmSetDensity;
window.fmToggleDensity = fmToggleDensity;
window.fmSetVideoXL = fmSetVideoXL;
window.fmToggleVideoXL = fmToggleVideoXL;
window.fmOpenInNewWindow = fmOpenInNewWindow;
// Path bar (inline onclick handlers) + keyboard extras
window.fmStartPathEdit = fmStartPathEdit;
window.fmPathbarClick = fmPathbarClick;
window.fmPathInputKey = fmPathInputKey;
window.fmPathInputBlur = fmPathInputBlur;
window.fmShowCrumbMenu = fmShowCrumbMenu;
window.fmShowShortcuts = fmShowShortcuts;
window.fmToggleQuickLook = fmToggleQuickLook;

// ── File Upload ─────────────────────────────────────────────────────────────
export async function fmUploadFiles(fileList) {
    const root = _getActiveFmRoot(); if (!root) return;
    const state = _getFmStateForRoot(root); if (!state) return;
    if (!fileList || fileList.length === 0) return;
    try {
        // Chunked upload engine: parallel transfers + progress panel. The directory
        // refreshes via the WS file-events "upload" broadcast when each file completes.
        await startUpload(Array.from(fileList), state.path);
    } catch(e) { addNotification(`Upload failed: ${e.message}`, 'error'); }
}

// ── Real-Time File Event Listener (Task 4 — Reactive Sync) ───────────────────

let _wsFileEvents = null;  // core/ws.js controller for /ws/files/events

/**
 * Connect to the file events WebSocket endpoint for real-time mutation notifications.
 * Auto-reconnects on disconnect after 5 seconds.
 */
function connectFileEventsWs() {
    if (_wsFileEvents && !_wsFileEvents.closed) return;

    // Not authenticated yet (login screen) — skip; the page reloads after a
    // successful login, which re-runs this module and connects with the JWT.
    if (!localStorage.getItem('token')) return;

    // core/ws.js builds the URL (token in the query string) and owns the
    // reconnect/backoff policy, so a dead NAS is not polled every 5 s (P3-08).
    _wsFileEvents = connectWs('/ws/files/events', handleFileEvent, {
        appId: 'files',
        name: 'file-events',
        onError: () => console.warn('[EthOS] File events WS error — reconnecting'),
    });
}

/**
 * Handle a received file mutation event.
 * 
 * Performs two actions:
 * 1. Silent Cache Invalidation: If the mutation's target matches the currently viewed directory,
 *    trigger a silent background refetch (no layout flicker).
 * 2. Optimistic UI Updates: For delete events, immediately remove items from DOM before backend completes.
 */
function handleFileEvent(ev) {
    // Desktop Trash badge: needs the signal even when no Files window is
    // open (this function used to return early before doing anything).
    if (ev && (ev.action === 'delete_soft' || ev.action === 'restore')) {
        document.dispatchEvent(new CustomEvent('ethos:trash-changed'));
    }

    const roots = document.querySelectorAll('.fm-container');
    if (!roots.length) return;

    const actionLabel = { copy: 'Copied', move: 'Moved', delete_soft: 'Deleted (to trash)', delete_hard: 'Permanently deleted', zip: 'Archived', extract: 'Extracted', upload: 'Uploaded', rename: 'Renamed', mkdir: 'Created folder', touch: 'Created file', restore: 'Restored' }[ev.action] || ev.action;
    const names = new Set((ev.items || []).map(i => i.name).filter(Boolean));
    const itemCount = ev.items?.length || 1;
    let notified = false;

    // Every open Files window is updated — previously only the focused
    // one was, so a second window kept showing a stale directory.
    roots.forEach(root => {
        const state = _getFmStateForRoot(root); if (!state) return;
        const currentPath = state.path || '/';
        if (!_isPathInDirectory(ev.path, currentPath) && !_isPathInDirectory(currentPath, ev.path)) return;

        // ── Silent cache invalidation (no spinner, no layout flicker) ────────
        _silentRefetch(root);
        // Actions that report their own summary (delete/restore/extract) must
        // not toast twice — the operation that started them already did.
        if (!notified && !['delete_soft', 'delete_hard', 'restore', 'extract'].includes(ev.action)) {
            // Single-item actions read better without a count ("Created folder in /x").
            const msg = ['mkdir', 'touch', 'rename'].includes(ev.action)
                ? `${actionLabel} in ${currentPath}`
                : `${actionLabel} ${itemCount} item${itemCount === 1 ? '' : 's'} in ${currentPath}`;
            addNotification(msg, 'success');
            notified = true;
        }

        // ── Optimistic UI updates ───────────────────────────────────────────
        // Deletions: drop the row immediately, before the refetch lands.
        const rows = root.querySelectorAll('.fm-file-item, .fm-list-row, .fm-search-result');
        let changed = false;
        if (['delete_soft', 'delete_hard'].includes(ev.action)) {
            rows.forEach(f => {
                if (names.has(f.dataset?.name || f.getAttribute('data-name'))) { f.remove(); changed = true; }
            });
        } else if (['copy', 'move'].includes(ev.action)) {
            // Items added by another client — refetch so they get full metadata.
            const present = new Set(Array.from(rows).map(f => f.dataset?.name || f.getAttribute('data-name')));
            if ((ev.items || []).some(i => i.status === 'ok' && i.name && !present.has(i.name))) _silentRefetch(root);
        }
        if (changed) names.forEach(n => state.selected.delete(n));
    });
}

/**
 * Silent background refetch of current directory contents.
 * No spinner shown, no layout flicker — just updates the grid data directly.
 */
async function _silentRefetch(root) {
    const state = _getFmStateForRoot(root); if (!state) return;
    if (state._searchTerm) return;  // don't clobber search results with a directory refetch
    if (state._inTrash) return;     // the trash view is not a directory listing
    const seq = state._loadSeq || 0;

    try {
        const resp = await apiFetch(`/api/files?path=${encodeURIComponent(state.path)}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        // A navigation/search started while this request was in flight — drop it.
        if ((state._loadSeq || 0) !== seq || state._inTrash || state._searchTerm) return;

        // Same visibility rules as fmLoad(): honour the hidden-files toggle so a
        // background refresh cannot reveal dotfiles the user has hidden.
        let files = data.files || [];
        if (!state._showHidden) files = files.filter(f => !f.name.startsWith('.'));
        state._canWrite = data.can_write !== false;

        // Update grid without showing loading state (no spinner animation)
        const contentEl = root.querySelector('.fm-content');
        if (contentEl) {
            if (state.viewMode === 'list') fmRenderListView(contentEl, files, state.path, state);
            else fmRenderGridView(contentEl, files, state.path, state);
            _fmApplyCanWrite(root, state);
            
            // Update status bar with new item count and selection state
            const itemsEl = root.querySelector('[data-testid="fm-statusbar-items"]');
            if (itemsEl) {
                const dirs = files.filter(f => f.is_dir).length;
                const fc = files.length - dirs;
                let txt = `${files.length} item${files.length === 1 ? '' : 's'}`;
                if (dirs > 0 && fc > 0) txt += ` (${dirs} folders, ${fc} files)`;
                else if (files.length > 0 && dirs === 0) txt += ' — files only';
                itemsEl.textContent = txt;
            }
            _fmUpdateSelectionStatus(root, state);
        }
    } catch (e) {
        // Silent failure — don't show error to user for background refetch (avoids noise)
        console.debug('[EthOS] Silent refetch failed:', e.message);
    }
}

/**
 * Check if a target path is within (or equal to) the given directory.
 * Used by handleFileEvent() to determine which directories are affected by mutations.
 */
function _isPathInDirectory(targetPath, dirPath) {
    const absTarget = targetPath.startsWith('/') ? targetPath : '/' + targetPath;
    const absDir = dirPath === '/' ? '/' : dirPath.endsWith('/') ? dirPath : dirPath + '/';
    return absTarget === dirPath || absTarget.startsWith(absDir);
}

// ── Initialize WebSocket connection on app load (auto-connect) ───────────────
connectFileEventsWs();  // Auto-connect when this module loads — every File Manager window gets real-time sync

// The upload engine emits this once the last queued transfer settles. Refresh
// every window here as well, so the listing stays correct even when the
// file-events WebSocket is down.
window.addEventListener('ethos:uploads-settled', () => {
    document.querySelectorAll('.fm-container').forEach(r => _silentRefetch(r));
});

// Live upload summary for the status bar (the engine throttles the event).
window.addEventListener('ethos:uploads-changed', (e) => {
    const detail = e.detail || getUploadStatus();
    document.querySelectorAll('.fm-container').forEach(r => _fmRenderUploadChip(r, detail));
});

document.addEventListener('keydown', fmHandleKeyboard);
