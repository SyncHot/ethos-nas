// EthOS NAS — App Drawer (Ubuntu/GNOME "Show Applications" style)
// Full-screen app grid with search. Click = open app, drag onto the desktop =
// pin icon, right-click = pin/unpin. The desktop starts empty — the user
// manages which icons live on it.

import { getVisibleApps, addToDesktop, removeFromDesktop, toggleDesktop } from '../core/desktop-layout.js';
import { getRegistry, refreshDesktopIcons } from '../core/registry.js';
import { getAppIcon } from '../core/app-icons.js';

let _drawerOpen = false;
let _drawerEl = null;
let _searchInput = null;
let _pkgApps = []; // dynamic package apps from Packages, e.g. [{id:'pkg-jellyfin',title,icon}]
let _ctxMenu = null;

// ── App list ─────────────────────────────────────────────────────────────

function _getAllApps() {
    const REG = getRegistry();
    const apps = [];
    for (const key of Object.keys(REG)) apps.push(REG[key]);
    for (const p of _pkgApps) {
        if (p.id && !REG[p.id]) apps.push(p); // pkg apps carry the 'pkg-' prefix
    }
    return apps;
}

export async function refreshDrawerPackages() {
    try {
        const resp = await fetch('/api/packages/desktop-icons');
        if (!resp.ok) return;
        const icons = await resp.json();
        _pkgApps = (Array.isArray(icons) ? icons : []).map(p => ({
            id: 'pkg-' + p.id,
            title: p.name || p.id,
            icon: (typeof p.icon === 'string' && p.icon.length <= 2) ? p.icon : '📦',
            iconUrl: typeof p.icon_url === 'string' ? p.icon_url : '',
            pkg: true,
        }));
        if (_drawerOpen) _renderContent();
    } catch (_) {}
}

function _matchesQuery(app, q) {
    if (!q) return true;
    return app.title.toLowerCase().includes(q) || app.id.toLowerCase().includes(q);
}

// ── DOM ──────────────────────────────────────────────────────────────────

export function initAppDrawer() {
    if (!_drawerEl) _createDrawerDOM();

    // Topbar "Show Applications" button — static element in index.html, bound once.
    const topBtn = document.getElementById('topbar-app-drawer-btn');
    if (topBtn && !topBtn._drawerBound) {
        topBtn.addEventListener('click', toggleDrawer);
        topBtn._drawerBound = true;
    }

    document.addEventListener('keydown', function onKeyDown(e) {
        if (e.key === 'Escape' && _drawerOpen) closeDrawer();
    });
    document.addEventListener('mousedown', function onMousedown(e) {
        if (!_drawerOpen || !_drawerEl) return;
        if (_ctxMenu && _ctxMenu.contains(e.target)) return;
        if (_drawerEl.contains(e.target)) return;
        if (e.target.closest('#topbar-app-drawer-btn')) return;
        closeDrawer();
    });
}

function _createDrawerDOM() {
    const drawer = document.createElement('div');
    drawer.id = 'appDrawerPanel';
    drawer.setAttribute('data-testid', 'app-drawer-panel');
    drawer.className = 'app-drawer-panel';
    drawer.setAttribute('aria-hidden', 'true');
    drawer.innerHTML =
        '<div class="drawer-search-wrap">' +
        '<input type="text" class="drawer-search" id="appDrawerSearch" data-testid="drawer-search" placeholder="Search applications" autocomplete="off" spellcheck="false">' +
        '</div>' +
        '<div class="drawer-content" id="appDrawerContent" data-testid="drawer-content"></div>' +
        '<div class="drawer-empty" id="appDrawerEmpty" data-testid="drawer-empty" style="display:none;">No applications found</div>';
    document.body.appendChild(drawer);
    _drawerEl = drawer;
    _searchInput = drawer.querySelector('#appDrawerSearch');
    _searchInput.addEventListener('input', () => _renderContent(_searchInput.value));
    // Clicking on empty drawer space does nothing (items handle their own clicks)
    drawer.querySelector('.drawer-content').addEventListener('mousedown', (e) => {
        if (e.target === e.currentTarget) e.stopPropagation();
    });
}

export function isDrawerOpen() { return _drawerOpen; }

export function toggleDrawer() {
    if (_drawerOpen) closeDrawer(); else openDrawer();
}

export function openDrawer() {
    if (!_drawerEl) { initAppDrawer(); return; }
    _drawerOpen = true;
    _drawerEl.classList.add('open');
    _drawerEl.setAttribute('aria-hidden', 'false');
    _renderContent('');
    if (_searchInput) { _searchInput.value = ''; setTimeout(() => _searchInput.focus(), 60); }
}

export function closeDrawer() {
    _drawerOpen = false;
    if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; }
    if (_drawerEl) { _drawerEl.classList.remove('open'); _drawerEl.setAttribute('aria-hidden', 'true'); }
}
function _renderContent(query) {
    if (!_drawerEl) return;
    const content = _drawerEl.querySelector('#appDrawerContent');
    const empty = _drawerEl.querySelector('#appDrawerEmpty');
    if (!content) return;

    const q = (query || '').trim().toLowerCase();
    const pinned = getVisibleApps();
    const apps = _getAllApps().filter(a => _matchesQuery(a, q));

    content.innerHTML = '';
    const frag = document.createDocumentFragment();
    for (const app of apps) {
        frag.appendChild(_createDrawerItem(app, pinned.includes(app.id)));
    }
    content.appendChild(frag);

    if (empty) empty.style.display = apps.length ? 'none' : '';
}

function _createDrawerItem(app, isPinned) {
    const item = document.createElement('div');
    item.className = 'drawer-item' + (isPinned ? ' pinned' : '');
    item.setAttribute('data-testid', `drawer-item-${app.id}`);
    item.draggable = true;
    item.title = app.title;

    // Drag → drop on desktop pins the icon
    item.addEventListener('dragstart', function(e) {
        e.dataTransfer.setData('text/plain', app.id);
        e.dataTransfer.effectAllowed = 'copyMove';
        item.classList.add('dragging');
        // Fade the drawer so the desktop (drop target) is visible underneath
        if (_drawerEl) _drawerEl.classList.add('drag-fade');
    });
    item.addEventListener('dragend', function() {
        item.classList.remove('dragging');
        if (_drawerEl) _drawerEl.classList.remove('drag-fade');
    });

    // Click → open the app
    item.addEventListener('click', function() {
        closeDrawer();
        if (window.openApp) window.openApp(app.id);
    });

    // Right-click → pin/unpin context menu
    item.addEventListener('contextmenu', function(e) {
        e.preventDefault();
        e.stopPropagation();
        _showItemCtx(e.clientX, e.clientY, app);
    });

    const icon = document.createElement('div');
    icon.className = 'drawer-item-icon';
    const ic = getAppIcon(app.id, 32);
    if (app.pkg && app.iconUrl) {
        icon.style.background = 'transparent';
        const img = document.createElement('img');
        img.src = app.iconUrl;
        img.alt = '';
        img.style.cssText = 'width:100%;height:100%;object-fit:cover;display:block;';
        icon.appendChild(img);
    } else if (ic) { icon.style.background = ic.gradient; icon.style.color = '#fff'; icon.innerHTML = ic.svg; }
    else icon.textContent = app.icon || '📦';

    const label = document.createElement('span');
    label.className = 'drawer-item-label';
    label.textContent = app.title;

    item.appendChild(icon);
    item.appendChild(label);
    return item;
}

// ── Right-click menu on drawer items ─────────────────────────────────────

function _showItemCtx(x, y, app) {
    if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; }
    const ctx = document.createElement('div');
    ctx.className = 'drawer-ctx-menu';
    ctx.setAttribute('data-testid', 'drawer-ctx-menu');
    const pinned = getVisibleApps().includes(app.id);

    const actions = [
        { label: '▶ Open', testid: 'drawer-ctx-open', run() { closeDrawer(); if (window.openApp) window.openApp(app.id); } },
        pinned
            ? { label: '🖥️ Remove from Desktop', testid: 'drawer-ctx-unpin', run() { removeFromDesktop(app.id); _renderContent(_searchInput ? _searchInput.value : ''); refreshDesktopIcons(); } }
            : { label: '📌 Pin to Desktop', testid: 'drawer-ctx-pin', run() { addToDesktop(app.id); _renderContent(_searchInput ? _searchInput.value : ''); refreshDesktopIcons(); } },
    ];

    actions.forEach((a, i) => {
        const el = document.createElement('div');
        el.className = 'drawer-ctx-item';
        el.setAttribute('data-testid', `drawer-ctx-${app.id}-${a.testid.split('-').pop()}`);
        el.textContent = a.label;
        el.addEventListener('click', (ev) => { ev.stopPropagation(); _closeItemCtx(); a.run(); });
        ctx.appendChild(el);
    });

    ctx.style.left = Math.min(x, window.innerWidth - 200) + 'px';
    ctx.style.top = Math.min(y, window.innerHeight - actions.length * 34 - 8) + 'px';
    document.body.appendChild(ctx);
    _ctxMenu = ctx;
    setTimeout(() => {
        document.addEventListener('mousedown', _ctxOutsideClose, true);
        document.addEventListener('contextmenu', _ctxOutsideClose, true);
    }, 10);
}

function _ctxOutsideClose(e) {
    if (_ctxMenu && !_ctxMenu.contains(e.target)) _closeItemCtx();
}

function _closeItemCtx() {
    if (_ctxMenu) { _ctxMenu.remove(); _ctxMenu = null; }
    document.removeEventListener('mousedown', _ctxOutsideClose, true);
    document.removeEventListener('contextmenu', _ctxOutsideClose, true);
}

// Public render API for tests/other modules
export function renderAppDrawer() {
    if (_drawerOpen) _renderContent(_searchInput ? _searchInput.value : '');
}

// ── Global API (used by the taskbar "Show Applications" button) ──────────
window._drawerOpen = () => _drawerOpen;
window._openDrawer = openDrawer;
window._closeDrawer = closeDrawer;
window._toggleDrawer = toggleDrawer;
