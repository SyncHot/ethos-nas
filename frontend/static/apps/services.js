/**
 * EthOS NAS — Service Manager (unified)
 *
 * One professional view over every service on the box:
 *   • Docker containers  (GET /api/containers, actions via POST /api/containers/{name}/action)
 *   • systemd units      (GET /api/systemd,    actions via POST /api/systemd/{name}/action)
 *
 * Features: summary chips, live search, type tabs, clear status badges,
 * start/stop/restart with confirm dialogs, boot-enable toggle for units,
 * silent auto-refresh while the window is open.
 */

import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiFetch } from '../core/api.js';
import { poll } from '../core/poller.js';
import { skeletonRows } from '../core/states.js';

const AUTO_REFRESH_MS = 30_000;

const state = {
    containers: [],      // [{name,image,status,state,ports,compose_project,...}]
    units: [],           // [{name,description,active_state,sub_state,enabled_state,...}]
    dockerError: null,   // string when the Docker source failed to load
    systemdError: null,  // string when the systemd source failed to load
    tab: 'all',          // 'all' | 'docker' | 'systemd'
    query: '',
    loading: false,
    lastUpdated: null,   // Date of the last successful refresh
};

let _autoStop = null; // shared poller stop handle (core/poller.js)

/* ── Status helpers ─────────────────────────────────────────────────────── */

function _dockerBadge(c) {
    if (c.status === 'running') return { cls: 'badge-green', label: 'Running' };
    if (c.status === 'exited') {
        const m = /^Exited \((\d+)\)/.exec(c.state || '');
        const code = m ? Number(m[1]) : 0;
        return code === 0
            ? { cls: 'badge-yellow', label: 'Stopped' }
            : { cls: 'badge-red', label: `Failed (${code})` };
    }
    if (c.status === 'paused') return { cls: 'badge-blue', label: 'Paused' };
    const s = c.status || 'unknown';
    return { cls: 'badge-blue', label: s.charAt(0).toUpperCase() + s.slice(1) };
}

function _unitBadge(u) {
    if (u.active_state === 'failed') return { cls: 'badge-red', label: 'Failed' };
    if (u.active_state === 'active') {
        // oneshot units finish with sub_state "exited" — that is a healthy state.
        return u.sub_state === 'exited'
            ? { cls: 'badge-green', label: 'Active' }
            : { cls: 'badge-green', label: 'Running' };
    }
    if (['activating', 'deactivating', 'reloading'].includes(u.active_state)) {
        return { cls: 'badge-blue', label: u.active_state.charAt(0).toUpperCase() + u.active_state.slice(1) + '…' };
    }
    return { cls: 'badge-yellow', label: 'Stopped' };
}

function _isRunning(item) {
    if (item.kind === 'docker') return item.status === 'running';
    return item.active_state === 'active';
}

function _isFailed(item) {
    if (item.kind === 'docker') {
        const m = /^Exited \((\d+)\)/.exec(item.state || '');
        return item.status === 'exited' && !!m && Number(m[1]) !== 0;
    }
    return item.active_state === 'failed';
}

/** Sort: running first, then failed (needs attention), then the rest — A→Z inside each group. */
function _sortItems(items) {
    const rank = it => (_isRunning(it) ? 0 : _isFailed(it) ? 1 : 2);
    return [...items].sort((a, b) => (rank(a) - rank(b)) || a.name.localeCompare(b.name));
}

function _visibleItems() {
    const q = state.query.trim().toLowerCase();
    let items;
    if (state.tab === 'docker') {
        items = state.containers.map(c => ({ kind: 'docker', ...c }));
    } else if (state.tab === 'systemd') {
        items = state.units.map(u => ({ kind: 'systemd', ...u }));
    } else {
        items = [
            ...state.containers.map(c => ({ kind: 'docker', ...c })),
            ...state.units.map(u => ({ kind: 'systemd', ...u })),
        ];
    }
    if (q) {
        items = items.filter(it => {
            const hay = [it.name, it.image || '', it.description || '', it.ports || '', it.compose_project || '']
                .join(' ').toLowerCase();
            return hay.includes(q);
        });
    }
    return _sortItems(items);
}

function _counts() {
    const all = [
        ...state.containers.map(c => ({ kind: 'docker', ...c })),
        ...state.units.map(u => ({ kind: 'systemd', ...u })),
    ];
    return {
        running: all.filter(_isRunning).length,
        failed: all.filter(_isFailed).length,
        stopped: all.length - all.filter(_isRunning).length - all.filter(_isFailed).length,
        total: all.length,
    };
}

/* ── Rendering ──────────────────────────────────────────────────────────── */

function _rowHtml(it) {
    const safe = escapeAttr(it.name);
    let icon, subLine, badge, detail;

    if (it.kind === 'docker') {
        icon = '🐳';
        subLine = it.image || '';
        badge = _dockerBadge(it);
        // docker's `state` is already human-friendly ("Up 3d", "Exited (1) 2h ago")
        detail = it.ports && it.ports !== '—' ? escapeHtml(it.ports) : '<span class="svc-muted">no published ports</span>';
    } else {
        icon = '⚙️';
        subLine = it.description || '';
        badge = _unitBadge(it);
        const en = it.enabled_state || '';
        if (en === 'enabled') detail = '<span style="color:#22c55e;font-size:11px;">● enabled at boot</span>';
        else if (en === 'disabled') detail = '<span class="svc-muted">○ disabled at boot</span>';
        else if (en) detail = `<span class="svc-muted">${escapeHtml(en)}</span>`;
        else detail = '<span class="svc-muted">—</span>';
    }

    const running = _isRunning(it);
    let actions;
    if (it.kind === 'docker') {
        actions = running
            ? `<button class="btn-ghost svc-btn" data-testid="services-action-${safe}-stop" onclick="window._servicesAction('docker','${safe}','stop')" title="Stop">⏹ Stop</button>
               <button class="btn-ghost svc-btn" data-testid="services-action-${safe}-restart" onclick="window._servicesAction('docker','${safe}','restart')" title="Restart">↻ Restart</button>`
            : `<button class="btn-ghost svc-btn" data-testid="services-action-${safe}-start" onclick="window._servicesAction('docker','${safe}','start')" title="Start">▶ Start</button>`;
    } else {
        const en = it.enabled_state || '';
        const canToggle = (en === 'enabled' || en === 'disabled');
        actions = running
            ? `<button class="btn-ghost svc-btn" data-testid="services-action-${safe}-stop" onclick="window._servicesAction('systemd','${safe}','stop')" title="Stop">⏹ Stop</button>
               <button class="btn-ghost svc-btn" data-testid="services-action-${safe}-restart" onclick="window._servicesAction('systemd','${safe}','restart')" title="Restart">↻ Restart</button>`
            : `<button class="btn-ghost svc-btn" data-testid="services-action-${safe}-start" onclick="window._servicesAction('systemd','${safe}','start')" title="Start">▶ Start</button>`;
        if (canToggle) {
            actions += en === 'enabled'
                ? `<button class="btn-ghost svc-btn" data-testid="services-action-${safe}-disable" onclick="window._servicesAction('systemd','${safe}','disable')" title="Disable at boot">🔕</button>`
                : `<button class="btn-ghost svc-btn" data-testid="services-action-${safe}-enable" onclick="window._servicesAction('systemd','${safe}','enable')" title="Enable at boot">🔛</button>`;
        }
    }

    return `
        <tr data-testid="services-row-${safe}" style="border-bottom:1px solid var(--border);cursor:default;">
            <td style="padding:9px 12px;vertical-align:top;">
                <div style="display:flex;align-items:center;gap:8px;font-weight:600;color:var(--text-1);font-size:13px;">
                    <span>${icon}</span>
                    <span style="font-family:monospace;font-size:12.5px;" title="${escapeAttr(it.name)}">${escapeHtml(it.name)}</span>
                </div>
                ${subLine ? `<div class="svc-muted" style="margin-top:2px;padding-left:24px;max-width:380px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeAttr(subLine)}">${escapeHtml(subLine)}</div>` : ''}
            </td>
            <td style="padding:9px 12px;vertical-align:top;white-space:nowrap;">
                <span class="badge ${badge.cls}" data-testid="services-status-${safe}">${badge.label}</span>
                ${it.kind === 'docker' && it.state ? `<div class="svc-muted" style="margin-top:3px;font-size:10.5px;">${escapeHtml(it.state)}</div>` : ''}
            </td>
            <td style="padding:9px 12px;vertical-align:top;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11.5px;color:var(--text-2);" title="${it.kind === 'docker' ? escapeAttr(it.ports || '') : ''}">${detail}</td>
            <td style="padding:9px 12px;vertical-align:top;text-align:right;white-space:nowrap;">${actions}</td>
        </tr>`;
}

function _render() {
    const tableEl = document.getElementById('servicesTable');
    if (!tableEl) return; // window closed

    const counts = _counts();
    const cDocker = state.containers.length;
    const cSystemd = state.units.length;

    // Summary chips
    const chipRun = document.getElementById('services-chip-running');
    const chipStop = document.getElementById('services-chip-stopped');
    const chipFail = document.getElementById('services-chip-failed');
    if (chipRun) chipRun.textContent = `● ${counts.running} running`;
    if (chipStop) chipStop.textContent = `○ ${counts.stopped} stopped`;
    if (chipFail) {
        chipFail.textContent = `⚠ ${counts.failed} failed`;
        chipFail.style.display = counts.failed > 0 ? '' : 'none';
    }

    // Tabs
    const tabDefs = [
        ['all', `All · ${cDocker + cSystemd}`],
        ['docker', `Containers · ${cDocker}`],
        ['systemd', `Systemd · ${cSystemd}`],
    ];
    for (const [id, label] of tabDefs) {
        const el = document.getElementById(`services-tab-${id}`);
        if (!el) continue;
        el.textContent = label;
        const active = state.tab === id;
        el.style.color = active ? 'var(--text-1)' : 'var(--text-3)';
        el.style.borderBottom = active ? '2px solid var(--accent)' : '2px solid transparent';
    }

    // Source error banners (only for sources relevant to the current tab)
    const bannerEl = document.getElementById('servicesBanners');
    if (bannerEl) {
        let html = '';
        const showDocker = state.tab !== 'systemd' && state.dockerError;
        const showSystemd = state.tab !== 'docker' && state.systemdError;
        if (showDocker) html += `<div class="svc-banner" data-testid="services-error-docker">⚠ Docker unavailable: ${escapeHtml(state.dockerError)}</div>`;
        if (showSystemd) html += `<div class="svc-banner" data-testid="services-error-systemd">⚠ Systemd unavailable: ${escapeHtml(state.systemdError)}</div>`;
        bannerEl.innerHTML = html;
    }

    // Table body
    const items = _visibleItems();
    let html;
    if (items.length === 0) {
        const msg = state.query
            ? `No services match “${escapeHtml(state.query)}”.`
            : 'No services found.';
        html = `<tr><td colspan="4" style="padding:36px;text-align:center;color:var(--text-3);font-size:13px;">${msg}</td></tr>`;
    } else {
        html = items.map(_rowHtml).join('');
    }
    tableEl.innerHTML = `<tbody>${html}</tbody>`;

    // Footer
    const foot = document.getElementById('servicesFooter');
    if (foot) {
        const when = state.lastUpdated ? state.lastUpdated.toLocaleTimeString() : '—';
        foot.textContent = `${counts.total} services · ${cDocker} containers + ${cSystemd} units · updated ${when}`;
    }
}

/* ── Data loading ───────────────────────────────────────────────────────── */

/** Fetch JSON, throwing an Error whose message carries the server's `detail` when present. */
async function _fetchJson(path) {
    const res = await apiFetch(path);
    if (!res.ok) {
        let detail = 'HTTP ' + res.status;
        try {
            const d = await res.json();
            if (d && d.detail) detail = typeof d.detail === 'string' ? d.detail : JSON.stringify(d.detail);
        } catch (_) { /* non-JSON error body — keep the HTTP status */ }
        throw new Error(detail);
    }
    return res.json();
}

async function _load(silent) {
    if (state.loading) return;
    state.loading = true;
    const tableEl = document.getElementById('servicesTable');
    if (!silent && tableEl) {
        tableEl.innerHTML = skeletonRows(8);
    }

    const [dockerRes, systemdRes] = await Promise.allSettled([
        _fetchJson('/api/containers'),
        _fetchJson('/api/systemd'),
    ]);

    if (dockerRes.status === 'fulfilled' && dockerRes.value) {
        state.containers = Array.isArray(dockerRes.value.containers) ? dockerRes.value.containers : [];
        state.dockerError = null;
    } else {
        const err = dockerRes.reason || {};
        state.dockerError = err.detail || err.message || 'request failed';
    }

    if (systemdRes.status === 'fulfilled' && systemdRes.value) {
        state.units = Array.isArray(systemdRes.value.services) ? systemdRes.value.services : [];
        state.systemdError = null;
    } else {
        const err = systemdRes.reason || {};
        state.systemdError = err.detail || err.message || 'request failed';
    }

    state.lastUpdated = new Date();
    state.loading = false;
    _render();
}

/* ── Public API (window globals, consistent with other apps) ───────────── */

export async function renderServicesPage(body) {
    if (!body) return;

    body.innerHTML = `
        <div style="height:100%;display:flex;flex-direction:column;background:var(--bg,#0f172a);font-family:system-ui,sans-serif;color:var(--text-1);" data-testid="services-app">
            <!-- Header: title + summary chips -->
            <div style="padding:14px 16px 0;display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                <h2 style="margin:0;font-size:17px;font-weight:700;color:var(--text-1);display:flex;align-items:center;gap:8px;">⚙️ Service Manager</h2>
                <span id="services-chip-running" class="badge badge-green" data-testid="services-chip-running">● 0 running</span>
                <span id="services-chip-stopped" class="badge badge-yellow" data-testid="services-chip-stopped">○ 0 stopped</span>
                <span id="services-chip-failed" class="badge badge-red" data-testid="services-chip-failed" style="display:none;">⚠ 0 failed</span>
            </div>

            <!-- Toolbar: search + refresh -->
            <div style="padding:12px 16px;display:flex;gap:8px;align-items:center;">
                <input id="servicesSearch" data-testid="services-search" type="text" placeholder="🔍 Search by name, image or description…"
                    oninput="window._servicesSearch(this.value)"
                    style="flex:1;padding:7px 12px;border-radius:8px;border:1px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:13px;outline:none;">
                <button class="btn-ghost" data-testid="services-refresh-btn" onclick="window._servicesLoad()" style="padding:7px 14px;font-size:12px;border-radius:8px;display:flex;align-items:center;gap:6px;" title="Refresh now">🔄 Refresh</button>
            </div>

            <!-- Tabs -->
            <div style="display:flex;gap:4px;padding:0 16px;border-bottom:1px solid var(--border);">
                ${['all', 'docker', 'systemd'].map(t => `
                    <button id="services-tab-${t}" data-testid="services-tab-${t}" onclick="window._servicesSetTab('${t}')"
                        style="padding:8px 14px;background:none;border:none;cursor:pointer;font-size:12.5px;font-weight:600;color:var(--text-3);border-bottom:2px solid transparent;">${t === 'all' ? 'All' : t === 'docker' ? 'Containers' : 'Systemd'} · 0</button>`).join('')}
            </div>

            <!-- Source error banners -->
            <div id="servicesBanners" data-testid="services-banners"></div>

            <!-- Table -->
            <div style="flex:1;overflow-y:auto;">
                <table class="app-table" id="servicesTable" data-testid="services-table" style="width:100%;font-size:12.5px;">
                    <thead><tr>
                        <th style="text-align:left;padding:8px 12px;min-width:260px;">Service</th>
                        <th style="text-align:left;padding:8px 12px;width:130px;">Status</th>
                        <th style="text-align:left;padding:8px 12px;">Details</th>
                        <th style="text-align:right;padding:8px 12px;width:210px;">Actions</th>
                    </tr></thead>
                    <tbody><tr><td colspan="4" style="padding:0;">${skeletonRows(8)}</td></tr></tbody>
                </table>
            </div>

            <!-- Footer -->
            <div id="servicesFooter" data-testid="services-footer" style="padding:8px 16px;border-top:1px solid var(--border);font-size:11px;color:var(--text-3);">Loading…</div>
        </div>`;

    // Restore search box content (e.g. after a re-render) and start auto-refresh.
    const searchEl = document.getElementById('servicesSearch');
    if (searchEl && state.query) searchEl.value = state.query;

    await _load(false);

    // Shared poller (TODO.md P3-09): one timer per app, paused while hidden and
    // stopped when the window closes.
    if (_autoStop) _autoStop();
    _autoStop = poll(() => _load(true), AUTO_REFRESH_MS,
                     { appId: 'services', immediate: false, name: 'services' });
}

window._servicesLoad = function () { _load(false); };

window._servicesSetTab = function (tab) {
    if (!['all', 'docker', 'systemd'].includes(tab)) return;
    state.tab = tab;
    _render();
};

window._servicesSearch = function (value) {
    state.query = value || '';
    _render();
};

window._servicesAction = async function (kind, name, action) {
    const labels = { start: 'Start', stop: 'Stop', restart: 'Restart', enable: 'Enable at boot for', disable: 'Disable at boot for' };
    const msg = kind === 'docker'
        ? `${labels[action] || action} container “${name}”?`
        : `${labels[action] || action} service “${name}”?`;
    if (!await window.showModalConfirm('Service Manager', msg)) return;

    try {
        const url = kind === 'docker'
            ? `/api/containers/${encodeURIComponent(name)}/action`
            : `/api/systemd/${encodeURIComponent(name)}/action`;
        const resp = await apiFetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            window.addNotification?.(d.detail || `Failed to ${action} ${name}`, 'error');
            return;
        }
        window.addNotification?.(`${labels[action] ? labels[action].replace(' at boot for', '') : action}: ${name}`, 'success');
        await _load(true); // silent refresh — keeps the user's scroll position & search
    } catch (e) {
        window.addNotification?.(e.message, 'error');
    }
};

