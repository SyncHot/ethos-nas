// EthOS NAS — Firewall & Access Control (rail: Overview · UFW · Intrusions · Logins · Bans)
import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiRequest } from '../core/api.js';
import { notify } from '../core/notification-center.js';
import { showConfirm, showFormModal } from '../core/modal.js';
import { uiIcon } from '../core/icons.js';
import { poll } from '../core/poller.js';
import { skeletonRows, emptyState, errorState } from '../core/states.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';

const SECTIONS = [
    { id: 'overview', label: 'Overview', title: 'Security Overview', subtitle: 'Firewall state, blocked traffic and login failures.', testId: 'sec-tab-overview' },
    { id: 'ufw', label: 'UFW Firewall', title: 'UFW Firewall', subtitle: 'Rules and defaults managed through UFW.', testId: 'sec-tab-ufw' },
    { id: 'autoban', label: 'Auto-ban', title: 'Auto-ban', subtitle: 'Temporary, escalating bans for repeat offenders — local networks are never blocked.', testId: 'sec-tab-autoban' },
    { id: 'intrusions', label: 'Intrusion Attempts', title: 'Intrusion Attempts', subtitle: 'Blocked packets from the UFW log, live.', testId: 'sec-tab-intrusions' },
    { id: 'logins', label: 'Failed Logins', title: 'Failed Logins', subtitle: 'Authentication failures grouped by source IP.', testId: 'sec-tab-logins' },
    { id: 'bans', label: 'Bans', title: 'Bans', subtitle: 'Manual (permanent) and automatic (temporary) UFW drop rules.', testId: 'sec-tab-bans' },
];

// Rules whose removal can cut off the NAS itself (installed by the backend
// baseline before `ufw enable`) — deleting them needs an explicit warning.
const BASELINE_COMMENTS = new Set(['ethos-web', 'ethos-api', 'ethos-lan']);

const TILE_ACTIONS = {
    'goto-intrusions': 'View attempts →',
    'goto-bans': 'Manage →',
    'goto-logins': 'View details →',
    'goto-autoban': 'Configure →',
};

// ── Shared UFW status fragments ─────────────────────────────────────────────
// The same badge/callout/action block appears on several tabs; keep one source
// of truth so wording, tones and testids cannot drift between sections.

function _ufwBadge(ufw = {}) {
    if (ufw.installed === false) return '<span class="ui-tag is-bad">not installed</span>';
    if (ufw.error) return '<span class="ui-tag is-warn" title="UFW state could not be read">unknown</span>';
    return ufw.enabled
        ? '<span class="ui-tag is-ok">enabled</span>'
        : '<span class="ui-tag is-bad">disabled</span>';
}

function _ufwDefaults(defaults = {}) {
    const value = defaults || {};
    return `${value.incoming ?? '?'} / ${value.outgoing ?? '?'} / ${value.routed ?? '?'}`;
}

function _ufwDisabledCallout(text, { testId = '', icon = false } = {}) {
    const attr = testId ? ` data-testid="${testId}"` : '';
    return `<div class="ui-callout"${attr}>${icon ? `${uiIcon('alert', 13)} ` : ''}${text}</div>`;
}

function _ufwLoggingOffCallout({ text, withButton = false }) {
    const button = withButton
        ? ' <div class="ui-row ui-panel-body"><button class="ui-btn small primary" data-action="logging-on">Enable logging</button></div>'
        : '';
    return `<div class="ui-callout">${uiIcon('alert', 13)} ${text}${button}</div>`;
}

function _ufwLoggingTag(known, enabled) {
    if (!known) return '';
    return enabled
        ? '<span class="ui-tag is-ok">UFW logging on</span>'
        : '<button class="ui-btn small primary" data-action="logging-on" data-testid="sec-intr-log-on">Enable UFW logging</button>';
}

function _ufwActions(enabled, addRuleTestId, className = 'ui-row') {
    return `<div class="${className}">
        <button class="ui-btn small" data-action="ufw-enable" data-testid="sec-ufw-enable" ${enabled ? 'disabled' : ''}>${uiIcon('lock', 12)} Enable</button>
        <button class="ui-btn small" data-action="ufw-disable" data-testid="sec-ufw-disable" ${enabled ? '' : 'disabled'}>${uiIcon('unlock', 12)} Disable</button>
        <button class="ui-btn small primary" data-action="add-rule" data-testid="${addRuleTestId}">${uiIcon('plus', 12)} Add rule</button>
    </div>`;
}

function _wireUfwActions(root) {
    root.querySelector('[data-action="ufw-enable"]')?.addEventListener('click', () => _setUfw(true));
    root.querySelector('[data-action="ufw-disable"]')?.addEventListener('click', () => _setUfw(false));
    root.querySelector('[data-action="add-rule"]')?.addEventListener('click', () => _openRuleForm());
    root.querySelector('[data-action="logging-on"]')?.addEventListener('click', () => _setLogging(true));
}

let shell = null;
let navToken = 0;
let activeTab = 'overview';
let loginsHours = 24;
let loginsLimit = 50;
let intrHours = 24;
let extOnly = false;
let live = true;
let liveTimer = null;
let selectedIps = new Set();
let autobanPreview = null;
let autobanTimer = null;

export async function renderAccessControlPage(body) {
    shell?.destroy();
    _stopIntrLive();
    selectedIps = new Set();
    shell = mountShell(body, {
        idPrefix: 'security',
        title: 'Firewall & Access Control',
        subtitle: 'UFW, blocked traffic and bans.',
        icon: '🛡️',
        sections: SECTIONS,
        note: 'Changes apply immediately to the host firewall.',
    });
    shell.onSection(_runSection);
    shell.setActive('overview');
}

function _runSection(id) {
    activeTab = id;
    _stopIntrLive();
    _stopAutobanLive();
    if (shell?.view) {
        shell.view.onclick = null;
        shell.view.onchange = null;
        shell.view.oninput = null;
        delete shell.view.dataset.loaded;
        delete shell.view.dataset.dirty;
    }
    const token = ++navToken;
    const isCurrent = () => token === navToken && shell && shell.root.isConnected && activeTab === id;
    const loader = LOADERS[id];
    if (loader) loader(isCurrent);
}

// ════════════════════════════════════════════════════════════════════════════
// Overview
// ════════════════════════════════════════════════════════════════════════════

async function loadOverview(isCurrent) {
    shell.view.innerHTML = skeletonRows(4);
    shell.setStatus('Loading firewall status…');
    try {
        const data = await apiRequest('/api/security/status');
        if (!isCurrent()) return;

        const ufw = data.ufw || {};
        const stateTag = _ufwBadge(ufw);

        const autoban = data.autoban || {};
        const autobanValue = autoban.enabled ? (autoban.dry_run ? 'DRY-RUN' : 'ON') : 'OFF';
        const tile = (testId, label, value, detail, actionLabel, action) => `<section class="ui-panel" data-testid="${testId}">
            <div class="ui-toolbar"><h2>${escapeHtml(label)}</h2>${actionLabel ? `<span class="ui-tag is-info">${escapeHtml(actionLabel)}</span>` : ''}</div>
            <div class="ui-panel-body">
                <div class="ui-summary-value">${value}</div>
                <p class="ui-muted">${escapeHtml(detail)}</p>
                ${action ? `<button class="ui-btn small" data-action="${action}">${escapeHtml(TILE_ACTIONS[action] || 'Open →')}</button>` : ''}
            </div>
        </section>`;

        shell.view.innerHTML = `
            <div class="ui-grid">
                <section class="ui-panel" data-testid="sec-card-ufw">
                    <div class="ui-toolbar">
                        <h2>${uiIcon('shield', 13)} UFW Firewall</h2>
                        ${stateTag}
                    </div>
                    <div class="ui-panel-body">
                        ${_kv([
                            ['Logging', ufw.logging ? 'on' : 'off'],
                            ['Rules', String(ufw.rules_count ?? '—')],
                            ['Defaults', _ufwDefaults(ufw.defaults)],
                        ])}
                        ${ufw.error ? `<p class="ui-callout error">${escapeHtml(ufw.error)}</p>` : ''}
                        ${_ufwActions(ufw.enabled, 'sec-add-rule', 'ui-row ui-panel-body')}
                    </div>
                </section>
                ${tile('sec-card-blocked', 'Blocked packets', Number(data.blocked_packets_24h ?? 0), 'last 24h', 'View attempts →', 'goto-intrusions')}
                ${tile('sec-card-bans', 'Active bans', Number(data.bans_count ?? 0), 'manual + auto UFW deny rules', 'Manage →', 'goto-bans')}
                ${tile('sec-card-logins', 'Failed logins', Number(data.failed_logins_24h ?? 0), 'last 24h', 'View details →', 'goto-logins')}
                ${tile('sec-card-autoban', 'Auto-ban', autobanValue, `${Number(autoban.banned_today ?? 0)} blocked today · ${Number(autoban.active_bans ?? 0)} active`, 'Configure →', 'goto-autoban')}
            </div>
            ${ufw.installed !== false && !ufw.error && !ufw.logging
                ? _ufwLoggingOffCallout({
                    text: 'UFW packet logging is off — intrusion attempts are not recorded.',
                    withButton: true,
                })
                : ''}`;

        _wireOverviewActions();
        shell.setStatus(ufw.enabled ? 'UFW enabled' : 'UFW disabled', ufw.enabled ? 'ok' : 'warn');
    } catch (err) {
        if (!isCurrent()) return;
        shell.view.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read firewall status', 'error');
        logError('Security: overview unavailable', err?.detail || err?.message);
    }
}

function _wireOverviewActions() {
    _wireUfwActions(shell.view);
    for (const [action, tab] of [['goto-intrusions', 'intrusions'], ['goto-bans', 'bans'],
        ['goto-logins', 'logins'], ['goto-autoban', 'autoban']]) {
        shell.view.querySelector(`[data-action="${action}"]`)?.addEventListener('click', () => shell.setActive(tab));
    }
}

async function _setUfw(enabled) {
    if (!enabled) {
        const ok = await showConfirm('Disable UFW', 'Disable the UFW firewall?', { danger: true });
        if (!ok) return;
    }
    try {
        await apiRequest(`/api/security/ufw/${enabled ? 'enable' : 'disable'}`, { method: 'POST' });
        notify({ kind: 'success', message: enabled ? 'UFW enabled' : 'UFW disabled' });
        shell.setActive(activeTab);
    } catch (err) {
        logError(`Security: UFW ${enabled ? 'enable' : 'disable'} failed`, err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'UFW action failed' });
    }
}

async function _setLogging(enabled) {
    try {
        await apiRequest('/api/security/ufw/logging', { method: 'POST', body: { enabled } });
        notify({ kind: 'success', message: `UFW logging ${enabled ? 'enabled' : 'disabled'}` });
        shell.setActive(activeTab);
    } catch (err) {
        logError('Security: UFW logging failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'UFW logging failed' });
    }
}

// ════════════════════════════════════════════════════════════════════════════
// UFW rules
// ════════════════════════════════════════════════════════════════════════════

async function loadUfw(isCurrent) {
    shell.view.innerHTML = skeletonRows(6);
    shell.setStatus('Loading UFW rules…');
    try {
        const data = await apiRequest('/api/security/ufw/rules');
        if (!isCurrent()) return;
        const rules = data.rules || [];
        const defaults = data.defaults || {};

        const rows = rules.map((rule, index) => {
            const num = rule.num ?? (index + 1);
            const baseline = BASELINE_COMMENTS.has((rule.comment || '').trim());
            return `<tr>
                <td>${num}</td>
                <td>${_actionTag(rule.action)}</td>
                <td>${escapeHtml(rule.direction || '—')}</td>
                <td class="path">${escapeHtml(rule.from || '—')}</td>
                <td class="path">${escapeHtml(rule.to || '—')}</td>
                <td>${rule.ipv6 ? 'v6' : ''}</td>
                <td>${escapeHtml(rule.comment || '—')}${baseline ? ' <span class="ui-tag is-warn" title="Baseline rule — removing it can cut off EthOS access">baseline</span>' : ''}</td>
                <td class="actions"><button class="ui-btn small danger" data-action="delete-rule"
                        data-num="${num}" data-comment="${escapeAttr((rule.comment || '').trim())}"
                        data-testid="sec-ufw-del-${num}">${uiIcon('trash', 12)} Delete</button></td>
            </tr>`;
        }).join('');

        shell.view.innerHTML = `
            <div class="ui-toolbar">
                <div>
                    <h2 data-testid="sec-ufw-status">${_ufwBadge({ enabled: data.enabled })}
                        logging ${data.logging ? 'on' : 'off'} · ${rules.length} rule(s)</h2>
                    <p class="ui-muted">Defaults (in/out/routed): ${escapeHtml(_ufwDefaults(defaults))}</p>
                </div>
                ${_ufwActions(data.enabled, 'sec-add-rule-btn')}
            </div>
            ${!data.enabled ? _ufwDisabledCallout('UFW is disabled — the ruleset below may be incomplete.', { testId: 'sec-ufw-inactive-note' }) : ''}
            ${rules.length
                ? `<div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table" data-testid="sec-ufw-table">
                        <thead><tr><th>#</th><th>Action</th><th>Direction</th><th>From</th><th>To</th><th>IPv6</th><th>Comment</th><th class="actions"></th></tr></thead>
                        <tbody>${rows}</tbody>
                    </table></div></div>`
                : emptyState({ icon: 'shield', title: 'No UFW rules configured', hint: 'Add a rule to allow or deny traffic.' })}`;

        _wireUfwActions(shell.view);
        shell.view.querySelectorAll('[data-action="delete-rule"]').forEach((button) => {
            button.addEventListener('click', () => _deleteRule(Number(button.dataset.num), button.dataset.comment || ''));
        });
        shell.setStatus(`${rules.length} rule(s)`, 'ok');
    } catch (err) {
        if (!isCurrent()) return;
        shell.view.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read UFW rules', 'error');
        logError('Security: UFW rules unavailable', err?.detail || err?.message);
    }
}

function _actionTag(action) {
    const value = String(action || '—');
    const tone = ['allow', 'accept'].includes(value) ? 'is-ok'
        : ['drop', 'reject', 'deny'].includes(value) ? 'is-bad' : 'is-warn';
    return `<span class="ui-tag ${tone}">${escapeHtml(value)}</span>`;
}

async function _openRuleForm() {
    const values = await showFormModal({
        title: 'Add UFW rule',
        confirmLabel: 'Save rule',
        bodyHtml: `
            <div class="ui-fields">
                <div class="ui-field"><label for="rule-action">Action</label>
                    <select id="rule-action" name="action" class="ui-select" aria-label="Rule action">
                        <option value="allow">allow</option><option value="deny">deny (drop)</option>
                        <option value="reject">reject</option><option value="limit">limit (rate-limit)</option>
                    </select></div>
                <div class="ui-field"><label for="rule-proto">Protocol</label>
                    <select id="rule-proto" name="protocol" class="ui-select" aria-label="Protocol">
                        <option value="tcp">tcp</option><option value="udp">udp</option>
                        <option value="any">any</option><option value="icmp">icmp (no port)</option>
                    </select></div>
                <div class="ui-field full"><label for="rule-port">Port (optional)</label>
                    <input id="rule-port" name="port" class="ui-input mono" placeholder="e.g. 22, 8000:8100"></div>
                <div class="ui-field full"><label for="rule-source">Source (optional)</label>
                    <input id="rule-source" name="source" class="ui-input mono" placeholder="any / IP / CIDR"></div>
                <div class="ui-field full"><label for="rule-comment">Comment (optional)</label>
                    <input id="rule-comment" name="comment" class="ui-input" placeholder="e.g. allow ssh from office"></div>
            </div>
            <p class="ui-muted ui-panel-body">ICMP is a protocol, not a port — the port field is ignored for it.</p>`,
    });
    if (!values) return;
    try {
        const result = await apiRequest('/api/security/ufw/rules', {
            method: 'POST',
            body: {
                action: values.action || 'allow',
                protocol: values.protocol || 'tcp',
                port: values.protocol === 'icmp' ? '' : (values.port || '').trim(),
                source: (values.source || '').trim(),
                comment: (values.comment || '').trim(),
            },
        });
        notify({ kind: 'success', message: result?.num ? `UFW rule added (#${result.num})` : 'UFW rule added' });
        shell.setActive('ufw');
    } catch (err) {
        logError('Security: adding UFW rule failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Could not add the rule' });
    }
}

async function _deleteRule(num, comment) {
    const baseline = BASELINE_COMMENTS.has((comment || '').trim());
    const ok = await showConfirm(
        baseline ? 'Delete baseline rule' : 'Delete rule',
        baseline
            ? `Rule #${num} ("${comment}") keeps EthOS reachable. Deleting it while UFW is enabled can cut off the web UI and the phones.\n\nDelete anyway?`
            : `Delete UFW rule #${num}?`,
        { danger: true },
    );
    if (!ok) return;
    try {
        await apiRequest(`/api/security/ufw/rules/${encodeURIComponent(num)}`, { method: 'DELETE' });
        notify({ kind: 'success', message: 'UFW rule deleted' });
        shell.setActive('ufw');
    } catch (err) {
        logError('Security: deleting UFW rule failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Could not delete the rule' });
    }
}

// ════════════════════════════════════════════════════════════════════════════
// Intrusion attempts
// ════════════════════════════════════════════════════════════════════════════

function _stopIntrLive() {
    if (liveTimer) { liveTimer(); liveTimer = null; }
}

function _startIntrLive(isCurrent) {
    _stopIntrLive();
    if (!live) return;
    liveTimer = poll(() => loadIntrusions(isCurrent || (() => activeTab === 'intrusions')), 5000,
        { appId: 'access-control', immediate: false, name: 'intrusions' });
}

async function loadIntrusions(isCurrent) {
    const content = shell.view;
    const hadContent = content.dataset.loaded === '1';
    if (!hadContent) content.innerHTML = skeletonRows(8);
    shell.setStatus(hadContent ? 'Refreshing…' : 'Loading blocked packets…');

    try {
        const [data, bans] = await Promise.all([
            apiRequest(`/api/security/firewall-log?hours=${intrHours}&limit=50&external=${extOnly ? 'true' : 'false'}`),
            apiRequest('/api/security/bans').catch((err) => {
                logError('Security: bans unavailable', err?.detail || err?.message);
                return { bans: [] };
            }),
        ]);
        if (!isCurrent()) return;
        content.dataset.loaded = '1';
        const bannedSet = new Set((bans.bans || []).map((ban) => ban.ip));

        const top = data.by_ip || [];
        const events = data.events || [];
        const selectable = top.slice(0, 25).filter((row) => !bannedSet.has(row.ip)).map((row) => row.ip);
        const allSelected = selectable.length > 0 && selectable.every((ip) => selectedIps.has(ip));

        const topRows = top.slice(0, 25).map((row) => {
            const banned = bannedSet.has(row.ip);
            const ipAttr = escapeAttr(row.ip);
            return `<tr>
                <td><input type="checkbox" class="ui-check" data-ip="${ipAttr}" data-action="select-ip"
                        ${selectedIps.has(row.ip) && !banned ? 'checked' : ''} ${banned ? 'disabled' : ''}></td>
                <td data-testid="sec-intr-ip-${ipAttr}" class="path">${escapeHtml(row.ip)}
                    ${banned ? ` <span class="ui-tag is-ok" data-testid="sec-intr-banned-badge-${ipAttr}">banned</span>` : ''}</td>
                <td><span class="ui-tag is-bad">${Number(row.count) || 0}</span></td>
                <td>${_portTags(row.ports)}</td>
                <td class="ui-muted">${_ts(row.first_ts)}</td>
                <td class="ui-muted">${_ts(row.last_ts)}</td>
                <td class="actions">${banned
                    ? `<button class="ui-btn small" data-action="unban" data-ip="${ipAttr}" data-testid="sec-intr-banned-${ipAttr}">${uiIcon('check', 12)} Banned</button>`
                    : `<button class="ui-btn small danger" data-action="ban" data-ip="${ipAttr}" data-testid="sec-intr-ban-${ipAttr}">${uiIcon('ban', 12)} Ban</button>`}</td>
            </tr>`;
        }).join('');

        const eventRows = events.map((event) => `<tr>
            <td class="ui-muted">${_ts(event.ts)}</td>
            <td class="path">${escapeHtml(event.ip || '—')}${event.ip && bannedSet.has(event.ip) ? ' <span class="ui-tag is-ok">banned</span>' : ''}</td>
            <td>${escapeHtml((event.proto || '?').toUpperCase())}</td>
            <td class="path">${escapeHtml(event.dport ? `${event.dport}${event.service ? ` ${event.service}` : ''}` : (event.service || '—'))}</td>
            <td class="path">${escapeHtml(event.sport || '—')}</td>
            <td>${escapeHtml(event.iface || '—')}</td>
        </tr>`).join('');

        const loggingTag = _ufwLoggingTag(data.logging_known, data.logging_enabled);

        content.innerHTML = `
            <div class="ui-toolbar">
                <div class="ui-row">
                    <button class="ui-btn small" data-action="live" data-testid="sec-intr-live">
                        ${live ? uiIcon('pause', 12) + ' Pause' : uiIcon('play', 12) + ' Live (5s)'}
                    </button>
                    <select class="ui-select" data-action="hours" data-testid="sec-intr-hours" aria-label="Intrusion time range">
                        ${_hourOptions(intrHours)}
                    </select>
                    <label class="ui-check"><input type="checkbox" data-action="external" data-testid="sec-intr-ext" ${extOnly ? 'checked' : ''}> External IPs only</label>
                </div>
                <div class="ui-row">
                    ${loggingTag}
                    <span class="ui-tag">${Number(data.total_events) || 0} blocked</span>
                </div>
            </div>
            ${data.logging_known && !data.logging_enabled
                ? _ufwLoggingOffCallout({ text: 'UFW packet logging is disabled — blocked packets are not recorded.' })
                : ''}
            ${top.length || events.length ? `
                ${top.length ? `
                    <div id="sec-intr-selbar" data-testid="sec-intr-selbar" class="ui-callout"
                         ${selectedIps.size ? '' : 'hidden'}>
                        <span>${selectedIps.size} selected</span>
                        <div class="ui-row">
                            <button class="ui-btn small danger" data-action="ban-selected" data-testid="sec-intr-ban-sel">${uiIcon('ban', 12)} Ban selected</button>
                            <button class="ui-btn small" data-action="clear-selection" data-testid="sec-intr-clear-sel">Clear</button>
                        </div>
                    </div>
                    <div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table" data-testid="sec-intr-top">
                        <thead><tr><th><input type="checkbox" data-action="select-all" data-testid="sec-intr-selall" ${allSelected ? 'checked' : ''} aria-label="Select all shown"></th>
                            <th>IP</th><th>Attempts</th><th>Targeted ports</th><th>First seen</th><th>Last seen</th><th class="actions"></th></tr></thead>
                        <tbody>${topRows}</tbody>
                    </table></div></div>` : ''}
                ${events.length ? `<div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table" data-testid="sec-intr-events">
                        <thead><tr><th>Time</th><th>Source IP</th><th>Proto</th><th>Target</th><th>From port</th><th>Iface</th></tr></thead>
                        <tbody>${eventRows}</tbody>
                    </table></div></div>` : ''}`
                : emptyState({ icon: 'shield', title: 'No blocked packets in this window', hint: 'Widen the time range or enable UFW logging.' })}`;

        _wireIntrusionActions(content, isCurrent);
        shell.setStatus(`${top.length} source(s) · ${events.length} event(s)`, 'ok');
    } catch (err) {
        if (!isCurrent()) return;
        if (!hadContent) {
            content.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        }
        shell.setStatus('Could not read the UFW log', 'error');
        logError('Security: intrusion log unavailable', err?.detail || err?.message);
    }
    _startIntrLive(isCurrent);
}

function _wireIntrusionActions(content, isCurrent) {
    content.onclick = (event) => {
        const control = event.target.closest('[data-action]');
        if (!control) return;
        const action = control.dataset.action;
        if (action === 'live') {
            live = !live;
            if (live) loadIntrusions(isCurrent);
            else _stopIntrLive();
            shell.setActive('intrusions');
        } else if (action === 'logging-on') {
            _setLogging(true);
        } else if (action === 'ban') {
            _banIp(control.dataset.ip);
        } else if (action === 'unban') {
            _unbanIp(control.dataset.ip);
        } else if (action === 'ban-selected') {
            _banSelected();
        } else if (action === 'clear-selection') {
            selectedIps.clear();
            content.querySelectorAll('[data-action="select-ip"], [data-action="select-all"]').forEach((box) => { box.checked = false; });
            _updateSelectionBar(content);
        }
    };
    content.onchange = (event) => {
        const control = event.target.closest('[data-action]');
        if (!control) return;
        const action = control.dataset.action;
        if (action === 'hours') {
            intrHours = Number(control.value) || 24;
            selectedIps.clear();
            shell.setActive('intrusions');
        } else if (action === 'external') {
            extOnly = Boolean(control.checked);
            selectedIps.clear();
            shell.setActive('intrusions');
        } else if (action === 'select-ip') {
            if (control.checked) selectedIps.add(control.dataset.ip);
            else selectedIps.delete(control.dataset.ip);
            _updateSelectionBar(content);
        } else if (action === 'select-all') {
            content.querySelectorAll('[data-action="select-ip"]').forEach((box) => {
                if (box.disabled) return;
                box.checked = control.checked;
                if (control.checked) selectedIps.add(box.dataset.ip);
                else selectedIps.delete(box.dataset.ip);
            });
            _updateSelectionBar(content);
        }
    };
}

function _updateSelectionBar(content) {
    const bar = content.querySelector('#sec-intr-selbar');
    if (!bar) return;
    bar.hidden = selectedIps.size === 0;
    const label = bar.querySelector('span');
    if (label) label.textContent = `${selectedIps.size} selected`;
}

function _portTags(ports, max = 5) {
    const shown = (ports || []).slice(0, max);
    const tags = shown.map((port) => `<span class="ui-tag">${escapeHtml(String(port.port))}${port.service ? ` ${escapeHtml(port.service)}` : ''}</span>`);
    if ((ports || []).length > max) tags.push(`<span class="ui-tag">+${ports.length - max}</span>`);
    return tags.join(' ') || '<span class="ui-muted">—</span>';
}

function _hourOptions(selected) {
    return [[1, 'Last hour'], [6, 'Last 6 hours'], [24, 'Last 24 hours'], [72, 'Last 3 days']]
        .map(([value, label]) => `<option value="${value}"${selected === value ? ' selected' : ''}>${label}</option>`).join('');
}

// ════════════════════════════════════════════════════════════════════════════
// Failed logins
// ════════════════════════════════════════════════════════════════════════════

async function loadLogins(isCurrent) {
    shell.view.innerHTML = skeletonRows(6);
    shell.setStatus('Loading failed logins…');
    try {
        const [data, bans] = await Promise.all([
            apiRequest(`/api/security/failed-logins?hours=${loginsHours}&limit=${loginsLimit}`),
            apiRequest('/api/security/bans').catch((err) => {
                logError('Security: bans unavailable', err?.detail || err?.message);
                return { bans: [] };
            }),
        ]);
        if (!isCurrent()) return;

        const bannedSet = new Set((bans.bans || []).map((ban) => ban.ip));
        const byIp = data.by_ip || [];
        const events = data.events || [];

        const topRows = byIp.slice(0, 25).map((row) => {
            const banned = bannedSet.has(row.ip);
            const ipAttr = escapeAttr(row.ip);
            const users = Object.entries(row.users || {}).map(([user, count]) => `${escapeHtml(user)} (${count})`).join(', ');
            return `<tr>
                <td class="path">${escapeHtml(row.ip)} ${banned ? '<span class="ui-tag is-ok">banned</span>' : ''}</td>
                <td><span class="ui-tag is-bad">${Number(row.count) || 0}</span></td>
                <td>${users || '—'}</td>
                <td class="ui-muted">${_ts(row.last_ts)}</td>
                <td class="actions">${banned
                    ? `<button class="ui-btn small" data-action="unban" data-ip="${ipAttr}" data-testid="sec-logins-unban-${ipAttr}">${uiIcon('check', 12)} Unban</button>`
                    : `<button class="ui-btn small danger" data-action="ban" data-ip="${ipAttr}" data-testid="sec-logins-ban-${ipAttr}">${uiIcon('ban', 12)} Ban</button>`}</td>
            </tr>`;
        }).join('');

        const eventRows = events.map((event) => `<tr>
            <td class="ui-muted">${_ts(event.ts)}</td>
            <td class="path">${escapeHtml(event.ip || '—')}</td>
            <td>${escapeHtml(event.user || '—')}</td>
            <td>${escapeHtml(event.type || '—')}</td>
        </tr>`).join('');

        shell.view.innerHTML = `
            <div class="ui-toolbar">
                <div class="ui-row">
                    <select class="ui-select" data-action="hours" data-testid="sec-logins-hours" aria-label="Failed logins time range">
                        ${_hourOptions(loginsHours)}
                    </select>
                    <span class="ui-tag">${Number(data.total_events) || 0} events total</span>
                    <button class="ui-btn small" data-action="more" data-testid="sec-logins-more">Load 50 more</button>
                </div>
            </div>
            ${byIp.length ? `<div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table" data-testid="sec-logins-top">
                    <thead><tr><th>IP</th><th>Attempts</th><th>Users targeted</th><th>Last seen</th><th class="actions"></th></tr></thead>
                    <tbody>${topRows}</tbody>
                </table></div></div>` : ''}
            ${events.length ? `<div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table" data-testid="sec-logins-events">
                    <thead><tr><th>Time</th><th>IP</th><th>User</th><th>Type</th></tr></thead>
                    <tbody>${eventRows}</tbody>
                </table></div></div>`
                : emptyState({ icon: 'lock', title: 'No failed logins in this window', hint: 'Try a wider time range.' })}`;

        shell.view.onclick = (event) => {
            const control = event.target.closest('[data-action]');
            if (!control) return;
            if (control.dataset.action === 'ban') _banIp(control.dataset.ip);
            else if (control.dataset.action === 'unban') _unbanIp(control.dataset.ip);
            else if (control.dataset.action === 'more') {
                loginsLimit += 50;
                shell.setActive('logins');
            }
        };
        shell.view.onchange = (event) => {
            const control = event.target.closest('[data-action="hours"]');
            if (!control) return;
            loginsHours = Number(control.value) || 24;
            shell.setActive('logins');
        };
        shell.setStatus(`${byIp.length} source(s) · ${events.length} event(s)`, 'ok');
    } catch (err) {
        if (!isCurrent()) return;
        shell.view.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read failed logins', 'error');
        logError('Security: failed logins unavailable', err?.detail || err?.message);
    }
}

// ════════════════════════════════════════════════════════════════════════════
// Bans
// ════════════════════════════════════════════════════════════════════════════

async function loadBans(isCurrent) {
    shell.view.innerHTML = skeletonRows(5);
    shell.setStatus('Loading bans…');
    try {
        const data = await apiRequest('/api/security/bans');
        if (!isCurrent()) return;
        const bans = data.bans || [];

        const rows = bans.map((ban, index) => `<tr>
            <td>${ban.num ?? index + 1}</td>
            <td class="path">${escapeHtml(ban.ip)}</td>
            <td>${ban.kind === 'auto'
                ? '<span class="ui-tag is-info">auto</span>'
                : '<span class="ui-tag is-warn">manual</span>'}</td>
            <td class="ui-muted">${ban.expires_at ? _ts(ban.expires_at) : 'permanent'}</td>
            <td class="ui-muted">${escapeHtml(ban.reason || '—')}</td>
            <td class="actions"><button class="ui-btn small" data-action="unban" data-ip="${escapeAttr(ban.ip)}"
                data-testid="sec-unban-${escapeAttr(ban.ip)}">${uiIcon('check', 12)} Unban</button></td>
        </tr>`).join('');

        shell.view.innerHTML = `
            <div class="ui-toolbar">
                <div class="ui-row">
                    <input id="sec-ban-ip" class="ui-input mono" data-testid="sec-ban-input" placeholder="e.g. 203.0.113.42 or 203.0.113.0/24">
                    <button class="ui-btn small danger" data-action="ban-input" data-testid="sec-ban-btn">${uiIcon('ban', 12)} Ban IP</button>
                </div>
                <span class="ui-tag">${bans.length} ban(s)</span>
            </div>
            <p class="ui-muted">Manual bans are permanent UFW deny rules tagged <code class="ui-code">ethos-ban</code>; automatic bans are temporary rules tagged <code class="ui-code">ethos-autoban</code> that expire on their own. Both survive restarts and reboots.</p>
            ${data.ufw_enabled === false ? _ufwDisabledCallout('UFW is disabled — bans are stored as rules but only enforced after the firewall is enabled.') : ''}
            ${bans.length ? `<div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table" data-testid="sec-bans-table">
                    <thead><tr><th>#</th><th>IP</th><th>Type</th><th>Expires</th><th>Reason</th><th class="actions"></th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div></div>`
                : emptyState({ icon: 'ban', title: 'No active bans', hint: 'Ban an IP here or from Intrusion Attempts.' })}`;

        shell.view.querySelector('[data-action="ban-input"]')?.addEventListener('click', () => {
            const input = shell.view.querySelector('#sec-ban-ip');
            _banIp(input?.value || '');
        });
        shell.view.querySelector('#sec-ban-ip')?.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') _banIp(event.target.value || '');
        });
        shell.view.querySelectorAll('[data-action="unban"]').forEach((button) => {
            button.addEventListener('click', () => _unbanIp(button.dataset.ip));
        });
        shell.setStatus(`${bans.length} ban(s)`, 'ok');
    } catch (err) {
        if (!isCurrent()) return;
        shell.view.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read bans', 'error');
        logError('Security: bans unavailable', err?.detail || err?.message);
    }
}

// ════════════════════════════════════════════════════════════════════════════
// Actions
// ════════════════════════════════════════════════════════════════════════════

async function _banIp(ip) {
    const value = String(ip || '').trim();
    if (!value) {
        notify({ kind: 'error', message: 'Enter an IP address to ban' });
        return;
    }
    const ok = await showConfirm('Ban IP', `Ban ${value} with a permanent UFW drop rule?`, { danger: true });
    if (!ok) return;
    try {
        const result = await apiRequest('/api/security/bans', { method: 'POST', body: { ip: value } });
        notify({ kind: 'success', message: result?.already_banned ? `${value} was already banned` : `${value} banned` });
        selectedIps.delete(value);
        shell.setActive(activeTab);
    } catch (err) {
        logError('Security: ban failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Could not ban the IP' });
    }
}

async function _unbanIp(ip) {
    if (!ip) return;
    const ok = await showConfirm('Unban IP', `Remove ban rules for ${ip}?`, { danger: true });
    if (!ok) return;
    try {
        await apiRequest(`/api/security/bans/${encodeURIComponent(ip)}`, { method: 'DELETE' });
        notify({ kind: 'success', message: `${ip} unbanned` });
        shell.setActive(activeTab);
    } catch (err) {
        logError('Security: unban failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Could not unban the IP' });
    }
}

async function _banSelected() {
    const ips = [...selectedIps];
    if (!ips.length) return;
    const preview = `${ips.slice(0, 8).join(', ')}${ips.length > 8 ? ` … (+${ips.length - 8} more)` : ''}`;
    const ok = await showConfirm('Ban IPs', `Ban ${ips.length} IP(s) with permanent UFW drop rules?\n\n${preview}`, { danger: true });
    if (!ok) return;
    try {
        const result = await apiRequest('/api/security/bans/bulk', { method: 'POST', body: { ips } });
        let message = `${result?.banned ?? 0} banned`;
        if (result?.already) message += `, ${result.already} already banned`;
        notify({ kind: 'success', message });
        for (const failure of result?.errors || []) {
            notify({ kind: 'error', message: `Failed to ban ${failure.ip}: ${failure.error}` });
        }
        selectedIps.clear();
        shell.setActive('intrusions');
    } catch (err) {
        logError('Security: bulk ban failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Bulk ban failed' });
    }
}

// ════════════════════════════════════════════════════════════════════════════
// Auto-ban
// ════════════════════════════════════════───────────────────────────

function _stopAutobanLive() {
    if (autobanTimer) { autobanTimer(); autobanTimer = null; }
}

function _startAutobanPoll(isCurrent) {
    _stopAutobanLive();
    autobanTimer = poll(() => loadAutoban(isCurrent || (() => activeTab === 'autoban')), 15000,
        { appId: 'access-control', immediate: false, name: 'autoban' });
}

async function loadAutoban(isCurrent) {
    // Never repaint over a form the admin is editing (the tab polls every 15 s).
    if (shell.view.dataset.loaded === '1' && shell.view.dataset.dirty === '1') {
        _startAutobanPoll(isCurrent);
        return;
    }
    if (shell.view.dataset.loaded !== '1') shell.view.innerHTML = skeletonRows(6);
    shell.setStatus('Loading auto-ban…');
    try {
        const data = await apiRequest('/api/security/autoban');
        if (!isCurrent()) return;
        shell.view.dataset.loaded = '1';
        _renderAutoban(data);
        const cfg = data.config || {};
        shell.setStatus(
            cfg.enabled ? (cfg.dry_run ? 'Auto-ban: dry-run' : 'Auto-ban: active') : 'Auto-ban: disabled',
            cfg.enabled && !cfg.dry_run ? 'ok' : 'warn',
        );
    } catch (err) {
        if (!isCurrent()) return;
        shell.view.innerHTML = errorState({ detail: _errText(err) });
        shell.setStatus('Could not read auto-ban', 'error');
        logError('Security: auto-ban unavailable', _errText(err));
    }
    _startAutobanPoll(isCurrent);
}

function _renderAutoban(data) {
    const cfg = data.config || {};
    const fl = cfg.failed_logins || {};
    const ub = cfg.ufw_blocks || {};
    const stats = data.stats || {};
    const active = data.active || [];
    const history = data.history || [];
    const dryRun = Boolean(cfg.dry_run);
    const protectedNets = data.protected_networks || [];

    const stateTag = cfg.enabled
        ? (dryRun
            ? '<span class="ui-tag is-warn">enabled · dry-run</span>'
            : '<span class="ui-tag is-ok">enabled</span>')
        : '<span class="ui-tag is-bad">disabled</span>';

    const check = (id, label, checked, hint = '') => `<label class="ui-check">
        <input type="checkbox" id="${id}" ${checked ? 'checked' : ''}> ${escapeHtml(label)}
    </label>${hint ? `<p class="ui-muted">${escapeHtml(hint)}</p>` : ''}`;

    const activeRows = active.map((ban) => `<tr>
        <td class="path">${escapeHtml(ban.ip)}</td>
        <td><span class="ui-tag is-bad">${Number(ban.strikes) || 1}</span></td>
        <td class="ui-muted">${escapeHtml(ban.reason || '—')}</td>
        <td class="ui-muted">${_ts(ban.expires_at)}</td>
        <td class="ui-muted">${_duration(ban.expires_in_seconds)}</td>
        <td class="actions"><button class="ui-btn small" data-action="ab-unban" data-ip="${escapeAttr(ban.ip)}"
            data-testid="sec-ab-unban-${escapeAttr(ban.ip)}">${uiIcon('check', 12)} Unban</button></td>
    </tr>`).join('');

    const historyRows = history.map((entry) => {
        const tone = entry.event === 'banned' ? 'is-bad'
            : entry.event === 'unbanned' ? 'is-ok'
                : entry.event === 'vanished' ? 'is-warn' : '';
        return `<tr>
            <td class="ui-muted">${_ts(entry.ts)}</td>
            <td><span class="ui-tag ${tone}">${escapeHtml(entry.event || '—')}</span></td>
            <td class="path">${escapeHtml(entry.ip || '—')}</td>
            <td class="ui-muted">${entry.minutes ? `${Number(entry.minutes)} min` : '—'}</td>
            <td class="ui-muted">${escapeHtml(entry.reason || '—')}</td>
        </tr>`;
    }).join('');

    const preview = autobanPreview;
    const previewCandidates = preview?.candidates || [];
    const previewRows = previewCandidates.map((item) => `<tr>
        <td class="path">${escapeHtml(item.ip)}</td>
        <td><span class="ui-tag is-bad">${Number(item.count) || 0}</span></td>
        <td>${(item.sources || []).map((source) => `<span class="ui-tag">${escapeHtml(source)}</span>`).join(' ')}</td>
        <td class="ui-muted">${escapeHtml(item.reason || '—')}</td>
        <td class="ui-muted">${_ts(item.last_ts)}</td>
    </tr>`).join('');

    const protectedTags = protectedNets.map((net) =>
        `<span class="ui-tag" title="${escapeAttr(net.label || '')}">${escapeHtml(net.cidr)}</span>`).join(' ');

    const allowlist = Array.isArray(cfg.allowlist) ? cfg.allowlist.join('\n') : '';

    shell.view.innerHTML = `
        <div class="ui-toolbar">
            <div>
                <h2 data-testid="sec-ab-state">${stateTag} <span class="ui-muted">last sweep ${escapeHtml(stats.last_run_at ? _ts(stats.last_run_at) : 'never')}</span></h2>
                <p class="ui-muted">Temporary UFW bans for repeat offenders. Loopback, private LAN, link-local, CGNAT and IPv6 ULA addresses are never blocked.</p>
            </div>
            <div class="ui-row">
                <button class="ui-btn small" data-action="ab-refresh" data-testid="sec-ab-refresh">${uiIcon('refresh', 12)} Refresh</button>
                <button class="ui-btn small" data-action="ab-preview" data-testid="sec-ab-preview">${uiIcon('activity', 12)} Preview now</button>
                <button class="ui-btn small danger" data-action="ab-apply" data-testid="sec-ab-apply">${uiIcon('ban', 12)} Apply now</button>
            </div>
        </div>
        ${data.ufw_enabled === false
            ? _ufwDisabledCallout('UFW is disabled — auto-ban can record state, but no rule is enforced until the firewall is enabled.')
            : ''}
        ${cfg.enabled && dryRun
            ? '<div class="ui-callout">Dry-run is on: candidates are shown here and in the log, but UFW is not touched.</div>'
            : ''}
        <div class="ui-grid">
            <section class="ui-panel">
                <div class="ui-toolbar"><h2>Policy</h2>${stateTag}</div>
                <div class="ui-panel-body">
                    <div class="ui-fields">
                        <div class="ui-field full">
                            ${check('ab-enabled', 'Enable automatic blocking', cfg.enabled)}
                            ${check('ab-dry-run', 'Dry-run only (preview, never touch UFW)', dryRun)}
                            ${check('ab-escalate', 'Escalate repeat offenders (1 h → 24 h → 7 d)', cfg.escalate)}
                        </div>
                        <div class="ui-field"><label for="ab-login-threshold">Failed logins before ban</label>
                            <input id="ab-login-threshold" class="ui-input" type="number" min="1" max="1000" value="${Number(fl.threshold) || 5}"></div>
                        <div class="ui-field"><label for="ab-login-window">Login window (minutes)</label>
                            <input id="ab-login-window" class="ui-input" type="number" min="1" max="1440" value="${Number(fl.window_minutes) || 10}"></div>
                        <div class="ui-field"><label for="ab-login-ban">Login ban (minutes)</label>
                            <input id="ab-login-ban" class="ui-input" type="number" min="5" max="43200" value="${Number(fl.ban_minutes) || 60}"></div>
                        <div class="ui-field">${check('ab-login-enabled', 'Use failed logins (sshd + web/PAM)', fl.enabled)}</div>
                        <div class="ui-field"><label for="ab-block-threshold">Blocked packets before ban</label>
                            <input id="ab-block-threshold" class="ui-input" type="number" min="1" max="1000" value="${Number(ub.threshold) || 40}"></div>
                        <div class="ui-field"><label for="ab-block-window">Block window (minutes)</label>
                            <input id="ab-block-window" class="ui-input" type="number" min="1" max="1440" value="${Number(ub.window_minutes) || 10}"></div>
                        <div class="ui-field"><label for="ab-block-ban">Block ban (minutes)</label>
                            <input id="ab-block-ban" class="ui-input" type="number" min="5" max="43200" value="${Number(ub.ban_minutes) || 60}"></div>
                        <div class="ui-field"><label for="ab-block-ports">Minimum distinct ports</label>
                            <input id="ab-block-ports" class="ui-input" type="number" min="1" max="100" value="${Number(ub.min_ports) || 5}"></div>
                        <div class="ui-field">${check('ab-block-enabled', 'Use UFW block floods (scanners)', ub.enabled)}</div>
                        <div class="ui-field"><label for="ab-max-active">Max active auto-bans</label>
                            <input id="ab-max-active" class="ui-input" type="number" min="1" max="2000" value="${Number(cfg.max_active_bans) || 200}"></div>
                        <div class="ui-field"><label for="ab-max-sweep">Max new bans per sweep</label>
                            <input id="ab-max-sweep" class="ui-input" type="number" min="1" max="100" value="${Number(cfg.max_bans_per_sweep) || 10}"></div>
                        <div class="ui-field"><label for="ab-max-day">Max new bans per day</label>
                            <input id="ab-max-day" class="ui-input" type="number" min="1" max="5000" value="${Number(cfg.max_bans_per_day) || 200}"></div>
                    </div>
                    <div class="ui-row ui-panel-body">
                        <button class="ui-btn small primary" data-action="ab-save" data-testid="sec-ab-save">${uiIcon('check', 12)} Save settings</button>
                        <span class="ui-tag">${Number(stats.active_bans ?? active.length) || 0} active</span>
                        <span class="ui-tag">${Number(stats.banned_today) || 0} banned today</span>
                    </div>
                </div>
            </section>
            <section class="ui-panel">
                <div class="ui-toolbar"><h2>Never blocked</h2><span class="ui-tag is-ok">always on</span></div>
                <div class="ui-panel-body">
                    <p class="ui-muted">Detected LAN: <span class="mono">${escapeHtml(data.detected_lan || 'unknown')}</span>. These ranges are refused before any rule is created:</p>
                    <div class="ui-row">${protectedTags}</div>
                    <div class="ui-field full">
                        <label for="ab-allowlist">Allowlist — one IP or CIDR per line</label>
                        <textarea id="ab-allowlist" class="ui-textarea mono" rows="4" data-testid="sec-ab-allowlist" placeholder="e.g. 45.33.32.156&#10;198.51.100.0/24">${escapeHtml(allowlist)}</textarea>
                    </div>
                    <p class="ui-muted">Unbanning an auto-banned address starts a ${Number(data.cooldown_minutes) || 30} min cooldown so it is not re-banned immediately.</p>
                </div>
            </section>
        </div>
        ${preview ? `<section class="ui-panel" data-testid="sec-ab-preview-panel">
            <div class="ui-toolbar"><h2>Preview</h2><span class="ui-tag">${previewCandidates.length} candidate(s)</span></div>
            <div class="ui-panel-body">
                ${previewCandidates.length ? `<div class="ui-table-wrap"><table class="ui-table" data-testid="sec-ab-preview-table">
                        <thead><tr><th>IP</th><th>Events</th><th>Source</th><th>Reason</th><th>Last seen</th></tr></thead>
                        <tbody>${previewRows}</tbody>
                    </table></div>`
                    : emptyState({ icon: 'shield', title: 'Nothing crossed the thresholds', hint: 'No address would be banned with the current policy.' })}
            </div>
        </section>` : ''}
        <section class="ui-panel" data-testid="sec-ab-active-panel">
            <div class="ui-toolbar"><h2>Active auto-bans</h2><span class="ui-tag">${active.length}</span></div>
            ${active.length ? `<div class="ui-table-wrap"><table class="ui-table" data-testid="sec-ab-active">
                    <thead><tr><th>IP</th><th>Strike</th><th>Reason</th><th>Expires</th><th>Remaining</th><th class="actions"></th></tr></thead>
                    <tbody>${activeRows}</tbody>
                </table></div>`
                : `<div class="ui-panel-body">${emptyState({ icon: 'ban', title: 'No active auto-bans', hint: 'Temporary bans appear here and expire on their own.' })}</div>`}
        </section>
        <section class="ui-panel" data-testid="sec-ab-history-panel">
            <div class="ui-toolbar"><h2>Recent activity</h2><span class="ui-tag">last ${history.length}</span></div>
            ${history.length ? `<div class="ui-table-wrap"><table class="ui-table" data-testid="sec-ab-history">
                    <thead><tr><th>Time</th><th>Event</th><th>IP</th><th>Duration</th><th>Reason</th></tr></thead>
                    <tbody>${historyRows}</tbody>
                </table></div>`
                : `<div class="ui-panel-body">${emptyState({ icon: 'activity', title: 'No auto-ban activity yet', hint: 'Run a preview or enable the engine.' })}</div>`}
        </section>`;

    _wireAutoban();
}

function _wireAutoban() {
    shell.view.oninput = () => { shell.view.dataset.dirty = '1'; };
    shell.view.querySelector('[data-action="ab-refresh"]')?.addEventListener('click', () => shell.setActive('autoban'));
    shell.view.querySelector('[data-action="ab-save"]')?.addEventListener('click', () => _saveAutoban());
    shell.view.querySelector('[data-action="ab-preview"]')?.addEventListener('click', () => _previewAutoban());
    shell.view.querySelector('[data-action="ab-apply"]')?.addEventListener('click', () => _applyAutoban());
    shell.view.querySelectorAll('[data-action="ab-unban"]').forEach((button) => {
        button.addEventListener('click', () => _unbanIp(button.dataset.ip));
    });
}

function _autobanFormValues() {
    const value = (selector, fallback = '') => shell.view.querySelector(selector)?.value ?? fallback;
    const num = (selector, fallback) => {
        const parsed = Number(value(selector));
        return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
    };
    const checked = (selector) => Boolean(shell.view.querySelector(selector)?.checked);
    return {
        enabled: checked('#ab-enabled'),
        dry_run: checked('#ab-dry-run'),
        escalate: checked('#ab-escalate'),
        failed_logins: {
            enabled: checked('#ab-login-enabled'),
            threshold: num('#ab-login-threshold', 5),
            window_minutes: num('#ab-login-window', 10),
            ban_minutes: num('#ab-login-ban', 60),
        },
        ufw_blocks: {
            enabled: checked('#ab-block-enabled'),
            threshold: num('#ab-block-threshold', 40),
            window_minutes: num('#ab-block-window', 10),
            ban_minutes: num('#ab-block-ban', 60),
            min_ports: num('#ab-block-ports', 5),
        },
        allowlist: String(value('#ab-allowlist')).split(/[\s,]+/).map((part) => part.trim()).filter(Boolean),
        max_active_bans: num('#ab-max-active', 200),
        max_bans_per_sweep: num('#ab-max-sweep', 10),
        max_bans_per_day: num('#ab-max-day', 200),
    };
}

async function _saveAutoban() {
    const body = _autobanFormValues();
    if (body.enabled && !body.dry_run) {
        const ok = await showConfirm('Enable auto-ban',
            'Repeat offenders will be blocked with temporary UFW rules. Loopback and local networks are never blocked. Continue?',
            { danger: true });
        if (!ok) return;
    }
    try {
        await apiRequest('/api/security/autoban', { method: 'PUT', body });
        autobanPreview = null;
        notify({ kind: 'success', message: 'Auto-ban settings saved' });
        shell.setActive('autoban');
    } catch (err) {
        logError('Security: saving auto-ban failed', _errText(err));
        notify({ kind: 'error', message: _errText(err) });
    }
}

async function _previewAutoban() {
    try {
        autobanPreview = await apiRequest('/api/security/autoban/run', {
            method: 'POST', body: { apply: false },
        });
        const count = (autobanPreview?.candidates || []).length;
        notify({ kind: count ? 'info' : 'success', message: count
            ? `${count} IP(s) would be banned`
            : 'Nothing would be banned right now' });
        shell.setActive('autoban');
    } catch (err) {
        logError('Security: auto-ban preview failed', _errText(err));
        notify({ kind: 'error', message: _errText(err) });
    }
}

async function _applyAutoban() {
    const ok = await showConfirm('Apply auto-ban now',
        'Block every current candidate with a temporary UFW rule? Preview first if you are unsure.',
        { danger: true });
    if (!ok) return;
    try {
        const result = await apiRequest('/api/security/autoban/run', {
            method: 'POST', body: { apply: true },
        });
        autobanPreview = null;
        const banned = (result?.banned || []).length;
        notify({ kind: banned ? 'success' : 'info', message: banned
            ? `${banned} IP(s) banned`
            : 'No IP matched the current policy' });
        for (const failure of result?.errors || []) {
            notify({ kind: 'error', message: `Could not ban ${failure.ip}: ${failure.error}` });
        }
        shell.setActive('autoban');
    } catch (err) {
        logError('Security: applying auto-ban failed', _errText(err));
        notify({ kind: 'error', message: _errText(err) });
    }
}

function _duration(seconds) {
    const total = Number(seconds);
    if (!Number.isFinite(total) || total <= 0) return '—';
    if (total < 90) return `${Math.round(total)} s`;
    if (total < 5400) return `${Math.round(total / 60)} min`;
    if (total < 172800) return `${Math.round(total / 3600)} h`;
    return `${Math.round(total / 86400)} d`;
}

function _errText(err) {
    const detail = err?.detail;
    if (Array.isArray(detail)) {
        return detail.map((item) => item?.msg || String(item)).join('; ');
    }
    return detail || err?.message || 'Unknown error';
}

// ════════════════════════════════════════════════════════════════════════
// Shared helpers
// ════════════════════════════════════════════════════════════════════════

const LOADERS = {
    overview: loadOverview,
    ufw: loadUfw,
    autoban: loadAutoban,
    intrusions: loadIntrusions,
    logins: loadLogins,
    bans: loadBans,
};

function _kv(rows) {
    return `<dl class="ui-kv">${rows.map(([key, value]) => `<div>
        <dt class="ui-kv-key">${escapeHtml(key)}</dt><dd class="ui-kv-value">${escapeHtml(value)}</dd>
    </div>`).join('')}</dl>`;
}

function _ts(value) {
    if (value === null || value === undefined || value === '') return '—';
    let date;
    if (typeof value === 'string' && !/^\d+(\.\d+)?$/.test(value)) {
        date = new Date(value.replace(/(\.\d{3})\d+/, '$1'));
    } else {
        let number = Number(value);
        if (!Number.isFinite(number)) return escapeHtml(String(value));
        if (number < 1e12) number *= 1000;
        date = new Date(number);
    }
    return Number.isNaN(date.getTime()) ? escapeHtml(String(value)) : escapeHtml(date.toLocaleString());
}
