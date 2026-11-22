// EthOS core — package ribbon host.
//
// A pinned native package may declare `ui.ribbon = {entry, title}`. At boot
// (and after install/uninstall) this module mounts that entry into a single
// right-edge host and gives it the same authenticated proxy + notification
// services as a package window. The package owns all of the visuals; this file
// only owns the host element, the lifetime and the desktop gutter.
//
// Contract: the entry must export `mount(host, ctx)` (or a default function)
// and may return a cleanup function. `ctx` exposes:
//   pkg, apiFetch, apiJsonFetch, proxy(path, opts),
//   notifications.{toast, emit, open}, log(), logger.{...},
//   escape.{html, attr, js}, openApp(appId), setDesktopGutter(px).

import { apiFetch, apiJsonFetch, apiRequest, getToken } from './api.js';
import { addNotification, openNotifications } from './notification-center.js';
import { log } from './logger.js';
import { escapeHtml, escapeAttr, jsAttr } from './utils.js';
import { refreshDesktopIcons } from './registry.js';

const HOST_ID = 'ethos-ribbon-host';
const MAX_GUTTER = 120;

let _mounted = null;   // {pkgId, version, cleanup, host}
let _gutter = 0;
let _refreshPromise = null;

function ensureHost() {
    let host = document.getElementById(HOST_ID);
    if (!host) {
        host = document.createElement('div');
        host.id = HOST_ID;
        host.setAttribute('aria-live', 'off');
        document.body.appendChild(host);
    }
    return host;
}

function packageBase(pkg) {
    return `/api/packages/${encodeURIComponent(pkg.id)}`;
}

function setGutter(px) {
    const value = Math.max(0, Math.min(MAX_GUTTER, Number(px) || 0));
    if (value === _gutter) return;
    _gutter = value;
    document.documentElement.style.setProperty('--desktop-right-gutter', `${value}px`);
    try { refreshDesktopIcons(); } catch { /* desktop not mounted yet */ }
}

function makeContext(pkg) {
    const base = packageBase(pkg);
    const logFor = (level) => (message, detail = '') => log(level, message, detail, { packageId: pkg.id });
    return {
        pkg,
        apiFetch,
        apiJsonFetch,
        proxy: (path, options) => apiJsonFetch(
            `${base}/proxy/${String(path || '').replace(/^\/+/, '')}`, options),
        notifications: {
            toast: (message, kind = 'info') => addNotification(message, kind),
            emit: (note = {}) => apiRequest('/api/notifications', {
                method: 'POST',
                body: { kind: 'info', ...note, package_id: pkg.id },
            }),
            open: (filter) => openNotifications(filter ? { filter } : {}),
        },
        log: (level, message, detail = '') => log(level, message, detail, { packageId: pkg.id }),
        logger: {
            debug: logFor('debug'),
            info: logFor('info'),
            warn: logFor('warning'),
            error: logFor('error'),
        },
        escape: { html: escapeHtml, attr: escapeAttr, js: jsAttr },
        openApp: (appId) => window.openApp?.(appId),
        setDesktopGutter: (px) => setGutter(px),
    };
}

function cleanupMounted() {
    if (!_mounted) return;
    const current = _mounted;
    _mounted = null;
    try { current.cleanup?.(); } catch (error) {
        log('warning', `Ribbon cleanup failed for ${current.pkgId}`, String(error?.message || error));
    }
    try { current.host?.replaceChildren(); } catch { /* host removed */ }
    setGutter(0);
}

async function mountCandidate(pkg) {
    const host = ensureHost();
    const response = await apiFetch(`${packageBase(pkg)}/ui/${pkg.ui.ribbon.entry}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const source = await response.text();
    const blobUrl = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }));
    let module;
    try {
        module = await import(/* webpackIgnore: true */ blobUrl);
    } finally {
        URL.revokeObjectURL(blobUrl);
    }
    const mount = typeof module.mount === 'function' ? module.mount
        : (typeof module.default === 'function' ? module.default : null);
    if (!mount) throw new Error('Module does not export mount(host, ctx)');
    const cleanup = await mount(host, makeContext(pkg));
    _mounted = {
        pkgId: pkg.id,
        version: String(pkg.version || ''),
        cleanup: typeof cleanup === 'function' ? cleanup : null,
        host,
    };
    log('info', `Package ribbon mounted: ${pkg.id}`, String(pkg.ui.ribbon.entry || ''));
}

async function refresh() {
    if (!getToken()) {
        cleanupMounted();
        return;
    }
    let packages = [];
    try {
        const data = await apiJsonFetch('/api/packages');
        packages = Array.isArray(data?.packages) ? data.packages : [];
    } catch (error) {
        if (error?.status === 401) {
            cleanupMounted();
            return;
        }
        log('warning', 'Could not list packages for the ribbon host',
            String(error?.detail || error?.message || error));
        return;
    }
    const candidates = packages.filter((pkg) => pkg?.id && pkg?.trust === 'pinned'
        && pkg?.ui?.mode === 'native' && pkg?.ui?.ribbon?.entry);
    if (candidates.length > 1) {
        log('warning', 'Multiple package ribbons declared; using the first one',
            candidates.map((pkg) => pkg.id).join(', '));
    }
    const candidate = candidates[0];
    if (!candidate) {
        cleanupMounted();
        return;
    }
    if (_mounted && _mounted.pkgId === candidate.id
        && _mounted.version === String(candidate.version || '')) {
        return;
    }
    cleanupMounted();
    try {
        await mountCandidate(candidate);
    } catch (error) {
        ensureHost().replaceChildren();
        setGutter(0);
        log('error', `Could not mount package ribbon ${candidate.id}`,
            String(error?.detail || error?.message || error));
    }
}

/** Re-read installed packages and mount/remount the ribbon if needed. */
export function refreshPackageRibbons() {
    if (_refreshPromise) return _refreshPromise;
    _refreshPromise = refresh().finally(() => { _refreshPromise = null; });
    return _refreshPromise;
}

/** Boot hook — the authenticated call is skipped until a token exists. */
export function initPackageRibbons() {
    if (getToken()) refreshPackageRibbons().catch(() => {});
}
