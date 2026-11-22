// EthOS NAS — Network (interfaces, connection and throughput; read-only).
// Firewall state and management live in the Firewall & Access Control app.
import { escapeAttr, escapeHtml, formatBytes, formatSpeed } from '../core/utils.js';
import { apiRequest, isAdmin } from '../core/api.js';
import { poll } from '../core/poller.js';
import { skeletonRows, emptyState, errorState } from '../core/states.js';
import { notify } from '../core/notification-center.js';
import { logError } from '../core/logger.js';
import { mountShell, applyBarWidths } from '../core/ui-shell.js';

let shell = null;
let _thruStop = null;
let _ifaceStop = null;
let _lastIfaces = null;
let _thruMinutes = 60;

function _alive() {
    return Boolean(shell && shell.root.isConnected);
}

export async function renderNetworkPage(body) {
    if (_thruStop) { _thruStop(); _thruStop = null; }
    if (_ifaceStop) { _ifaceStop(); _ifaceStop = null; }
    _lastIfaces = null;
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'network',
        title: 'Network',
        subtitle: 'Interfaces, connection and throughput — read-only.',
        icon: '🌐',
        sections: [],
        rail: false,
    });
    loadInterfaces();
}

// ── Interfaces ──────────────────────────────────────────────────────────────

async function loadInterfaces() {
    const isCurrent = _alive;
    if (_ifaceStop) { _ifaceStop(); _ifaceStop = null; }
    const content = shell.view;
    content.innerHTML = skeletonRows(6);
    shell.setStatus('Loading interfaces…');

    try {
        const [data, netStatus] = await Promise.all([
            apiRequest('/api/network/interfaces'),
            apiRequest('/api/network/status').catch((err) => {
                logError('Network: status unavailable', err?.detail || err?.message);
                return {};
            }),
        ]);
        if (!isCurrent()) return;

        const interfaces = Array.isArray(data) ? data : (data.interfaces || []);
        const rows = interfaces.map((iface) => {
            const up = iface.status === 'up';
            const rx = iface.bytes_recv_human || (iface.rx_bytes != null ? formatBytes(iface.rx_bytes) : '—');
            const tx = iface.bytes_sent_human || (iface.tx_bytes != null ? formatBytes(iface.tx_bytes) : '—');
            const ip = iface.ipv4 || iface.ip || '';
            const errors = (Number(iface.errors_in) || 0) + (Number(iface.errors_out) || 0)
                + (Number(iface.drops_in) || 0) + (Number(iface.drops_out) || 0);
            return `<tr data-testid="net-iface-row-${escapeAttr(iface.name || 'if')}">
                <td>${escapeHtml(iface.name || '—')}</td>
                <td class="path">${escapeHtml(ip || '—')}${ip ? `<button class="ui-btn small" data-copy="${escapeAttr(ip)}" title="Copy IP address" aria-label="Copy IP address" data-testid="net-copy-ip-${escapeAttr(iface.name || 'if')}">⧉</button>` : ''}</td>
                <td class="path">${escapeHtml(iface.mac || '—')}${iface.mac ? `<button class="ui-btn small" data-copy="${escapeAttr(iface.mac)}" title="Copy MAC address" aria-label="Copy MAC address" data-testid="net-copy-mac-${escapeAttr(iface.name || 'if')}">⧉</button>` : ''}</td>
                <td><span class="ui-tag ${up ? 'is-ok' : 'is-warn'}">${up ? 'Up' : 'Down'}</span></td>
                <td>${iface.speed_mbps ? `${escapeHtml(String(iface.speed_mbps))} Mb/s` : '—'}</td>
                <td class="num">${escapeHtml(rx)}<div class="ui-muted" data-rx-rate="${escapeAttr(iface.name || '')}">—</div></td>
                <td class="num">${escapeHtml(tx)}<div class="ui-muted" data-tx-rate="${escapeAttr(iface.name || '')}">—</div></td>
                <td class="num">${errors ? `<span class="ui-tag is-warn">${errors}</span>` : '—'}</td>
            </tr>`;
        }).join('');

        content.innerHTML = `
            ${_throughputCard()}
            ${_connectionCard(netStatus)}
            <div class="ui-toolbar">
                <h2>Interfaces</h2>
                <div class="ui-row">
                    <span class="ui-tag">${interfaces.length} interface${interfaces.length === 1 ? '' : 's'}</span>
                    <button class="ui-btn small" data-refresh-ifaces data-testid="net-refresh-ifaces">Refresh</button>
                </div>
            </div>
            ${interfaces.length ? `<div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table" data-testid="net-interfaces-table">
                    <thead><tr><th>Interface</th><th>IP</th><th>MAC</th><th>Status</th><th>Speed</th><th class="num">RX</th><th class="num">TX</th><th class="num">Errors</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div></div>`
                : emptyState({ icon: 'network', title: 'No network interfaces found', hint: 'Check the host network configuration.' })}`;

        _wireThroughputWindow(content);
        _wireInterfaceActions(content);
        applyBarWidths(content);
        _lastIfaces = { t: Date.now(), map: _ifaceCounters(interfaces) };
        _startIfaceRates();
        await loadThroughput(isCurrent);
        if (!isCurrent()) return;
        shell.setStatus(`${interfaces.length} interface(s)`, 'ok');
    } catch (err) {
        if (!isCurrent()) return;
        content.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read interfaces', 'error');
        logError('Network: interfaces unavailable', err?.detail || err?.message);
    }
}

function _connectionCard(status) {
    const gateway = status?.gateway;
    const dns = Array.isArray(status?.dns_servers) ? status.dns_servers : [];
    const rows = [];
    if (gateway) {
        rows.push(`<div><dt class="ui-kv-key">Default gateway</dt><dd class="ui-kv-value mono">${escapeHtml(gateway.ip)}${gateway.interface ? ` <span class="ui-tag">${escapeHtml(gateway.interface)}</span>` : ''}</dd></div>`);
        if (gateway.source_ip) {
            rows.push(`<div><dt class="ui-kv-key">Source IP</dt><dd class="ui-kv-value mono">${escapeHtml(gateway.source_ip)}</dd></div>`);
        }
    } else {
        rows.push('<div><dt class="ui-kv-key">Default gateway</dt><dd class="ui-kv-value">No default route</dd></div>');
    }
    rows.push(`<div><dt class="ui-kv-key">DNS servers</dt><dd class="ui-kv-value mono">${dns.length ? dns.map((d) => escapeHtml(d)).join(' · ') : 'None configured'}</dd></div>`);

    return `<section class="ui-panel" data-testid="net-connection-card">
        <div class="ui-toolbar">
            <h2>Connection</h2>
            ${isAdmin() ? `<button class="ui-btn small" data-open-firewall data-testid="net-firewall-btn">🛡️ Firewall</button>` : ''}
        </div>
        <div class="ui-panel-body"><dl class="ui-kv">${rows.join('')}</dl></div>
    </section>`;
}

function _throughputCard() {
    const windows = [[30, '30 min'], [120, '2 h'], [720, '12 h']];
    return `<section class="ui-panel" data-testid="net-throughput-card">
        <div class="ui-toolbar">
            <h2>Throughput</h2>
            <div class="ui-row">
                <span id="netThruNow" class="ui-muted mono">—</span>
                ${windows.map(([minutes, label]) => `<button class="ui-btn small${minutes === _thruMinutes ? ' primary' : ''}"
                    data-window="${minutes}" data-testid="net-thru-win-${minutes}">${label}</button>`).join('')}
            </div>
        </div>
        <div class="ui-panel-body">
            <canvas id="netThruCanvas" class="ui-canvas"></canvas>
        </div>
    </section>`;
}

function _wireThroughputWindow(content) {
    content.querySelectorAll('[data-window]').forEach((button) => {
        button.addEventListener('click', () => {
            _thruMinutes = Number(button.dataset.window) || 60;
            content.querySelectorAll('[data-window]').forEach((other) => {
                other.classList.toggle('primary', other === button);
            });
            loadThroughput();
        });
    });
}

function _wireInterfaceActions(content) {
    content.querySelector('[data-refresh-ifaces]')?.addEventListener('click', () => loadInterfaces());
    content.querySelector('[data-open-firewall]')?.addEventListener('click', () => window.openApp?.('security'));
    content.querySelectorAll('[data-copy]').forEach((button) => {
        button.addEventListener('click', async () => {
            const value = button.dataset.copy;
            if (!value) return;
            try {
                await navigator.clipboard.writeText(value);
                notify({ kind: 'success', message: `${value} copied` });
            } catch (err) {
                logError('Network: clipboard unavailable', err?.message || String(err));
                notify({ kind: 'error', message: 'Could not copy to the clipboard' });
            }
        });
    });
}

function _ifaceCounters(interfaces) {
    const map = new Map();
    for (const iface of interfaces) {
        map.set(iface.name, {
            rx: Number(iface.bytes_recv) || 0,
            tx: Number(iface.bytes_sent) || 0,
        });
    }
    return map;
}

/** Poll the counters every 5 s and update only the rate cells (no re-render). */
function _startIfaceRates() {
    if (_ifaceStop) { _ifaceStop(); _ifaceStop = null; }
    _ifaceStop = poll(_ratesTick, 5_000, { appId: 'network', immediate: false, name: 'network-ifaces' });
}

async function _ratesTick() {
    if (!_alive()) return;
    try {
        const data = await apiRequest('/api/network/interfaces');
        if (!_alive()) return;
        const interfaces = Array.isArray(data) ? data : (data.interfaces || []);
        const now = Date.now();
        const previous = _lastIfaces;
        const dt = previous ? Math.max(0.5, (now - previous.t) / 1000) : 0;
        const map = _ifaceCounters(interfaces);
        if (previous && dt) {
            for (const [name, counters] of map) {
                const before = previous.map.get(name);
                if (!before) continue;
                const rx = Math.max(0, (counters.rx - before.rx) / dt);
                const tx = Math.max(0, (counters.tx - before.tx) / dt);
                const rxEl = shell.view.querySelector(`[data-rx-rate="${CSS.escape(name)}"]`);
                const txEl = shell.view.querySelector(`[data-tx-rate="${CSS.escape(name)}"]`);
                if (rxEl) rxEl.textContent = `↓ ${formatSpeed(rx)}`;
                if (txEl) txEl.textContent = `↑ ${formatSpeed(tx)}`;
            }
        }
        _lastIfaces = { t: now, map };
    } catch (err) {
        logError('Network: interface counters unavailable', err?.detail || err?.message);
    }
}

function _cssToken(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function _drawSparkline(canvas, points) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 600;
    const height = canvas.clientHeight || 120;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const context = canvas.getContext('2d');
    context.scale(dpr, dpr);
    context.clearRect(0, 0, width, height);

    if (!points.length) {
        context.fillStyle = _cssToken('--text-3');
        context.font = '12px system-ui, sans-serif';
        context.textAlign = 'center';
        context.fillText('Collecting samples… (one point per minute)', width / 2, height / 2);
        return;
    }

    const padL = 8, padR = 8, padT = 10, padB = 18;
    const maxValue = Math.max(...points.map((point) => Math.max(point.rx_bps, point.tx_bps)), 1);
    const x = (index) => points.length === 1
        ? (padL + width - padR) / 2
        : padL + (width - padL - padR) * index / (points.length - 1);
    const y = (value) => height - padB - (height - padT - padB) * (value / maxValue);

    context.strokeStyle = _cssToken('--ui-border');
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(padL, height - padB);
    context.lineTo(width - padR, height - padB);
    context.stroke();

    const series = [
        { key: 'tx_bps', color: _cssToken('--ui-ok') },
        { key: 'rx_bps', color: _cssToken('--ui-accent') },
    ];
    for (const line of series) {
        context.strokeStyle = line.color;
        context.lineWidth = 1.5;
        context.beginPath();
        points.forEach((point, index) => {
            if (index === 0) context.moveTo(x(index), y(point[line.key]));
            else context.lineTo(x(index), y(point[line.key]));
        });
        context.stroke();
    }

    const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    context.fillStyle = _cssToken('--text-3');
    context.font = '10px system-ui, sans-serif';
    context.textAlign = 'left';
    context.fillText(formatSpeed(maxValue), padL, 8);
    context.fillText(fmtTime(points[0].ts), padL, height - 4);
    context.textAlign = 'right';
    context.fillText(fmtTime(points[points.length - 1].ts), width - padR, height - 4);
}

async function loadThroughput() {
    if (_thruStop) { _thruStop(); _thruStop = null; }

    const draw = async () => {
        if (!_alive()) return;
        try {
            const data = await apiRequest(`/api/network/stats/history?minutes=${_thruMinutes}`);
            if (!_alive()) return;
            const points = data.samples || [];
            _drawSparkline(document.getElementById('netThruCanvas'), points);
            const now = document.getElementById('netThruNow');
            if (now) {
                now.textContent = points.length
                    ? `↓ ${formatSpeed(points[points.length - 1].rx_bps)} · ↑ ${formatSpeed(points[points.length - 1].tx_bps)}`
                    : 'collecting…';
            }
        } catch (err) {
            logError('Network: throughput history unavailable', err?.detail || err?.message);
        }
    };

    await draw();
    if (!_alive()) return;
    _thruStop = poll(draw, 30_000, { appId: 'network', immediate: false, name: 'network-draw' });
}
