// EthOS NAS — Network (rail: Interfaces · Firewall)
import { escapeHtml, formatBytes, formatSpeed } from '../core/utils.js';
import { apiRequest } from '../core/api.js';
import { poll } from '../core/poller.js';
import { skeletonRows, emptyState, errorState } from '../core/states.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';

const SECTIONS = [
    { id: 'interfaces', label: 'Interfaces', title: 'Network Interfaces', subtitle: 'Links, addresses and throughput history.', testId: 'net-tab-interfaces' },
    { id: 'firewall', label: 'Firewall', title: 'Raw Firewall Rules', subtitle: 'Read-only iptables view — UFW rules live in Security.', testId: 'net-tab-firewall' },
];

let shell = null;
let navToken = 0;
let currentSection = 'interfaces';
let _thruStop = null;
let _thruMinutes = 60;

function _active() {
    return Boolean(shell && shell.root.isConnected && currentSection === 'interfaces');
}

export async function renderNetworkPage(body) {
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'network',
        title: 'Network',
        subtitle: 'Interfaces, throughput and the firewall.',
        icon: '🌐',
        sections: SECTIONS,
        note: 'Firewall changes are made in the Security app.',
    });
    shell.onSection(_runSection);
    shell.setActive('interfaces');
}

function _runSection(id) {
    currentSection = id;
    if (id !== 'interfaces' && _thruStop) {
        _thruStop();
        _thruStop = null;
    }
    const token = ++navToken;
    const isCurrent = () => token === navToken && shell && shell.root.isConnected;
    if (id === 'interfaces') loadInterfaces(isCurrent);
    else if (id === 'firewall') loadFirewall(isCurrent);
}

// ── Interfaces ──────────────────────────────────────────────────────────────

async function loadInterfaces(isCurrent) {
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
            return `<tr>
                <td>${escapeHtml(iface.name || '—')}</td>
                <td class="path">${escapeHtml(iface.ipv4 || iface.ip || '—')}</td>
                <td class="path">${escapeHtml(iface.mac || '—')}</td>
                <td><span class="ui-tag ${up ? 'is-ok' : 'is-warn'}">${up ? 'Up' : 'Down'}</span></td>
                <td>${iface.speed_mbps ? `${escapeHtml(String(iface.speed_mbps))} Mb/s` : '—'}</td>
                <td class="num">${escapeHtml(rx)}</td>
                <td class="num">${escapeHtml(tx)}</td>
            </tr>`;
        }).join('');

        content.innerHTML = `
            ${_throughputCard()}
            ${_connectionCard(netStatus)}
            <div class="ui-toolbar">
                <h2>Interfaces</h2>
                <span class="ui-tag">${interfaces.length} interface${interfaces.length === 1 ? '' : 's'}</span>
            </div>
            ${interfaces.length ? `<div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table" data-testid="net-interfaces-table">
                    <thead><tr><th>Interface</th><th>IP</th><th>MAC</th><th>Status</th><th>Speed</th><th class="num">RX</th><th class="num">TX</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div></div>`
                : emptyState({ icon: 'network', title: 'No network interfaces found', hint: 'Check the host network configuration.' })}`;

        _wireThroughputWindow(content);
        applyBarWidths(content);
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
        <h2>Connection</h2>
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
            loadThroughput(_active);
        });
    });
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

async function loadThroughput(isCurrent) {
    if (_thruStop) { _thruStop(); _thruStop = null; }

    const draw = async () => {
        if (!_active() || (isCurrent && !isCurrent())) return;
        try {
            const data = await apiRequest(`/api/network/stats/history?minutes=${_thruMinutes}`);
            if (!_active()) return;
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
    if (!_active()) return;
    _thruStop = poll(draw, 30_000, { appId: 'network', immediate: false, name: 'network-draw' });
}

// ── Firewall ────────────────────────────────────────────────────────────────

async function loadFirewall(isCurrent) {
    const content = shell.view;
    content.innerHTML = skeletonRows(5);
    shell.setStatus('Loading firewall rules…');

    try {
        const [status, data] = await Promise.all([
            apiRequest('/api/network/firewall/status').catch((err) => {
                logError('Network: firewall status unavailable', err?.detail || err?.message);
                return {};
            }),
            apiRequest('/api/network/firewall').catch((err) => {
                logError('Network: firewall rules unavailable', err?.detail || err?.message);
                return { rules: [] };
            }),
        ]);
        if (!isCurrent()) return;

        const rules = data.rules || [];
        const state = status.active === undefined
            ? '<span class="ui-tag">unknown</span>'
            : (status.active ? '<span class="ui-tag is-ok">active</span>' : '<span class="ui-tag is-warn">inactive</span>');
        const backend = status.using_nftables ? 'nftables' : 'iptables';

        const rows = rules.map((rule) => `<tr>
            <td>${escapeHtml(rule.chain)}</td>
            <td>${escapeHtml(rule.protocol)}</td>
            <td class="path">${escapeHtml(rule.source)}</td>
            <td class="path">${escapeHtml(rule.destination)}</td>
            <td>${rule.dport || rule.sport ? escapeHtml(rule.dport || rule.sport) : '—'}</td>
            <td><span class="ui-tag ${rule.action === 'ACCEPT' ? 'is-ok' : 'is-warn'}">${escapeHtml(rule.action)}</span></td>
            <td class="ui-muted">${escapeHtml(rule.comment || '—')}</td>
        </tr>`).join('');

        content.innerHTML = `
            <div class="ui-callout info" data-testid="fw-status">
                ${state} <strong>${escapeHtml(backend)}</strong> backend · ${rules.length} raw rule(s).
                This view is read-only — change rules, bans and logging in the Firewall &amp; Access Control app.
                <div class="ui-row ui-panel-body">
                    <button class="ui-btn small primary" data-action="open-security" data-testid="fw-open-security">Open Firewall &amp; Access Control →</button>
                </div>
            </div>
            ${rules.length ? `<div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table" data-testid="fw-rules-table">
                    <thead><tr><th>Chain</th><th>Protocol</th><th>Source</th><th>Dest</th><th>Port</th><th>Action</th><th>Comment</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div></div>`
                : emptyState({ icon: 'shield', title: 'No firewall rules found', hint: 'UFW rules are managed in the Security app.' })}`;

        content.querySelector('[data-action="open-security"]')?.addEventListener('click', () => {
            if (typeof window.openApp === 'function') window.openApp('security');
        });
        shell.setStatus(`${rules.length} raw rule(s) · ${backend}`, 'ok');
    } catch (err) {
        if (!isCurrent()) return;
        content.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read firewall rules', 'error');
        logError('Network: firewall view failed', err?.detail || err?.message);
    }
}
