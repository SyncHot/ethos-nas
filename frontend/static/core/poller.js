// EthOS NAS — one shared, visibility-aware poller (TODO.md P3-09)
//
// Before this, eleven apps each ran their own `setInterval`: the interval kept
// firing while the tab was hidden, two apps polled the same endpoint at
// different rates, downloads additionally scheduled a follow-up fetch 3 s after
// every render, and nothing was ever cleared when the window closed — so closing
// and reopening an app stacked a new timer on the old one.
//
// `poll()` gives every app the same behaviour:
//
//   • pauses while `document.hidden` (background tab = zero requests) and runs
//     once immediately when the tab becomes visible again;
//   • skips a tick if the previous run is still in flight (slow NAS, small
//     interval → no pile-up);
//   • backs off on consecutive failures (2× per failure, capped) and resets on
//     the first success, so a dead endpoint is not hammered;
//   • stops automatically when the app's last window closes (window-manager's
//     `onAppClose`) and on `document` teardown.
//
// Usage:
//
//   import { poll } from '../core/poller.js';
//   const stopPoll = poll(() => fetchTasks(), 2000, { appId: 'gallery' });
//   // later, if the app closes its own window: stopPoll();
//
// `fn` may be async; its rejection is logged once per backoff step.

import { onAppClose } from './window-manager.js';

const MAX_BACKOFF_FACTOR = 8;

const _active = new Set(); // for tests / debugging: every live poller

export function poll(fn, intervalMs, { appId = null, immediate = true, name = '' } = {}) {
    const base = Math.max(250, Number(intervalMs) || 1000);
    let timer = null;
    let stopped = false;
    let busy = false;
    let failures = 0;
    let lastErrorAt = 0;

    const label = name || appId || 'poller';

    function delay() {
        return base * Math.min(MAX_BACKOFF_FACTOR, 2 ** failures);
    }

    async function tick() {
        if (stopped || document.hidden) return;
        if (busy) return; // previous run still going — skip, do not queue
        busy = true;
        try {
            await fn();
            failures = 0;
        } catch (err) {
            failures += 1;
            // Log the transition, not every retry: a down NAS would otherwise
            // fill the console.
            const now = Date.now();
            if (failures === 1 || now - lastErrorAt > 60_000) {
                console.debug(`[ethos] ${label} poll failed (${failures}×, next in ${delay()} ms):`,
                              err?.message || err);
                lastErrorAt = now;
            }
        } finally {
            busy = false;
            schedule();
        }
    }

    function schedule() {
        if (stopped) return;
        clearTimeout(timer);
        timer = setTimeout(tick, delay());
        if (timer && typeof timer.unref === 'function') timer.unref();
    }

    function stop() {
        stopped = true;
        clearTimeout(timer);
        timer = null;
        _active.delete(handle);
        document.removeEventListener('visibilitychange', onVisibility);
    }

    function onVisibility() {
        if (document.hidden) return;
        clearTimeout(timer);
        timer = null;
        tick(); // catch up immediately after the tab comes back
    }

    const handle = { stop, get stopped() { return stopped; }, get label() { return label; } };
    _active.add(handle);

    document.addEventListener('visibilitychange', onVisibility);
    if (appId) {
        // Multi-window apps only fire this when the last window closes.
        onAppClose(appId, stop);
    }

    if (immediate) tick();
    else schedule();

    return stop;
}

/** Live pollers (debugging / tests): `[{label, stopped}]`. */
export function activePollers() {
    return [..._active].map((p) => ({ label: p.label, stopped: p.stopped }));
}

/** Stop everything (used by tests and the restart overlay). */
export function stopAllPollers() {
    for (const p of [..._active]) p.stop();
}
