// EthOS Music — shared API helpers for the /music PWA.
//
// Everything goes through `core/api.js` (Bearer token from localStorage, 401
// handling). This module only adds what is specific to music: track keys, the
// two different media URLs (browser playback vs. a same-origin download the
// page can read into a Blob) and the small formatters the views share.

import { apiFetch, getToken } from '../core/api.js';

export function token() {
    return getToken() || '';
}

/** JSON fetch that throws the backend's `detail` instead of a bare status. */
export async function json(path, options = {}) {
    const res = await apiFetch(path, options);
    if (!res.ok) {
        let detail = '';
        try {
            const payload = await res.clone().json();
            detail = payload?.detail || payload?.error || '';
        } catch { /* non-JSON error body */ }
        const err = new Error(detail || `HTTP ${res.status}`);
        err.status = res.status;
        err.detail = detail;
        throw err;
    }
    if (res.status === 204) return null;
    return res.json();
}

/** Server-wide track identity (matches the Android app and the desktop app). */
export function tkey(track) {
    return `${track?.id || ''}|${track?.title || ''}`;
}

/** Append the JWT to a same-origin media URL (`<audio>` cannot send headers). */
export function withToken(url) {
    if (!url || !url.startsWith('/')) return url || '';
    return `${url}${url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token())}`;
}

/**
 * Source for the `<audio>` element. Offline copies are resolved by player.js
 * first; this is the online fallback. Podcast enclosures are public URLs and
 * play directly, everything else streams through the NAS proxy.
 */
export function playSrc(track) {
    if (!track) return '';
    const url = track.file_url || (track.id ? `/api/music/stream?video_id=${encodeURIComponent(track.id)}` : '');
    if (!url) return '';
    return url.startsWith('/') ? withToken(url) : url;
}

/**
 * Source for a device download. Must be same-origin so the page can read the
 * body into a Blob: NAS library files and YouTube streams already are, a
 * podcast enclosure goes through the NAS audio proxy (third-party CDNs usually
 * send no CORS headers).
 */
export function downloadSrc(track) {
    if (!track) return '';
    const file = track.file_url || '';
    if (file.startsWith('/api/')) return file;
    if (/^https?:\/\//i.test(file)) {
        return `/api/music/episode-audio?url=${encodeURIComponent(file)}`;
    }
    if (track.id) return `/api/music/stream?video_id=${encodeURIComponent(track.id)}`;
    return '';
}

/** Podcast episodes keep a resume position and may be downloaded separately. */
export function isEpisode(track) {
    return !!track && (track.is_episode === true || /^https?:\/\//i.test(track.file_url || ''));
}

export function artUrl(track) {
    return track?.thumbnail || track?.thumb || track?.cover || '';
}

export function fmtDur(sec) {
    let s = Math.max(0, Math.floor(Number(sec) || 0));
    const h = Math.floor(s / 3600);
    s %= 3600;
    const m = Math.floor(s / 60);
    s %= 60;
    if (h) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    return `${m}:${String(s).padStart(2, '0')}`;
}

export function fmtBytes(value) {
    const n = Number(value) || 0;
    if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
    if (n >= 1e3) return `${Math.round(n / 1e3)} KB`;
    return `${n} B`;
}

/** Podcast id derived from the RSS URL — the NAS computes the same value. */
export async function sha16(text) {
    try {
        const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
        return Array.from(new Uint8Array(digest))
            .map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
    } catch {
        return String(text).length.toString(16).padStart(16, '0');
    }
}
