// EthOS Music — audio engine for the /music PWA.
//
// One module-level singleton so playback survives re-renders and view
// switches. Mirrors the desktop app's Engine (queue, shuffle, repeat, sleep
// timer, Media Session, session restore) and adds what a phone needs:
//
//   * a downloaded copy is preferred over the network (`offline.localAudio`);
//   * when the stream fails but the track is on the device, playback switches
//     to the local file instead of stopping;
//   * podcast episodes remember their position and resume from it.

import { isEpisode, playSrc, tkey } from './api.js';
import * as offline from './offline.js';

export class Engine {
    constructor() {
        this.audio = new Audio();
        this.audio.preload = 'metadata';
        this.queue = [];
        this.index = -1;
        this.shuffle = false;
        this.repeat = 'off'; // off | all | one
        this._subs = {};
        this._seq = 0;
        this._retried = false;
        this._restoredPending = false;
        this._restoredPos = 0;
        this._lastSessionSave = 0;
        this._lastPositionSave = 0;
        this._sleepTimer = null;
        this._sleepMinutes = 0;
        this._sleepEndsAt = 0;

        this.audio.addEventListener('timeupdate', () => {
            const pos = this.audio.currentTime || 0;
            const dur = this.audio.duration || 0;
            this._emit('time', { pos, dur });
            this._updatePositionState();
            if (Date.now() - this._lastSessionSave > 5000) {
                this._lastSessionSave = Date.now();
                this.saveSession();
            }
            const t = this.current;
            if (t && isEpisode(t) && Date.now() - this._lastPositionSave > 5000) {
                this._lastPositionSave = Date.now();
                offline.savePosition(t, pos).catch(() => {});
            }
        });
        this.audio.addEventListener('loadedmetadata', () => {
            const t = this.current;
            if (t && isEpisode(t)) {
                offline.loadPosition(t).then((pos) => {
                    if (pos > 5 && pos < (this.audio.duration || Infinity) - 2) {
                        try { this.audio.currentTime = pos; } catch { /* ignore */ }
                    }
                }).catch(() => {});
            }
            this._emit('time', { pos: this.audio.currentTime || 0, dur: this.audio.duration || 0 });
        });
        this.audio.addEventListener('play', () => { this._emit('playing'); this._updateMediaSession(); this.saveSession(); });
        this.audio.addEventListener('pause', () => {
            this._emit('playing');
            this._updateMediaSession();
            this.saveSession();
            const t = this.current;
            if (t && isEpisode(t)) offline.savePosition(t, this.audio.currentTime || 0).catch(() => {});
        });
        this.audio.addEventListener('ended', () => this._onEnded());
        this.audio.addEventListener('error', () => this._onError());
        this._setupMediaSession();
    }

    // ── Media Session ─────────────────────────────────────────────────────

    _setupMediaSession() {
        if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
        const ms = navigator.mediaSession;
        const set = (action, handler) => {
            try { ms.setActionHandler(action, handler); } catch { /* unsupported action */ }
        };
        set('play', () => { if (this.current) this.toggle(); });
        set('pause', () => this.audio.pause());
        set('previoustrack', () => { if (this.current) this.prev(); });
        set('nexttrack', () => { if (this.current) this.next(); });
        set('seekbackward', (d) => this.seek(Math.max(0, (this.audio.currentTime || 0) - (d?.seekOffset || 10))));
        set('seekforward', (d) => this.seek((this.audio.currentTime || 0) + (d?.seekOffset || 10)));
        set('seekto', (d) => { if (d && Number.isFinite(d.seekTime)) this.seek(d.seekTime); });
        set('stop', () => this.audio.pause());
    }

    _updateMediaSession() {
        if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
        const t = this.current;
        try {
            navigator.mediaSession.metadata = t ? new MediaMetadata({
                title: t.title || 'EthOS Music',
                artist: t.artist || '',
                album: isEpisode(t) ? 'Podcast' : 'EthOS Music',
                artwork: t.thumbnail ? [{ src: t.thumbnail, sizes: '512x512', type: 'image/jpeg' }] : [],
            }) : null;
            navigator.mediaSession.playbackState = this.isPlaying ? 'playing' : 'paused';
        } catch { /* MediaMetadata unavailable */ }
        this._updatePositionState();
    }

    /** Lock-screen scrubber (duration + position); silently skipped for live streams. */
    _updatePositionState() {
        if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
        const ms = navigator.mediaSession;
        if (typeof ms.setPositionState !== 'function') return;
        const duration = Number(this.audio.duration);
        if (!Number.isFinite(duration) || duration <= 0) return;
        try {
            ms.setPositionState({
                duration,
                playbackRate: this.audio.playbackRate || 1,
                position: Math.min(Math.max(0, this.audio.currentTime || 0), duration),
            });
        } catch { /* live or unsupported */ }
    }

    // ── session (survives a reload) ────────────────────────────────────────

    saveSession() {
        if (!this.current) return;
        try {
            localStorage.setItem('ethos.music.session', JSON.stringify({
                queue: this.queue,
                index: this.index,
                pos: Number(this.audio.currentTime) || 0,
                shuffle: this.shuffle,
                repeat: this.repeat,
            }));
        } catch { /* private mode */ }
    }

    restoreSession() {
        try {
            if (this.queue.length || this.current) return;
            const raw = JSON.parse(localStorage.getItem('ethos.music.session') || 'null');
            if (!raw || !Array.isArray(raw.queue) || !raw.queue.length) return;
            this.queue = raw.queue;
            this.index = Math.min(Math.max(0, Number(raw.index) || 0), raw.queue.length - 1);
            this.shuffle = !!raw.shuffle;
            this.repeat = raw.repeat || 'off';
            if (!this.current) return;
            this._restoredPending = true;
            this._restoredPos = Number(raw.pos) || 0;
            this._emit('track');
            this._updateMediaSession();
        } catch { /* corrupt save */ }
    }

    get current() { return this.queue[this.index] || null; }
    get isPlaying() { return !this.audio.paused && !this.audio.ended; }

    // ── subscriptions ──────────────────────────────────────────────────────

    on(event, fn, key = null) {
        const arr = this._subs[event] || (this._subs[event] = []);
        if (key != null) {
            for (let i = arr.length - 1; i >= 0; i--) if (arr[i].key === key) arr.splice(i, 1);
        }
        arr.push({ fn, key });
    }

    off(event, key) {
        const arr = this._subs[event] || [];
        for (let i = arr.length - 1; i >= 0; i--) if (arr[i].key === key) arr.splice(i, 1);
    }

    _emit(event, data) {
        (this._subs[event] || []).forEach((sub) => sub.fn(data));
    }

    // ── transport ──────────────────────────────────────────────────────────

    playQueue(tracks, start = 0) {
        const list = (tracks || []).filter(Boolean);
        if (!list.length) return;
        this.queue = list.slice();
        this.index = Math.min(Math.max(0, start), this.queue.length - 1);
        this._playCurrent();
    }

    _pickNext() {
        const n = this.queue.length;
        if (n <= 1) return this.index;
        if (this.shuffle) {
            let i;
            do { i = Math.floor(Math.random() * n); } while (i === this.index);
            return i;
        }
        return (this.index + 1) % n;
    }

    async _playCurrent() {
        const track = this.current;
        if (!track) return;
        const seq = ++this._seq;
        this._retried = false;
        this._restoredPending = false;
        this._emit('track');
        this._updateMediaSession();
        this.saveSession();
        // HLS radio on a browser without native support needs hls.js.
        if (track.is_live && track.hls) {
            const native = this.audio.canPlayType('application/vnd.apple.mpegurl') !== '';
            if (!native) {
                const attached = await this._attachHls(playSrc(track));
                if (seq !== this._seq) return;
                if (attached) { this.audio.play().catch(() => {}); return; }
            }
        } else {
            this._destroyHls();
        }
        let src = null;
        try { src = await offline.localAudio(track); } catch { src = null; }
        if (seq !== this._seq) return;                       // another track was chosen
        this.audio.src = src || playSrc(track);
        this.audio.play().catch(() => { /* browsers may block until a gesture */ });
        if (!track.is_live) {
            import('./api.js').then(({ json }) => json('/api/music/history', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ track }),
            })).catch(() => {});
        }
    }

    async _attachHls(url) {
        const Hls = window.Hls;
        if (!Hls || !Hls.isSupported()) return false;
        try {
            this._destroyHls();
            const hls = new Hls({ enableWorker: true, lowLatencyMode: false });
            hls.loadSource(url);
            hls.attachMedia(this.audio);
            this._hls = hls;
            return true;
        } catch {
            this._destroyHls();
            return false;
        }
    }

    _destroyHls() {
        if (!this._hls) return;
        try { this._hls.destroy(); } catch { /* ignore */ }
        this._hls = null;
    }

    _onEnded() {
        if (this.repeat === 'one') {
            this.audio.currentTime = 0;
            this.audio.play().catch(() => {});
            return;
        }
        const n = this.queue.length;
        if (!n) return;
        if (!this.shuffle && this.index === n - 1 && this.repeat !== 'all') {
            this._emit('playing');
            return;
        }
        this.index = this._pickNext();
        this._playCurrent();
    }

    async _onError() {
        const track = this.current;
        if (!track) return;
        // A downloaded copy is the safest fallback when the stream breaks.
        try {
            const local = await offline.localAudio(track);
            if (local && this.audio.src !== local) {
                const wasPlaying = !this.audio.paused;
                this.audio.src = local;
                if (wasPlaying) this.audio.play().catch(() => {});
                return;
            }
        } catch { /* fall through */ }
        if (this._retried) {
            this._emit('error', { track });
            return;
        }
        this._retried = true;
        const wasPlaying = !this.audio.paused;
        setTimeout(() => {
            try { this.audio.load(); } catch { /* ignore */ }
            if (wasPlaying) this.audio.play().catch(() => {});
        }, 600);
    }

    next() {
        if (!this.queue.length) return;
        this.index = this._pickNext();
        this._playCurrent();
    }

    prev() {
        if (!this.queue.length) return;
        if ((this.audio.currentTime || 0) > 3) { this.audio.currentTime = 0; return; }
        const n = this.queue.length;
        this.index = (this.index - 1 + n) % n;
        this._playCurrent();
    }

    toggle() {
        if (!this.current) return;
        if (this._restoredPending) {
            const track = this.current;
            const pos = this._restoredPos || 0;
            this._restoredPending = false;
            this.audio.src = playSrc(track);
            if (pos > 1) {
                this.audio.addEventListener('loadedmetadata', () => {
                    try { this.audio.currentTime = pos; } catch { /* live */ }
                }, { once: true });
            }
            this.audio.play().catch(() => {});
            return;
        }
        if (this.isPlaying) this.audio.pause();
        else this.audio.play().catch(() => {});
    }

    seek(seconds) {
        try { this.audio.currentTime = seconds; } catch { /* live stream */ }
    }

    addToQueue(track) {
        if (!track) return;
        this.queue.push(track);
        this._emit('queue');
    }

    playNext(track) {
        if (!track) return;
        this.queue.splice(this.index + 1, 0, track);
        this._emit('queue');
    }

    jumpTo(index) {
        if (index < 0 || index >= this.queue.length) return;
        this.index = index;
        this._playCurrent();
    }

    /** Drop one queue entry (the player keeps playing the current track). */
    removeAt(index) {
        if (index < 0 || index >= this.queue.length) return;
        const removingCurrent = index === this.index;
        this.queue.splice(index, 1);
        if (!this.queue.length) {
            this.audio.pause();
            this.index = -1;
            this._emit('track');
            this._emit('queue');
            return;
        }
        if (index < this.index) this.index -= 1;
        else if (removingCurrent) {
            this.index = Math.min(this.index, this.queue.length - 1);
            this._playCurrent();
        }
        this._emit('queue');
    }

    clearQueue() {
        this.queue = [];
        this.index = -1;
        this.audio.pause();
        try { this.audio.removeAttribute('src'); this.audio.load(); } catch { /* ignore */ }
        // saveSession() ignores an empty queue, so drop the stored one explicitly.
        try { localStorage.removeItem('ethos.music.session'); } catch { /* ignore */ }
        this._emit('track');
        this._emit('queue');
    }

    toggleShuffle() {
        this.shuffle = !this.shuffle;
        this._emit('mode');
    }

    cycleRepeat() {
        this.repeat = this.repeat === 'off' ? 'all' : this.repeat === 'all' ? 'one' : 'off';
        this._emit('mode');
    }

    // ── sleep timer ────────────────────────────────────────────────────────

    setSleepTimer(minutes) {
        if (this._sleepTimer) { clearTimeout(this._sleepTimer); this._sleepTimer = null; }
        this._sleepMinutes = minutes > 0 ? minutes : 0;
        this._sleepEndsAt = this._sleepMinutes ? Date.now() + this._sleepMinutes * 60_000 : 0;
        if (!this._sleepMinutes) { this._emit('sleep'); return; }
        const tick = () => {
            if (Date.now() >= this._sleepEndsAt) {
                this.audio.pause();
                this._sleepMinutes = 0;
                this._sleepEndsAt = 0;
                this._sleepTimer = null;
                this._emit('sleep');
                return;
            }
            this._emit('sleep');
            this._sleepTimer = setTimeout(tick, 1000);
        };
        this._emit('sleep');
        this._sleepTimer = setTimeout(tick, 1000);
    }

    get sleepMinutes() { return this._sleepMinutes || 0; }
    get sleepLeftMs() { return this._sleepEndsAt ? Math.max(0, this._sleepEndsAt - Date.now()) : 0; }

    /** True when the track is stored on the device. */
    isLocal(track) {
        return offline.hasLocal(track);
    }
}

export const engine = new Engine();

/** Convenience for views that only need a key→boolean map. */
export function localKeys() {
    return new Set(offline.downloadsSnapshot().filter((row) => row.status === 'done').map((row) => row.key));
}

export { tkey };
