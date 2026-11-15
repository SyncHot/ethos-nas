// EthOS NAS — per-user desktop wallpaper (core)
//
// Mirrors the desktop-layout.js pattern: localStorage cache + fire-and-forget
// server sync, with the server record as source of truth after login.
// Built-in wallpapers are pure-CSS gradients (.wallpaper-* classes in index.html);
// "custom" swaps in an uploaded image served from /api/appearance/wallpaper/image.

import { apiFetch, getToken } from './api.js';
import { currentLayoutOwner } from './desktop-layout.js';

export const WALLPAPER_IDS = ['aurora', 'dusk', 'meadow', 'tide', 'graphite'];
export const CUSTOM_ID = 'custom';
export const DEFAULT_WALLPAPER = 'aurora';

const STORAGE_KEY = 'ethos-wallpaper';
const LEGACY_STORAGE_KEY = 'ethos-wallpaper';  // pre-namespacing, shared
const API_PATH = '/api/appearance/wallpaper';

// One shared cache key meant the next account in the same browser painted the
// previous user's wallpaper until the server round-trip landed.
function _storageKey() {
    return `${STORAGE_KEY}:${currentLayoutOwner() || 'anon'}`;
}

function _migrateLegacyCache() {
    try {
        if (!currentLayoutOwner()) return;
        const key = _storageKey();
        const legacy = localStorage.getItem(LEGACY_STORAGE_KEY);
        if (legacy && !localStorage.getItem(key)) localStorage.setItem(key, legacy);
        localStorage.removeItem(LEGACY_STORAGE_KEY);
    } catch (_) {}
}

function _el() {
    return document.getElementById('desktopBg') || document.querySelector('.desktop-bg');
}

/** Locally cached wallpaper id (falls back to the default). */
export function getLocalWallpaper() {
    try {
        _migrateLegacyCache();
        const v = localStorage.getItem(_storageKey());
        if (v && (WALLPAPER_IDS.includes(v) || v === CUSTOM_ID)) return v;
    } catch (_) {}
    return DEFAULT_WALLPAPER;
}

function _saveLocal(id) {
    try { localStorage.setItem(_storageKey(), id); } catch (_) {}
}

/**
 * Apply a wallpaper to the desktop. `id` is one of WALLPAPER_IDS or CUSTOM_ID.
 * Unknown ids fall back to the default built-in.
 */
export function applyWallpaper(id) {
    const el = _el();
    if (!el) return;
    // Clear any previous inline custom background first.
    el.style.backgroundImage = '';
    el.style.backgroundSize = '';
    el.style.backgroundPosition = '';
    for (const w of WALLPAPER_IDS) el.classList.remove('wallpaper-' + w);

    if (id === CUSTOM_ID) {
        const token = encodeURIComponent(getToken() || '');
        el.style.backgroundImage = `url('${API_PATH}/image?token=${token}')`;
        el.style.backgroundSize = 'cover';
        el.style.backgroundPosition = 'center';
    } else if (WALLPAPER_IDS.includes(id)) {
        el.classList.add('wallpaper-' + id);
    } else {
        el.classList.add('wallpaper-' + DEFAULT_WALLPAPER);
    }
}

let _syncQueue = Promise.resolve();

/** Fire-and-forget sync to the server; skipped when logged out, silent on failure. */
function _syncToServer(id) {
    if (!getToken()) return;
    const payload = JSON.stringify({ wallpaper: id });
    _syncQueue = _syncQueue.then(() => apiFetch(API_PATH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: payload,
    })).catch(() => {});
}

/** Save a built-in wallpaper (local cache + apply + server sync). */
export function saveWallpaper(id) {
    if (!WALLPAPER_IDS.includes(id)) return;
    _saveLocal(id);
    applyWallpaper(id);
    _syncToServer(id);
}

/**
 * Load this user's wallpaper from the server (source of truth):
 * - 200 → server value replaces local state and is applied.
 * - 404 → no per-user record yet: seed it with the local choice, keep local.
 * - 401/network error → keep local state.
 */
export async function loadRemoteWallpaper() {
    if (!getToken()) return null;
    try {
        const res = await apiFetch(API_PATH);
        if (res.status === 404) {
            const id = getLocalWallpaper();
            applyWallpaper(id);
            _syncToServer(id);
            return null;
        }
        if (!res.ok) return null;
        const data = await res.json();
        const id = (data && WALLPAPER_IDS.includes(data.wallpaper)) ? data.wallpaper
            : (data && data.wallpaper === CUSTOM_ID) ? CUSTOM_ID
            : DEFAULT_WALLPAPER;
        _saveLocal(id);
        applyWallpaper(id);
        return { wallpaper: id, custom_url: data.custom_url || null };
    } catch (_) {}
    return null;
}

/** Upload a custom image (jpg/png/webp) and switch to it. Throws on failure. */
export async function uploadCustomWallpaper(file) {
    const form = new FormData();
    form.append('file', file);
    const res = await apiFetch(API_PATH + '/upload', { method: 'POST', body: form });
    if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try { detail = (await res.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
    }
    _saveLocal(CUSTOM_ID);
    applyWallpaper(CUSTOM_ID);
    return CUSTOM_ID;
}

/** Reset to the default wallpaper and delete the user's custom image. */
export async function resetWallpaper() {
    const res = await apiFetch(API_PATH, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _saveLocal(DEFAULT_WALLPAPER);
    applyWallpaper(DEFAULT_WALLPAPER);
    return DEFAULT_WALLPAPER;
}

/** Authenticated URL of the caller's custom image (for previews). */
export function customImageUrl() {
    const token = encodeURIComponent(getToken() || '');
    return `${API_PATH}/image?token=${token}`;
}
