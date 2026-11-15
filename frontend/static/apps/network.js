// EthOS NAS — Network Manager App (Interfaces + Firewall)
import { escapeHtml, formatBytes, formatSpeed } from '../core/utils.js';
import { apiFetch } from '../core/api.js';
import { addNotification } from '../core/notification-center.js';
import { poll } from '../core/poller.js';
import { skeletonRows } from '../core/states.js';

let _netBody = null;
function _netActive() { return !!(_netBody && _netBody.isConnected); }

export async function renderNetworkPage(body) {
    _netBody = body;
    body.innerHTML = `<div style="padding:0;display:flex;flex-direction:column;height:100%;background:var(--surface-0);color: var(--text-1);font-family:system-ui,sans-serif;">
        <div style="display:flex;border-bottom:1px solid var(--border);flex-shrink:0;">
            <button data-testid="net-tab-interfaces" class="net-tab active" data-tab="interfaces" style="flex:1;padding:12px 20px;background:none;border:none;color: var(--text-2);font-size:13px;font-weight:600;cursor:pointer;border-bottom:2px solid transparent;transition:all 0.15s;">🌐 Interfaces</button>
            <button data-testid="net-tab-firewall" class="net-tab" data-tab="firewall" style="flex:1;padding:12px 20px;background:none;border:none;color: var(--text-2);font-size:13px;font-weight:600;cursor:pointer;border-bottom:2px solid transparent;transition:all 0.15s;">🛡️ Firewall</button>
        </div>
        <div id="netContent" style="flex:1;overflow-y:auto;padding:16px 20px;">${skeletonRows(6)}</div>
    </div>`;

    // Tab switching
    body.querySelectorAll('.net-tab').forEach(btn => {
        btn.onclick = () => {
            body.querySelectorAll('.net-tab').forEach(b => { b.style.color = 'var(--text-2)'; b.style.borderBottomColor = 'transparent'; });
            btn.style.color = 'white'; btn.style.borderBottomColor = '#3b82f6';
            const tab = btn.dataset.tab;
            if (tab === 'interfaces') loadInterfaces(body);
            else if (tab === 'firewall') loadFirewall(body);
        };
    });

    loadInterfaces(body);
}

// Connection card — default gateway, source IP and DNS servers.
// `st` is the (optional) payload of GET /api/network/status; renders gracefully
// when the endpoint is unavailable or the host has no route/DNS configured.
function renderConnectionCard(st) {
    const gw = st && st.gateway;
    const dns = (st && Array.isArray(st.dns_servers)) ? st.dns_servers : [];
    let rows = '';
    if (gw) {
        rows += `<div style="display:flex;justify-content:space-between;gap:12px;">
            <span style="color: var(--text-2);">Default gateway</span>
            <span style="color:white;font-family:monospace;">${escapeHtml(gw.ip)}
                ${gw.interface ? `<span style="color:var(--text-3);font-size:11px;">via ${escapeHtml(gw.interface)}</span>` : ''}</span></div>`;
        if (gw.source_ip) {
            rows += `<div style="display:flex;justify-content:space-between;gap:12px;">
                <span style="color: var(--text-2);">Source IP</span>
                <span style="color:white;font-family:monospace;">${escapeHtml(gw.source_ip)}</span></div>`;
        }
    } else {
        rows += '<div style="font-size:12px;color:var(--text-3);">No default route found</div>';
    }
    if (dns.length) {
        rows += `<div style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;">
            <span style="color: var(--text-2);">DNS servers</span>
            <span style="font-family:monospace;font-size:12px;color:white;">${dns.map(d => escapeHtml(d)).join(' · ')}</span></div>`;
    } else {
        rows += '<div style="font-size:12px;color:var(--text-3);">No DNS servers configured</div>';
    }
    return `<div class="glass-card" data-testid="net-connection-card" style="padding:16px 20px;margin-bottom:16px;">
        <div style="font-size:11px;color: var(--text-3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;">🌐 Connection</div>
        <div style="display:flex;flex-direction:column;gap:8px;font-size:13px;">${rows}</div>
    </div>`;
}

async function loadInterfaces(body) {
    const content = body.querySelector('#netContent');
    try {
        // /interfaces is the canonical contract ({interfaces:[...], total});
        // /status is best-effort — its failure must not break the table.
        const [ifResp, stResp] = await Promise.all([
            apiFetch('/api/network/interfaces'),
            apiFetch('/api/network/status').catch(() => null),
        ]);
        if (!ifResp.ok) throw new Error('HTTP ' + ifResp.status);
        const data = await ifResp.json();
        const ifaces = Array.isArray(data) ? data : (data.interfaces || []);
        const netStatus = stResp && stResp.ok ? await stResp.json() : {};

        let html = renderThroughputCard() + renderConnectionCard(netStatus);
        if (!ifaces.length) {
            html += '<div style="color: var(--text-3);text-align:center;padding:20px;">No network interfaces found.</div>';
        } else {
            html += '<table class="app-table" data-testid="net-interfaces-table"><thead><tr><th>Interface</th><th>IP</th><th>MAC</th><th>Status</th><th>Speed</th><th>RX</th><th>TX</th></tr></thead><tbody>';
            for (const iface of ifaces) {
                const up = iface.status === 'up';
                const cls = up ? 'badge-green' : 'badge-yellow';
                const rx = iface.bytes_recv_human || (iface.rx_bytes != null ? formatBytes(iface.rx_bytes) : '—');
                const tx = iface.bytes_sent_human || (iface.tx_bytes != null ? formatBytes(iface.tx_bytes) : '—');
                html += `<tr><td style="font-weight:600;color:white;">${escapeHtml(iface.name || '—')}</td>
                    <td style="color: var(--text-2);font-family:monospace;">${escapeHtml(iface.ipv4 || iface.ip || '—')}</td>
                    <td style="color: var(--text-3);font-family:monospace;font-size:12px;">${escapeHtml(iface.mac || '—')}</td>
                    <td><span class="${cls}" style="padding:2px 8px;border-radius:4px;font-size:11px;">${up ? 'Up' : 'Down'}</span></td>
                    <td style="color: var(--text-2);font-size:12px;">${iface.speed_mbps ? escapeHtml(String(iface.speed_mbps)) + ' Mb/s' : '—'}</td>
                    <td style="color: var(--text-2);font-family:monospace;font-size:12px;">${escapeHtml(rx)}</td>
                    <td style="color: var(--text-2);font-family:monospace;font-size:12px;">${escapeHtml(tx)}</td></tr>`;
            }
            html += '</tbody></table>';
        }
        content.innerHTML = html;
        loadThroughput();
    } catch (e) {
        content.innerHTML = `<div style="color:#ef4444;text-align:center;padding:20px;">❌ ${escapeHtml(e.message)}</div>`;
    }
}

// ── Throughput history chart ────────────────────────────────────────────────
let _thruStop = null; // stop() of the shared poller (core/poller.js)
let _thruMinutes = 60;   // default window: last hour

function renderThroughputCard() {
    const wins = [[30, '30 min'], [120, '2 h'], [720, '12 h']];
    return `<div class="glass-card" data-testid="net-throughput-card" style="padding:16px 20px;margin-bottom:16px;">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px;">
            <div style="font-size:11px;color: var(--text-3);text-transform:uppercase;letter-spacing:.5px;">📈 Throughput</div>
            <div style="display:flex;align-items:center;gap:6px;font-size:12px;flex-wrap:wrap;">
                <span id="netThruNow" style="color:var(--text-2);font-family:monospace;margin-right:4px;">—</span>
                ${wins.map(([m, label]) => `<button data-testid="net-thru-win-${m}" class="${m === _thruMinutes ? 'btn-primary' : 'btn-ghost'}" onclick="window._netThruWindow(${m})" style="padding:3px 10px;font-size:11px;">${label}</button>`).join('')}
            </div>
        </div>
        <canvas id="netThruCanvas" style="width:100%;height:120px;display:block;"></canvas>
    </div>`;
}

function _drawSparkline(canvas, points) {
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 600, h = canvas.clientHeight || 120;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    if (!points.length) {
        ctx.fillStyle = 'var(--text-3)';
        ctx.font = '12px system-ui, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText('Collecting samples… (one point per minute)', w / 2, h / 2);
        return;
    }

    const padL = 8, padR = 8, padT = 10, padB = 18;
    const maxV = Math.max(...points.map(p => Math.max(p.rx_bps, p.tx_bps)), 1);
    const x = i => points.length === 1 ? (padL + w - padR) / 2 : padL + (w - padL - padR) * i / (points.length - 1);
    const y = v => h - padB - (h - padT - padB) * (v / maxV);

    // Baseline grid line.
    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, h - padB); ctx.lineTo(w - padR, h - padB);
    ctx.stroke();

    const series = [
        { key: 'tx_bps', color: '#22c55e' },  // TX green (behind)
        { key: 'rx_bps', color: '#3b82f6' },  // RX blue (front)
    ];
    for (const s of series) {
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        points.forEach((p, i) => { i === 0 ? ctx.moveTo(x(i), y(p[s.key])) : ctx.lineTo(x(i), y(p[s.key])); });
        ctx.stroke();
    }

    // Axis labels: max rate (top-left), time span (bottom).
    const fmtT = ts => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    ctx.fillStyle = 'var(--text-3)';
    ctx.font = '10px system-ui, sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText(formatSpeed(maxV), padL, 8);
    ctx.fillText(fmtT(points[0].ts), padL, h - 4);
    ctx.textAlign = 'right';
    ctx.fillText(fmtT(points[points.length - 1].ts), w - padR, h - 4);
}

async function loadThroughput() {
    if (_thruStop) { _thruStop(); _thruStop = null; }
    const draw = async () => {
        // The poller already stops with the window; this only guards a draw that
        // was in flight while the app switched away.
        if (!_netActive()) return;
        try {
            const resp = await apiFetch(`/api/network/stats/history?minutes=${_thruMinutes}`);
            if (!resp.ok || !_netActive()) return;
            const data = await resp.json();
            const points = data.samples || [];
            _drawSparkline(document.getElementById('netThruCanvas'), points);
            const nowEl = document.getElementById('netThruNow');
            if (nowEl) {
                nowEl.textContent = points.length
                    ? `↓ ${formatSpeed(points[points.length - 1].rx_bps)} · ↑ ${formatSpeed(points[points.length - 1].tx_bps)}`
                    : 'collecting…';
            }
        } catch (_) { /* keep the last drawing on transient errors */ }
    };
    await draw();
    // Shared poller: pauses in a hidden tab, stops when the window closes.
    if (_thruStop) _thruStop();
    _thruStop = poll(draw, 30_000, { appId: 'network', immediate: false, name: 'network-draw' });
}

window._netThruWindow = function (minutes) {
    _thruMinutes = minutes;
    document.querySelectorAll('[data-testid^="net-thru-win-"]').forEach(b => {
        const m = Number(String(b.dataset.testid).split('-').pop());
        b.className = m === _thruMinutes ? 'btn-primary' : 'btn-ghost';
    });
    loadThroughput();
};

async function loadFirewall(body) {
    const content = body.querySelector('#netContent');
    content.innerHTML = skeletonRows(5);

    try {
        const [statusResp, rulesResp] = await Promise.all([
            apiFetch('/api/network/firewall/status'),
            apiFetch('/api/network/firewall'),
        ]);
        const status = statusResp.ok ? await statusResp.json() : {};
        const data = rulesResp.ok ? await rulesResp.json() : { rules: [], policies: {} };
        const rules = data.rules || [];

        let html = `<div style="color:#f59e0b;font-size:12px;background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.3);border-radius:10px;padding:12px 14px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
            <span>⚠️ This is a read-only view of the raw <b>iptables</b> ruleset. The firewall is managed through <b>UFW</b> — change rules, bans and logging in the Firewall &amp; Access Control app.</span>
            <button class="btn-primary" data-testid="fw-open-security" onclick="openApp('security')" style="font-size:11px;padding:4px 12px;white-space:nowrap;">Open Firewall &amp; Access Control →</button>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div style="display:flex;align-items:center;gap:12px;">
                <span data-testid="fw-status" style="font-size:13px;color: var(--text-2);">
                    ${status.active !== undefined
                        ? (status.active ? '🟢 iptables service active' : '⚪ iptables service not active')
                        : '⏳ Unknown'}
                    ${status.using_nftables ? '(nftables backend)' : '(iptables backend)'}
                    · ${rules.length} raw rules · UFW state is shown in the Firewall app
                </span>
            </div>
        </div>`;

        if (rules.length === 0) {
            html += '<div style="color: var(--text-3);text-align:center;padding:40px;">No firewall rules found.</div>';
        } else {
            html += `<table class="app-table" data-testid="fw-rules-table" style="width:100%;"><thead><tr>
                <th style="text-align:left;padding:8px 10px;">Chain</th>
                <th style="text-align:left;padding:8px 10px;">Protocol</th>
                <th style="text-align:left;padding:8px 10px;">Source</th>
                <th style="text-align:left;padding:8px 10px;">Dest</th>
                <th style="text-align:left;padding:8px 10px;">Port</th>
                <th style="text-align:left;padding:8px 10px;">Action</th>
                <th style="text-align:left;padding:8px 10px;">Comment</th>
            </tr></thead><tbody>`;
            for (const rule of rules) {
                html += `<tr>
                    <td style="font-weight:600;color:white;padding:8px 10px;font-size:12px;">${escapeHtml(rule.chain)}</td>
                    <td style="color: var(--text-2);padding:8px 10px;font-size:12px;">${escapeHtml(rule.protocol)}</td>
                    <td style="color: var(--text-3);font-family:monospace;font-size:11px;padding:8px 10px;">${escapeHtml(rule.source)}</td>
                    <td style="color: var(--text-3);font-family:monospace;font-size:11px;padding:8px 10px;">${escapeHtml(rule.destination)}</td>
                    <td style="color: var(--text-2);padding:8px 10px;font-size:12px;">${rule.dport || rule.sport ? escapeHtml(rule.dport || rule.sport) : '—'}</td>
                    <td style="padding:8px 10px;font-size:12px;"><span class="${rule.action === 'ACCEPT' ? 'badge-green' : 'badge-yellow'}" style="padding:2px 6px;">${escapeHtml(rule.action)}</span></td>
                    <td style="color: var(--text-3);padding:8px 10px;font-size:11px;">${escapeHtml(rule.comment || '—')}</td>
                </tr>`;
            }
            html += '</tbody></table>';
        }
        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = `<div style="color:#ef4444;text-align:center;padding:20px;">❌ ${escapeHtml(e.message)}</div>`;
    }
}

// ── Global helpers for inline onclick handlers ──

window._fwRefresh = function() {
    const body = document.querySelector('#netContent');
    if (body) loadFirewall(body.closest('.app-window') || body);
};
