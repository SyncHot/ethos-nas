// EthOS NAS — Application Registry + Desktop Icons rendering (T-MW: right-click context menu)

import { getVisibleApps, addToDesktop, removeFromDesktop, resetToDefault, getPositions, setPosition, clearPositions } from './desktop-layout.js';
import { openWindows as _openWins, focusWindow as _focusWin, restoreWindowWin as _restoreWin, isMultiWindowApp } from './window-manager.js';
import { getAppIcon, APP_ICON_GRADIENTS, FALLBACK_GRADIENT } from './app-icons.js';
import { escapeAttr, escapeHtml } from './utils.js';
import { isAdmin } from './api.js';
import { openTrashFromDesktop, emptyTrashFromDesktop, refreshTrashBadge } from './desktop-trash.js';

const APP_REGISTRY = {
    overview:    { id: 'overview', title: 'System Overview', icon: '📊' },
    storage:     { id: 'storage', title: 'Storage', icon: '' },
    files:       { id: 'files', title: 'Files', icon: '📁' },
    trash:       { id: 'trash', title: 'Trash', icon: '🗑️' },
    terminal:    { id: 'terminal', title: 'Terminal', icon: '⌨️' },
    network:     { id: 'network', title: 'Network', icon: '🌐' },
    security:    { id: 'security', title: 'Firewall & Access Control', icon: '🛡️' },
    services:    { id: 'services', title: 'Services', icon: '⚙️' },
    settings:    { id: 'settings', title: 'Settings', icon: '🔧' },
    processes:   { id: 'processes', title: 'Process Manager', icon: '📋' },
    logs:        { id: 'logs', title: 'Log Viewer', icon: '📜' },
    packages:    { id: 'packages', title: 'Packages', icon: '📦' },
    smart:       { id: 'smart',     title: 'SMART Health', icon: '💿' },
    systemd:     { id: 'systemd',   title: 'System Services', icon: '⚙️' },
    cron:        { id: 'cron',      title: 'Cron Jobs', icon: '⏰' },
    search:      { id: 'search',   title: 'Universal Search', icon: '🔍' },
    'shared-links': { id: 'shared-links', title: 'Shared Links', icon: '🔗' },
};

export function getRegistry() {
    const out = {};
    for (const [id, app] of Object.entries(APP_REGISTRY)) {
        if (isAppVisible(id)) out[id] = app;
    }
    return out;
}

// UI-only visibility: these apps are useless to non-admins (every endpoint is
// admin-only). The backend still answers 403; this just avoids a dead window.
const APP_MIN_ROLE = { security: 'admin' };

export function isAppVisible(appId) {
    return APP_MIN_ROLE[appId] !== 'admin' || isAdmin();
}

function _buildIconHtml(app) {
    const ic = getAppIcon(app.id, 30);
    const bg = APP_ICON_GRADIENTS[app.id] || FALLBACK_GRADIENT;
    const inner = ic ? ic.svg : app.icon;
    // Trash shows a live item count; the badge is (re)filled after every render.
    const badge = app.id === 'trash'
        ? `<span class="desktop-trash-badge" id="desktopTrashBadge" data-testid="desktop-trash-badge" aria-label="Items in Trash" hidden></span>`
        : '';
    return `<div class="desktop-icon" data-testid="desktop-icon-${app.id}" draggable="true"
                    ondragstart="window._onIconDragStart(event, '${app.id}')"
                    onclick="openApp('${app.id}')" oncontextmenu="_showDesktopCtx(event, '${app.id}')">
        <div class="icon-img" style="background:${bg};color:#fff">${inner}${badge}</div>
        <span class="icon-label">${app.title}</span>
    </div>`;
}

// ── T-MW: Right-click context menu on desktop icons (event delegation) ─────

let _desktopCtxMenu = null;

export function _showDesktopCtx(e, appId) {
    if (!e || !(e instanceof Event)) return;
    e.preventDefault(); e.stopPropagation();
    if (_desktopCtxMenu) { _desktopCtxMenu.remove(); _desktopCtxMenu = null; }

    const cx = e.clientX ?? 50, cy = e.clientY ?? 50;
    const ctx = document.createElement('div');
    Object.assign(ctx.style, { position: 'fixed', left: cx + 'px', top: cy + 'px', zIndex: 9001, background: 'var(--surface-1)', border: '1px solid var(--border)', borderRadius: '10px', padding: '4px 0', minWidth: '180px', boxShadow: '0 16px 48px rgba(0,0,0,0.5)', fontFamily: "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif" });
    ctx.setAttribute('data-testid', 'icon-ctx-' + appId);

    const existingCount = _countWindowsByApp(appId);

    const isVisible = getVisibleApps().includes(appId);

    // Trash is a shortcut into Files, not a window app of its own, so it
    // gets its own menu instead of the generic Open / New Window entries.
    const items = appId === 'trash'
        ? [
            { label: '🗑 Open Trash', dataTestId: 'icon-ctx-trash-open', action() { openTrashFromDesktop(); _closeDesktopCtx(); } },
            { label: '🧺 Empty Trash', dataTestId: 'icon-ctx-trash-empty', action() { emptyTrashFromDesktop(); _closeDesktopCtx(); } },
            { label: '🔄 Refresh count', dataTestId: 'icon-ctx-trash-refresh', action() { refreshTrashBadge(); _closeDesktopCtx(); } },
        ]
        : [
            { label: existingCount > 0 ? '🖥️ Open / Focus' : '▶ Open', dataTestId: 'icon-ctx-' + appId + '-open', action() { if (existingCount > 0) { _focusExistingWindow(appId); } else { openApp(appId); } _closeDesktopCtx(); } },
        ];

    // Only multi-window apps (Files) offer "New Window" — single-instance
    // apps focus their existing window instead.
    if (isMultiWindowApp(appId)) {
        items.push({ label: '📑 New Window', dataTestId: 'icon-ctx-' + appId + '-new-window', action() { openApp(appId); _closeDesktopCtx(); } });
    }

    // Hide / Show on Desktop toggle
    if (isVisible) {
        items.push({ label: '🖥️ Remove from Desktop', dataTestId: 'icon-ctx-' + appId + '-hide', action() { window._removeIcon(appId); _closeDesktopCtx(); } });
    } else {
        // App is unpinned — pin it to the desktop
        items.push({ label: '📌 Pin to Desktop', dataTestId: 'icon-ctx-' + appId + '-show', action() {
            addToDesktop(appId);
            refreshDesktopIcons(); _closeDesktopCtx();
        }});
    }

    // Reset desktop (unpin everything)
    items.push({ label: '↩ Reset Desktop (unpin all)', dataTestId: 'icon-ctx-' + appId + '-reset', action() { resetToDefault(); refreshDesktopIcons(); _closeDesktopCtx(); } });

    items.forEach(it => {
        const el = document.createElement('div');
        Object.assign(el.style, { padding: '8px 16px', fontSize: '13px', color: 'var(--text-1)', cursor: 'pointer', transition: 'background 0.1s' });
        el.textContent = it.label;
        if (it.dataTestId) el.setAttribute('data-testid', it.dataTestId);
        el.onmouseenter = () => { el.style.background = 'rgba(91,154,255,0.15)'; };
        el.onmouseleave = () => { el.style.background = 'transparent'; };
        el.onclick = (ev2) => { ev2.stopPropagation(); it.action(); };
        ctx.appendChild(el);
    });

    document.body.appendChild(ctx);
    _desktopCtxMenu = ctx;

    const closeCtx = (ev3) => { if (_desktopCtxMenu && !_desktopCtxMenu.contains(ev3.target)) { _closeDesktopCtx(); } };
    setTimeout(() => { document.addEventListener('mousedown', closeCtx, true); document.addEventListener('contextmenu', () => { _closeDesktopCtx(); }); }, 10);
}

export function _closeDesktopCtx() {
    if (_desktopCtxMenu) { _desktopCtxMenu.remove(); _desktopCtxMenu = null; }
}

function _countWindowsByApp(appId) {
    let n = 0; for (const [k, v] of _openWins.entries()) { if (v.appId === appId) n++; } return n;
}

function _focusExistingWindow(appId) {
    for (const [k, v] of _openWins.entries()) {
        if (v.appId === appId && v.element) {
            if (v.element.classList.contains('minimized')) _restoreWin(v.element);
            else _focusWin(v.element);
            return;
        }
    }
}

// Event delegation: attach contextmenu listener to #desktopIcons container.
// Built-in icons carry their own inline oncontextmenu handler — the delegation
// only handles dynamic package icons (testid without 'pkg-' prefix).
export function initDesktopCtxMenu() {
    const container = document.getElementById('desktopIcons');
    if (!container || container._ctxAttached) return;
    container.addEventListener('contextmenu', (e) => {
        const iconEl = e.target.closest('.desktop-icon');
        if (!iconEl) { _showDesktopBackgroundCtx(e); return; } // empty desktop
        const testId = iconEl.getAttribute('data-testid') || '';
        const m1 = testId.match(/^desktop-icon-(.+)$/);
        if (!m1) return;
        const rawId = m1[1];
        if (APP_REGISTRY[rawId]) return; // built-in: inline handler already ran
        _showDesktopCtx(e, 'pkg-' + rawId);
    });
    container._ctxAttached = true;
}

/** Right-click on empty desktop space — desktop-wide commands. */
function _showDesktopBackgroundCtx(e) {
    if (!e || !(e instanceof Event)) return;
    e.preventDefault(); e.stopPropagation();
    if (_desktopCtxMenu) { _desktopCtxMenu.remove(); _desktopCtxMenu = null; }

    const ctx = document.createElement('div');
    Object.assign(ctx.style, {
        position: 'fixed', left: (e.clientX ?? 50) + 'px', top: (e.clientY ?? 50) + 'px',
        zIndex: 9001, background: 'var(--surface-1)', border: '1px solid var(--border)',
        borderRadius: '10px', padding: '4px 0', minWidth: '200px',
        boxShadow: '0 16px 48px rgba(0,0,0,0.5)',
        fontFamily: "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif",
    });
    ctx.setAttribute('data-testid', 'desktop-ctx');

    const items = [
        { label: '🧹 Auto-arrange icons', testid: 'desktop-ctx-auto-arrange', action() { autoArrangeDesktopIcons(); _closeDesktopCtx(); } },
    ];
    for (const it of items) {
        const el = document.createElement('div');
        Object.assign(el.style, { padding: '8px 16px', fontSize: '13px', color: 'var(--text-1)', cursor: 'pointer', transition: 'background 0.1s' });
        el.textContent = it.label;
        if (it.testid) el.setAttribute('data-testid', it.testid);
        el.onmouseenter = () => { el.style.background = 'rgba(91,154,255,0.15)'; };
        el.onmouseleave = () => { el.style.background = 'transparent'; };
        el.onclick = (ev2) => { ev2.stopPropagation(); it.action(); };
        ctx.appendChild(el);
    }

    document.body.appendChild(ctx);
    _desktopCtxMenu = ctx;
    const closeCtx = (ev3) => { if (_desktopCtxMenu && !_desktopCtxMenu.contains(ev3.target)) _closeDesktopCtx(); };
    setTimeout(() => { document.addEventListener('mousedown', closeCtx, true); document.addEventListener('contextmenu', () => { _closeDesktopCtx(); }); }, 10);
}

// ── Dynamic package icons (pkg-*) — cached so the single-pass renderer can draw them ──
let _pkgIconData = []; // [{id, name, icon}] from /api/packages/desktop-icons

export function setPkgIconData(icons) {
    _pkgIconData = Array.isArray(icons) ? icons : [];
    renderDesktopIcons();
}

function _buildPkgIconHtml(pkg) {
    const icon = typeof pkg.icon === 'string' && pkg.icon.length <= 2 ? pkg.icon : '📦';
    const inner = pkg.icon_url
        ? `<img src="${escapeAttr(pkg.icon_url)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:inherit;">`
        : escapeHtml(icon);
    const bg = pkg.icon_url ? 'background:#0ea5e9' : 'background:linear-gradient(135deg,#0ea5e9,#0284c7)';
    return `<div class="desktop-icon" data-testid="desktop-icon-${escapeAttr(pkg.id)}" draggable="true"
                ondragstart="window._onIconDragStart(event, 'pkg-${escapeAttr(pkg.id)}')"
                onclick="openApp('pkg-${escapeAttr(pkg.id)}')" oncontextmenu="_showDesktopCtx(event, 'pkg-${escapeAttr(pkg.id)}')">
        <div class="icon-img" style="${bg}">${inner}</div>
        <span class="icon-label">${escapeHtml(pkg.name || pkg.id)}</span></div>`;
}

// ── Free desktop placement ───────────────────────────────────────────
// Icons live on a fixed grid (like Windows/macOS) instead of a flow layout, so
// the user can drop one anywhere on the desktop. Cells — not pixels — are what
// gets stored, so a different window size does not scatter everything.
const CELL_W = 96, CELL_H = 100, CELL_GAP = 8;
const STEP_X = CELL_W + CELL_GAP;
const STEP_Y = CELL_H + CELL_GAP;
const PAD_LEFT = 24, PAD_RIGHT = 24, PAD_BOTTOM = 96, PAD_TOP_EXTRA = 16;

function _gridGeometry() {
    const el = document.getElementById('desktopIcons');
    const cs = getComputedStyle(document.documentElement);
    const barH = parseFloat(cs.getPropertyValue('--topbar-h')) || 42;
    // While the desktop is hidden (login overlay) clientWidth is 0 — fall back
    // to the viewport so the very first render still gets a sane grid.
    const vw = (el && el.clientWidth) || window.innerWidth;
    const vh = (el && el.clientHeight) || window.innerHeight;
    const leftPad = PAD_LEFT, topPad = barH + PAD_TOP_EXTRA;
    const cols = Math.max(1, Math.floor((vw - leftPad - PAD_RIGHT + CELL_GAP) / STEP_X));
    const rows = Math.max(1, Math.floor((vh - topPad - PAD_BOTTOM + CELL_GAP) / STEP_Y));
    return { leftPad, topPad, cols, rows };
}

/** Resolve a rendered icon's data-testid to its layout id. */
function _iconId(el) {
    const raw = (el.getAttribute('data-testid') || '').replace(/^desktop-icon-/, '');
    return APP_REGISTRY[raw] ? raw : 'pkg-' + raw;
}

/**
 * Decide a cell for every pinned icon.
 * Stored cells win when they still fit the grid; the rest fill the remaining
 * cells column-major (top→bottom, then next column), like a real desktop.
 */
function _computePlacement(ids) {
    const geom = _gridGeometry();
    const stored = getPositions();
    const key = (c, r) => c + ':' + r;
    const used = new Set();
    const map = new Map();

    for (const id of ids) {
        const p = stored[id];
        if (!p || typeof p.c !== 'number' || typeof p.r !== 'number') continue;
        const c = Math.max(0, Math.min(geom.cols - 1, p.c));
        const r = Math.max(0, Math.min(geom.rows - 1, p.r));
        if (used.has(key(c, r))) continue;
        used.add(key(c, r));
        map.set(id, { c, r });
    }
    // The Trash shortcut belongs in the bottom-right corner until the user drags
    // it elsewhere: claim the free cell closest to that corner before the
    // column-major auto-fill (so "Auto-arrange" also restores it there).
    for (const id of ids) {
        if (id !== 'trash' || map.has(id)) continue;
        let spot = null;
        for (let c = geom.cols - 1; c >= 0 && !spot; c--) {
            for (let r = geom.rows - 1; r >= 0 && !spot; r--) {
                if (!used.has(key(c, r))) spot = { c, r };
            }
        }
        if (!spot) continue;
        used.add(key(spot.c, spot.r));
        map.set(id, spot);
    }
    for (const id of ids) {
        if (map.has(id)) continue;
        let spot = null;
        for (let c = 0; c < geom.cols && !spot; c++) {
            for (let r = 0; r < geom.rows && !spot; r++) {
                if (!used.has(key(c, r))) spot = { c, r };
            }
        }
        if (!spot) spot = { c: 0, r: 0 }; // grid smaller than the icon count
        used.add(key(spot.c, spot.r));
        map.set(id, spot);
    }
    return { map, geom };
}

export function renderDesktopIcons() {
    const container = document.getElementById('desktopIcons');
    if (!container) return;

    // Single pass in PIN order — the visible list is authoritative for desktop
    // layout. Rendering from it (not the drawer order) guarantees a pinned app
    // always shows, even if the saved layout predates that app.
    const visibleApps = getVisibleApps().filter(isAppVisible);
    const pkgsById = new Map(_pkgIconData.map(p => [p.id, p]));

    let html = '';
    for (const id of visibleApps) {
        if (id.startsWith('pkg-')) {
            const pkg = pkgsById.get(id.slice(4));
            if (pkg) html += _buildPkgIconHtml(pkg); // unloaded package — appears once data loads
            continue;
        }
        const app = APP_REGISTRY[id];
        if (app) html += _buildIconHtml(app);
    }
    container.innerHTML = html;

    // Free placement: pin each element to its grid cell.
    const { map, geom } = _computePlacement(visibleApps);
    for (const el of container.querySelectorAll('.desktop-icon')) {
        const cell = map.get(_iconId(el));
        if (!cell) continue;
        el.style.left = `${geom.leftPad + cell.c * STEP_X}px`;
        el.style.top = `${geom.topPad + cell.r * STEP_Y}px`;
    }

    // The badge element is recreated by innerHTML above — refill it from the
    // cached/API count so it never flickers away after a re-render.
    refreshTrashBadge();
}

// Re-render desktop icons (built-in + dynamic pkg-* in one pass).
export function refreshDesktopIcons() {
    renderDesktopIcons();
}

// ── Desktop icon removal ──────────────────────────────────────────────
window._removeIcon = function(appId) {
    removeFromDesktop(appId);
    refreshDesktopIcons();
};

// ── Drag-and-drop reordering of desktop icons ───────────────────────────
const ICON_DRAG_TYPE = 'application/x-ethos-desktop-icon';

window._onIconDragStart = function(event, appId) {
    // Marker type so the drawer-drop hint doesn't fire for own-icon drags
    event.dataTransfer.setData(ICON_DRAG_TYPE, appId);
    event.dataTransfer.effectAllowed = 'move';
    const el = event.currentTarget;
    if (el && el.classList) setTimeout(() => el.classList.add('dragging'), 0); // after drag-image capture
};

function _setDropTarget(container, el) {
    const cur = container.querySelector('.desktop-icon.drop-target');
    if (cur && cur !== el) cur.classList.remove('drop-target');
    if (el) el.classList.add('drop-target');
}

function _clearDropTarget() {
    document.querySelectorAll('#desktopIcons .desktop-icon.drop-target')
        .forEach(el => el.classList.remove('drop-target'));
}

function _cellAtPoint(container, geom, event) {
    const rect = container.getBoundingClientRect();
    const col = Math.round((event.clientX - rect.left - geom.leftPad) / STEP_X);
    const row = Math.round((event.clientY - rect.top - geom.topPad) / STEP_Y);
    return {
        c: Math.max(0, Math.min(geom.cols - 1, col)),
        r: Math.max(0, Math.min(geom.rows - 1, row)),
    };
}

function _nearestFreeCell(map, geom, target, movingId = null) {
    const taken = new Set();
    for (const [id, cell] of map) {
        if (id !== movingId) taken.add(cell.c + ':' + cell.r);
    }
    const key = (cell) => cell.c + ':' + cell.r;
    if (!taken.has(key(target))) return target;

    let best = null, bestDist = Infinity;
    for (let c = 0; c < geom.cols; c++) {
        for (let r = 0; r < geom.rows; r++) {
            if (taken.has(c + ':' + r)) continue;
            const distance = (c - target.c) ** 2 + (r - target.r) ** 2;
            if (distance < bestDist) {
                bestDist = distance;
                best = { c, r };
            }
        }
    }
    return best || target;
}

/**
 * Drop handler: put the dragged icon in the grid cell under the pointer and
 * persist it. Dropping onto an occupied cell moves the icon to the nearest free
 * one instead of silently doing nothing.
 */
function _placeIconOnDrop(container, appId, e) {
    const { map, geom } = _computePlacement(getVisibleApps());
    const target = _cellAtPoint(container, geom, e);
    const cell = _nearestFreeCell(map, geom, target, appId);
    setPosition(appId, cell.c, cell.r);
    refreshDesktopIcons();
}

/** Put every icon back on the automatic grid (drops manual placement). */
export function autoArrangeDesktopIcons() {
    clearPositions();
    refreshDesktopIcons();
}

window._desktopAutoArrange = autoArrangeDesktopIcons;

export function initDesktopReorder() {
    const container = document.getElementById('desktopIcons');
    if (!container || container._reorderAttached) return;
    container._reorderAttached = true;

    container.addEventListener('dragover', (e) => {
        if (!e.dataTransfer || !Array.from(e.dataTransfer.types).includes(ICON_DRAG_TYPE)) return;
        e.preventDefault(); // allow drop on the grid
        e.dataTransfer.dropEffect = 'move';
        _setDropTarget(container, e.target.closest ? e.target.closest('.desktop-icon') : null);
    });

    container.addEventListener('dragleave', (e) => {
        if (!container.contains(e.relatedTarget)) _clearDropTarget();
    });

    container.addEventListener('drop', (e) => {
        const appId = e.dataTransfer && e.dataTransfer.getData(ICON_DRAG_TYPE);
        if (!appId) return; // not an icon drag — let the drawer-drop zone handle it
        e.preventDefault();
        e.stopPropagation();
        _clearDropTarget();
        container.querySelectorAll('.desktop-icon.dragging').forEach(el => el.classList.remove('dragging'));
        _placeIconOnDrop(container, appId, e);
    });

    // dragend fires on the source icon even when the drop is cancelled/invalid.
    container.addEventListener('dragend', () => {
        _clearDropTarget();
        container.querySelectorAll('.desktop-icon.dragging').forEach(el => el.classList.remove('dragging'));
    });
}

// ── Desktop drop zone — drag an app from the App Drawer onto the desktop to pin it ──
// Attached to document.body because the desktop icon grid is fit-content and can
// be empty (zero size) — the drop target must be the whole desktop area.
let _dropGhost = null;

function _isOverDesktop(e) {
    const t = e.target;
    if (t.closest && (t.closest('.app-window') || t.closest('.taskbar') || t.closest('.topbar') || t.closest('.app-drawer-panel') || t.closest('#appDrawerPanel'))) return false;
    return true;
}

export function initDesktopDropZone() {
    if (document.body._dropBound) return;
    document.body.addEventListener('dragover', function(e) {
        if (!e.dataTransfer || !e.dataTransfer.types) return;
        const types = Array.from(e.dataTransfer.types);
        if (types.includes(ICON_DRAG_TYPE)) return; // reordering own desktop icon — not a drawer drag
        // Files item drags (dropped on the Trash icon) are not app pins.
        if (types.includes('application/x-ethos-fm-items')) return;
        if (!types.includes('text/plain')) return;
        if (!_isOverDesktop(e)) { if (_dropGhost) _dropGhost.classList.remove('active'); return; }
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
        if (!_dropGhost) {
            _dropGhost = document.createElement('div');
            _dropGhost.id = 'desktopDropHint';
            document.body.appendChild(_dropGhost);
        }
        _dropGhost.classList.add('active');
    });
    document.body.addEventListener('dragleave', function(e) {
        if (e.relatedTarget) return; // moving within the document, not leaving
        if (_dropGhost) _dropGhost.classList.remove('active');
    });
    document.body.addEventListener('drop', function(e) {
        if (_dropGhost) _dropGhost.classList.remove('active');
        if (!_isOverDesktop(e)) return;
        const types = e.dataTransfer && e.dataTransfer.types ? Array.from(e.dataTransfer.types) : [];
        // Files drags set text/plain to file names — never treat those as
        // app ids (the Trash icon has its own drop handler).
        if (types.includes('application/x-ethos-fm-items')) return;
        const appId = e.dataTransfer.getData('text/plain');
        if (!appId || !/^[a-z0-9-]+$/i.test(appId)) return;
        e.preventDefault();
        const visible = getVisibleApps();
        if (visible.includes(appId)) return;
        const container = document.getElementById('desktopIcons');
        const { map, geom } = _computePlacement(visible);
        const target = container ? _cellAtPoint(container, geom, e) : { c: 0, r: 0 };
        const cell = _nearestFreeCell(map, geom, target);
        // Insert after the icon whose center is closest to the left of the drop point
        let pos = -1;
        if (container) {
            const icons = Array.from(container.querySelectorAll('.desktop-icon'));
            let anchorId = null;
            let bestDx = -Infinity;
            for (const el of icons) {
                const r = el.getBoundingClientRect();
                const cx = r.left + r.width / 2;
                if (cx <= e.clientX && cx > bestDx) { bestDx = cx; anchorId = el.getAttribute('data-testid'); }
            }
            if (anchorId) {
                const rawId = anchorId.replace(/^desktop-icon-/, '');
                const visible = getVisibleApps();
                // testid is the raw id for built-ins; package icons are stored as "pkg-{id}"
                const ai = visible.includes(rawId) ? visible.indexOf(rawId) : visible.indexOf('pkg-' + rawId);
                if (ai >= 0) pos = ai + 1;
            }
        }
        addToDesktop(appId, pos);
        setPosition(appId, cell.c, cell.r);
        refreshDesktopIcons();
        // Pinned — close the drawer (Ubuntu behavior after dropping on the desktop)
        if (window._closeDrawer) window._closeDrawer();
    });
    document.body._dropBound = true;
}
