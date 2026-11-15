// EthOS NAS — System Overview (desktop shell style).
// Live CPU / memory / disk / temperature / network gauges + container count.
// Data: 2 s poll of /api/system/status (rate computed from counter deltas),
// 60 s poll of the network throughput history for the sparkline, 15 s poll
// of /api/containers. All timers self-terminate when the window is closed
// (isConnected guard) — there is no app-close lifecycle hook in this codebase.

import { escapeHtml } from '../core/utils.js';
import { apiFetch } from '../core/api.js';
import { poll } from '../core/poller.js';

const _ov = { timer: null, histTimer: null, contTimer: null, lastNet: null };

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

function _bar(pct, color) {
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    return `<div style="height:8px;border-radius:4px;background:var(--surface-2);overflow:hidden;">
        <div data-barfill style="height:100%;width:${p}%;border-radius:4px;background:${color};transition:width .6s ease;"></div>
    </div>`;
}

function _card(title, icon) {
    return `<div class="glass-card" style="padding:14px 16px;">
        <div style="font-size:11px;color:var(--text-3);font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">${icon} ${title}</div>
    </div>`;
}

export async function renderOverviewPage(body) {
    body.innerHTML = `
        <div id="ovRoot" data-testid="overview-root" style="height:100%;overflow-y:auto;padding:16px 20px;font-family:system-ui,sans-serif;color:var(--text-1);">
            <!-- header -->
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;">
                <h2 style="margin:0;font-size:17px;font-weight:700;display:flex;align-items:center;gap:8px;"><span>📊</span> System Overview</h2>
                <div style="font-size:12px;color:var(--text-3);">
                    <span data-testid="overview-hostname" id="ovHostname">—</span>
                    <span style="margin:0 6px;">·</span>
                    up <span data-testid="overview-uptime" id="ovUptime">—</span>
                </div>
            </div>

            <!-- row 1: CPU / Memory / Temperature -->
            <div style="display:grid;grid-template-columns:2fr 1.4fr 0.8fr;gap:12px;margin-bottom:12px;">
                <div class="glass-card" id="ovCardCpu" style="padding:14px 16px;">
                    <div style="font-size:11px;color:var(--text-3);font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">🧠 CPU</div>
                    <div style="display:flex;align-items:center;gap:12px;">
                        <span data-testid="overview-cpu" id="ovCpuPct" style="font-size:26px;font-weight:700;">—</span>
                        <div style="flex:1;display:flex;flex-direction:column;gap:3px;" id="ovCpuCores"></div>
                    </div>
                </div>
                <div class="glass-card" id="ovCardRam" style="padding:14px 16px;">
                    <div style="font-size:11px;color:var(--text-3);font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">💾 Memory</div>
                    <div data-testid="overview-ram" id="ovRamText" style="font-size:14px;font-weight:600;margin-bottom:8px;">—</div>
                    ${_bar(0, '#3b82f6')}
                </div>
                <div class="glass-card" style="padding:14px 16px;display:flex;flex-direction:column;justify-content:center;">
                    <div style="font-size:11px;color:var(--text-3);font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">🌡️ Temp</div>
                    <span data-testid="overview-temp" id="ovTemp" style="font-size:22px;font-weight:700;">—</span>
                </div>
            </div>

            <!-- row 2: Disk / Network -->
            <div style="display:grid;grid-template-columns:1fr 1.6fr;gap:12px;margin-bottom:12px;">
                <div class="glass-card" id="ovCardDisk" style="padding:14px 16px;">
                    <div style="font-size:11px;color:var(--text-3);font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">💿 Disk /</div>
                    <div data-testid="overview-disk" id="ovDiskText" style="font-size:14px;font-weight:600;margin-bottom:8px;">—</div>
                    ${_bar(0, '#22c55e')}
                </div>
                <div class="glass-card" style="padding:14px 16px;">
                    <div style="font-size:11px;color:var(--text-3);font-weight:600;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px;">🌐 Network (live)</div>
                    <div style="display:flex;align-items:center;gap:16px;font-size:14px;font-weight:600;margin-bottom:8px;">
                        <span data-testid="overview-net-rx" id="ovNetRx" style="color:#3b82f6;">↓ —</span>
                        <span data-testid="overview-net-tx" id="ovNetTx" style="color:#f59e0b;">↑ —</span>
                    </div>
                    <canvas id="ovNetChart" width="560" height="48" style="width:100%;height:48px;"></canvas>
                </div>
            </div>

            <!-- row 3: containers -->
            <div class="glass-card" style="padding:12px 16px;display:flex;align-items:center;justify-content:space-between;">
                <span style="font-size:13px;color:var(--text-2);">🐳 Containers</span>
                <span data-testid="overview-containers" id="ovContainers" style="font-size:13px;font-weight:600;">—</span>
            </div>
        </div>`;

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
            coresEl.innerHTML = s.cpu_per_core.slice(0, 16).map(p =>
                `<div style="height:4px;border-radius:2px;background:var(--surface-2);overflow:hidden;flex:1;">
                    <div style="height:100%;width:${Math.max(0, Math.min(100, p))}%;background:#3b82f6;"></div>
                 </div>`).join('');
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
    line('rx_bps', '#3b82f6');   // download
    line('tx_bps', '#f59e0b');   // upload
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
