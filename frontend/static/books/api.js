// EthOS Books — shared API, URLs and formatting for the /books PWA.
//
// The package service is reached through the core proxy with the caller's JWT
// (`Authorization: Bearer`), and every book byte comes from the core file API
// (`/api/files/raw`), so `path_scope` stays the only filesystem gate. Media
// elements cannot send headers, so `<audio>`/download URLs carry `?token=`.
//
// Downloaded copies live at `/__books/...` and are answered by `sw.js` from
// Cache Storage (`ethos-books-v1`), including synthesised `206` slices.

import { apiFetch, getToken } from '../core/api.js';

export const BASE = '/api/packages/ethos-books/proxy';
export const LOCAL_PREFIX = '/__books/';
export const AUDIO_EXTS = new Set(['m4b', 'm4a', 'mp3', 'flac', 'ogg', 'opus']);

export function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

export function escAttr(value) {
    return esc(value);
}

export async function detailOf(res, fallback = '') {
    try {
        const payload = await res.clone().json();
        return payload?.detail || payload?.error || fallback || `HTTP ${res.status}`;
    } catch {
        return fallback || `HTTP ${res.status}`;
    }
}

/** JSON call against the package proxy. Throws an Error with `.detail`. */
export async function json(path, { method = 'GET', body } = {}) {
    const options = { method };
    if (body !== undefined) {
        options.body = JSON.stringify(body);
        options.headers = { 'Content-Type': 'application/json' };
    }
    const res = await apiFetch(`${BASE}/${String(path).replace(/^\/+/, '')}`, options);
    if (!res.ok) {
        const detail = await detailOf(res, `HTTP ${res.status}`);
        const error = new Error(detail);
        error.detail = detail;
        error.status = res.status;
        throw error;
    }
    if (res.status === 204) return null;
    return res.json();
}

export const library = () => json('library');
export const putProgress = (bookId, payload) => json(`books/${encodeURIComponent(bookId)}/progress`, { method: 'PUT', body: payload });
export const getSettings = () => json('settings');
export const putSettings = (settings) => json('settings', { method: 'PUT', body: settings });
export const coverPath = (bookId, version = '') =>
    `${BASE}/covers/${encodeURIComponent(bookId)}${version ? `?v=${encodeURIComponent(version)}` : ''}`;

export function rawPath(path) {
    return `/api/files/raw?path=${encodeURIComponent(path)}`;
}

/** Streamable URL for `<audio>` (media elements cannot send an auth header). */
export function mediaUrl(path) {
    const token = getToken() || '';
    return `${rawPath(path)}&token=${encodeURIComponent(token)}`;
}

/** `/__books/<id>` (a book file) or `/__books/<id>/t<index>` (one track). */
export function localKey(bookId, trackIndex = null) {
    return trackIndex == null
        ? `${LOCAL_PREFIX}${encodeURIComponent(bookId)}`
        : `${LOCAL_PREFIX}${encodeURIComponent(bookId)}/t${Number(trackIndex)}`;
}

// ── Formatting ─────────────────────────────────────────────────────────────

export function fmtBytes(value) {
    let size = Number(value) || 0;
    if (size <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return `${size >= 10 || unit === 0 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`;
}

export function fmtTime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    return hours
        ? `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
        : `${minutes}:${String(secs).padStart(2, '0')}`;
}

export function pct(value) {
    return Math.max(0, Math.min(100, Math.round((Number(value) || 0) * 100)));
}

export function baseName(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    return parts.length ? parts[parts.length - 1] : '/';
}

export function fileTitle(name) {
    return String(name || '').replace(/\.[^.]+$/, '').replace(/[_]+/g, ' ').trim();
}

export function trackTitle(track) {
    return String(track?.title || '').trim() || fileTitle(baseName(track?.path));
}

export function bookTracks(book) {
    if (Array.isArray(book?.tracks) && book.tracks.length) return book.tracks;
    return [{ path: book?.path || '', title: book?.title || '', size: book?.size || 0 }];
}

export function isAudiobook(book) {
    return book?.kind === 'audiobook' || AUDIO_EXTS.has(String(book?.format || '').toLowerCase());
}

export function progressOf(state, bookId) {
    return (state?.progress || {})[bookId] || null;
}

export function percentOf(book, progress) {
    if (!progress) return 0;
    if (progress.finished) return 100;
    return pct(progress.percent);
}

export function matchesQuery(book, query) {
    const needle = String(query || '').trim().toLowerCase();
    if (!needle) return true;
    return [book.title, book.author, book.series, book.language]
        .some((value) => String(value || '').toLowerCase().includes(needle));
}
