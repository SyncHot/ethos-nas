// EthOS NAS — the only module that knows the Logs API and socket shape.
//
// Apps read history with `fetchLogs()` and stream with `streamLogs()`; they never
// build `/api/logs` URLs or open `/ws/logs` themselves (docs/logs.md). Auth,
// reconnect/backoff and the close-code policy live in core/ws.js.

import { apiJsonFetch } from './api.js';
import { connectWs } from './ws.js';

/** Unified event history (admin only). */
export async function fetchLogs({ limit = 200, level = '', app = '', source = '', search = '', since = 0 } = {}) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (level) params.set('level', level);
    if (app) params.set('app', app);
    if (source) params.set('source', source);
    if (search) params.set('search', search);
    if (since) params.set('since', String(since));
    return apiJsonFetch(`/api/logs?${params.toString()}`);
}

export const fetchLogApps = () => apiJsonFetch('/api/logs/apps');
export const fetchLogStats = () => apiJsonFetch('/api/logs/stats');
export const fetchLogFiles = () => apiJsonFetch('/api/logs/files');
export const fetchLogContainers = () => apiJsonFetch('/api/logs/containers');
export const fetchJournal = (lines = 200, unit = '') =>
    apiJsonFetch(`/api/logs/journal?lines=${lines}${unit ? `&unit=${encodeURIComponent(unit)}` : ''}`);
export const tailLogFile = (path, lines = 200) =>
    apiJsonFetch(`/api/logs/tail?path=${encodeURIComponent(path)}&lines=${lines}`);
export const clearLogs = () => apiJsonFetch('/api/logs', { method: 'DELETE' });

/**
 * Stream one source as JSON frames (docs/logs.md).
 *
 * `source`: `app` | `container:<name>` | `file:<path>` | `journal` (+ `unit`).
 * Returns the `connectWs` controller: `{ close(), send(), ... }`.
 */
export function streamLogs({
    source = 'app',
    tail = 200,
    unit = '',
    appId = 'logs',
    name = '',
    onEntry,
    onLine,
    onError,
    onEnd,
    onTruncated,
    onOpen,
    onClose,
} = {}) {
    const params = new URLSearchParams({ source });
    if (source !== 'app' && tail) params.set('tail', String(tail));
    if (unit) params.set('unit', unit);

    return connectWs(`/ws/logs?${params.toString()}`, (message) => {
        if (!message || typeof message !== 'object') return;
        switch (message.type) {
            case 'entry': onEntry?.(message.entry); break;
            case 'line': onLine?.(message.line, message.source || source); break;
            case 'end': onEnd?.(message.detail || ''); break;
            case 'truncated': onTruncated?.(message.sent || 0); break;
            case 'error': onError?.(message.detail || 'Log stream error'); break;
            default: break;
        }
    }, {
        appId,
        isJson: true,
        name: name || `logs:${source}`,
        onOpen,
        onError: () => onError?.('Could not open the log stream'),
        onClose: (event) => {
            if (event?.code === 4003) onError?.('Admin access required for this source');
            else if (event?.code === 4001) onError?.('Session expired — sign in again');
            onClose?.(event);
        },
    });
}
