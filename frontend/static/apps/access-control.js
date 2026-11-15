// EthOS NAS — Firewall & Access Control (UFW, Failed Logins, Bans)
import { escapeHtml, escapeAttr, jsAttr } from '../core/utils.js';
import { apiFetch, readErrorDetail } from '../core/api.js';
import { addNotification } from '../core/notification-center.js';
import { uiIcon } from '../core/icons.js';
import { poll } from '../core/poller.js';
import { skeletonRows } from '../core/states.js';

let _secWindow = null;
let _secLoginsH = 24;
let _secLoginsLimit = 50;
let _secIntrH = 24;
let _secExtOnly = false;
let _secLive = true;
let _secLiveTimer = null;
let _secIntrSel = new Set();

const _BADGE = 'padding:2px 8px;border-radius:4px;font-size:11px;';
const _ERR = (m) => `<div class="alert alert-error" style="justify-content:center;margin:20px 0;">${uiIcon('alert', 16)}<span>${escapeHtml(m)}</span></div>`;

// Rules whose removal can cut off the NAS itself (installed by the backend
// baseline before `ufw enable`) — deleting them needs an explicit warning.
const _BASELINE_COMMENTS = new Set(['ethos-web', 'ethos-api', 'ethos-lan']);

// Safe argument for an inline `onclick="...( 'ARG' )"` handler. escapeHtml()
// does not touch quotes, so values from logs/UFW must be escaped separately.


function _ts(t) {
    if (t === null || t === undefined || t === '') return '—';
    let d;
    if (typeof t === 'string' && !/^\d+(\.\d+)?$/.test(t)) {
        // Backend sends naive local ISO strings (e.g. 2026-11-07T22:30:44.463366).
        // Trim sub-millisecond precision so every engine parses it reliably.
        d = new Date(t.replace(/(\.\d{3})\d+/, '$1'));
    } else {
        let n = Number(t);
        if (!isFinite(n)) return escapeHtml(String(t));
        if (n < 1e12) n *= 1000;      // unix seconds → ms
        d = new Date(n);
    }
    return isNaN(d.getTime()) ? escapeHtml(String(t)) : d.toLocaleString();
}

function _actionBadge(a) {
    const cls = (a === 'allow' || a === 'accept') ? 'badge-green'
        : (a === 'drop' || a === 'reject' || a === 'deny') ? 'badge-red' : 'badge-yellow';
    return `<span class="${cls}" style="${_BADGE}">${escapeHtml(String(a || '—'))}</span>`;
}

export async function renderAccessControlPage(body) {
    _secWindow = body;
    const tabBtn = (id, label, icon) => `<button class="app-tab sec-tab${id === 'overview' ? ' active' : ''}" data-tab="${id}" data-testid="sec-tab-${id}">${uiIcon(icon, 15)}<span>${label}</span></button>`;

    body.innerHTML = `
        <div style="padding:12px 16px 0 16px;border-bottom:1px solid var(--border);display:flex;gap:2px;background:rgba(0,0,0,0.2);overflow-x:auto;">
            ${tabBtn('overview', 'Overview', 'chart')}
            ${tabBtn('ufw', 'UFW Firewall', 'shield')}
            ${tabBtn('intrusions', 'Intrusion Attempts', 'activity')}
            ${tabBtn('logins', 'Failed Logins', 'lock')}
            ${tabBtn('bans', 'Bans', 'ban')}
        </div>
        <div id="secContent" style="padding:16px;overflow-y:auto;height:calc(100% - 49px);"></div>
    `;

    body.querySelectorAll('.sec-tab').forEach(btn => {
        btn.onclick = () => {
            body.querySelectorAll('.sec-tab').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            loadSecTab(body, btn.dataset.tab);
        };
    });

    loadSecTab(body, 'overview');
}

async function loadSecTab(body, tab) {
    const content = body.querySelector('#secContent');
    if (!content) return;
    _stopIntrLive();
    switch (tab) {
        case 'ufw': return loadUfw(body);
        case 'intrusions': return loadIntrusions(body);
        case 'logins': return loadLogins(body);
        case 'bans': return loadBans(body);
        default: return loadOverview(body);
    }
}

// ── Intrusion Attempts (live UFW BLOCK log) ───────────────────────────────
function _secActive() {
    return !!(_secWindow && _secWindow.isConnected);
}

function _stopIntrLive() {
    // `_secLiveTimer` holds the shared poller's stop() handle (core/poller.js).
    if (_secLiveTimer) { _secLiveTimer(); _secLiveTimer = null; }
}

function _startIntrLive() {
    _stopIntrLive();
    if (!_secLive) return;
    _secLiveTimer = poll(() => loadIntrusions(_secWindow), 5000,
                         { appId: 'access-control', immediate: false, name: 'intrusions' });
}

function _portBadges(ports, max = 5) {
    const shown = (ports || []).slice(0, max);
    let html = shown.map(p => {
        const label = p.service ? `${p.port} ${escapeHtml(p.service)}` : String(p.port);
        return `<span style="display:inline-block;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.3);border-radius:5px;padding:1px 6px;font-family:monospace;font-size:10px;color:#fca5a5;margin:1px;">${label}</span>`;
    }).join('');
    if ((ports || []).length > max) {
        html += `<span style="color: var(--text-3);font-size:10px;margin-left:4px;" title="${escapeAttr(ports.slice(max).map(p => p.port).join(', '))}">+${ports.length - max} more</span>`;
    }
    return html || '<span style="color: var(--text-3);font-size:11px;">—</span>';
}

async function loadIntrusions(body) {
    const content = body.querySelector('#secContent');
    if (!content) { _stopIntrLive(); return; }
    if (content.dataset.loaded !== '1') {
        content.innerHTML = skeletonRows(8);
    }
    try {
        const [resp, bansResp] = await Promise.all([
            apiFetch(`/api/security/firewall-log?hours=${_secIntrH}&limit=50&external=${_secExtOnly ? 'true' : 'false'}`),
            apiFetch('/api/security/bans').catch(() => null),
        ]);
        if (!resp.ok) throw new Error(await readErrorDetail(resp));
        const d = await resp.json();
        let bannedSet = new Set();
        if (bansResp?.ok) {
            const bd = await bansResp.json().catch(() => ({}));
            bannedSet = new Set((bd.bans || []).map(b => b.ip));
        }
        // Stale-response guard: user switched tabs while the fetch was in flight
        if (body.querySelector('.sec-tab.active')?.dataset.tab !== 'intrusions') return;
        content.dataset.loaded = '1';

        const liveDot = _secLive && _secActive()
            ? '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#4ade80;animation:pulse 1.5s infinite;"></span>'
            : '';

        let html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;">
            <button class="btn-ghost btn-sm" data-testid="sec-intr-live" onclick="window._secIntrLive()" style="display:inline-flex;align-items:center;gap:6px;">${liveDot}${_secLive ? uiIcon('pause', 12) + ' Pause' : uiIcon('play', 12) + ' Live (5s)'}</button>
            <span style="font-size:12px;color: var(--text-2);">Window</span>
            <select data-testid="sec-intr-hours" aria-label="Intrusion time range" onchange="window._secIntrHours(this.value)" style="padding:6px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:12px;">
                <option value="1" ${_secIntrH === 1 ? 'selected' : ''}>Last hour</option>
                <option value="6" ${_secIntrH === 6 ? 'selected' : ''}>Last 6 hours</option>
                <option value="24" ${_secIntrH === 24 ? 'selected' : ''}>Last 24 hours</option>
                <option value="72" ${_secIntrH === 72 ? 'selected' : ''}>Last 3 days</option>
            </select>
            <label style="font-size:12px;color: var(--text-2);display:inline-flex;align-items:center;gap:5px;cursor:pointer;">
                <input type="checkbox" data-testid="sec-intr-ext" ${_secExtOnly ? 'checked' : ''} onchange="window._secIntrExternal(this.checked)" style="accent-color:#3b82f6;" />
                External IPs only
            </label>
            <span style="font-size:12px;color: var(--text-3);">${d.total_events ?? 0} blocked packets</span>
            ${!d.logging_known
                ? ''
                : d.logging_enabled
                    ? `<span class="badge-green" style="${_BADGE}">UFW logging on</span>`
                    : `<button class="btn-primary btn-sm" data-testid="sec-intr-log-on" onclick="window._secIntrLogging(true)">Enable UFW logging</button>`}
        </div>`;

        if (d.logging_known && !d.logging_enabled) {
            html += '<div class="alert alert-warning" style="margin-bottom:14px;">' + uiIcon('alert', 15) + '<span>UFW packet logging is disabled — blocked packets are not being recorded. Enable it above to start capturing intrusion attempts.</span></div>';
        }

        if (!d.by_ip?.length && !d.events?.length) {
            html += '<div style="color: var(--text-3);text-align:center;padding:40px;">No blocked packets in this window.</div>';
            content.innerHTML = html;
            _startIntrLive();
            return;
        }

        if (d.by_ip?.length) {
            // Banned IPs are not selectable for bulk banning
            const selectableIps = d.by_ip.slice(0, 25).filter(r => !bannedSet.has(r.ip)).map(r => r.ip);
            const allChecked = selectableIps.length > 0 && selectableIps.every(ip => _secIntrSel.has(ip));
            html += `<h4 style="color:white;font-size:13px;margin:0 0 8px;">Top offending IPs</h4>
            <div id="sec-intr-selbar" data-testid="sec-intr-selbar" style="display:${_secIntrSel.size ? 'flex' : 'none'};align-items:center;gap:10px;margin-bottom:10px;padding:8px 12px;background:rgba(239,68,68,0.07);border:1px solid rgba(239,68,68,0.35);border-radius:10px;">
                <span class="sel-count" style="font-size:12px;color:#fca5a5;">${_secIntrSel.size} selected</span>
                <button class="btn-danger btn-sm" data-testid="sec-intr-ban-sel" onclick="window._secIntrBanSelected()">${uiIcon('ban', 13)} Ban selected</button>
                <button class="btn-ghost btn-sm" data-testid="sec-intr-clear-sel" onclick="window._secIntrClearSel()">Clear</button>
            </div>
            <table class="app-table" data-testid="sec-intr-top" style="width:100%;margin-bottom:20px;"><thead><tr>
                <th style="padding:8px 6px;width:30px;"><input type="checkbox" data-testid="sec-intr-selall" ${allChecked ? 'checked' : ''} onchange="window._secIntrSelectAll(this.checked)" title="Select all shown" style="accent-color:#ef4444;cursor:pointer;"></th>
                <th style="text-align:left;padding:8px 10px;">IP</th>
                <th style="text-align:left;padding:8px 10px;">Attempts</th>
                <th style="text-align:left;padding:8px 10px;">Targeted ports</th>
                <th style="text-align:left;padding:8px 10px;">First seen</th>
                <th style="text-align:left;padding:8px 10px;">Last seen</th>
                <th style="text-align:right;padding:8px 10px;"></th>
            </tr></thead><tbody>`;
            for (const row of d.by_ip.slice(0, 25)) {
                const isBanned = bannedSet.has(row.ip);
                const ipAttr = escapeAttr(row.ip);
                const ipJs = jsAttr(row.ip);
                html += `<tr${isBanned ? ' style="opacity:.6;"' : ''}>
                    <td style="padding:8px 6px;"><input type="checkbox" class="sec-intr-cb" data-ip="${ipAttr}" ${_secIntrSel.has(row.ip) && !isBanned ? 'checked' : ''} ${isBanned ? 'disabled title="Already banned"' : ''} onchange="window._secIntrToggle('${ipJs}', this.checked)" style="accent-color:#ef4444;cursor:pointer;"></td>
                    <td data-testid="sec-intr-ip-${ipAttr}" style="color:white;font-family:monospace;font-size:12px;padding:8px 10px;">${escapeHtml(row.ip)}${isBanned ? ` <span class="badge-green" data-testid="sec-intr-banned-badge-${ipAttr}" style="${_BADGE}">banned</span>` : ''}</td>
                    <td style="padding:8px 10px;"><span class="badge-red" style="${_BADGE}">${row.count}</span></td>
                    <td style="padding:8px 10px;max-width:320px;">${_portBadges(row.ports)}</td>
                    <td style="color: var(--text-3);font-size:11px;padding:8px 10px;">${_ts(row.first_ts)}</td>
                    <td style="color: var(--text-3);font-size:11px;padding:8px 10px;">${_ts(row.last_ts)}</td>
                    <td style="text-align:right;padding:8px 10px;">
                        ${isBanned
                            ? `<button class="btn-success btn-sm" data-testid="sec-intr-banned-${ipAttr}" onclick="window._secUnbanIp('${ipJs}')" title="${ipAttr} is already banned — click to unban">${uiIcon('check', 13)} Banned</button>`
                            : `<button class="btn-danger btn-sm" data-testid="sec-intr-ban-${ipAttr}" onclick="window._secBanIp('${ipJs}')" title="Ban ${ipAttr} (permanent UFW drop)">${uiIcon('ban', 13)} Ban</button>`}
                    </td>
                </tr>`;
            }
            html += '</tbody></table>';
        }

        if (d.events?.length) {
            html += `<h4 style="color:white;font-size:13px;margin:0 0 8px;">Recent blocked packets <span style="color: var(--text-3);font-weight:400;">(latest ${d.events.length})</span></h4>
            <table class="app-table" data-testid="sec-intr-events" style="width:100%;"><thead><tr>
                <th style="text-align:left;padding:8px 10px;">Time</th>
                <th style="text-align:left;padding:8px 10px;">Source IP</th>
                <th style="text-align:left;padding:8px 10px;">Proto</th>
                <th style="text-align:left;padding:8px 10px;">Target</th>
                <th style="text-align:left;padding:8px 10px;">From port</th>
                <th style="text-align:left;padding:8px 10px;">Iface</th>
            </tr></thead><tbody>`;
            for (const ev of d.events) {
                const target = ev.dport
                    ? `${ev.dport}${ev.service ? ' ' + escapeHtml(ev.service) : ''}`
                    : (ev.service || '—');
                html += `<tr>
                    <td style="color: var(--text-3);font-size:11px;padding:8px 10px;white-space:nowrap;">${_ts(ev.ts)}</td>
                    <td style="color: var(--text-2);font-family:monospace;font-size:11px;padding:8px 10px;">${escapeHtml(ev.ip || '—')}${ev.ip && bannedSet.has(ev.ip) ? ` <span class="badge-green" style="${_BADGE}">banned</span>` : ''}</td>
                    <td style="color: var(--text-3);font-size:11px;padding:8px 10px;text-transform:uppercase;">${escapeHtml(ev.proto || '?')}</td>
                    <td style="color: var(--text-2);font-family:monospace;font-size:11px;padding:8px 10px;">${target}</td>
                    <td style="color: var(--text-3);font-family:monospace;font-size:11px;padding:8px 10px;">${escapeHtml(ev.sport || '—')}</td>
                    <td style="color: var(--text-3);font-size:11px;padding:8px 10px;">${escapeHtml(ev.iface || '—')}</td>
                </tr>`;
            }
            html += '</tbody></table>';
        }

        content.innerHTML = html;
    } catch (e) {
        if (content.dataset.loaded === '1') return; // keep last good view on transient errors
        content.innerHTML = _ERR(e.message);
    }
    _startIntrLive();
}

// ── Overview ────────────────────────────────────────────────────────────────
async function loadOverview(body) {
    const content = body.querySelector('#secContent');
    content.innerHTML = skeletonRows(4);
    try {
        const resp = await apiFetch('/api/security/status');
        if (!resp.ok) throw new Error(await readErrorDetail(resp));
        const s = await resp.json();
        const ufw = s.ufw || {};
        const failCount = s.failed_logins_24h ?? 0;

        const ufwBadge = ufw.installed === false
            ? `<span class="badge-red" style="${_BADGE}">not installed</span>`
            : ufw.error
                ? `<span class="badge-yellow" style="${_BADGE}" title="UFW state could not be read">unknown</span>`
                : ufw.enabled
                    ? `<span class="badge-green" style="${_BADGE}">enabled</span>`
                    : `<span class="badge-red" style="${_BADGE}">disabled</span>`;
        const card = 'flex:1;min-width:250px;background:var(--surface-2);border:1px solid var(--border);border-radius:12px;padding:16px;'
        const blocked = s.blocked_packets_24h ?? 0;
        const bans = s.bans_count ?? 0;

        content.innerHTML = `
        <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:stretch;">
            <div data-testid="sec-card-ufw" style="${card}">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                    <span class="section-title" style="font-size:14px;">${uiIcon('shield', 15)} UFW Firewall</span>${ufwBadge}
                </div>
                <div style="color: var(--text-2);font-size:12px;line-height:1.9;">
                    Logging: <span style="color:white;">${ufw.logging ? 'on' : 'off'}</span><br>
                    Rules: <span style="color:white;">${ufw.rules_count ?? '—'}</span><br>
                    Defaults (in/out/routed): <span style="color:white;">${escapeHtml(ufw.defaults?.incoming ?? '?')} / ${escapeHtml(ufw.defaults?.outgoing ?? '?')} / ${escapeHtml(ufw.defaults?.routed ?? '?')}</span>
                </div>
                ${ufw.error ? `<div style="color:#f59e0b;font-size:11px;margin-top:8px;">${escapeHtml(ufw.error)}</div>` : ''}
                <div style="display:flex;gap:6px;margin-top:12px;flex-wrap:wrap;">
                    <button class="btn-ghost btn-sm" data-testid="sec-ufw-enable" onclick="window._secEnable()" style="${ufw.enabled ? 'opacity:0.4;pointer-events:none;' : ''}">${uiIcon('lock', 13)} Enable</button>
                    <button class="btn-ghost btn-sm" data-testid="sec-ufw-disable" onclick="window._secDisable()" style="${!ufw.enabled ? 'opacity:0.4;pointer-events:none;' : ''}">${uiIcon('unlock', 13)} Disable</button>
                    <button class="btn-primary btn-sm" data-testid="sec-add-rule" onclick="window._secOpenForm()">${uiIcon('plus', 13)} Add Rule</button>
                </div>
            </div>
            <div data-testid="sec-card-blocked" style="${card}">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                    <span class="section-title" style="font-size:14px;">${uiIcon('activity', 15)} Blocked packets</span>
                    <span class="badge-blue" style="${_BADGE}">last 24h</span>
                </div>
                <div data-testid="sec-blocked-count" style="font-size:34px;font-weight:800;color:white;margin:8px 0;">${blocked}</div>
                <button class="btn-ghost btn-sm" data-testid="sec-intr-open" onclick="window._secGoto('intrusions')">View Attempts →</button>
            </div>
            <div data-testid="sec-card-bans" style="${card}">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                    <span class="section-title" style="font-size:14px;">${uiIcon('ban', 15)} Active bans</span>
                    <span class="badge-yellow" style="${_BADGE}">ufw drop</span>
                </div>
                <div data-testid="sec-bans-count" style="font-size:34px;font-weight:800;color:white;margin:8px 0;">${bans}</div>
                <button class="btn-ghost btn-sm" data-testid="sec-bans-open" onclick="window._secGoto('bans')">Manage →</button>
            </div>
            <div data-testid="sec-card-logins" style="${card}">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                    <span class="section-title" style="font-size:14px;">${uiIcon('lock', 15)} Failed Logins</span>
                    <span class="badge-blue" style="${_BADGE}">last 24h</span>
                </div>
                <div data-testid="sec-fail-count" style="font-size:34px;font-weight:800;color:white;margin:8px 0;">${failCount}</div>
                <button class="btn-ghost btn-sm" data-testid="sec-logins-open" onclick="window._secGoto('logins')">View Details →</button>
            </div>
        </div>
        ${ufw.installed !== false && !ufw.error && !ufw.logging
            ? `<div class="alert alert-warning" style="margin-top:14px;">${uiIcon('alert', 15)}<span>UFW packet logging is off — intrusion attempts are not recorded.</span><button class="btn-primary btn-sm" onclick="window._secIntrLogging(true)" style="margin-left:auto;">Enable logging</button></div>`
            : ''}`;
    } catch (e) {
        content.innerHTML = _ERR(e.message);
    }
}

// ── UFW Firewall ────────────────────────────────────────────────────────────
async function loadUfw(body) {
    const content = body.querySelector('#secContent');
    content.innerHTML = skeletonRows(6);
    try {
        const resp = await apiFetch('/api/security/ufw/rules');
        if (!resp.ok) throw new Error(await readErrorDetail(resp));
        const d = await resp.json();
        const rules = d.rules || [];
        const def = d.defaults || {};

        let html = `<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:8px;">
            <span data-testid="sec-ufw-status" style="font-size:13px;color: var(--text-2);">
                ${d.enabled ? `<span class="badge-green" style="${_BADGE}">enabled</span>` : `<span class="badge-red" style="${_BADGE}">disabled</span>`}
                · logging ${d.logging ? 'on' : 'off'} · ${rules.length} rules
                · defaults (in/out/routed) ${escapeHtml(def.incoming ?? '?')} / ${escapeHtml(def.outgoing ?? '?')} / ${escapeHtml(def.routed ?? '?')}
            </span>
            <div style="display:flex;gap:6px;flex-wrap:wrap;">
                <button class="btn-ghost btn-sm" data-testid="sec-ufw-enable" onclick="window._secEnable()" style="${d.enabled ? 'opacity:0.4;pointer-events:none;' : ''}">${uiIcon('lock', 13)} Enable</button>
                <button class="btn-ghost btn-sm" data-testid="sec-ufw-disable" onclick="window._secDisable()" style="${!d.enabled ? 'opacity:0.4;pointer-events:none;' : ''}">${uiIcon('unlock', 13)} Disable</button>
                <button class="btn-primary btn-sm" data-testid="sec-add-rule-btn" onclick="window._secOpenForm()">${uiIcon('plus', 13)} Add Rule</button>
            </div>
        </div>`;

        if (!d.enabled) {
            html += '<div class="alert alert-warning" data-testid="sec-ufw-inactive-note" style="margin-bottom:14px;">' + uiIcon('alert', 15) + '<span>UFW is disabled — the ruleset below may be incomplete. Enable UFW to have your rules applied and fully managed.</span></div>';
        }

        if (!rules.length) {
            html += '<div style="color: var(--text-3);text-align:center;padding:40px;">No UFW rules configured.</div>';
        } else {
            html += `<table class="app-table" data-testid="sec-ufw-table" style="width:100%;"><thead><tr>
                <th style="text-align:left;padding:8px 10px;">#</th>
                <th style="text-align:left;padding:8px 10px;">Action</th>
                <th style="text-align:left;padding:8px 10px;">Direction</th>
                <th style="text-align:left;padding:8px 10px;">From</th>
                <th style="text-align:left;padding:8px 10px;">To</th>
                <th style="text-align:left;padding:8px 10px;">IPv6</th>
                <th style="text-align:left;padding:8px 10px;">Comment</th>
                <th style="text-align:right;padding:8px 10px;"></th>
            </tr></thead><tbody>`;
            rules.forEach((r, i) => {
                const num = r.num ?? (i + 1);
                const isBaseline = _BASELINE_COMMENTS.has((r.comment || '').trim());
                html += `<tr>
                    <td style="color: var(--text-3);font-size:12px;padding:8px 10px;">${num}</td>
                    <td style="padding:8px 10px;">${_actionBadge(r.action)}</td>
                    <td style="color: var(--text-2);font-size:12px;padding:8px 10px;">${escapeHtml(r.direction || '—')}</td>
                    <td style="color: var(--text-3);font-family:monospace;font-size:11px;padding:8px 10px;">${escapeHtml(r.from || '—')}</td>
                    <td style="color: var(--text-3);font-family:monospace;font-size:11px;padding:8px 10px;">${escapeHtml(r.to || '—')}</td>
                    <td style="color: var(--text-3);font-size:11px;padding:8px 10px;">${r.ipv6 ? 'v6' : ''}</td>
                    <td style="color: var(--text-3);font-size:11px;padding:8px 10px;">${escapeHtml(r.comment || '—')}${isBaseline ? ' <span class="badge-yellow" style="' + _BADGE + '" title="Baseline rule — removing it can cut off EthOS access">baseline</span>' : ''}</td>
                    <td style="text-align:right;padding:8px 10px;">
                        <button class="btn-danger btn-sm" data-testid="sec-ufw-del-${num}" onclick="window._secDeleteRule(${num}, '${jsAttr((r.comment || '').trim())}')">${uiIcon('trash', 13)} Delete</button>
                    </td>
                </tr>`;
            });
            html += '</tbody></table>';
        }
        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = _ERR(e.message);
    }
}

// ── Failed Logins ───────────────────────────────────────────────────────────
async function loadLogins(body) {
    const content = body.querySelector('#secContent');
    content.innerHTML = skeletonRows(6);
    try {
        const [resp, bansResp] = await Promise.all([
            apiFetch(`/api/security/failed-logins?hours=${_secLoginsH}&limit=${_secLoginsLimit}`),
            apiFetch('/api/security/bans').catch(() => null),
        ]);
        if (!resp.ok) throw new Error(await readErrorDetail(resp));
        const d = await resp.json();
        let bannedSet = new Set();
        if (bansResp?.ok) {
            const bd = await bansResp.json().catch(() => ({}));
            bannedSet = new Set((bd.bans || []).map(b => b.ip));
        }
        const byIp = d.by_ip || [];
        const events = d.events || [];

        let html = `<div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;">
            <span style="font-size:12px;color: var(--text-2);">Window</span>
            <select data-testid="sec-logins-hours" aria-label="Failed logins time range" onchange="window._secLoginsHours(this.value)" style="padding:6px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:12px;">
                <option value="1" ${_secLoginsH === 1 ? 'selected' : ''}>Last hour</option>
                <option value="6" ${_secLoginsH === 6 ? 'selected' : ''}>Last 6 hours</option>
                <option value="24" ${_secLoginsH === 24 ? 'selected' : ''}>Last 24 hours</option>
                <option value="72" ${_secLoginsH === 72 ? 'selected' : ''}>Last 3 days</option>
            </select>
            <span style="font-size:12px;color: var(--text-3);">${d.total_events ?? 0} events total</span>
            <button class="btn-ghost btn-sm" data-testid="sec-logins-more" onclick="window._secLoginsMore()">Load 50 more</button>
        </div>`;

        if (!byIp.length && !events.length) {
            html += '<div style="color: var(--text-3);text-align:center;padding:40px;">No failed logins in this window.</div>';
            content.innerHTML = html;
            return;
        }

        if (byIp.length) {
            html += `<h4 style="color:white;font-size:13px;margin:0 0 8px;">Top offending IPs</h4>
            <table class="app-table" data-testid="sec-logins-top" style="width:100%;margin-bottom:20px;"><thead><tr>
                <th style="text-align:left;padding:8px 10px;">IP</th>
                <th style="text-align:left;padding:8px 10px;">Attempts</th>
                <th style="text-align:left;padding:8px 10px;">Users targeted</th>
                <th style="text-align:left;padding:8px 10px;">Last seen</th>
                <th style="text-align:right;padding:8px 10px;"></th>
            </tr></thead><tbody>`;
            for (const row of byIp.slice(0, 25)) {
                const users = Object.entries(row.users || {}).map(([u, n]) => `${escapeHtml(u)} (${n})`).join(', ');
                const isBanned = bannedSet.has(row.ip);
                html += `<tr${isBanned ? ' style="opacity:.7;"' : ''}>
                    <td style="color:white;font-family:monospace;font-size:12px;padding:8px 10px;">${escapeHtml(row.ip)}${isBanned ? ` <span class="badge-green" style="${_BADGE}">banned</span>` : ''}</td>
                    <td style="padding:8px 10px;"><span class="badge-red" style="${_BADGE}">${row.count}</span></td>
                    <td style="color: var(--text-2);font-size:11px;padding:8px 10px;">${users || '—'}</td>
                    <td style="color: var(--text-3);font-size:11px;padding:8px 10px;">${_ts(row.last_ts)}</td>
                    <td style="text-align:right;padding:8px 10px;">
                        ${isBanned
                            ? `<button class="btn-success btn-sm" data-testid="sec-logins-unban-${escapeAttr(row.ip)}" onclick="window._secUnbanIp('${jsAttr(row.ip)}')">${uiIcon('check', 13)} Unban</button>`
                            : `<button class="btn-danger btn-sm" data-testid="sec-logins-ban-${escapeAttr(row.ip)}" onclick="window._secBanIp('${jsAttr(row.ip)}')">${uiIcon('ban', 13)} Ban</button>`}
                    </td>
                </tr>`;
            }
            html += '</tbody></table>';
        }

        html += `<h4 style="color:white;font-size:13px;margin:0 0 8px;">Recent events <span style="color: var(--text-3);font-weight:400;">(latest ${events.length})</span></h4>
        <table class="app-table" data-testid="sec-logins-events" style="width:100%;"><thead><tr>
            <th style="text-align:left;padding:8px 10px;">Time</th>
            <th style="text-align:left;padding:8px 10px;">IP</th>
            <th style="text-align:left;padding:8px 10px;">User</th>
            <th style="text-align:left;padding:8px 10px;">Type</th>
        </tr></thead><tbody>`;
        for (const ev of events) {
            html += `<tr>
                <td style="color: var(--text-3);font-size:11px;padding:8px 10px;">${_ts(ev.ts)}</td>
                <td style="color: var(--text-2);font-family:monospace;font-size:11px;padding:8px 10px;">${escapeHtml(ev.ip || '—')}</td>
                <td style="color: var(--text-2);font-size:11px;padding:8px 10px;">${escapeHtml(ev.user || '—')}</td>
                <td style="color: var(--text-3);font-size:11px;padding:8px 10px;">${escapeHtml(ev.type || '—')}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = _ERR(e.message);
    }
}

// ── Bans ────────────────────────────────────────────────────────────────────
async function loadBans(body) {
    const content = body.querySelector('#secContent');
    content.innerHTML = skeletonRows(5);
    try {
        const resp = await apiFetch('/api/security/bans');
        if (!resp.ok) throw new Error(await readErrorDetail(resp));
        const d = await resp.json();
        const bans = d.bans || [];

        let html = `<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;">
            <input id="sec-ban-ip" data-testid="sec-ban-input" type="text" placeholder="e.g. 203.0.113.42 or 203.0.113.0/24" style="flex:1;min-width:200px;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:13px;font-family:monospace;" />
            <button class="btn-danger" data-testid="sec-ban-btn" onclick="window._secBanIp()" style="padding:8px 18px;font-size:13px;">${uiIcon('ban', 14)} Ban IP</button>
        </div>
        <div style="color: var(--text-3);font-size:11px;margin-bottom:14px;">Bans are permanent UFW deny rules (all protocols) tagged <code>ethos-ban</code>. They survive UFW restarts and reboots.</div>`;

        if (d.ufw_enabled === false) {
            html += '<div class="alert alert-warning" style="margin-bottom:14px;">' + uiIcon('alert', 15) + '<span>UFW is disabled — bans are stored as rules but only enforced after the firewall is enabled.</span></div>';
        }

        if (!bans.length) {
            html += '<div style="color: var(--text-3);text-align:center;padding:40px;">No active bans.</div>';
        } else {
            html += `<table class="app-table" data-testid="sec-bans-table" style="width:100%;"><thead><tr>
                <th style="text-align:left;padding:8px 10px;">#</th>
                <th style="text-align:left;padding:8px 10px;">IP</th>
                <th style="text-align:right;padding:8px 10px;"></th>
            </tr></thead><tbody>`;
            bans.forEach((b, i) => {
                html += `<tr>
                    <td style="color: var(--text-3);font-size:12px;padding:8px 10px;">${b.num ?? i + 1}</td>
                    <td style="color:white;font-family:monospace;font-size:12px;padding:8px 10px;">${escapeHtml(b.ip)}</td>
                    <td style="text-align:right;padding:8px 10px;">
                        <button class="btn-success btn-sm" data-testid="sec-unban-${escapeAttr(b.ip)}" onclick="window._secUnbanIp('${jsAttr(b.ip)}')">${uiIcon('check', 13)} Unban</button>
                    </td>
                </tr>`;
            });
            html += '</tbody></table>';
        }
        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = _ERR(e.message);
    }
}

// ── Global inline handlers ──────────────────────────────────────────────────
window._secRefresh = function() {
    if (!_secWindow || !_secWindow.isConnected) return;
    const tab = _secWindow.querySelector('.sec-tab.active')?.dataset.tab || 'overview';
    loadSecTab(_secWindow, tab);
};

window._secGoto = function(tab) {
    const btn = _secWindow?.querySelector(`.sec-tab[data-tab="${tab}"]`);
    if (btn) btn.click();
};

window._secEnable = async function() {
    try {
        const resp = await apiFetch('/api/security/ufw/enable', { method: 'POST' });
        if (!resp.ok) { addNotification('Failed to enable UFW: ' + await readErrorDetail(resp), 'error'); return; }
        addNotification('UFW enabled', 'success');
    } catch (e) { addNotification('Failed to enable UFW: ' + e.message, 'error'); return; }
    window._secRefresh();
};

window._secDisable = async function() {
    if (!await window.showModalConfirm('Disable UFW', 'Disable the UFW firewall?')) return;
    try {
        const resp = await apiFetch('/api/security/ufw/disable', { method: 'POST' });
        if (!resp.ok) { addNotification('Failed to disable UFW: ' + await readErrorDetail(resp), 'error'); return; }
        addNotification('UFW disabled', 'success');
    } catch (e) { addNotification('Failed to disable UFW: ' + e.message, 'error'); return; }
    window._secRefresh();
};

window._secOpenForm = function() {
    const existing = document.getElementById('sec-rule-form');
    if (existing) existing.remove();

    const dialog = document.createElement('div');
    dialog.id = 'sec-rule-form';
    dialog.setAttribute('data-testid', 'sec-rule-form');
    dialog.style.cssText = 'position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;';
    dialog.innerHTML = `<div style="background:var(--surface-1);border:1px solid var(--border);border-radius:14px;padding:20px;width:440px;max-width:92vw;box-shadow:0 20px 60px rgba(0,0,0,0.6);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
            <span class="section-title" style="font-size:15px;">${uiIcon('plus', 15)} Add UFW Rule</span>
            <button onclick="document.getElementById('sec-rule-form').remove()" style="background:none;border:none;color: var(--text-2);font-size:18px;cursor:pointer;">×</button>
        </div>
        <div style="display:flex;gap:10px;margin-bottom:10px;">
            <div style="flex:1;">
                <label style="font-size:11px;color: var(--text-2);display:block;margin-bottom:4px;">Action</label>
                <select id="sec-rule-action" aria-label="Rule action" style="width:100%;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:12px;">
                    <option value="allow" selected>allow</option>
                    <option value="deny">deny (drop)</option>
                    <option value="reject">reject</option>
                    <option value="limit">limit (rate-limit)</option>
                </select>
            </div>
            <div style="flex:1;">
                <label style="font-size:11px;color: var(--text-2);display:block;margin-bottom:4px;">Protocol</label>
                <select id="sec-rule-proto" aria-label="Protocol" style="width:100%;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:12px;">
                    <option value="tcp" selected>tcp</option>
                    <option value="udp">udp</option>
                    <option value="any">any</option>
                    <option value="icmp">icmp (no port)</option>
                </select>
            </div>
        </div>
        <div style="display:flex;gap:10px;margin-bottom:10px;">
            <div style="flex:1;">
                <label style="font-size:11px;color: var(--text-2);display:block;margin-bottom:4px;">Port (optional)</label>
                <input id="sec-rule-port" type="text" placeholder="e.g. 22, 8000:8100" style="width:100%;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:12px;font-family:monospace;" />
            </div>
            <div style="flex:1;">
                <label style="font-size:11px;color: var(--text-2);display:block;margin-bottom:4px;">Source (optional)</label>
                <input id="sec-rule-source" type="text" placeholder="any / IP / CIDR" style="width:100%;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:12px;font-family:monospace;" />
            </div>
        </div>
        <div style="margin-bottom:12px;">
            <label style="font-size:11px;color: var(--text-2);display:block;margin-bottom:4px;">Comment (optional)</label>
            <input id="sec-rule-comment" type="text" placeholder="e.g. allow ssh from office" style="width:100%;padding:8px 10px;border-radius:8px;border:1.5px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:12px;" />
        </div>
        <div id="sec-rule-error" style="color:#ef4444;font-size:12px;margin-bottom:10px;min-height:16px;"></div>
        <div style="display:flex;justify-content:flex-end;gap:8px;">
            <button class="btn-ghost" onclick="document.getElementById('sec-rule-form').remove()">Cancel</button>
            <button class="btn-primary" onclick="window._secSaveRule()">Save Rule</button>
        </div>
    </div>`;
    document.body.appendChild(dialog);
};

window._secSaveRule = async function() {
    const proto = document.getElementById('sec-rule-proto')?.value || 'tcp';
    const payload = {
        action: document.getElementById('sec-rule-action')?.value || 'allow',
        protocol: proto,
        // ICMP is a protocol, not a port — UFW rejects `8/icmp`.
        port: proto === 'icmp' ? '' : (document.getElementById('sec-rule-port')?.value?.trim() || ''),
        source: document.getElementById('sec-rule-source')?.value?.trim() || '',
        comment: document.getElementById('sec-rule-comment')?.value?.trim() || '',
    };
    const errorEl = document.getElementById('sec-rule-error');
    try {
        const resp = await apiFetch('/api/security/ufw/rules', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!resp.ok) {
            if (errorEl) errorEl.textContent = await readErrorDetail(resp);
            return;
        }
        const d = await resp.json().catch(() => ({}));
        const dialog = document.getElementById('sec-rule-form');
        if (dialog) dialog.remove();
        addNotification(d.num ? `UFW rule added (#${d.num})` : 'UFW rule added', 'success');
        window._secGoto('ufw');
    } catch (e) {
        if (errorEl) errorEl.textContent = e.message;
    }
};

window._secDeleteRule = async function(num, comment) {
    const base = _BASELINE_COMMENTS.has((comment || '').trim());
    const msg = base
        ? `Rule #${num} ("${comment}") is a baseline rule that keeps EthOS reachable. Deleting it while UFW is enabled can cut off the web UI and the phones.\n\nDelete anyway?`
        : `Delete UFW rule #${num}?`;
    if (!await window.showModalConfirm(base ? 'Delete baseline rule' : 'Delete Rule', msg)) return;
    try {
        const resp = await apiFetch(`/api/security/ufw/rules/${encodeURIComponent(num)}`, { method: 'DELETE' });
        if (!resp.ok) { addNotification('Failed to delete rule: ' + await readErrorDetail(resp), 'error'); return; }
        addNotification('UFW rule deleted', 'success');
    } catch (e) { addNotification('Failed to delete rule: ' + e.message, 'error'); return; }
    window._secRefresh();
};

window._secBanIp = async function(ip) {
    const value = (ip ?? document.getElementById('sec-ban-ip')?.value ?? '').toString().trim();
    if (!value) { addNotification('Enter an IP address to ban', 'error'); return; }
    if (!await window.showModalConfirm('Ban IP', `Ban ${value} with a permanent UFW drop rule?`)) return;
    try {
        const resp = await apiFetch('/api/security/bans', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ip: value }),
        });
        if (!resp.ok) { addNotification('Failed to ban IP: ' + await readErrorDetail(resp), 'error'); return; }
        const d = await resp.json().catch(() => ({}));
        addNotification(d.already_banned ? `${value} was already banned` : `${value} banned`, 'success');
        _secIntrSel.delete(value);
    } catch (e) { addNotification('Failed to ban IP: ' + e.message, 'error'); return; }
    window._secRefresh();
};

window._secUnbanIp = async function(ip) {
    if (!await window.showModalConfirm('Unban IP', `Remove ban rules for ${ip}?`)) return;
    try {
        const resp = await apiFetch(`/api/security/bans/${encodeURIComponent(ip)}`, { method: 'DELETE' });
        if (!resp.ok) { addNotification('Failed to unban IP: ' + await readErrorDetail(resp), 'error'); return; }
        addNotification(`${ip} unbanned`, 'success');
    } catch (e) { addNotification('Failed to unban IP: ' + e.message, 'error'); return; }
    window._secRefresh();
};

window._secLoginsHours = function(value) {
    _secLoginsH = Number(value) || 24;
    window._secRefresh();
};

window._secIntrLive = function() {
    _secLive = !_secLive;
    if (_secActive()) loadIntrusions(_secWindow);
};

window._secIntrHours = function(value) {
    _secIntrH = Number(value) || 24;
    _secIntrSel.clear();  // selection refers to IPs of the previous window
    window._secRefresh();
};

window._secIntrExternal = function(checked) {
    _secExtOnly = !!checked;
    _secIntrSel.clear();
    window._secRefresh();
};

// ── Intrusion Attempts: multi-select banning ─────────────────────────────
function _secIntrUpdateSelBar() {
    const bar = document.querySelector('#sec-intr-selbar');
    if (!bar) return;
    const n = _secIntrSel.size;
    bar.style.display = n ? 'flex' : 'none';
    const label = bar.querySelector('.sel-count');
    if (label) label.textContent = `${n} selected`;
}

window._secIntrToggle = function(ip, checked) {
    if (checked) _secIntrSel.add(ip); else _secIntrSel.delete(ip);
    _secIntrUpdateSelBar();
};

window._secIntrSelectAll = function(checked) {
    document.querySelectorAll('.sec-intr-cb').forEach(cb => {
        if (cb.disabled) return; // banned rows are not selectable
        cb.checked = checked;
        if (checked) _secIntrSel.add(cb.dataset.ip); else _secIntrSel.delete(cb.dataset.ip);
    });
    _secIntrUpdateSelBar();
};

window._secIntrClearSel = function() {
    _secIntrSel.clear();
    document.querySelectorAll('.sec-intr-cb, [data-testid="sec-intr-selall"]').forEach(cb => { cb.checked = false; });
    _secIntrUpdateSelBar();
};

window._secIntrBanSelected = async function() {
    const ips = [..._secIntrSel];
    if (!ips.length) return;
    const preview = ips.slice(0, 8).join(', ') + (ips.length > 8 ? ` … (+${ips.length - 8} more)` : '');
    if (!await window.showModalConfirm('Ban IPs', `Ban ${ips.length} IP(s) with permanent UFW drop rules?\n\n${preview}`)) return;
    try {
        const resp = await apiFetch('/api/security/bans/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ips }),
        });
        if (!resp.ok) throw new Error(await readErrorDetail(resp));
        const d = await resp.json();
        let msg = `${d.banned ?? 0} banned`;
        if (d.already) msg += `, ${d.already} already banned`;
        addNotification(msg, 'success');
        for (const err of d.errors || []) addNotification(`Failed to ban ${err.ip}: ${err.error}`, 'error');
        _secIntrSel.clear();
    } catch (e) { addNotification('Bulk ban failed: ' + e.message, 'error'); return; }
    window._secRefresh();
};

window._secIntrLogging = async function(enabled) {
    try {
        const resp = await apiFetch('/api/security/ufw/logging', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
        if (!resp.ok) { addNotification('Failed to set UFW logging: ' + await readErrorDetail(resp), 'error'); return; }
        addNotification(`UFW logging ${enabled ? 'enabled' : 'disabled'}`, 'success');
    } catch (e) { addNotification('Failed to set UFW logging: ' + e.message, 'error'); return; }
    window._secRefresh();
};

window._secLoginsMore = function() {
    _secLoginsLimit += 50;
    window._secRefresh();
};
