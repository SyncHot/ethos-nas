// EthOS NAS — one hardened WebSocket helper (TODO.md P3-08)
//
// Every websocket in the app goes through `connectWs()`. It returns a
// *controller*, not the raw socket — the old version returned the original
// socket, so after a reconnect the caller held a dead handle and could not close
// the live one.
//
//   const ctl = connectWs('/ws/stats', (msg) => paint(msg), {
//       appId: 'overview',            // auto-close when that app's window closes
//       onOpen: () => {}, onClose: (ev) => {}, onError: (ev) => {},
//       isJson: true,                 // parse JSON payloads (default)
//       heartbeatMs: 25000,           // ping the server to keep the socket warm
//   });
//   ctl.send({ subscribe: 'disks' });
//   ctl.close();                      // intentional: no reconnect
//
// Behaviour:
//   • exponential backoff with jitter (1 s → 30 s cap) — a dead NAS is not
//     retried 12× a minute by every open app;
//   • the first successful open resets the backoff;
//   • close codes 4001/4003 (auth, used by the PTY/logs endpoints) stop for good
//     and call `onAuthFailure` — retrying with an expired token only burns
//     sockets;
//   • a lost token also stops it (logout / expiry clears localStorage);
//   • closing the owning app's window closes the socket (`appId` + onAppClose).

import { onAppClose } from './window-manager.js';

const BASE_DELAY_MS = 1000;
const MAX_DELAY_MS = 30000;
const AUTH_CLOSE_CODES = new Set([4001, 4003, 4401, 4403]);

export function _wsUrl(url) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    let full = url.startsWith('ws') || url.startsWith('wss') ? url : `${proto}//${location.host}${url}`;
    // Attach JWT token so backend WS auth middleware can validate it.
    if (!full.includes('?')) full += '?';
    else if (!full.endsWith('&')) full += '&';
    const token = localStorage.getItem('token') || '';
    full += `token=${encodeURIComponent(token)}`;
    return full;
}

export function getProtocol() {
    return location.protocol === 'https:' ? 'wss:' : 'ws:';
}

/** Exponential backoff with full jitter (so N tabs do not retry in lockstep). */
export function _backoffDelay(attempt) {
    const raw = Math.min(BASE_DELAY_MS * 2 ** attempt, MAX_DELAY_MS);
    return Math.round(raw * (0.5 + Math.random() * 0.5));
}

/**
 * Open a websocket and keep it open. Returns a controller:
 * `{close(code, reason), send(data), readyState, closed, raw}`.
 */
export function connectWs(url, onMessage, opts = {}) {
    const {
        appId = null,
        isJson = true,
        reconnect = true,
        onOpen = null,
        onClose = null,
        onError = null,
        onAuthFailure = null,
        onReconnectScheduled = null,
        stopOnCodes = null,      // permanent failures for this endpoint (e.g. 4002 target rejected)
        heartbeatMs = 0,
        binaryType = null,       // 'arraybuffer' for PTY-style endpoints
        name = '',
    } = opts;

    let socket = null;
    let attempt = 0;
    let closed = false;          // caller asked to stop
    let retryTimer = null;
    let heartbeat = null;
    let unsubscribeApp = null;

    const label = name || url;

    function stopTimers() {
        if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
        if (heartbeat) { clearInterval(heartbeat); heartbeat = null; }
    }

    function scheduleRetry(ev) {
        if (closed || !reconnect) return;
        if (!localStorage.getItem('token')) {
            closed = true;
            onAuthFailure?.(ev);
            return;
        }
        const delay = _backoffDelay(attempt++);
        retryTimer = setTimeout(open, delay);
        try { onReconnectScheduled?.(delay, ev); } catch (err) { console.warn('[ethos ws]', label, err); }
    }

    function open() {
        if (closed) return;
        retryTimer = null;
        try {
            socket = new WebSocket(_wsUrl(url));
        } catch (err) {
            onError?.(err);
            scheduleRetry({ code: 0, reason: String(err) });
            return;
        }
        const current = socket;
        if (binaryType) current.binaryType = binaryType;

        current.onopen = () => {
            if (closed || socket !== current) return;
            attempt = 0;
            onOpen?.(current);
            if (heartbeatMs > 0) {
                heartbeat = setInterval(() => {
                    if (current.readyState === WebSocket.OPEN) {
                        try { current.send(JSON.stringify({ type: 'ping' })); } catch (_) { /* closing */ }
                    }
                }, heartbeatMs);
            }
        };

        current.onmessage = (event) => {
            if (socket !== current) return;
            if (!isJson) { onMessage(event.data); return; }
            try { onMessage(JSON.parse(event.data)); } catch (_) { /* non-JSON frame */ }
        };

        current.onerror = (event) => { if (socket === current) onError?.(event); };

        current.onclose = (event) => {
            if (socket !== current) return;
            socket = null;
            stopTimers();
            if (closed) return;
            if (onClose) { try { onClose(event); } catch (err) { console.warn('[ethos ws]', label, err); } }
            if (AUTH_CLOSE_CODES.has(event.code)) {
                closed = true;
                onAuthFailure?.(event);
                return;
            }
            if (Array.isArray(stopOnCodes) && stopOnCodes.includes(event.code)) {
                closed = true;   // permanent condition — retrying cannot help
                return;
            }
            scheduleRetry(event);
        };
    }

    function close(code = 1000, reason = 'client closed') {
        closed = true;
        stopTimers();
        unsubscribeApp?.();
        unsubscribeApp = null;
        const current = socket;
        socket = null;
        if (current && current.readyState <= WebSocket.OPEN) {
            try { current.close(code, reason); } catch (_) { /* already gone */ }
        }
    }

    function send(data) {
        if (!socket || socket.readyState !== WebSocket.OPEN) return false;
        try {
            // Strings and binary frames go out verbatim (the PTY sends raw bytes);
            // only plain objects are serialised. Stringifying a Uint8Array here
            // used to type `{"0":101,…}` into the shell.
            const isBinary = data instanceof ArrayBuffer || ArrayBuffer.isView(data)
                || (typeof Blob !== 'undefined' && data instanceof Blob);
            socket.send(typeof data === 'string' || isBinary ? data : JSON.stringify(data));
            return true;
        } catch (_) {
            return false;
        }
    }

    if (appId) unsubscribeApp = onAppClose(appId, () => close(1000, 'app closed'));

    open();

    return {
        close,
        send,
        get readyState() { return socket ? socket.readyState : WebSocket.CLOSED; },
        get closed() { return closed; },
        /** Live socket — only for `binaryType` / focus wiring, never to attach handlers. */
        get raw() { return socket; },
    };
}
