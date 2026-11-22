// EthOS NAS — Desktop Layout Persistence Module
// Manages which apps appear on desktop, in what order. Persisted per user:
// localStorage is the instant first-paint cache; the server (keyed by account)
// is the source of truth so layouts survive browser-storage clears and follow
// the user across machines/browsers.
//
// The local cache is namespaced by the JWT's username. A single shared key made
// two accounts in one browser overwrite each other, and — because a missing
// server record was seeded from that same cache — copy one user's pins onto the
// other's brand-new record.
// Ubuntu-style: the desktop starts EMPTY — apps are pinned to it by the user
// (drag from the App Drawer or via right-click). The order list keeps a stable
// registry order for the drawer; `visible` is the list of pinned desktop icons.

import { apiFetch, getToken } from './api.js';

const STORAGE_KEY_PREFIX = "ethos-desktop-layout:";  // + username (or 'anon')
const LEGACY_STORAGE_KEY = "ethos-desktop-layout";   // pre-namespacing, shared
const MIGRATION_KEY_PREFIX = "ethos-desktop-layout-migrated:";
const TRASH_SEED_KEY_PREFIX = "ethos-desktop-trash-seeded:"; // + username (or 'anon')
const API_PATH = "/api/packages/desktop/layout";

// Must match frontend/static/core/registry.js (APP_REGISTRY) and
// backend/routers/packages.py (APP_REGISTRY_KEYS).
const DEFAULT_ORDER = [
    "overview", "storage", "files", "trash", "terminal",
    "network", "security", "services", "settings", "processes", "logs", "packages",
    "smart", "cron",
    "search", "shared-links", "sharing",
];

/**
 * Username from the JWT (`username` claim) — the same key the backend stores
 * layouts under. Empty when signed out.
 */
function _currentUsername() {
    try {
        const token = getToken();
        if (!token) return "";
        let payload = token.split(".")[1] || "";
        if (!payload) return "";
        while (payload.length % 4) payload += "=";
        const data = JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/")));
        return String(data.username || "").slice(0, 64);
    } catch (_) {
        return "";
    }
}

function _storageKey() {
    return STORAGE_KEY_PREFIX + (_currentUsername() || "anon");
}

function _load() {
    try {
        const raw = localStorage.getItem(_storageKey());
        if (!raw) return null;
        const data = JSON.parse(raw);
        if (data && typeof data === "object" && Array.isArray(data.order) && Array.isArray(data.visible)) {
            return data;
        }
    } catch (_) {}
    return null;
}

// Local cache only — fast first paint + offline fallback. Server sync is separate.
function _save(data) {
    try { localStorage.setItem(_storageKey(), JSON.stringify(data)); } catch (_) {}
}

// Sync queue: each save POSTs the full layout snapshot, so delivering them in
// call order guarantees the server always ends up with the newest state.
// Without this, rapid pin/unpin/reorder actions could interleave and an older
// snapshot would overwrite a newer one (pins lost on refresh).
let _syncQueue = Promise.resolve();

/**
 * Fire-and-forget sync of a layout to the server (per-user persistence).
 * Skipped when not logged in; failures are silent — the next save retries.
 */
function _syncToServer(layout) {
    if (!getToken()) return;
    const payload = JSON.stringify(layout);
    _syncQueue = _syncQueue.then(() => apiFetch(API_PATH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: payload,
    })).catch(() => {});
}

/**
 * One-time per-user migration of the pre-namespacing shared cache.
 * Old builds used ONE key for every account; adopt it for the signed-in user
 * (so their desktop survives the upgrade) and drop it so the next account in
 * this browser starts clean instead of inheriting these pins.
 */
function _migrateLegacyCache() {
    try {
        const username = _currentUsername();
        if (!username) return; // signed out: leave the legacy key alone
        const key = _storageKey();
        const legacy = localStorage.getItem(LEGACY_STORAGE_KEY);
        if (legacy && !localStorage.getItem(key)) {
            localStorage.setItem(key, legacy);
        }
        localStorage.removeItem(LEGACY_STORAGE_KEY);
    } catch (_) {}
}

/**
 * One-time migration: older versions pinned all default apps to the desktop.
 * Keep the user's chosen order, but start with an empty desktop so the user
 * manages the icons themselves. Runs per user, once.
 */
function _migrateIfNeeded() {
    try {
        const marker = MIGRATION_KEY_PREFIX + (_currentUsername() || "anon");
        if (localStorage.getItem(marker)) return;
        localStorage.setItem(marker, "1");
        const saved = _load();
        if (saved && saved.visible && saved.visible.length) {
            _save({ order: saved.order, visible: [], positions: saved.positions || {} });
        }
    } catch (_) {}
}

/**
 * Get current layout. Returns { order, visible, positions }.
 * `positions` maps appId → { c, r } grid cell for free desktop placement.
 */
export function getLayout() {
    _migrateLegacyCache();
    _migrateIfNeeded();
    const saved = _load();
    if (saved && saved.order && saved.visible) {
        return { order: saved.order, visible: saved.visible, positions: saved.positions || {} };
    }
    return { order: [...DEFAULT_ORDER], visible: [], positions: {} };
}

/**
 * Save layout (local cache + per-user server sync).
 * `positions` is optional so existing callers keep working; it defaults to the
 * positions already stored, so pin/unpin never wipes manual icon placement.
 */
export function saveLayout(order, visible, positions) {
    const pos = positions || getPositions();
    _save({ order, visible, positions: pos });
    _syncToServer({ order, visible, positions: pos });
}

/** Manual icon placement: { appId: { c, r } } in grid cells. */
export function getPositions() {
    return getLayout().positions || {};
}

/** Place one icon at a grid cell (persisted immediately). */
export function setPosition(appId, col, row) {
    const layout = getLayout();
    const positions = { ...layout.positions, [appId]: { c: col | 0, r: row | 0 } };
    saveLayout(layout.order, layout.visible, positions);
    return positions;
}

/** Drop all manual placement — icons fall back to the automatic grid. */
export function clearPositions() {
    const layout = getLayout();
    saveLayout(layout.order, layout.visible, {});
}

/**
 * Get the list of app IDs pinned to the desktop.
 */
export function getVisibleApps() {
    const layout = getLayout();
    return layout.visible || [];
}

/**
 * Get the full ordered list for rendering (drawer order).
 */
export function getOrderedList() {
    const layout = getLayout();
    return layout.order || [...DEFAULT_ORDER];
}

/**
 * Pin an app to the desktop (insert at given position in the pinned list).
 * Position -1 appends to the end. No-op if already pinned.
 * Returns the new visible array.
 */
export function addToDesktop(appId, position = -1) {
    const visible = [...getVisibleApps()];
    if (!visible.includes(appId)) {
        if (position < 0 || position >= visible.length) visible.push(appId);
        else visible.splice(position, 0, appId);
        saveLayout(getOrderedList(), visible);
    }
    return visible;
}

/**
 * Unpin an app from the desktop. Returns the new visible array.
 */
export function removeFromDesktop(appId) {
    const visible = getVisibleApps().filter(id => id !== appId);
    // Unpinning also drops the manual placement, otherwise a re-pinned icon
    // would reappear in a cell the user has since given to something else.
    const positions = { ...getPositions() };
    delete positions[appId];
    saveLayout(getOrderedList(), visible, positions);
    return visible;
}

/**
 * Toggle an app between pinned and unpinned on the desktop.
 * Returns the new visible array.
 */
export function toggleDesktop(appId) {
    const visible = getVisibleApps();
    if (visible.includes(appId)) return removeFromDesktop(appId);
    return addToDesktop(appId);
}

// Backward-compatible alias (old name used by context menus / drawer)
export const toggleHidden = toggleDesktop;

/**
 * Reorder pinned desktop icons. `from`/`to` are indices in the visible list.
 */
export function reorderDesktop(from, to) {
    const visible = [...getVisibleApps()];
    if (from < 0 || from >= visible.length) return visible;
    const [moved] = visible.splice(from, 1);
    const clamped = Math.max(0, Math.min(to, visible.length));
    visible.splice(clamped, 0, moved);
    saveLayout(getOrderedList(), visible);
    return visible;
}

/**
 * Reset layout — empty desktop, default registry order (drawer order).
 */
export function resetToDefault() {
    saveLayout([...DEFAULT_ORDER], [], {});
}

/**
 * Load this user's layout from the server (source of truth) and apply it to
 * the local cache. Returns the applied layout, or null when nothing changed:
 * - 200 → server layout replaces local state.
 * - 404 → no per-user record yet: upload THIS user's local state as the initial
 *   record (one-time migration from localStorage-only) and keep it.
 * - 401/network error → keep local state.
 */
export async function loadRemoteLayout() {
    if (!getToken()) return null;
    // Adopt the legacy shared cache for this account before it can be used as a
    // seed value — otherwise a brand-new record starts as someone else's desktop.
    _migrateLegacyCache();
    try {
        const res = await apiFetch(API_PATH);
        if (res.status === 404) { _syncToServer(getLayout()); return null; }
        if (!res.ok) return null;
        const data = await res.json();
        if (data && Array.isArray(data.order) && Array.isArray(data.visible)) {
            const positions = (data.positions && typeof data.positions === 'object') ? data.positions : {};
            _save({ order: data.order, visible: data.visible, positions });
            return { order: data.order, visible: data.visible, positions };
        }
    } catch (_) {}
    return null;
}

/**
 * Username that owns the layout currently in use ('' when signed out).
 */
export function currentLayoutOwner() {
    return _currentUsername();
}

/**
 * Pin the Trash shortcut once per account/browser.
 *
 * The desktop is deliberately empty by default, so the Trash icon is seeded on
 * the first load instead of being hard-coded in the layout. The marker is set
 * BEFORE pinning, so a user who unpins Trash later never sees it come back.
 * Returns true when the layout changed and the desktop should re-render.
 */
export function ensureTrashPinned() {
    if (!getToken()) return false;
    const marker = TRASH_SEED_KEY_PREFIX + (_currentUsername() || "anon");
    try {
        if (localStorage.getItem(marker)) return false;
        localStorage.setItem(marker, "1");
    } catch (_) {
        return false; // storage blocked — do not touch the layout
    }
    if (getVisibleApps().includes("trash")) return false;
    addToDesktop("trash");
    return true;
}

/**
 * Check if an app is currently pinned to the desktop.
 */
export function isPinned(appId) {
    return getVisibleApps().includes(appId);
}

// Backward-compatible alias
export const isHidden = (appId) => !isPinned(appId);
