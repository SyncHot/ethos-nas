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
import { formatBytes, formatSpeed, formatUptime, escapeAttr, escapeHtml } from '../core/utils.js';
import { mountShell, applyBarWidths } from '../core/ui-shell.js';

const _ov = { ws: null, stops: [], lastNet: null, unsub: null };
let shell = null;

// Rates need "—" for a missing counter; core/formatSpeed assumes a number.
function fmtRate(bps) {
    if (bps === null || bps === undefined || !isFinite(bps)) return '—';
    return formatSpeed(Math.max(0, bps));
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
        <div id="ovAlerts" data-testid="overview-alerts" style="display:none;"></div>
        <div class="ui-summary">
            <div><span class="ui-summary-label">Hostname</span>
                <span class="ui-summary-value small" data-testid="overview-hostname" id="ovHostname">—</span></div>
            <div><span class="ui-summary-label">Uptime</span>
                <span class="ui-summary-value small" data-testid="overview-uptime" id="ovUptime">—</span></div>
            <div data-open-app="smart" role="button" tabindex="0" title="Open SMART Health" style="cursor:pointer;">
                <span class="ui-summary-label">Temperature</span>
                <span class="ui-summary-value" data-testid="overview-temp" id="ovTemp">—</span></div>
            <div data-open-app="services" role="button" tabindex="0" title="Open Service Manager" style="cursor:pointer;">
                <span class="ui-summary-label">Containers</span>
                <span class="ui-summary-value small" data-testid="overview-containers" id="ovContainers">—</span></div>
        </div>

        <div class="ui-grid ui-grid-2">
            <section class="ui-panel" id="ovCardCpu" data-open-app="processes" role="button" tabindex="0" title="Open Process Manager" style="cursor:pointer;">
                <h2>CPU</h2>
                <div class="ui-panel-body ui-row" style="align-items:center;">
                    <span data-testid="overview-cpu" id="ovCpuPct" class="ui-summary-value">—</span>
                    <div id="ovCpuCores" class="ui-list" style="flex:1;min-width:0;"></div>
                </div>
            </section>
            <section class="ui-panel" id="ovCardRam" data-open-app="processes" role="button" tabindex="0" title="Open Process Manager" style="cursor:pointer;">
                <h2>Memory</h2>
                <div class="ui-panel-body">
                    <p data-testid="overview-ram" id="ovRamText" class="ui-muted">—</p>
                    ${_bar(0)}
                    <p data-testid="overview-swap" id="ovSwapText" class="ui-muted" style="margin-top:10px;">—</p>
                    <div id="ovSwapBar">${_bar(0)}</div>
                </div>
            </section>
            <section class="ui-panel" id="ovCardDisk" data-open-app="storage" role="button" tabindex="0" title="Open Storage" style="cursor:pointer;">
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
    // Drill-downs: a card/summary tile opens the app that explains the number.
    shell.view.addEventListener('click', (event) => {
        const target = event.target.closest('[data-open-app]');
        if (!target) return;
        const appId = target.dataset.openApp;
        if (appId && typeof window.openApp === 'function') window.openApp(appId);
    });
    shell.view.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        const target = event.target.closest('[data-open-app]');
        if (!target) return;
        event.preventDefault();
        window.openApp?.(target.dataset.openApp);
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
        if (ramEl) ramEl.textContent = `${formatBytes(mem.used)} / ${formatBytes(mem.total)} (${Math.round(mem.percent || 0)}%)`;
        _setBar(document.getElementById('ovCardRam'), mem.percent);

        // Swap — a NAS that quietly swaps is about to feel slow.
        const swap = s.swap || {};
        const swapEl = document.getElementById('ovSwapText');
        if (swapEl) {
            swapEl.textContent = swap.total
                ? `Swap ${formatBytes(swap.used)} / ${formatBytes(swap.total)} (${Math.round(swap.percent || 0)}%)`
                : 'Swap not configured';
        }
        _setBar(document.getElementById('ovSwapBar'), swap.percent || 0);

        // Temperature
        const tempEl = document.getElementById('ovTemp');
        if (tempEl) tempEl.textContent = s.temperature != null ? `${Math.round(s.temperature)}°C` : '—';

        // Disk
        const d = s.disk || {};
        const diskEl = document.getElementById('ovDiskText');
        if (diskEl) diskEl.textContent = `${formatBytes(d.used)} / ${formatBytes(d.total)} (${Math.round(d.percent || 0)}%)`;
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
        if (up) up.textContent = formatUptime(s.uptime_seconds);
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

// ── Health alert strip ──────────────────────────────────────────────────────
// The health monitor already persists the facts (failed units, disk thresholds,
// SMART verdicts, temperatures); Overview surfaces them instead of hiding them
// in Settings → Health, and every alert links to the app that can fix it.
function _healthAlerts(health) {
    if (!health) return [];
    const alerts = [];
    const monitor = health.monitor || {};
    const diskWarn = Number(monitor.disk_warn_percent) || 90;
    const tempWarn = Number(monitor.temp_warn_celsius) || 80;

    const failed = (health.failed_units || []).map((unit) => unit.name).filter(Boolean);
    if (failed.length) {
        alerts.push({
            kind: 'bad', app: 'services', action: 'Open Services',
            text: `${failed.length} failed systemd unit(s): ${failed.slice(0, 3).join(', ')}${failed.length > 3 ? '…' : ''}`,
        });
    }

    const full = (health.disks || []).filter((disk) => Number(disk.percent) >= diskWarn);
    if (full.length) {
        alerts.push({
            kind: 'warn', app: 'storage', action: 'Open Storage',
            text: `Disk almost full: ${full.map((disk) => `${disk.mount} ${Math.round(disk.percent)}%`).join(', ')}`,
        });
    }

    const failing = (health.smart?.results || []).filter((disk) => disk.ok && disk.passed === false);
    if (failing.length) {
        alerts.push({
            kind: 'bad', app: 'smart', action: 'Open SMART',
            text: `SMART reports a failing disk: ${failing.map((disk) => disk.device).join(', ')}`,
        });
    }

    const hot = (health.temps || []).filter((temp) => Number(temp.current) >= tempWarn);
    if (hot.length) {
        alerts.push({
            kind: 'warn', app: 'smart', action: 'Open SMART',
            text: `Temperature above ${tempWarn} °C: ${hot.map((temp) => `${temp.name || 'sensor'} ${Math.round(temp.current)}°C`).join(', ')}`,
        });
    }
    return alerts;
}

function _paintHealth(health) {
    const host = document.getElementById('ovAlerts');
    if (!host) return;
    const alerts = _healthAlerts(health);
    host.style.display = alerts.length ? '' : 'none';
    host.innerHTML = alerts.map((alert) => `<div class="ui-callout${alert.kind === 'bad' ? ' error' : ''}" data-testid="overview-alert">
        <span>${escapeHtml(alert.text)}</span>
        <button class="ui-btn small" data-open-app="${escapeAttr(alert.app)}">${escapeHtml(alert.action)}</button>
    </div>`).join('');
}

async function _loadHealth() {
    if (!_rootAlive()) return;
    try {
        const res = await apiFetch('/api/health/status');
        if (!res.ok) return;
        _paintHealth(await res.json());
    } catch (_) { /* health is optional — never break the dashboard */ }
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
    _loadHealth();
    _openStatsWs();
    // Shared visibility-aware pollers (core/poller.js): they pause while the tab
    // is hidden and stop when the window closes, so no timer outlives the app.
    // The slow HTTP tick is only a fallback for a socket that cannot connect.
    _ov.stops = [
        poll(_tick, 10_000, { appId: 'overview', immediate: false, name: 'overview-stats-fallback' }),
        poll(_loadContainers, 15_000, { appId: 'overview', immediate: false, name: 'overview-containers' }),
        poll(_loadHealth, 60_000, { appId: 'overview', immediate: false, name: 'overview-health' }),
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
