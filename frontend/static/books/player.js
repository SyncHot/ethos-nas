// EthOS Books — audiobook engine for the /books PWA.
//
// One module-level singleton so playback survives view switches and re-renders.
// Everything the phone needs for background listening lives here:
//
//   • `<audio>` + Media Session (lock-screen metadata, play/pause, next/prev,
//     seek) so playback continues with the screen off;
//   • a downloaded copy is preferred over the network, and a failed stream
//     falls back to the cached file (or the other way round);
//   • the reading position is written through `offline.queueProgress` every few
//     seconds, on pause and when the page goes away;
//   • speed, ±skip and a sleep timer match the desktop player's settings.
//
// The locator format is the package's: `locator` = track index (string),
// `position_s` = seconds inside that track, `percent` weighted by track size.

import { bookTracks, fmtTime, mediaUrl, pct } from './api.js';
import * as offline from './offline.js';

const SESSION_KEY = 'ethos.books.session';

export class Engine {
    constructor() {
        this.audio = new Audio();
        this.audio.preload = 'metadata';
        this.book = null;
        this.tracks = [];
        this.index = 0;
        this.speed = 1;
        this.skip = 30;
        this.onProgress = null;
        this._subs = {};
        this._lastSession = 0;
        this._lastSave = 0;
        this._sleepTimer = null;
        this._sleepAt = 0;
        this._restored = false;

        this.audio.addEventListener('timeupdate', () => {
            this._emit('time', { pos: this.audio.currentTime || 0, dur: this.audio.duration || 0 });
            const now = Date.now();
            if (now - this._lastSession > 5000) { this._lastSession = now; this.saveSession(); }
            if (now - this._lastSave > 5000) { this._lastSave = now; this.saveProgress(); }
        });
        this.audio.addEventListener('loadedmetadata', () => {
            this._emit('time', { pos: this.audio.currentTime || 0, dur: this.audio.duration || 0 });
            this._updateMediaSession();
        });
        this.audio.addEventListener('play', () => {
            this._emit('playing');
            this._updateMediaSession();
            this.saveSession();
        });
        this.audio.addEventListener('pause', () => {
            this._emit('playing');
            this._updateMediaSession();
            this.saveSession();
            this.saveProgress();
        });
        this.audio.addEventListener('ended', () => this._onEnded());
        this.audio.addEventListener('error', () => this._onError());
        this._setupMediaSession();
    }

    // ── Media Session ─────────────────────────────────────────────────────

    _setupMediaSession() {
        if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
        const session = navigator.mediaSession;
        const set = (action, handler) => {
            try { session.setActionHandler(action, handler); } catch { /* unsupported */ }
        };
        set('play', () => this.play());
        set('pause', () => this.audio.pause());
        set('previoustrack', () => this.prev());
        set('nexttrack', () => this.next());
        set('seekbackward', (details) => this.seekBy(-(details?.seekOffset || this.skip)));
        set('seekforward', (details) => this.seekBy(details?.seekOffset || this.skip));
        set('seekto', (details) => { if (Number.isFinite(details?.seekTime)) this.seek(details.seekTime); });
        set('stop', () => this.audio.pause());
    }

    async _updateMediaSession() {
        if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
        const track = this.current;
        if (!this.book || !track) {
            try { navigator.mediaSession.metadata = null; } catch { /* ignore */ }
            return;
        }
        let artwork = [];
        try {
            const cover = await offline.coverUrl(this.book, { fetch: false });
            if (cover) artwork = [{ src: cover, sizes: '512x512', type: 'image/webp' }];
        } catch { /* cover is optional */ }
        try {
            navigator.mediaSession.metadata = new MediaMetadata({
                title: String(this.book.title || 'Audiobook'),
                artist: String(this.book.author || ''),
                album: this.tracks.length > 1 ? `Rozdział ${this.index + 1} z ${this.tracks.length}` : 'Audiobook',
                artwork,
            });
            navigator.mediaSession.playbackState = this.isPlaying ? 'playing' : 'paused';
        } catch { /* MediaMetadata unavailable */ }
        this._setPositionState();
    }

    _setPositionState() {
        if (typeof navigator === 'undefined' || !('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
        const duration = Number(this.audio.duration) || 0;
        if (!duration || !Number.isFinite(duration)) return;
        try {
            navigator.mediaSession.setPositionState({
                duration,
                position: Math.min(duration, Math.max(0, this.audio.currentTime || 0)),
                playbackRate: this.audio.playbackRate || 1,
            });
        } catch { /* older browsers */ }
    }

    // ── state ─────────────────────────────────────────────────────────────

    get current() {
        return this.tracks[this.index] || null;
    }

    get isPlaying() {
        return !this.audio.paused && !this.audio.ended;
    }

    get hasBook() {
        return !!this.book;
    }

    on(event, fn, key = null) {
        const list = this._subs[event] || (this._subs[event] = []);
        if (key != null) {
            for (let i = list.length - 1; i >= 0; i -= 1) if (list[i].key === key) list.splice(i, 1);
        }
        list.push({ fn, key });
    }

    off(event, key) {
        const list = this._subs[event] || [];
        for (let i = list.length - 1; i >= 0; i -= 1) if (list[i].key === key) list.splice(i, 1);
    }

    _emit(event, data) {
        (this._subs[event] || []).forEach((sub) => {
            try { sub.fn(data); } catch { /* a subscriber must not break playback */ }
        });
    }

    // ── opening a book ────────────────────────────────────────────────────

    /**
     * Load an audiobook: index and position come from the shared progress
     * (`locator` = track, `position_s` = seconds). Never autoplays — phones
     * require a user gesture.
     */
    playBook(book, { settings = {}, progress = null, autoplay = false } = {}) {
        if (!book) return;
        this.book = book;
        this.tracks = bookTracks(book);
        this.speed = Number(settings.speed) || 1;
        this.skip = Number(settings.skip_seconds) || 30;
        const index = Math.max(0, Math.min(this.tracks.length - 1, Number(progress?.locator) || 0));
        const position = Number(progress?.position_s) || 0;
        this._load(index, position);
        if (autoplay) this.play();
        else this._emit('track');
        this.saveSession();
    }

    _load(index, position = 0) {
        this.index = Math.max(0, Math.min(this.tracks.length - 1, Number(index) || 0));
        const track = this.current;
        if (!track) return;
        this._usingLocal = !!offline.trackSource(this.book, this.index);
        this.audio.src = this._usingLocal ? offline.trackSource(this.book, this.index) : mediaUrl(track.path);
        this.audio.playbackRate = this.speed;
        if (position > 0) {
            const seekOnce = () => {
                try { this.audio.currentTime = position; } catch { /* not seekable yet */ }
                this.audio.removeEventListener('loadedmetadata', seekOnce);
            };
            this.audio.addEventListener('loadedmetadata', seekOnce);
        }
        this._emit('track', { index: this.index });
        this._updateMediaSession();
    }

    /** Local file failed (evicted cache) — retry the same position from the NAS. */
    _onError() {
        const track = this.current;
        if (!track) return;
        if (this._usingLocal) {
            const at = Number(this.audio.currentTime) || 0;
            this._usingLocal = false;
            this.audio.src = mediaUrl(track.path);
            this.audio.playbackRate = this.speed;
            if (at > 0) {
                const seekOnce = () => {
                    try { this.audio.currentTime = at; } catch { /* ignore */ }
                    this.audio.removeEventListener('loadedmetadata', seekOnce);
                };
                this.audio.addEventListener('loadedmetadata', seekOnce);
            }
            if (this.isPlaying) this.audio.play().catch(() => {});
            return;
        }
        this._emit('error', { index: this.index });
    }

    _onEnded() {
        if (this.index < this.tracks.length - 1) {
            this._load(this.index + 1, 0);
            this.audio.play().catch(() => {});
        } else {
            this.saveProgress(true);
            this._emit('ended');
        }
    }

    // ── transport ─────────────────────────────────────────────────────────

    play() {
        if (!this.current) return;
        this.audio.play().catch(() => this._emit('blocked'));
    }

    pause() {
        this.audio.pause();
    }

    toggle() {
        if (this.isPlaying) this.pause();
        else this.play();
    }

    next() {
        if (this.index < this.tracks.length - 1) this._load(this.index + 1, 0);
    }

    prev() {
        // "Previous" restarts the chapter first, like every audiobook app.
        if ((this.audio.currentTime || 0) > 12) { this.seek(0); return; }
        if (this.index > 0) this._load(this.index - 1, 0);
        else this.seek(0);
    }

    jumpTo(index) {
        this._load(index, 0);
        this.play();
    }

    seek(seconds) {
        const value = Number(seconds) || 0;
        try { this.audio.currentTime = Math.max(0, Math.min(this.audio.duration || value, value)); } catch { /* ignore */ }
        this._setPositionState();
    }

    seekBy(delta) {
        this.seek((this.audio.currentTime || 0) + (Number(delta) || 0));
    }

    setSpeed(rate) {
        this.speed = Math.max(0.5, Math.min(3, Number(rate) || 1));
        this.audio.playbackRate = this.speed;
        this._setPositionState();
        this.saveSession();
        return this.speed;
    }

    sleep(minutes) {
        if (this._sleepTimer) clearTimeout(this._sleepTimer);
        this._sleepTimer = null;
        this._sleepAt = 0;
        const value = Number(minutes) || 0;
        if (value <= 0) return 0;
        this._sleepTimer = setTimeout(() => {
            this.pause();
            this._sleepTimer = null;
            this._sleepAt = 0;
            this._emit('sleep', { minutes: value });
        }, value * 60000);
        this._sleepAt = Date.now() + value * 60000;
        return value;
    }

    sleepMinutesLeft() {
        if (!this._sleepTimer) return 0;
        return Math.max(0, Math.round((this._sleepAt - Date.now()) / 60000));
    }

    stop() {
        if (this._sleepTimer) clearTimeout(this._sleepTimer);
        this._sleepTimer = null;
        try { this.audio.pause(); } catch { /* ignore */ }
        this.book = null;
        this.tracks = [];
        this.index = 0;
        this.audio.removeAttribute('src');
        this.audio.load();
        try { localStorage.removeItem(SESSION_KEY); } catch { /* private mode */ }
        try { if ('mediaSession' in navigator) navigator.mediaSession.metadata = null; } catch { /* ignore */ }
    }

    // ── progress ──────────────────────────────────────────────────────────

    /** Weighted by track size, so a book with uneven chapters still advances evenly. */
    percent() {
        if (!this.book || !this.tracks.length) return 0;
        const sizes = this.tracks.map((track) => Number(track.size) || 0);
        const anySize = sizes.some((size) => size > 0);
        const weight = (index) => (anySize ? sizes[index] : 1);
        const total = anySize ? sizes.reduce((sum, size) => sum + size, 0) : this.tracks.length;
        if (!total) return 0;
        let done = 0;
        for (let i = 0; i < this.index; i += 1) done += weight(i);
        const fraction = this.audio.duration ? Math.min(1, (this.audio.currentTime || 0) / this.audio.duration) : 0;
        done += weight(this.index) * fraction;
        return Math.max(0, Math.min(1, done / total));
    }

    payload(finished = false) {
        return {
            percent: this.percent(),
            locator: String(this.index),
            position_s: Number(this.audio.currentTime) || 0,
            finished: finished || (this.index === this.tracks.length - 1
                && this.audio.duration > 0
                && (this.audio.currentTime / this.audio.duration) >= 0.995),
        };
    }

    saveProgress(finished = false) {
        if (!this.book || !this.current) return;
        const payload = this.payload(finished);
        try { this.onProgress?.(this.book.id, payload); } catch { /* ignore */ }
        this._emit('progress', payload);
    }

    // ── session (survives a reload / an app update) ───────────────────────

    saveSession() {
        if (!this.book) return;
        try {
            localStorage.setItem(SESSION_KEY, JSON.stringify({
                book: {
                    id: this.book.id,
                    title: this.book.title,
                    author: this.book.author,
                    format: this.book.format,
                    kind: this.book.kind,
                    path: this.book.path,
                    size: this.book.size,
                    tracks: this.tracks,
                    cover: !!this.book.cover,
                },
                index: this.index,
                pos: Number(this.audio.currentTime) || 0,
                speed: this.speed,
            }));
        } catch { /* private mode / quota */ }
    }

    /** Restore the last book without playing (no gesture yet). */
    restoreSession() {
        if (this.book) return null;
        try {
            const raw = JSON.parse(localStorage.getItem(SESSION_KEY) || 'null');
            if (!raw?.book?.id) return null;
            this.book = raw.book;
            this.tracks = bookTracks(raw.book);
            this.index = Math.max(0, Math.min(this.tracks.length - 1, Number(raw.index) || 0));
            this.speed = Number(raw.speed) || 1;
            this.audio.src = offline.trackSource(this.book, this.index)
                ? offline.trackSource(this.book, this.index)
                : mediaUrl(this.current?.path || '');
            this.audio.playbackRate = this.speed;
            const position = Number(raw.pos) || 0;
            if (position > 0) {
                const seekOnce = () => {
                    try { this.audio.currentTime = position; } catch { /* ignore */ }
                    this.audio.removeEventListener('loadedmetadata', seekOnce);
                };
                this.audio.addEventListener('loadedmetadata', seekOnce);
            }
            this._restored = true;
            this._updateMediaSession();
            this._emit('track', { index: this.index });
            return { book: this.book, index: this.index, pos: position };
        } catch { return null; }
    }

    get restored() {
        return this._restored;
    }
}

export const engine = new Engine();

export function durationLabel(seconds) {
    return fmtTime(seconds);
}

export function percentLabel(value) {
    return `${pct(value)}%`;
}
