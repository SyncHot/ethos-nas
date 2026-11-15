// EthOS NAS — Window Manager (multi-window, drag-drop, snap, taskbar)
import { escapeHtml, escapeAttr } from './utils.js';
import { apiFetch } from './api.js';  // auth-header injection (plain fetch → 401 on protected routes)
import { showUnifiedToast } from './notification-center.js';  // Unified toast replaces inline showToast()
import { appMiniTileHtml } from './app-icons.js';
import { setLogContext } from './logger.js';

// Browser diagnostics carry the app name of the focused window (docs/logs.md).
function _setLogApp(appId) {
    setLogContext({ app: appId || 'core' });
}

function _syncLogContext() {
    const focused = [...openWindows.values()].find((w) => w.element.classList.contains('focused'));
    _setLogApp(focused?.appId);
}

let windowIdCounter = 0;
export const openWindows = new Map(); // id -> { appId, icon, title, element, zIndex, ... }
const SNAP_THRESHOLD = 18;
const SNAP_ANIM_MS = 260;
const MIN_WIN_W = 400;
const MIN_WIN_H = 300;
// Edge + corner grips. The CSS (.rz-*) sizes/hides them; startResize() drives them.
// They are part of the window template because every window must be resizable
// from any edge or corner — the old single 0×0 handle was effectively unusable.
const RESIZE_HANDLES = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw']
    .map((dir) => `<div class="resize-handle rz-${dir}" data-dir="${dir}"></div>`)
    .join('');
let _snapPreview = null;
let _pendingSnap = null;

export function getFocusedWindowId() {
    let bestWin = null, bestZ = -1;
    for (const [k, v] of openWindows.entries()) {
        if (v.zIndex > bestZ) { bestWin = k; bestZ = v.zIndex; }
    }
    return bestWin || null;
}

function _ensureSnapPreview() {
    if (!_snapPreview) { _snapPreview = document.createElement('div'); _snapPreview.className = 'snap-preview'; document.body.appendChild(_snapPreview); }
    return _snapPreview;
}
function _hideSnapPreview() { if (_snapPreview) _snapPreview.classList.remove('active'); }

function _snapTarget(winEl, edge) {
    const TOPBAR_H = 36; // keep in sync with --topbar-h in index.html
    const vw = window.innerWidth, vh = window.innerHeight - TOPBAR_H, hdrH = 48;
    let l = 0, t = TOPBAR_H, w = vw, h = vh;
    switch (edge) {
        case 'top': w = vw; h = Math.floor(vh / 2); break;
        case 'left': w = Math.floor(vw / 2); t = TOPBAR_H; break;
        case 'right': l = Math.floor(vw / 2); w = Math.floor(vw / 2); t = TOPBAR_H; break;
        case 'left-top': w = Math.floor(vw / 2); h = Math.floor(vh / 2); break;
        case 'right-top': l = Math.floor(vw / 2); w = Math.floor(vw / 2); h = Math.floor(vh / 2); break;
    }
    return { left: l + 'px', top: t + 'px', width: w + 'px', height: h + 'px' };
}

const APP_INFO = {
    containers: ['🐳', 'Docker Containers'],
    volumes: ['💾', 'Docker Volumes'], images: ['🖼️', 'Docker Images'], docker: ['🐳', 'Docker Manager'],
    storage: ['💿', 'Storage'], files: ['📁', 'File Manager'], terminal: ['⌨️', 'Terminal'],
    security: ['🛡️', 'Firewall & Access Control'], smart: ['💿', 'SMART Health'], systemd: ['⚙️', 'System Services'],
    network: ['🌐', 'Network'],
    settings: ['🔧', 'Settings'], processes: ['📋', 'Process Manager'], logs: ['📜', 'Log Viewer'],
    packages: ['📦', 'Packages'],
    cron: ['⏰', 'Cron Jobs'],
};

// Package identity (name + icon_url) comes from the catalog/manifest. Cached here
// so a package window gets the real title and icon in the header and taskbar,
// not the "pkg-<id>" fallback.
const _pkgIdentity = new Map();

function _pkgFallbackName(appId) {
    return String(appId || '').replace(/^pkg-/, '').replace(/[-_]+/g, ' ')
        .replace(/\b\w/g, (c) => c.toUpperCase());
}

function _pkgIconHtml(meta) {
    const url = meta && typeof meta.icon_url === 'string' ? meta.icon_url : '';
    if (url.startsWith('https://') || url.startsWith('http://')) {
        return `<img src="${escapeAttr(url)}" alt="" style="width:18px;height:18px;border-radius:4px;object-fit:cover;display:block;">`;
    }
    return escapeHtml(meta && typeof meta.icon === 'string' && meta.icon.length <= 2 ? meta.icon : '📦');
}

/** Cache manifest identity for packages and refresh any open package windows. */
export function setPackageIdentity(packages) {
    let changed = false;
    for (const pkg of Array.isArray(packages) ? packages : []) {
        if (!pkg || !pkg.id) continue;
        _pkgIdentity.set(String(pkg.id), {
            name: typeof pkg.name === 'string' ? pkg.name : '',
            icon: typeof pkg.icon === 'string' ? pkg.icon : '',
            icon_url: typeof pkg.icon_url === 'string' ? pkg.icon_url : '',
        });
        changed = true;
    }
    if (!changed) return;
    for (const w of openWindows.values()) {
        if (!String(w.appId).startsWith('pkg-')) continue;
        const meta = _pkgIdentity.get(w.appId.slice(4));
        if (!meta) continue;
        w.baseTitle = meta.name || _pkgFallbackName(w.appId);
        w.icon = _pkgIconHtml(meta);
        if (w._titleIcon) w._titleIcon.innerHTML = w.icon;
    }
    _refreshInstanceNumbers();
    renderTaskbar();
}

// Apps that may have multiple windows open at once. Every other app is
// single-instance: opening it again focuses (or restores) the existing window.
export const MULTI_WINDOW_APPS = new Set(['files', 'editor']); // Files, Code Editor
export function isMultiWindowApp(appId) { return MULTI_WINDOW_APPS.has(appId); }

function _findOpenWindowByApp(appId) {
    for (const [k, v] of openWindows.entries()) {
        if (v.appId === appId && v.element && !v.element.classList.contains('closing')) return v;
    }
    return null;
}

export function openApp(appId, renderFn) {
    // Single-instance apps: focus/restore the existing window instead of opening a duplicate
    if (!isMultiWindowApp(appId)) {
        const existing = _findOpenWindowByApp(appId);
        if (existing) {
            if (existing.element.classList.contains('minimized')) restoreWindowWin(existing.element);
            else focusWindow(existing.element);
            return;
        }
    }

    const o = document.getElementById('modalOverlay');
    if (!o) return;    windowIdCounter++;
    const winEl = document.createElement('div');
    winEl.className = 'app-window opening';
    const uniqueId = windowIdCounter;
    winEl.id = `win-${appId}-${uniqueId}`;
    winEl.setAttribute('data-testid', 'window-' + appId);

    const titleIconSpan = document.createElement('span'); titleIconSpan.className = 'window-title-icon';
    const titleTextSpan = document.createElement('span'); titleTextSpan.className = 'window-title-text';
    winEl.innerHTML = `<div class="window-header"><div class="window-controls"><button class="win-btn win-btn-close close-btn" data-glyph="×" title="Close" aria-label="Close window"></button><button class="win-btn win-btn-min minimize-btn" data-glyph="−" title="Minimize" aria-label="Minimize window"></button><button class="win-btn win-btn-max maximize-btn" data-glyph="+" title="Maximize" aria-label="Maximize or restore window"></button></div><span class="window-title"></span></div><div class="window-body"></div>${RESIZE_HANDLES}`;

    const minimizeBtn = winEl.querySelector('.minimize-btn');
    const maximizeBtn = winEl.querySelector('.maximize-btn');
    const closeBtn = winEl.querySelector('.close-btn');
    if (minimizeBtn) minimizeBtn.addEventListener('click', e => { e.stopPropagation(); minimizeWindowWin(minimizeBtn); });
    if (maximizeBtn) maximizeBtn.addEventListener('click', e => { e.stopPropagation(); toggleMaximizeWin(maximizeBtn); });
    if (closeBtn) closeBtn.addEventListener('click', e => { e.stopPropagation(); closeWindowWin(closeBtn); });

    const winTitleContainer = winEl.querySelector('.window-title');
    if (winTitleContainer) winTitleContainer.prepend(titleIconSpan, titleTextSpan);

    const vw = window.innerWidth, vh = window.innerHeight;
    // Keep a small top offset so windows don't overlap the topbar,
    // but otherwise center vertically for a natural look.
    const TOP_OFFSET = 48;
    const staggerX = 40 + ((windowIdCounter - 1) % 5) * 25;
    const staggerY = ((windowIdCounter - 1) % 4) * 6;
    let calcLeft = Math.max(20, Math.min(vw - 300, vw / 2 - 500 + staggerX));
    const winH = Math.min(vh * 0.85, 750);
    let calcTop = Math.max(TOP_OFFSET, (vh - winH) / 2 + staggerY);

    winEl.style.position = 'absolute';
    winEl.style.left = calcLeft + 'px';
    winEl.style.top = calcTop + 'px';
    winEl.style.width = Math.min(vw * 0.85, 1100) + 'px';
    winEl.style.height = Math.min(vh - calcTop - 40, winH) + 'px';
    // New windows must start above EVERY existing one: focusWindow() bumps a
    // window's z-index on each click, so `openWindows.size + 502` frequently
    // collided with (or fell below) a window the user had clicked a few times.
    // Apps that resolve "the active window" while initialising (File Manager's
    // fmLoad) then rendered into the OLD window and left the new one blank.
    const zBase = Math.max(502, ...[...openWindows.values()].map((w) => w.zIndex)) + 1;
    winEl.style.zIndex = zBase;

    o.appendChild(winEl);
    winEl.addEventListener('mousedown', () => focusWindow(winEl));
    const pkgMeta = appId.startsWith('pkg-') ? _pkgIdentity.get(appId.slice(4)) : null;
    const [icon, title] = pkgMeta
        ? [_pkgIconHtml(pkgMeta), pkgMeta.name || _pkgFallbackName(appId)]
        : (APP_INFO[appId] || ['📊', _pkgFallbackName(appId)]);
    const tileHtml = appMiniTileHtml(appId); // gradient mini-tile for known apps, '' otherwise

    // T-MW: Count existing windows for this appId to determine instance number
    let instanceNum = _countWindowsByApp(appId) + 1;
    const displayTitle = instanceNum > 1 ? `${title} [${instanceNum}]` : title;
    if (titleIconSpan) {
        if (tileHtml) titleIconSpan.innerHTML = tileHtml;
        else titleIconSpan.textContent = icon;
    }
    if (titleTextSpan) { titleTextSpan.textContent = displayTitle; }
    // Add data-testid for the window title span when >1 instance is open
    const winIdSuffix = uniqueId.toString().replace(/[^a-zA-Z0-9]/g, '');
    if (instanceNum > 1 && titleTextSpan) {
        titleTextSpan.setAttribute('data-testid', 'window-title-' + appId + '-' + instanceNum);
    }

    o.classList.add('visible'); startDragWin(winEl);
    const shownIcon = tileHtml || icon;
    openWindows.set(winEl.id, { appId, icon: shownIcon, title: displayTitle, baseTitle: title, element: winEl, zIndex: zBase });
    // Register the taskbar item only AFTER the window is in openWindows,
    // otherwise renderTaskbar() misses it and the button appears one render late.
    addToTaskbar(appId, shownIcon, displayTitle);

    // Focus the new window BEFORE rendering its content, so initialisers that
    // resolve the active window see this one (not the previously focused one).
    focusWindow(winEl);
    const winBody = winEl.querySelector('.window-body');
    if (renderFn) renderFn(winBody);

    openWindows.set(winEl.id, { appId, icon: shownIcon, title: displayTitle, baseTitle: title, element: winEl, zIndex: zBase, _titleIcon: titleIconSpan, _titleText: titleTextSpan, _restoredLeft: calcLeft + 'px', _restoredTop: calcTop + 'px', _restoredWidth: winEl.style.width, _restoredHeight: winEl.style.height });
    // Newly opened windows always come to the front (above every existing window)
    focusWindow(winEl);
    setTimeout(() => { if (winEl.parentNode) winEl.classList.remove('opening'); }, 320);
}

// T-MW helper: count windows of a given appId currently open
function _countWindowsByApp(appId) {
    let n = 0; for (const [k, v] of openWindows.entries()) { if (v.appId === appId) n++; } return n;
}

// T-MW helper: refresh all window title instance numbers when windows are opened/closed
export function _refreshInstanceNumbers() {
    // Count per appId
    const counts = {}; for (const [k, v] of openWindows.entries()) { counts[v.appId] = (counts[v.appId] || 0) + 1; }
    let idx = {}; // reset index per app as we iterate
    for (const [k, v] of openWindows.entries()) {
        idx[v.appId] = (idx[v.appId] || 0) + 1;
        const n = idx[v.appId];
        if (v._titleText) {
            const baseTitle = v.baseTitle || (APP_INFO[v.appId] ? APP_INFO[v.appId][1] : _pkgFallbackName(v.appId));
            const displayTitle = n > 1 ? `${baseTitle} [${n}]` : baseTitle;
            v.title = displayTitle;
            v._titleText.textContent = displayTitle;
            if (n > 1) {
                v._titleText.setAttribute('data-testid', 'window-title-' + v.appId + '-' + n);
            } else {
                v._titleText.removeAttribute('data-testid');
            }
        }
    }
}



export function focusWindow(el) {
    if (!el || !openWindows.has(el.id)) return;
    for (const w of openWindows.values()) w.element.classList.remove('focused');
    let maxZ = Math.max(...openWindows.values().map(w => w.zIndex), 500);
    // Keep window z-indexes bounded: focusWindow bumps maxZ+1 on every mousedown, so in long
    // sessions a window's zIndex can grow past the fixed global overlays (FM context menu z 9000,
    // video overlay 9000/9001, properties dialog 9500, drop zone 9995, unified modal 10000) and
    // cover them. Renormalize to a compact range once we exceed the cap (order preserved).
    if (maxZ > 800) {
        const sorted = [...openWindows.values()].sort((a, b) => a.zIndex - b.zIndex);
        sorted.forEach((w, i) => { w.zIndex = 502 + i; w.element.style.zIndex = 502 + i; });
        maxZ = 501 + sorted.length;
    }
    el.style.zIndex = maxZ + 1;
    el.classList.add('focused');
    el.style.opacity = '1';
    for (const [k, v] of openWindows.entries()) { if (v.element === el) v.zIndex = maxZ + 1; }
    // Focus happens on every mousedown, so this must not rebuild the bar: the
    // old code wiped and re-created every taskbar item each time a window was
    // clicked (TODO.md P3-07).
    updateTaskbarActive();
    // Attribute browser diagnostics to the app the user is looking at (docs/logs.md).
    const focusedEntry = [...openWindows.values()].find((w) => w.element === el);
    if (focusedEntry && focusedEntry.appId) _setLogApp(focusedEntry.appId);
}

/**
 * Cheap taskbar refresh: toggles `active`/`minimized-window` on existing items,
 * drops items whose window is gone and appends any missing one. Never touches
 * the system entry.
 */
export function updateTaskbarActive() {
    const tb = document.getElementById('taskbar');
    if (!tb) return;
    const windowsArray = Array.from(openWindows.values());
    const focused = windowsArray.find((w) => w.element.classList.contains('focused'));
    const seen = new Set();

    for (const w of windowsArray) {
        const winId = w.element.id;
        seen.add(winId);
        const minimized = w.element.classList.contains('minimized');
        let item = tb.querySelector(`.taskbar-item[data-win-id="${winId}"]`);
        if (!item) {
            item = _createTaskbarItem(w);   // new window since the last sync
            const sysEntry = tb.querySelector('.taskbar-system');
            if (sysEntry) tb.insertBefore(item, sysEntry);
            else tb.appendChild(item);
        }
        item.classList.toggle('active', w === focused && !minimized);
        item.classList.toggle('minimized-window', minimized);
    }

    tb.querySelectorAll('.taskbar-item[data-win-id]').forEach((item) => {
        if (!seen.has(item.dataset.winId)) item.remove();   // window closed
    });
    _syncTaskbarSystemEntry();
}

function _createTaskbarItem(w) {
    const btn = document.createElement('div');
    btn.className = 'taskbar-item';
    btn.setAttribute('data-win-id', w.element.id);
    btn.setAttribute('role', 'button');
    btn.setAttribute('tabindex', '0');
    btn.setAttribute('aria-label', `${w.title} window`);
    btn.innerHTML = `<span class="taskbar-item-icon">${w.icon}</span><span>${escapeHtml(w.title)}</span>`;
    const targetEl = w.element;
    btn.onclick = function () {
        if (targetEl.classList.contains('minimized')) restoreWindowWin(targetEl);
        else if (targetEl.classList.contains('focused')) minimizeWindowWin(targetEl.querySelector('.minimize-btn'));
        else focusWindow(targetEl);
    };
    btn.oncontextmenu = function (e) {
        e.preventDefault(); e.stopPropagation();
        showTaskbarContextMenu(e.clientX, e.clientY, targetEl);
    };
    btn.onkeydown = function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); btn.onclick(); }
        else if (e.shiftKey && e.key === 'F10') {
            e.preventDefault();
            const r = btn.getBoundingClientRect();
            showTaskbarContextMenu(r.left, r.top, targetEl);
        }
    };
    return btn;
}

function _syncTaskbarSystemEntry() {
    const tb = document.getElementById('taskbar');
    const sys = tb?.querySelector('.taskbar-system');
    if (!sys) return;
    const rc = document.getElementById('docker-running');
    const label = sys.querySelector('span:last-child');
    if (!label) return;
    const suffix = rc && rc.textContent !== '—' ? ` (${rc.textContent} running)` : '';
    const text = `EthOS NAS${suffix}`;
    if (label.textContent !== text) label.textContent = text;
}


// ── Close guards (T-CG): apps can veto closing a dirty window ────────────────
// One guard per app, but it receives the *window element*: multi-window apps
// (files, editor) tell their instances apart by comparing `winEl`, which is what
// P3-07's "per-window guard" means in practice.
const _closeGuards = new Map(); // appId → (winEl) => boolean; returning false cancels the close
export function registerCloseGuard(appId, fn) { _closeGuards.set(appId, fn); }

// ── Close listeners (TODO.md P3-07/P3-09): background work tears itself down ──
// `onAppClose('gallery', stopPoll)` is how a poller learns its window is gone,
// so closing an app leaves zero timers behind.
const _closeListeners = new Map(); // appId → Set<fn>

export function onAppClose(appId, fn) {
    if (!_closeListeners.has(appId)) _closeListeners.set(appId, new Set());
    _closeListeners.get(appId).add(fn);
    return () => _closeListeners.get(appId)?.delete(fn);
}

function _notifyClosed(appId) {
    // Only fire when the *last* window of that app is gone: multi-window apps
    // (files, editor) keep their pollers alive while one instance remains.
    const stillOpen = [...openWindows.values()].some((v) => v.appId === appId);
    if (stillOpen) return;
    const fns = _closeListeners.get(appId);
    if (!fns) return;
    for (const fn of fns) {
        try { fn(); } catch (e) { console.warn('[ethos] close listener failed:', e); }
    }
    _closeListeners.delete(appId);
}
function _checkCloseGuard(winEl) {
    const entry = openWindows.get(winEl.id);
    if (!entry) return true;
    const guard = _closeGuards.get(entry.appId);
    if (guard) { try { return guard(winEl) !== false; } catch (e) { return true; } }
    return true;
}

export function closeWindowWin(btn) {
    const winEl = btn.closest('.app-window');
    if (!winEl) return;
    if (!_checkCloseGuard(winEl)) return; // app vetoed the close (e.g. unsaved changes)
    document.querySelectorAll('img[src^="blob:"]').forEach(img => { try { URL.revokeObjectURL(img.src); } catch (e) {} });
    winEl.classList.remove('focused', 'opening', 'restoring');
    winEl.classList.add('closing');
    const onEnd = () => {
        winEl.classList.remove('closing');
        const entry = openWindows.get(winEl.id);
        openWindows.delete(winEl.id); winEl.remove(); renderTaskbar(); _refreshInstanceNumbers();
        if (openWindows.size === 0) document.getElementById('modalOverlay').classList.remove('visible');
        winEl.removeEventListener('animationend', onEnd);
        if (entry) _notifyClosed(entry.appId);
        _syncLogContext();
        _syncBarsAutohide();
    };
    winEl.addEventListener('animationend', onEnd);
    setTimeout(() => { if (winEl.parentNode) { winEl.classList.remove('closing', 'opening', 'restoring'); const entry = openWindows.get(winEl.id); openWindows.delete(winEl.id); winEl.remove(); renderTaskbar(); _refreshInstanceNumbers(); if (entry) _notifyClosed(entry.appId); _syncLogContext(); _syncBarsAutohide(); } }, SNAP_ANIM_MS + 80);
}

export function minimizeWindowWin(btn) {
    const winEl = btn.closest('.app-window');
    if (!winEl) return;
    winEl.classList.remove('focused', 'opening', 'restoring');
    winEl.classList.add('minimizing');
    const onEnd = () => { winEl.classList.remove('minimizing'); winEl.classList.add('minimized'); updateTaskbarActive(); winEl.removeEventListener('animationend', onEnd); _syncBarsAutohide(); };
    winEl.addEventListener('animationend', onEnd);
}

export function restoreWindowWin(el) {
    if (!el || !openWindows.has(el.id)) return;
    el.classList.remove('minimized'); el.classList.add('restoring');
    focusWindow(el); updateTaskbarActive();
    _syncBarsAutohide();
    setTimeout(() => el.classList.remove('restoring'), 340);
}

export function toggleMaximizeWin(btn) {
    const winEl = btn.closest('.app-window');
    if (!winEl) return;
    const entry = openWindows.get(winEl.id);
    if (winEl.classList.contains('maximized')) {
        winEl.classList.remove('maximized'); btn.setAttribute('data-glyph', '+');
        if (entry) { winEl.style.width = entry._restoredWidth; winEl.style.height = entry._restoredHeight; winEl.style.left = entry._restoredLeft; winEl.style.top = entry._restoredTop; }
    } else {
        if (entry) { entry._restoredLeft = winEl.style.left; entry._restoredTop = winEl.style.top; entry._restoredWidth = winEl.style.width; entry._restoredHeight = winEl.style.height; }
        winEl.classList.add('maximized'); btn.setAttribute('data-glyph', '−');
    }
    _syncBarsAutohide();
}

/**
 * Immersive mode: while a maximized window is on screen the top bar and the dock
 * slide out of the way (`:root.bars-autohide` in index.html) and come back when
 * the pointer touches the top/bottom edge strips.
 */
export function _syncBarsAutohide() {
    const anyMaximized = Array.from(openWindows.values()).some((w) => w.element
        && w.element.classList.contains('maximized')
        && !w.element.classList.contains('minimized')
        && !w.element.classList.contains('closing'));
    document.documentElement.classList.toggle('bars-autohide', anyMaximized);
    if (!anyMaximized) {
        _lastReveal = null;
        _peekBars(false);
    }
}

function _peekBars(on) {
    document.querySelector('.topbar')?.classList.toggle('peek', on);
    document.querySelector('.taskbar')?.classList.toggle('peek', on);
}

let _chromeWired = false;
let _barBands = { top: 42, dock: 52 };
let _lastReveal = null;

function _measureBarBands() {
    const topbar = document.querySelector('.topbar');
    const taskbar = document.querySelector('.taskbar');
    const dockVisible = taskbar && getComputedStyle(taskbar).display !== 'none';
    _barBands = {
        top: topbar?.offsetHeight || 42,
        dock: dockVisible ? (taskbar.offsetHeight || 52) : 0,
    };
}

/**
 * Wire immersive mode (idempotent). Reveal/hide is driven by pointer position
 * rather than enter/leave on the bars: the bars only slide under the pointer
 * once they are already revealed, so leave events never fire and `peek` stuck.
 */
export function initWindowChrome() {
    if (_chromeWired) return;
    _chromeWired = true;
    _measureBarBands();
    window.addEventListener('resize', _measureBarBands, { passive: true });

    document.addEventListener('pointermove', (e) => {
        if (!document.documentElement.classList.contains('bars-autohide')) {
            if (_lastReveal) { _lastReveal = null; _peekBars(false); }
            return;
        }
        // A 6 px hot edge (the .bar-trigger strips). Anything deeper would cover
        // the maximized window's own close/minimize buttons in the top band.
        const HOT = 6;
        const ghostY = window.innerHeight - HOT;
        const dockTop = _barBands.dock > 0 ? window.innerHeight - _barBands.dock : Infinity;
        const nearEdge = e.clientY <= HOT || e.clientY >= ghostY;
        const overVisibleBar = _lastReveal
            && (e.clientY <= _barBands.top || e.clientY >= dockTop);
        const reveal = nearEdge || overVisibleBar;
        if (reveal === _lastReveal) return;
        _lastReveal = reveal;
        _peekBars(reveal);
    }, { passive: true });

    document.addEventListener('pointerleave', () => {
        _lastReveal = null;
        _peekBars(false);
    });
}

export function startResize(winEl, direction, ev) {
    if (!winEl || !direction || winEl.classList.contains('maximized')) return;
    ev.preventDefault();
    ev.stopPropagation();
    focusWindow(winEl);
    const origin = _getCurrentPos(winEl);
    // Pointer origin is kept separate from the geometry: spreading _getCurrentPos
    // last would overwrite x/y with the window's position instead of the cursor.
    const start = { px: ev.clientX, py: ev.clientY, ...origin };
    winEl.style.transition = 'none';
    const onMove = (e) => {
        const dx = e.clientX - start.px;
        const dy = e.clientY - start.py;
        let { x, y, w, h } = start;
        if (direction.includes('e')) w = start.w + dx;
        if (direction.includes('s')) h = start.h + dy;
        if (direction.includes('w')) { w = start.w - dx; x = start.x + dx; }
        if (direction.includes('n')) { h = start.h - dy; y = start.y + dy; }
        // Clamp to the minimum size by pinning the opposite edge in place.
        if (w < MIN_WIN_W) { if (direction.includes('w')) x = start.x + start.w - MIN_WIN_W; w = MIN_WIN_W; }
        if (h < MIN_WIN_H) { if (direction.includes('n')) y = start.y + start.h - MIN_WIN_H; h = MIN_WIN_H; }
        x = Math.max(0, x);
        y = Math.max(0, y);
        w = Math.min(w, window.innerWidth - x);
        h = Math.min(h, _desktopBottom() - y);
        winEl.style.left = Math.round(x) + 'px';
        winEl.style.top = Math.round(y) + 'px';
        winEl.style.width = Math.round(w) + 'px';
        winEl.style.height = Math.round(h) + 'px';
    };
    const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        winEl.style.transition = '';
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
}

function _getCurrentPos(winEl) {
    return { x: parseFloat(winEl.style.left) || 0, y: parseFloat(winEl.style.top) || 0, w: parseFloat(winEl.style.width) || 0, h: parseFloat(winEl.style.height) || 0 };
}

/**
 * Bottom edge of the usable desktop. The dock is painted above windows, so a
 * window that slides under it loses its bottom resize grips and can never be
 * dragged back — clamp geometry here instead.
 */
function _desktopBottom() {
    const taskbar = document.querySelector('.taskbar');
    if (taskbar && getComputedStyle(taskbar).display === 'none') return window.innerHeight;
    const raw = getComputedStyle(document.documentElement).getPropertyValue('--taskbar-h').trim();
    const dockH = parseFloat(raw) || 52;
    return Math.max(240, window.innerHeight - dockH);
}

export function startDragWin(winEl) {
    const hdr = winEl.querySelector('.window-header');
    if (!hdr) return;
    let drag = { active: false, offX: 0, offY: 0, snapSide: null };
    let _lastClick = 0;

    hdr.onmousedown = e => {
        if (e.target.closest('.win-btn') || e.target.closest('.resize-handle')) return;
        const now = Date.now();
        if (now - _lastClick < 320) { e.preventDefault(); e.stopPropagation(); toggleMaximizeWin(winEl.querySelector('.maximize-btn')); _lastClick = 0; return; }
        _lastClick = now;
        e.preventDefault(); e.stopPropagation();
        if (winEl.classList.contains('maximized')) return;
        if (_pendingSnap && _pendingSnap.winEl === winEl) {
            const t = _snapTarget(winEl, _pendingSnap.side);
            winEl.style.left = t.left; winEl.style.top = t.top; winEl.style.width = t.width; winEl.style.height = t.height;
            const entry = openWindows.get(winEl.id);
            if (entry) { entry._restoredLeft = winEl.style.left; entry._restoredTop = winEl.style.top; entry._restoredWidth = winEl.style.width; entry._restoredHeight = winEl.style.height; }
            _pendingSnap = null; _hideSnapPreview();
        } else { drag.active = true; winEl.classList.remove('opening', 'closing', 'minimizing', 'restoring'); winEl.style.transition = 'none'; drag.offX = e.clientX - parseFloat(winEl.style.left) || 0; drag.offY = e.clientY - parseFloat(winEl.style.top) || 0; hdr.classList.add('dragging'); focusWindow(winEl); }

        const onMove = ev => {
            if (!drag.active) return; ev.preventDefault();
            let nx = ev.clientX - drag.offX, ny = Math.max(0, ev.clientY - drag.offY);
            if (nx < 0) nx = 0; if (ny < 0) ny = 0;
            const vw = window.innerWidth;
            if (nx + parseFloat(winEl.style.width) > vw) nx = vw - parseFloat(winEl.style.width);
            // Never park a window under the dock — its controls (and the bottom
            // resize grips) would become unreachable.
            const maxTop = _desktopBottom() - parseFloat(winEl.style.height);
            if (ny > maxTop) ny = Math.max(0, maxTop);
            winEl.style.left = nx + 'px'; winEl.style.top = ny + 'px';
            const pos = _getCurrentPos(winEl), threshold = SNAP_THRESHOLD;
            let snapSide = null;
            if (pos.y <= threshold && ev.clientY <= 48) snapSide = 'top';
            else if (pos.x <= threshold && pos.y > 0) snapSide = 'left';
            else { const vw2 = window.innerWidth; if (pos.x + pos.w >= vw2 - threshold) snapSide = 'right'; }
            const preview = _ensureSnapPreview();
            if (snapSide && snapSide !== drag.snapSide) { drag.snapSide = snapSide; _pendingSnap = { winEl, side: snapSide }; const t = _snapTarget(winEl, snapSide); Object.assign(preview.style, { left: t.left, top: t.top, width: t.width, height: t.height }); preview.classList.add('active'); }
            else if (!snapSide && drag.snapSide) { drag.snapSide = null; _pendingSnap = null; preview.classList.remove('active'); }
        };

        const onUp = () => {
            if (drag.active) {
                if (_pendingSnap && _pendingSnap.winEl === winEl) {
                    const t = _snapTarget(winEl, _pendingSnap.side);
                    winEl.style.transition = 'all ' + SNAP_ANIM_MS + 'ms cubic-bezier(0.16, 1, 0.3, 1)';
                    winEl.style.left = t.left; winEl.style.top = t.top; winEl.style.width = t.width; winEl.style.height = t.height;
                    const entry = openWindows.get(winEl.id);
                    if (entry) { entry._restoredLeft = winEl.style.left; entry._restoredTop = winEl.style.top; entry._restoredWidth = winEl.style.width; entry._restoredHeight = winEl.style.height; }
                    focusWindow(winEl);
                } else { winEl.style.transition = ''; }
                _pendingSnap = null; _hideSnapPreview();
            }
            drag.active = false; hdr.classList.remove('dragging');
            document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp);
        };
        document.addEventListener('mousemove', onMove); document.addEventListener('mouseup', onUp);
    };

    // Touch support
    hdr.ontouchstart = e => { if (e.target.closest('.win-btn')) return; const t = e.touches[0]; if (winEl.classList.contains('maximized')) return; drag.active = true; winEl.style.transition = 'none'; focusWindow(winEl); drag.offX = t.clientX - parseFloat(winEl.style.left) || 0; drag.offY = t.clientY - parseFloat(winEl.style.top) || 0; const onMove = ev => { if (!drag.active) return; const tt = ev.touches[0]; let nx = tt.clientX - drag.offX, ny = Math.max(0, tt.clientY - drag.offY); if (nx < 0) nx = 0; if (ny < 0) ny = 0; winEl.style.left = nx + 'px'; winEl.style.top = ny + 'px'; }; const onUp = () => { drag.active = false; winEl.style.transition = ''; document.removeEventListener('touchmove', onMove); document.removeEventListener('touchend', onUp); }; document.addEventListener('touchmove', onMove, { passive: false }); document.addEventListener('touchend', onUp); };

    // Resize grips — wired once per window element.
    winEl.querySelectorAll('.resize-handle').forEach((handle) => {
        if (handle.dataset.wired === '1') return;
        handle.dataset.wired = '1';
        handle.addEventListener('mousedown', (e) => startResize(winEl, handle.dataset.dir, e));
    });
}

// ── Taskbar helpers ──

let taskbarApps = [];

export function addToTaskbar(id, icon, title) { taskbarApps.push({ id, icon, title }); renderTaskbar(); }

export function renderTaskbar() {
    // Structural render (bootstrap, open/close, docker count changed). Focus
    // changes go through updateTaskbarActive() instead.
    const tb = document.getElementById('taskbar'); if (!tb) return;
    tb.querySelectorAll('.taskbar-item').forEach((el) => el.remove());

    let focusedWinId = null;
    for (const [k, v] of openWindows.entries()) { if (v.element.classList.contains('focused')) { focusedWinId = k; break; } }
    const windowsArray = Array.from(openWindows.values());
    for (var i = 0; i < windowsArray.length; i++) {
        var w = windowsArray[i];
        const isMinimized = w.element.classList.contains('minimized');
        const isActive = focusedWinId === w.element.id && !isMinimized;
        var btn = document.createElement('div');
        btn.className = 'taskbar-item' + (isActive ? ' active' : '') + (isMinimized ? ' minimized-window' : '');
        btn.setAttribute('data-win-id', w.element.id);
        btn.innerHTML = `<span class="taskbar-item-icon">${w.icon}</span><span>${escapeHtml(w.title)}</span>`;
        var targetEl = w.element;
        btn.onclick = function () { if (targetEl.classList.contains('minimized')) restoreWindowWin(targetEl); else if (targetEl.classList.contains('focused')) minimizeWindowWin(targetEl.querySelector('.minimize-btn')); else focusWindow(targetEl); };
        btn.oncontextmenu = function (e) { e.preventDefault(); e.stopPropagation(); showTaskbarContextMenu(e.clientX, e.clientY, targetEl); };
        tb.appendChild(btn);
    }
    const sys = document.createElement('div'); sys.className = 'taskbar-item taskbar-system';
    sys.innerHTML = `<span class="taskbar-item-icon">🖥️</span><span>EthOS NAS</span>`; tb.appendChild(sys);
    const rc = document.getElementById('docker-running');
    if (rc && rc.textContent !== '—') sys.querySelector('span:last-child').textContent += ` (${rc.textContent} running)`;
}

let _taskbarMenu = null;
export function showTaskbarContextMenu(x, y, winEl) {
    if (_taskbarMenu) { _taskbarMenu.remove(); _taskbarMenu = null; }
    const entry = winEl ? openWindows.get(winEl.id) : null;
    if (!entry) return;
    _taskbarMenu = document.createElement('div');
    Object.assign(_taskbarMenu.style, { position: 'fixed', left: x + 'px', top: y + 'px', zIndex: 9000, background: 'var(--surface-1)', border: '1px solid var(--border)', borderRadius: '10px', padding: '4px 0', minWidth: '180px', boxShadow: '0 16px 48px rgba(0,0,0,0.5)', fontFamily: "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif" });
    const items = [
        { label: entry.element.classList.contains('minimized') ? '▶ Restore' : (entry.element.classList.contains('maximized') ? '⊡ Restore size' : '❐ Maximize'), action() { if (entry.element.classList.contains('minimized')) restoreWindowWin(winEl); else toggleMaximizeWin(winEl.querySelector('.maximize-btn')); } },
        { label: '✕ Close', action() { closeWindowWin(winEl.querySelector('.close-btn')); } },
    ];
    items.forEach(it => {
        const el = document.createElement('div');
        Object.assign(el.style, { padding: '8px 16px', fontSize: '13px', color: 'var(--text-1)', cursor: 'pointer', transition: 'background 0.1s' });
        el.textContent = it.label; el.onmouseenter = () => el.style.background = 'rgba(91,154,255,0.15)'; el.onmouseleave = () => el.style.background = 'transparent';
        el.onclick = () => { _taskbarMenu.remove(); _taskbarMenu = null; it.action(); };
        _taskbarMenu.appendChild(el);
    });
    document.body.appendChild(_taskbarMenu);
    const closeMenu = () => { if (_taskbarMenu) { _taskbarMenu.remove(); _taskbarMenu = null; } document.removeEventListener('click', closeMenu); }; setTimeout(() => document.addEventListener('click', closeMenu), 10);
}

// Desktop click to unfocus all windows (works even with pointer-events:none on overlay,
// because clicks pass through to the desktop and still bubble up to document)
document.addEventListener('mousedown', e => {
    if (e.target.closest('.app-window') || e.target.closest('.taskbar') || e.target.closest('#topbar')) return;
    for (const w of openWindows.values()) w.element.classList.remove('focused');
    renderTaskbar();
});

export function closeCurrentModal() { _closeAllWindows(); }
function _closeAllWindows() { openWindows.forEach(v => v.element.remove()); openWindows.clear(); taskbarApps = []; const o = document.getElementById('modalOverlay'); if (o) o.classList.remove('visible'); renderTaskbar(); }


// ════════════════════════════════════════════
// Topbar: user menu dropdown + logout / restart / reboot
// ════════════════════════════════════════════

export function initTopbarMenu() {
    const userBtn = document.querySelector('[data-testid="topbar-user-menu-btn"]');
    const dropdown = document.querySelector('[data-testid="topbar-dropdown"]');
    if (!userBtn || !dropdown) return;

    // Always show the avatar button — content inside updates after auth check
    userBtn.style.display = 'flex';

    // Logout handler
    const logoutBtn = dropdown.querySelector('[data-testid="topbar-logout"]');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', () => {
            closeTopbarDropdown();
            localStorage.removeItem('token');
            localStorage.removeItem('user');
            document.getElementById('loginOverlay').style.display = 'flex';
        });
    }

    // Restart app handler — confirm, then full-screen overlay with countdown + auto-refresh
    const restartBtn = dropdown.querySelector('[data-testid="topbar-restart-app"]');
    if (restartBtn) {
        restartBtn.addEventListener('click', async () => {
            closeTopbarDropdown();
            const ok = await window.showModalConfirm('Restart App',
                '⚠️ Restart EthOS NAS?\n\n' +
                'The web server will restart and this page will refresh automatically.'
            );
            if (!ok) return;
            try {
                const res = await apiFetch('/api/system/restart', { method: 'POST' });
                if (res.ok) showRestartOverlay();
                else {
                    let detail = '';
                    try { detail = (await res.json()).detail || ''; } catch (_) {}
                    showUnifiedToast(detail || 'Restart failed', 'error');
                }
            } catch (_) {
                showUnifiedToast('Could not reach the server', 'error');
            }
        });
    }

    // Reboot server handler — requires confirmation dialog, uses unified toast for feedback
    const rebootBtn = dropdown.querySelector('[data-testid="topbar-reboot-server"]');
    if (rebootBtn) {
        rebootBtn.addEventListener('click', async () => {
            closeTopbarDropdown();
            const ok = await window.showModalConfirm('Reboot Server',
                '⚠️ Reboot Server\n\n' +
                'This will shut down and restart the entire server.\n' +
                'All connections will be lost. Continue?'
            );
            if (!ok) return;
            try {
                const res = await apiFetch('/api/system/shutdown', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'reboot', confirm: true }),
                });
                if (res.ok) showUnifiedToast('Server is rebooting...', 'info');
                else { const d = await res.json(); showUnifiedToast(d.detail || 'Reboot failed', 'error'); }
            } catch (_) {}
        });
    }

    // Toggle dropdown on button click
    userBtn.addEventListener('click', e => {
        e.stopPropagation();
        dropdown.classList.toggle('open');
    });

    // Close on outside click or Escape
    document.addEventListener('mousedown', e => {
        if (dropdown.classList.contains('open') && !e.target.closest('.topbar-user-chip') && !e.target.closest('.topbar-dropdown')) {
            closeTopbarDropdown();
        }
    });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape' && dropdown.classList.contains('open')) closeTopbarDropdown();
    });

    // Fetch user info to update username display in dropdown header (needs JWT → apiFetch)
    apiFetch('/api/auth/me')
        .then(r => r.ok ? r.json() : null)
        .then(data => {
            if (!data || data.error) return;
            const uname = String(data.username || 'user');
            const initials = _initials(uname);
            for (const id of ['topbar-username-display', 'topbar-username-short']) {
                const el = document.getElementById(id); if (el) el.textContent = uname;
            }
            for (const id of ['topbar-avatar', 'topbar-avatar-lg']) {
                const el = document.getElementById(id); if (el) el.textContent = initials;
            }
        })
        .catch(() => {});
}

function _initials(name) {
    const parts = String(name).trim().split(/\s+/).filter(Boolean);
    if (!parts.length) return '?';
    return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

export function closeTopbarDropdown() {
    const dropdown = document.querySelector('[data-testid="topbar-dropdown"]');
    if (dropdown) dropdown.classList.remove('open');
}


// ════════════════════════════════════════════
// Restart overlay — countdown + health probe, then page refresh
// ════════════════════════════════════════════

export function showRestartOverlay() {
    if (document.getElementById('restartOverlay')) return; // already showing

    const COUNTDOWN_START = 6;  // seconds of grace before switching to "waiting" state
    const WAIT_TIMEOUT = 90;    // give up after this many seconds of waiting

    const overlay = document.createElement('div');
    overlay.id = 'restartOverlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:10001;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.78);backdrop-filter:blur(10px);animation:fadeIn .2s ease-out;';
    overlay.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;gap:14px;padding:36px 48px;border-radius:18px;background:var(--surface-0);border:1px solid var(--border);box-shadow:0 24px 64px rgba(0,0,0,.5);min-width:320px;">
            <div class="spinner" style="width:34px;height:34px;border-width:3px;"></div>
            <div id="restartOverlayTitle" style="font-size:16px;font-weight:600;color:var(--text-1);">Restarting EthOS NAS…</div>
            <div id="restartOverlayCount" style="font-size:44px;font-weight:700;color:var(--accent,#5b9aff);line-height:1;font-variant-numeric:tabular-nums;">${COUNTDOWN_START}</div>
            <div id="restartOverlayHint" style="font-size:12px;color:var(--text-3);">This page will refresh automatically</div>
        </div>`;
    document.body.appendChild(overlay);

    const titleEl = overlay.querySelector('#restartOverlayTitle');
    const countEl = overlay.querySelector('#restartOverlayCount');
    const hintEl  = overlay.querySelector('#restartOverlayHint');

    let remaining = COUNTDOWN_START;
    let waited = 0;
    let done = false;

    // Any HTTP response (even a 401) means the server is back up.
    // Any 4xx from the app itself (e.g. 401 unauthenticated) proves uvicorn is
    // back up — but a reverse proxy in front of :9000 answers 502/503 during the
    // re-exec gap, so only non-5xx responses count as "up".
    async function serverUp() {
        try { const r = await fetch('/api/system/status', { method: 'GET' }); return r.status < 500; }
        catch (_) { return false; }
    }

    const timer = setInterval(async () => {
        if (done) return;
        if (await serverUp()) { done = true; clearInterval(timer); location.reload(); return; }
        if (remaining > 1) {
            remaining -= 1;
            countEl.textContent = String(remaining);
        } else {
            // Countdown finished — switch to waiting state
            titleEl.textContent = 'Waiting for server…';
            hintEl.textContent = 'The server is taking longer than expected to come back';
            waited += 1;
            countEl.textContent = `${waited}s`;
            if (waited >= WAIT_TIMEOUT) {
                done = true; clearInterval(timer);
                titleEl.textContent = 'Server did not come back';
                hintEl.innerHTML = '<button id="restartOverlayRetry" style="margin-top:6px;padding:8px 20px;font-size:13px;border-radius:9px;border:none;background:var(--accent,#5b9aff);color:white;cursor:pointer;">Try reloading now</button>';
                overlay.querySelector('#restartOverlayRetry').addEventListener('click', () => location.reload());
            }
        }
    }, 1000);
}


// ════════════════════════════════════════════
// Toast notifications — replaced by unified NotificationCenter
// import { showUnifiedToast } from './notification-center.js' at top of file
// ════════════════════════════════════════════
