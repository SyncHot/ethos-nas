// EthOS NAS — System Overview (desktop shell style).
// Live CPU / memory / disk / temperature / network gauges + container count.
// Data: 2 s poll of /api/system/status (rate computed from counter deltas),
// 60 s poll of the network throughput history for the sparkline, 15 s poll
// of /api/containers. All timers self-terminate when the window is closed
// (isConnected guard) — there is no app-close lifecycle hook in this codebase.

import { escapeHtml } from '../core/utils.js';
import { apiFetch } from '../core/api.js';
import { poll } from '../core/poller.js';
import { mountShell, applyBarWidths } from '../core/ui-shell.js';

const _ov = { timer: null, histTimer: null, contTimer: null, lastNet: null };
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
                <h2>Network (live)</h2>
                <div class="ui-panel-body">
                    <div class="ui-row">
                        <span data-testid="overview-net-rx" id="ovNetRx" class="ui-tag is-info">↓ —</span>
                        <span data-testid="overview-net-tx" id="ovNetTx" class="ui-tag is-warn">↑ —</span>
                    </div>
                    <canvas id="ovNetChart" width="560" height="48" class="ui-canvas" style="height:48px;"></canvas>
                </div>
            </section>
        </div>`;

    applyBarWidths(shell.view);
    shell.setStatus('Live · 2 s refresh', 'ok');
    _startOverview();
}

function _setBar(el, pct) {
    const fill = el?.querySelector('[data-barfill]');
    if (fill) fill.style.width = `${Math.max(0, Math.min(100, Number(pct) || 0))}%`;
}

async function _tick() {
    const root = document.getElementById('ovRoot');
    if (!root || !root.isConnected) return _stopOverview();
    try {
        const res = await apiFetch('/api/system/status');
        if (!res.ok) return;
        const s = await res.json();

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
    } catch (_) { /* transient — next tick retries */ }
}

async function _loadNetHistory() {
    const root = document.getElementById('ovRoot');
    if (!root || !root.isConnected) return;
    try {
        const res = await apiFetch('/api/network/stats/history?minutes=30');
        if (!res.ok) return;
        const d = await res.json();
        _drawNetChart(d.samples || []);
    } catch (_) { /* ignore */ }
}

function _drawNetChart(samples) {
    const canvas = document.getElementById('ovNetChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (!samples.length) return;

    let max = 1;
    for (const s of samples) max = Math.max(max, s.rx_bps || 0, s.tx_bps || 0);
    const n = samples.length;

    const line = (key, color) => {
        ctx.beginPath();
        samples.forEach((s, i) => {
            const x = (i / Math.max(1, n - 1)) * W;
            const y = H - ((s[key] || 0) / max) * (H - 4) - 2;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.stroke();
    };
    const token = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    line('rx_bps', token('--ui-accent'));
    line('tx_bps', token('--ui-warn'));
}

async function _loadContainers() {
    const root = document.getElementById('ovRoot');
    if (!root || !root.isConnected) return;
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

function _startOverview() {
    _stopOverview();
    _tick();
    _loadNetHistory();
    _loadContainers();
    // Shared visibility-aware pollers (core/poller.js): they pause while the tab
    // is hidden and stop when the window closes, so no timer outlives the app.
    _ov.stops = [
        poll(_tick, 2000, { appId: 'overview', immediate: false, name: 'overview-stats' }),
        poll(_loadNetHistory, 60_000, { appId: 'overview', immediate: false, name: 'overview-net' }),
        poll(_loadContainers, 15_000, { appId: 'overview', immediate: false, name: 'overview-containers' }),
    ];
}

function _stopOverview() {
    for (const stop of _ov.stops || []) {
        try { stop(); } catch (_) { /* already gone */ }
    }
    _ov.stops = [];
    _ov.lastNet = null;
}
