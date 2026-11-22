// EthOS NAS — System Overview (desktop shell style).
// Live CPU / memory / disk / temperature / network rate + container count.
// Data: /ws/stats pushes a system snapshot every 2 s (fallback: a slow 10 s
// /api/system/status poll), /api/containers is polled every 15 s. The socket
// and the timers self-terminate when the window is closed (isConnected guard)
// — there is no app-close lifecycle hook in this codebase.

import { apiFetch } from '../core/api.js';
import { poll } from '../core/poller.js';
import { connectWs } from '../core/ws.js';
import { onAppClose } from '../core/window-manager.js';
import { mountShell, applyBarWidths } from '../core/ui-shell.js';

const _ov = { ws: null, stops: [], lastNet: null, unsub: null };
let shell = null;

function fmtBytes(n) {
    if (n === null || n === undefined || !isFinite(n)) return '—';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n >= 100 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function fmtRate(bps) {
    if (bps === null || bps === undefined || !isFinite(bps)) return '—';
    return fmtBytes(Math.max(0, bps)) + '/s';
}

function fmtUptime(sec) {
    if (!sec && sec !== 0) return '—';
    const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m`;
}

function _tone(percent) {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    return value > 80 ? 'bad' : value > 50 ? 'warn' : 'ok';
}

function _bar(pct) {
    const value = Math.max(0, Math.min(100, Number(pct) || 0));
    return `<div class="ui-bar">
        <div class="ui-bar-fill ${_tone(value)}" data-barfill data-width="${value}"></div>
    </div>`;
}

export async function renderOverviewPage(body) {
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'overview',
        title: 'System Overview',
        subtitle: 'Live host resources — refreshed every 2 seconds.',
        icon: '📊',
        sections: [{ id: 'overview', label: 'Overview' }],
        rail: false,
        note: 'Live data from /api/system/status.',
    });
    shell.view.id = 'ovRoot';
    shell.view.innerHTML = `
        <div class="ui-summary">
            <div><span class="ui-summary-label">Hostname</span>
                <span class="ui-summary-value small" data-testid="overview-hostname" id="ovHostname">—</span></div>
            <div><span class="ui-summary-label">Uptime</span>
                <span class="ui-summary-value small" data-testid="overview-uptime" id="ovUptime">—</span></div>
            <div><span class="ui-summary-label">Temperature</span>
                <span class="ui-summary-value" data-testid="overview-temp" id="ovTemp">—</span></div>
            <div><span class="ui-summary-label">Containers</span>
                <span class="ui-summary-value small" data-testid="overview-containers" id="ovContainers">—</span></div>
        </div>

        <div class="ui-grid ui-grid-2">
            <section class="ui-panel" id="ovCardCpu">
                <h2>CPU</h2>
                <div class="ui-panel-body ui-row" style="align-items:center;">
                    <span data-testid="overview-cpu" id="ovCpuPct" class="ui-summary-value">—</span>
                    <div id="ovCpuCores" class="ui-list" style="flex:1;min-width:0;"></div>
                </div>
            </section>
            <section class="ui-panel" id="ovCardRam">
                <h2>Memory</h2>
                <div class="ui-panel-body">
                    <p data-testid="overview-ram" id="ovRamText" class="ui-muted">—</p>
                    ${_bar(0)}
                </div>
            </section>
            <section class="ui-panel" id="ovCardDisk">
                <h2>Disk /</h2>
                <div class="ui-panel-body">
                    <p data-testid="overview-disk" id="ovDiskText" class="ui-muted">—</p>
                    ${_bar(0)}
                </div>
            </section>
            <section class="ui-panel">
                <div class="ui-toolbar">
                    <h2>Network (live)</h2>
                    <button class="ui-btn small" data-action="open-network" data-testid="overview-net-details">Details →</button>
                </div>
                <div class="ui-panel-body">
                    <div class="ui-row">
                        <span data-testid="overview-net-rx" id="ovNetRx" class="ui-tag is-info">↓ —</span>
                        <span data-testid="overview-net-tx" id="ovNetTx" class="ui-tag is-warn">↑ —</span>
                    </div>
                </div>
            </section>
        </div>`;

    applyBarWidths(shell.view);
    shell.view.querySelector('[data-action="open-network"]')?.addEventListener('click', () => {
        if (typeof window.openApp === 'function') window.openApp('network');
    });
    shell.setStatus('Connecting · live WebSocket', 'ok');
    _startOverview();
}

function _setBar(el, pct) {
    const fill = el?.querySelector('[data-barfill]');
    if (fill) fill.style.width = `${Math.max(0, Math.min(100, Number(pct) || 0))}%`;
}

async function _tick() {
    if (!_rootAlive()) return _stopOverview();
    try {
        const res = await apiFetch('/api/system/status');
        if (!res.ok) return;
        _paintStats(await res.json());
    } catch (_) { /* transient — next tick retries */ }
}

function _rootAlive() {
    const root = document.getElementById('ovRoot');
    return Boolean(root && root.isConnected);
}

function _paintStats(s) {
    if (!_rootAlive()) { _stopOverview(); return; }
    // CPU
    const cpuEl = document.getElementById('ovCpuPct');
        if (cpuEl) cpuEl.textContent = `${Math.round(s.cpu_percent || 0)}%`;
        const coresEl = document.getElementById('ovCpuCores');
        if (coresEl && Array.isArray(s.cpu_per_core)) {
            coresEl.innerHTML = s.cpu_per_core.slice(0, 16).map((p) => {
                const value = Math.max(0, Math.min(100, Number(p) || 0));
                return `<div class="ui-bar" style="height:4px;"><div class="ui-bar-fill ${_tone(value)}" data-width="${value}"></div></div>`;
            }).join('');
            applyBarWidths(coresEl);
        }

        // Memory
        const mem = s.memory || {};
        const ramEl = document.getElementById('ovRamText');
        if (ramEl) ramEl.textContent = `${fmtBytes(mem.used)} / ${fmtBytes(mem.total)} (${Math.round(mem.percent || 0)}%)`;
        _setBar(document.getElementById('ovCardRam'), mem.percent);

        // Temperature
        const tempEl = document.getElementById('ovTemp');
        if (tempEl) tempEl.textContent = s.temperature != null ? `${Math.round(s.temperature)}°C` : '—';

        // Disk
        const d = s.disk || {};
        const diskEl = document.getElementById('ovDiskText');
        if (diskEl) diskEl.textContent = `${fmtBytes(d.used)} / ${fmtBytes(d.total)} (${Math.round(d.percent || 0)}%)`;
        _setBar(document.getElementById('ovCardDisk'), d.percent);

        // Network rate from counter deltas between ticks (~2 s apart)
        const net = s.network || {};
        if (_ov.lastNet) {
            const dt = Math.max(0.5, (Date.now() - _ov.lastNet.t) / 1000);
            const rx = Math.max(0, (net.bytes_recv - _ov.lastNet.rx) / dt);
            const tx = Math.max(0, (net.bytes_sent - _ov.lastNet.tx) / dt);
            const rxE = document.getElementById('ovNetRx');
            const txE = document.getElementById('ovNetTx');
            if (rxE) rxE.textContent = `↓ ${fmtRate(rx)}`;
            if (txE) txE.textContent = `↑ ${fmtRate(tx)}`;
        }
        _ov.lastNet = { rx: net.bytes_recv, tx: net.bytes_sent, t: Date.now() };

        // Header
        const hn = document.getElementById('ovHostname');
        if (hn) hn.textContent = s.hostname || '—';
        const up = document.getElementById('ovUptime');
        if (up) up.textContent = fmtUptime(s.uptime_seconds);
}

async function _loadContainers() {
    if (!_rootAlive()) return;
    try {
        const res = await apiFetch('/api/containers');
        if (!res.ok) return;
        const d = await res.json();
        const list = d.containers || [];
        const running = list.filter(c => c.status === 'running').length;
        const el = document.getElementById('ovContainers');
        if (el) el.textContent = `${running} running / ${list.length} total`;
    } catch (_) { /* docker may be down */ }
}

function _setStatus(text, tone) {
    if (shell && _rootAlive()) shell.setStatus(text, tone);
}

function _openStatsWs() {
    if (_ov.ws || document.hidden || !_rootAlive()) return;
    _ov.ws = connectWs('/ws/stats', (msg) => {
        if (!document.hidden) _paintStats(msg);
    }, {
        appId: 'overview',
        name: 'overview-stats',
        isJson: true,
        onOpen: () => _setStatus('Live · WebSocket', 'ok'),
        onClose: () => _setStatus('Polling · 10 s', 'warn'),
        onAuthFailure: () => _setStatus('Session expired · polling', 'error'),
    });
}

function _onVisibility() {
    if (!_rootAlive()) return _stopOverview();
    if (document.hidden) {
        _ov.ws?.close();
        _ov.ws = null;
        return;
    }
    _tick();
    _openStatsWs();
}

function _startOverview() {
    _stopOverview();
    _ov.unsub = onAppClose('overview', () => _stopOverview());
    _tick();
    _loadContainers();
    _openStatsWs();
    // Shared visibility-aware pollers (core/poller.js): they pause while the tab
    // is hidden and stop when the window closes, so no timer outlives the app.
    // The slow HTTP tick is only a fallback for a socket that cannot connect.
    _ov.stops = [
        poll(_tick, 10_000, { appId: 'overview', immediate: false, name: 'overview-stats-fallback' }),
        poll(_loadContainers, 15_000, { appId: 'overview', immediate: false, name: 'overview-containers' }),
    ];
    document.addEventListener('visibilitychange', _onVisibility);
}

function _stopOverview() {
    _ov.ws?.close();
    _ov.ws = null;
    _ov.unsub?.();
    _ov.unsub = null;
    document.removeEventListener('visibilitychange', _onVisibility);
    for (const stop of _ov.stops || []) {
        try { stop(); } catch (_) { /* already gone */ }
    }
    _ov.stops = [];
    _ov.lastNet = null;
}
