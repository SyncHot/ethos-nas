// EthOS NAS — Bootstrap (ES modules entry point)
import { renderDesktopIcons, refreshDesktopIcons, setPkgIconData, initDesktopCtxMenu, initDesktopDropZone, initDesktopReorder, _showDesktopCtx as _showDc, isAppVisible } from './core/registry.js';
import { loadRemoteLayout, ensureTrashPinned } from './core/desktop-layout.js';
import { loadRemoteWallpaper } from './core/wallpaper.js';
import { initDesktopTrash, openTrashFromDesktop } from './core/desktop-trash.js';
window._showDesktopCtx = _showDc;  // needed for inline oncontextmenu handlers

// ── App Drawer ────────────────────────────────────────────────────────────
import { initAppDrawer, refreshDrawerPackages } from './apps/app-drawer.js';
import { openApp as _openApp, openWindows, closeCurrentModal, closeWindowWin as _closeWindowWin, initTopbarMenu, initWindowChrome, renderTaskbar, setPackageIdentity, registerCloseGuard as _registerCloseGuard, focusWindow as _focusWindow, showRestartOverlay } from './core/window-manager.js';
import { apiFetch, apiJsonFetch, apiRequest } from './core/api.js';
import { pickFile, pickFolder } from './core/file-picker.js';
import { connectWs as _connectWs } from './core/ws.js';
import { resolveDefaultTerminalTarget as _resolveTerminalTarget } from './core/terminal-target.js';
import { fetchLogs as _fetchLogs } from './core/log-stream.js';

// ── Unified Components (Task 3) ───────────────────────────────────────────────
import { showConfirm } from './core/unified-confirm.js';
import { showAlert, showConfirm as showModalConfirm, showPrompt, showFormModal } from './core/modal.js';
import { startTask } from './core/progress.js';
import { escapeHtml, escapeAttr, jsAttr } from './core/utils.js';
import { log as _logDiagnostic } from './core/logger.js';
import { addNotification, dismissNotification, toggleNotificationDrawer, showToast, startNotificationPolling, pollUnreadCount, openNotifications } from './core/notification-center.js';

// ── File Manager ──────────────────────────────────────────────────────────────
import { 
    initFileManager, fmWindowStates, toggleSidebar as _toggleSidebar, fmLoad as _fmLoad, 
    fmNavigateTo as _fmNavigateTo, fmOpenItem as _fmOpenItem, fmGoUp as _fmGoUp, 
    fmNewFolder as _fmNewFolder, fmNewFile as _fmNewFile, fmDeleteSelected as _fmDeleteSelected, 
    fmCopySelected as _fmCopySelected, fmCutSelected as _fmCutSelected, fmPaste as _fmPaste, 
    fmSelectAll as _fmSelectAll, fmShowItemContextMenu as _fmShowItemContextMenu, 
    fmSearch as _fmSearch, fmUploadFiles as _fmUploadFiles,
    fmRenameSelected as _fmRenameSelected,
    fmOpenVideoPlayer as _fmOpenVideoPlayer, fmCloseVideoPlayer as _fmCloseVideoPlayer,
    fmVideoPrev as _fmVideoPrev, fmVideoNext as _fmVideoNext, fmVideoDelete as _fmVideoDelete,
    fmOpenTrash as _fmOpenTrash, fmTrashRestore as _fmTrashRestore, fmEmptyTrash as _fmEmptyTrash
} from './apps/files.js';

import { fmToggleHidden as _fmToggleHidden, fmSetViewMode as _fmSetViewMode } from './apps/fm-controls.js';

import { renderProcessesPage } from './apps/processes.js';
import { renderLogsPage, openLogs } from './apps/logs.js';
import { renderPackagesPage } from './apps/packages.js';
import { renderTerminalPage } from './apps/terminal.js';
import { renderSettingsPage } from './apps/settings.js';
import { renderOverviewPage } from './apps/overview.js';
import { renderStoragePage } from './apps/storage.js';
import { renderNetworkPage } from './apps/network.js';
import { renderSmartPage } from './apps/smart.js';
import { renderSystemdPage } from './apps/systemd.js';
import { renderServicesPage } from './apps/services.js';
import { renderCronPage } from './apps/cron.js';
import { renderSearchPage } from './apps/search.js';
import { renderShareLinksPage } from './apps/share-links.js';
import { renderAccessControlPage } from './apps/access-control.js';

window._rawOpenApp = _openApp;

// ── Expose Unified Components globally (accessible via window.showConfirm etc.) ─
window.showConfirm = showConfirm;
window.showAlert = showAlert;
window.showModalConfirm = showModalConfirm;
window.showPrompt = showPrompt;
window.addNotification = addNotification;
window.dismissNotification = dismissNotification;
window.toggleNotificationDrawer = toggleNotificationDrawer;
window.showToast = showToast;
window.startNotificationPolling = startNotificationPolling;  // called by login.js after overlay login
window.pollUnreadCount = pollUnreadCount;                    // exposed for tests / manual refresh

// Expose state containers globally for fm-controls.js (avoids cross-module Map duplication)
window.__openWindows = openWindows;
window.__fmWindowStates = fmWindowStates;

const RENDERERS = {
    storage: renderStoragePage,
    overview: renderOverviewPage,
    files: initFileManager, terminal: renderTerminalPage, network: renderNetworkPage, settings: renderSettingsPage,
    processes: renderProcessesPage, logs: renderLogsPage, packages: renderPackagesPage,
    smart: renderSmartPage,
    services: renderServicesPage,
    systemd: renderSystemdPage,
    cron: renderCronPage,
    security: renderAccessControlPage,
    search: renderSearchPage,
    'shared-links': renderShareLinksPage,
};

function _openAppWrapper(appId) {
    const fn = RENDERERS[appId];
    if (fn && window._rawOpenApp) { window._rawOpenApp(appId, fn); }
}

window.openApp = _openAppWrapper;
window.closeCurrentModal = closeCurrentModal;
window._closeAllWindows = function() {};

// ── Expose File Manager functions globally ────────────────────────────────────
window.toggleSidebar = _toggleSidebar; window.fmNavigateTo = _fmNavigateTo;
window.fmOpenItem = _fmOpenItem; window.fmGoUp = _fmGoUp;
window.fmNewFolder = _fmNewFolder; window.fmNewFile = _fmNewFile;
window.fmDeleteSelected = _fmDeleteSelected; window.fmCopySelected = _fmCopySelected;
window.fmCutSelected = _fmCutSelected; window.fmPaste = _fmPaste;
window.fmSelectAll = _fmSelectAll; window.fmShowItemContextMenu = _fmShowItemContextMenu;
window.fmSearch = _fmSearch; window.fmUploadFiles = _fmUploadFiles;
window.fmToggleHidden = _fmToggleHidden; window.fmSetViewMode = _fmSetViewMode;
window.fmRenameSelected = _fmRenameSelected;
window.fmOpenVideoPlayer = _fmOpenVideoPlayer;
window.openEditorForPath = (path) => {
    // The editor moved to Packages; reuse its instance when it is loaded.
    if (typeof window.__ethosEditorOpen === 'function') { window.__ethosEditorOpen(path); return; }
    if (path) window.__fmEditorCtx = { path };
    window.openApp('editor');
};
window.openGitDiffFor = (repo, path, side = 'changes') => {
    if (!repo) return;
    if (typeof window.__ethosGitDiffOpen === 'function') { window.__ethosGitDiffOpen(repo, path, side); return; }
    window.__gitDiffRequest = { repo, path, side };
    window.openApp('git-diff');
};
window.fmCloseVideoPlayer = _fmCloseVideoPlayer;
window.fmVideoPrev = _fmVideoPrev;
window.fmVideoNext = _fmVideoNext;
window.fmVideoDelete = _fmVideoDelete;
window.fmOpenTrash = _fmOpenTrash;
window.fmTrashRestore = _fmTrashRestore;
window.fmEmptyTrash = _fmEmptyTrash;
window.fmLoad = _fmLoad;

// ── Render desktop + dynamic icons ────────────────────────────────────────────
// First paint comes from the local cache; then this account's layout is
// restored from the server (per-user persistence) and re-rendered if applied.
renderDesktopIcons();
loadRemoteLayout().then((layout) => {
    // The desktop is empty by design; seed the Trash shortcut once per user so
    // it is available without digging through the App Drawer.
    const seeded = ensureTrashPinned();
    if (layout || seeded) refreshDesktopIcons();
});
loadRemoteWallpaper();  // per-user desktop wallpaper (server is source of truth)
initDesktopCtxMenu();  // T-MW: event delegation for right-click context menu on desktop icons
initDesktopDropZone(); // App Drawer → desktop drag-and-drop (pin apps)
initDesktopReorder();  // drag desktop icons to reorder them

async function _loadDynamicIcons() {
    try {
        const resp = await fetch('/api/packages/desktop-icons');
        if (!resp.ok) return;
        const icons = await resp.json();
        setPkgIconData(icons); // caches + re-renders desktop in pin order
        setPackageIdentity(icons); // window headers/taskbar use the same name + icon
        refreshDrawerPackages(); // keep the App Drawer in sync with Packages
    } catch { /* best-effort: dynamic icons are optional */ }
}
_loadDynamicIcons().catch(() => {});
// Packages uses this after install/update/uninstall to refresh desktop icons.
window.refreshPackageIcons = _loadDynamicIcons;

// Initialize app drawer (taskbar "Show Applications" button + fullscreen grid)
initAppDrawer();
renderTaskbar(); // ensure the Show Applications button exists before any window opens
startNotificationPolling(); // opens /ws/notifications + 60 s safety-net badge poll
initDesktopTrash(); // Trash shortcut badge + drop-to-trash target

// ── Generic package window renderer (for dynamic pkg-* apps from desktop icons) ─
async function _renderNativePackage(pkg, container) {
    // Title and header icon come from the manifest; setPackageIdentity also
    // keeps the taskbar entry and instance numbering in sync.
    setPackageIdentity([pkg]);
    container.style.padding = '0';
    container.style.overflow = 'hidden';
    container.textContent = 'Loading package UI…';
    try {
        const response = await apiFetch(`/api/packages/${encodeURIComponent(pkg.id)}/ui/${pkg.ui.entry}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const source = await response.text();
        const blobUrl = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
        let module;
        try {
            module = await import(/* webpackIgnore: true */ blobUrl);
        } finally {
            URL.revokeObjectURL(blobUrl);
        }
        const render = typeof module.render === 'function' ? module.render
            : (typeof module.default === 'function' ? module.default : null);
        if (!render) throw new Error('Module does not export render(container, ctx)');
        const base = `/api/packages/${encodeURIComponent(pkg.id)}`;
        const ctx = {
            pkg,
            apiFetch,
            apiJsonFetch,
            pickFile,
            pickFolder,
            notify: addNotification,
            // Global Notification Center (docs/notifications.md) — one toast stack,
            // one history sheet. Packages never render their own notification UI.
            notifications: {
                toast: (message, kind = 'info') => addNotification(message, kind),
                emit: (note = {}) => apiRequest('/api/notifications', {
                    method: 'POST',
                    body: { kind: 'info', ...note, package_id: pkg.id },
                }),
                open: (filter) => openNotifications(filter ? { filter } : {}),
            },
            // Shared UI so native packages do not hand-roll progress bars or modals.
            progress: { startTask },
            modal: { confirm: showConfirm, alert: showAlert, form: showFormModal },
            escape: { html: escapeHtml, attr: escapeAttr, js: jsAttr },
            // Shell services a packaged app may need (window bookkeeping, a
            // terminal/WS target, the unified log reader). Pass-throughs only —
            // the core keeps ownership of the window manager and log store.
            windows: { list: openWindows, registerCloseGuard: _registerCloseGuard, focusWindow: _focusWindow, renderTaskbar, showRestartOverlay },
            ws: { connect: _connectWs },
            terminal: { resolveDefaultTarget: _resolveTerminalTarget },
            logs: { fetch: _fetchLogs, open: openLogs },
            // Unified logging (docs/logs.md). The server verifies `pkg.id` and
            // stores the entry as `pkg:<id>`; packages never keep their own log
            // store or viewer.
            log: (level, message, detail = '') => _logDiagnostic(level, message, detail, { packageId: pkg.id }),
            logger: {
                debug: (message, detail = '') => _logDiagnostic('debug', message, detail, { packageId: pkg.id }),
                info: (message, detail = '') => _logDiagnostic('info', message, detail, { packageId: pkg.id }),
                warn: (message, detail = '') => _logDiagnostic('warning', message, detail, { packageId: pkg.id }),
                error: (message, detail = '') => _logDiagnostic('error', message, detail, { packageId: pkg.id }),
            },
            proxy: (path, options) => apiJsonFetch(`${base}/proxy/${String(path || '').replace(/^\/+/, '')}`, options),
            reload: () => _renderNativePackage(pkg, container),
        };
        container.replaceChildren();
        await render(container, ctx);
    } catch (err) {
        const box = document.createElement('div');
        box.style.cssText = 'padding:28px;color:#f87171;font:13px system-ui,sans-serif;line-height:1.6;';
        box.textContent = `Nie udało się załadować UI pakietu: ${err?.detail || err?.message || err}`;
        container.replaceChildren(box);
    }
}

async function _renderGenericPackage(winId, container = null) {
    const winEl = container || document.querySelector(`[data-testid="window-${winId}"] .window-body`);
    if (!winEl) return;
    // Extract package id from appId: "pkg-jellyfin" → "jellyfin"
    const pkgId = (winId || '').replace(/^pkg-/, '');
    try {
        const pkg = await apiJsonFetch(`/api/packages/${encodeURIComponent(pkgId)}`);
        setPackageIdentity([pkg]);

        // Try to get current docker status for this package (read-only endpoint)
        let dockerStatus = 'not_configured';
        try {
            const status = await apiJsonFetch(`/api/packages/${encodeURIComponent(pkgId)}/status`);
            dockerStatus = status.status || 'not_configured';
        } catch { /* best-effort: status probe is optional */ }

        if (pkg.ui && pkg.ui.mode === 'native' && pkg.ui.entry) {
            await _renderNativePackage(pkg, winEl);
            return;
        }

        let badgeClass;
        if (dockerStatus === 'running') badgeClass = 'badge-green';
        else if (dockerStatus === 'stopped' || dockerStatus === 'exited') badgeClass = 'badge-yellow';
        else badgeClass = 'badge-blue';
        let badgeText;
        if (dockerStatus === 'running') badgeText = 'Running';
        else if (dockerStatus === 'stopped') badgeText = 'Stopped';
        else if (dockerStatus === 'error') badgeText = 'Error';
        else badgeText = 'N/A (no compose)';

        const el = (tag, attrs = {}, ...children) => {
            const node = document.createElement(tag);
            for (const [k, v] of Object.entries(attrs)) {
                if (v == null) continue;
                if (k === 'testid') node.setAttribute('data-testid', v);
                else if (k === 'class') node.className = v;
                else node.setAttribute(k, String(v));
            }
            for (const c of children) if (c) node.appendChild(c);
            return node;
        };
        const txt = (t) => document.createTextNode(t);

        const icon = pkg.icon || '📦';
        const name = pkg.name || pkgId;
        const headerTitle = el('span', { style: 'display:flex;align-items:center;gap:8px;font-size:18px;font-weight:700;color:#f8fafc;' });
        if (pkg.icon_url) {
            const iconImg = document.createElement('img');
            iconImg.src = pkg.icon_url;
            iconImg.alt = '';
            iconImg.style.cssText = 'width:22px;height:22px;border-radius:5px;object-fit:cover;display:block;';
            headerTitle.appendChild(iconImg);
        } else {
            headerTitle.appendChild(txt(icon));
        }
        headerTitle.appendChild(txt(name));
        const root = el('div', { style: 'display:flex;flex-direction:column;height:100%;background:#111827;font-family:system-ui,sans-serif;color:#e2e8f0;' });

        // Header
        root.appendChild(el('div', { style: 'padding:16px 20px;border-bottom:1px solid rgba(255,255,255,.06);display:flex;align-items:center;justify-content:space-between;' },
            headerTitle,
            el('span', { class: badgeClass, testid: `pkg-status-${pkg.id}`, style: 'padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;text-transform:uppercase;' }, txt(badgeText))));

        // Packages that expose a web UI open it inside the EthOS window.
        const uiPort = Number(pkg.ui && pkg.ui.port) || 0;
        if (uiPort > 0 && dockerStatus !== 'stopped') {
            const scheme = pkg.ui.scheme || 'http';
            const uiPath = pkg.ui.path || '/';
            const loopback = ['127.0.0.1', '::1', '[::1]', '0.0.0.0'];
            const uiHost = loopback.includes(location.hostname) ? 'localhost' : location.hostname;
            const uiUrl = `${scheme}://${uiHost}:${uiPort}${uiPath}`;
            // Package window UIs cannot see the EthOS <html class>, so the theme
            // travels in the query string and the frame reloads on toggle.
            const currentTheme = () => (document.documentElement.classList.contains('dark') ? 'dark' : 'light');
            const themedUiUrl = (theme) => `${uiUrl}${uiUrl.includes('?') ? '&' : '?'}theme=${theme}`;
            const toolbar = el('div', { style: 'padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.06);display:flex;gap:8px;justify-content:flex-end;background:rgba(15,23,42,.6);' });
            const openTab = el('button', { class: 'btn-ghost', testid: `pkg-open-tab-${pkg.id}`, style: 'padding:5px 10px;font-size:11px;border-radius:7px;' }, txt('Open in new tab'));
            openTab.addEventListener('click', () => window.open(themedUiUrl(currentTheme()), '_blank', 'noopener'));
            const reload = el('button', { class: 'btn-ghost', testid: `pkg-reload-ui-${pkg.id}`, style: 'padding:5px 10px;font-size:11px;border-radius:7px;' }, txt('Reload UI'));
            reload.addEventListener('click', () => {
                frame.src = 'about:blank';
                setTimeout(() => { frame.src = themedUiUrl(currentTheme()); }, 60);
            });
            toolbar.append(reload, openTab);
            const frame = el('iframe', {
                testid: `pkg-ui-frame-${pkg.id}`,
                src: themedUiUrl(currentTheme()),
                title: `${name} interface`,
                sandbox: 'allow-scripts allow-forms allow-same-origin allow-popups allow-downloads',
                style: 'flex:1;width:100%;min-height:0;border:0;background:#0b1220;display:block;',
            });
            root.append(toolbar, frame);
            winEl.replaceChildren(root);
            frame.dataset.theme = currentTheme();
            const themeObserver = new MutationObserver(() => {
                if (!document.body.contains(root)) { themeObserver.disconnect(); return; }
                const theme = currentTheme();
                if (frame.dataset.theme === theme) return;
                frame.dataset.theme = theme;
                frame.src = themedUiUrl(theme);
            });
            themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
            return;
        }

        // Content
        const content = el('div', { style: 'flex:1;overflow-y:auto;padding:24px 20px;display:flex;flex-direction:column;align-items:center;gap:16px;' },
            el('div', { style: 'font-size:56px;line-height:1;' }, txt(icon)),
            el('div', { style: 'text-align:center;' },
                el('div', { style: 'font-size:20px;font-weight:700;color:#f8fafc;margin-bottom:4px;' }, txt(name)),
                el('div', { style: 'font-size:13px;color:#64748b;' }, txt(`v${pkg.version || '?'}`))));
        if (pkg.description) content.appendChild(el('p', { style: 'color:#94a3b8;font-size:14px;text-align:center;max-width:320px;line-height:1.5;margin-top:8px;' }, txt(pkg.description)));
        const metaRow = el('div', { style: 'display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:8px;' });
        if (pkg.category) metaRow.appendChild(el('span', { class: 'badge-blue', style: 'padding:4px 10px;border-radius:6px;font-size:11px;font-weight:600;text-transform:uppercase;' }, txt(pkg.category)));
        if (pkg.author) metaRow.appendChild(el('span', { style: 'color:#64748b;font-size:12px;padding:4px 10px;' }, txt(`by ${pkg.author}`)));
        content.appendChild(metaRow);

        // Docker actions
        const dockerRow = el('div', { style: 'display:flex;gap:10px;margin-top:16px;flex-wrap:wrap;justify-content:center;' });
        if (dockerStatus === 'not_configured') {
            dockerRow.appendChild(el('p', { style: 'color:#64748b;font-size:13px;' }, txt('No Docker services configured for this package.')));
        } else {
            for (const [act, label, color] of [['start', '▶ Start', '#22c55e'], ['restart', '↻ Restart', '#eab308'], ['stop', '⏹ Stop', '#ef4444']]) {
                const btn = el('button', { class: 'btn-ghost', testid: `pkg-action-${pkg.id}-${act}`, style: `padding:8px 16px;font-size:12px;border-radius:8px;color:${color};` }, txt(label));
                btn.addEventListener('click', () => window._pkgAction(pkgId, act, btn));
                dockerRow.appendChild(btn);
            }
        }
        content.appendChild(dockerRow);

        // Uninstall
        const unBtn = el('button', { class: 'btn-primary', style: 'padding:8px 18px;font-size:13px;border-radius:8px;background:#ef4444;color:white;border:none;cursor:pointer;' }, txt('Uninstall'));
        unBtn.addEventListener('click', () => window._uninstallPkg(pkgId));
        content.appendChild(el('div', { style: 'margin-top:20px;padding-top:16px;border-top:1px solid rgba(255,255,255,.06);' }, unBtn));

        if (!pkg.description) content.appendChild(el('p', { style: 'color:#64748b;font-size:12px;text-align:center;margin-top:12px;' }, txt('No description available.')));
        root.appendChild(content);

        // Footer
        root.appendChild(el('div', { style: 'padding:10px 20px;border-top:1px solid rgba(255,255,255,.04);text-align:center;' },
            el('span', { style: 'font-size:11px;color:#64748b;' }, txt(`Package ID: ${pkgId}`))));

        winEl.replaceChildren(root);
    } catch {
        const msg = document.createElement('p');
        msg.textContent = `Package "${pkgId}" not found or could not be loaded.`;
        const box = document.createElement('div');
        box.style.cssText = 'display:flex;align-items:center;justify-content:center;height:100%;color:#64748b;font-size:15px;text-align:center;';
        box.appendChild(msg);
        winEl.replaceChildren(box);
    }
}


// Expose package uninstall globally for the generic window button
window._uninstallPkg = async function(pkgId) {
    if (!await showModalConfirm('Uninstall', `Uninstall "${pkgId}"?`)) return;
    try {
        const data = await apiJsonFetch(`/api/packages/${encodeURIComponent(pkgId)}`, { method: 'DELETE' });
        // Close the generic package window immediately; the job updates the icons.
        const winEl = document.querySelector(`[data-testid="window-pkg-${pkgId}"]`);
        if (winEl) {
            const closeBtn = winEl.querySelector('.close-btn');
            if (closeBtn) _closeWindowWin(closeBtn);
        }
        if (data && data.job_id && typeof window._pkgWatchJob === 'function') {
            window._pkgWatchJob(data.job_id, pkgId);
        } else {
            _loadDynamicIcons();
            addNotification(`Package "${pkgId}" uninstalled`, 'success');
            const pkgCenterBody = document.querySelector('[data-testid="window-packages"] .window-body');
            if (pkgCenterBody && typeof renderPackagesPage === 'function') await renderPackagesPage(pkgCenterBody);
        }
    } catch (err) {
        addNotification(err.detail || err.message, 'error');
    }
};

// Override openApp for pkg-* apps to use the generic renderer
const _origOpenAppWrapper = window.openApp;
// Apps that moved to Packages: internal links keep using the old id.
const APP_ID_ALIASES = {
    gallery: 'pkg-ethos-photos', music: 'pkg-ethos-music', videos: 'pkg-ethos-video',
    surveillance: 'pkg-ethos-surveillance', shares: 'pkg-ethos-shares',
    editor: 'pkg-ethos-editor', 'git-diff': 'pkg-ethos-git-diff', cleaner: 'pkg-ethos-cleaner',
    docker: 'pkg-ethos-docker',
};
window.openApp = function(appId) {
    appId = APP_ID_ALIASES[appId] || appId;
    // Trash is a shortcut into Files, not a window app of its own.
    if (appId === 'trash') { openTrashFromDesktop(); return; }
    if (!isAppVisible(appId)) {
        addNotification('This app is available to administrators only.', 'info');
        return;
    }
    if (appId.startsWith('pkg-')) {
        // Use raw OpenApp with the generic package renderer
        const fn = (body) => _renderGenericPackage(appId, body);
        if (fn && window._rawOpenApp) { window._rawOpenApp(appId, fn); }
        return;
    }
    _origOpenAppWrapper(appId);
};

// ── Topbar clock & user menu (macOS menu-bar style: "Sat 22 Aug 14:05:33") ──
const _DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const _MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function _updateClock() {
    const el = document.getElementById('topbar-clock'); if (!el) return;
    const d = new Date();
    const p2 = n => String(n).padStart(2, '0');
    el.textContent = `${_DAYS[d.getDay()]} ${d.getDate()} ${_MONTHS[d.getMonth()]}  ${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}
setInterval(_updateClock, 1000); _updateClock();
initTopbarMenu();
initWindowChrome();  // immersive-mode edge strips that reveal the bars

// ── PWA service worker (TODO.md P3-14) ─────────────────────────────────────
// Network-first, so it only helps when the NAS is unreachable (it serves the
// cached shell instead of a browser error page). Requires a secure context:
// https, or http on localhost — a plain-http LAN install simply skips it.
if ('serviceWorker' in navigator && window.isSecureContext) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js').catch((err) => {
            // Not fatal: the app works exactly as before without a worker.
            console.debug('[ethos] service worker not registered:', err?.message || err);
        });
    });
}
