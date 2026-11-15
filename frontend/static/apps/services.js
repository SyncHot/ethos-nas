// EthOS NAS — Service Manager (single-pane shell)
// One view over Docker containers (compose projects) and systemd units.
import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiRequest } from '../core/api.js';
import { poll } from '../core/poller.js';
import { skeletonRows, emptyState } from '../core/states.js';
import { showConfirm } from '../core/modal.js';
import { notify } from '../core/notification-center.js';
import { uiIcon } from '../core/icons.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';

const AUTO_REFRESH_MS = 30_000;

const state = {
    containers: [],
    units: [],
    dockerError: null,
    systemdError: null,
    tab: 'all',
    query: '',
    loading: false,
    lastUpdated: null,
};

let shell = null;
let _autoStop = null;

function _dockerBadge(container) {
    if (container.status === 'running') return { tone: 'is-ok', label: 'Running' };
    if (container.status === 'exited') {
        const match = /^Exited \((\d+)\)/.exec(container.state || '');
        const code = match ? Number(match[1]) : 0;
        return code === 0 ? { tone: 'is-warn', label: 'Stopped' } : { tone: 'is-bad', label: `Failed (${code})` };
    }
    if (container.status === 'paused') return { tone: 'is-info', label: 'Paused' };
    const status = container.status || 'unknown';
    return { tone: 'is-info', label: status.charAt(0).toUpperCase() + status.slice(1) };
}

function _unitBadge(unit) {
    if (unit.active_state === 'failed') return { tone: 'is-bad', label: 'Failed' };
    if (unit.active_state === 'active') return { tone: 'is-ok', label: unit.sub_state === 'exited' ? 'Active' : 'Running' };
    if (['activating', 'deactivating', 'reloading'].includes(unit.active_state)) {
        return { tone: 'is-info', label: `${unit.active_state.charAt(0).toUpperCase()}${unit.active_state.slice(1)}…` };
    }
    return { tone: 'is-warn', label: 'Stopped' };
}

function _isRunning(item) {
    return item.kind === 'docker' ? item.status === 'running' : item.active_state === 'active';
}

function _isFailed(item) {
    if (item.kind === 'docker') {
        const match = /^Exited \((\d+)\)/.exec(item.state || '');
        return item.status === 'exited' && Boolean(match) && Number(match[1]) !== 0;
    }
    return item.active_state === 'failed';
}

function _sortItems(items) {
    const rank = (item) => (_isRunning(item) ? 0 : _isFailed(item) ? 1 : 2);
    return [...items].sort((a, b) => (rank(a) - rank(b)) || a.name.localeCompare(b.name));
}

function _visibleItems() {
    const query = state.query.trim().toLowerCase();
    let items;
    if (state.tab === 'docker') items = state.containers.map((item) => ({ kind: 'docker', ...item }));
    else if (state.tab === 'systemd') items = state.units.map((item) => ({ kind: 'systemd', ...item }));
    else items = [
        ...state.containers.map((item) => ({ kind: 'docker', ...item })),
        ...state.units.map((item) => ({ kind: 'systemd', ...item })),
    ];
    if (query) {
        items = items.filter((item) => [item.name, item.image || '', item.description || '', item.ports || '', item.compose_project || '']
            .join(' ').toLowerCase().includes(query));
    }
    return _sortItems(items);
}

function _counts() {
    const all = [
        ...state.containers.map((item) => ({ kind: 'docker', ...item })),
        ...state.units.map((item) => ({ kind: 'systemd', ...item })),
    ];
    const running = all.filter(_isRunning).length;
    const failed = all.filter(_isFailed).length;
    return { running, failed, stopped: all.length - running - failed, total: all.length };
}

function _rowHtml(item) {
    const safe = escapeAttr(item.name);
    const running = _isRunning(item);
    let icon;
    let subLine;
    let badge;
    let detail;
    let actions;

    if (item.kind === 'docker') {
        icon = '🐳';
        subLine = item.image || '';
        badge = _dockerBadge(item);
        detail = item.ports && item.ports !== '—' ? escapeHtml(item.ports) : '<span class="ui-muted">no published ports</span>';
        actions = running
            ? `<button class="ui-btn small" data-service-action="stop" data-kind="docker" data-name="${safe}" data-testid="services-action-${safe}-stop">⏹ Stop</button>
               <button class="ui-btn small" data-service-action="restart" data-kind="docker" data-name="${safe}" data-testid="services-action-${safe}-restart">↻ Restart</button>`
            : `<button class="ui-btn small" data-service-action="start" data-kind="docker" data-name="${safe}" data-testid="services-action-${safe}-start">▶ Start</button>`;
    } else {
        icon = '⚙️';
        subLine = item.description || '';
        badge = _unitBadge(item);
        const enabled = item.enabled_state || '';
        detail = enabled === 'enabled'
            ? '<span class="ui-tag is-ok">enabled at boot</span>'
            : enabled ? `<span class="ui-tag">${escapeHtml(enabled)}</span>` : '<span class="ui-muted">—</span>';
        actions = running
            ? `<button class="ui-btn small" data-service-action="stop" data-kind="systemd" data-name="${safe}" data-testid="services-action-${safe}-stop">⏹ Stop</button>
               <button class="ui-btn small" data-service-action="restart" data-kind="systemd" data-name="${safe}" data-testid="services-action-${safe}-restart">↻ Restart</button>`
            : `<button class="ui-btn small" data-service-action="start" data-kind="systemd" data-name="${safe}" data-testid="services-action-${safe}-start">▶ Start</button>`;
        if (enabled === 'enabled' || enabled === 'disabled') {
            actions += enabled === 'enabled'
                ? `<button class="ui-btn small" data-service-action="disable" data-kind="systemd" data-name="${safe}" data-testid="services-action-${safe}-disable" title="Disable at boot">Disable</button>`
                : `<button class="ui-btn small" data-service-action="enable" data-kind="systemd" data-name="${safe}" data-testid="services-action-${safe}-enable" title="Enable at boot">Enable</button>`;
        }
    }

    return `<tr data-testid="services-row-${safe}">
        <td>
            <div>${icon} <span class="path">${escapeHtml(item.name)}</span></div>
            ${subLine ? `<p class="ui-muted" title="${escapeAttr(subLine)}">${escapeHtml(subLine)}</p>` : ''}
        </td>
        <td data-testid="services-status-${safe}">
            <span class="ui-tag ${badge.tone}">${escapeHtml(badge.label)}</span>
            ${item.kind === 'docker' && item.state ? `<p class="ui-muted">${escapeHtml(item.state)}</p>` : ''}
        </td>
        <td>${detail}</td>
        <td class="actions"><div class="ui-row" style="justify-content:flex-end;">${actions}</div></td>
    </tr>`;
}

function _render() {
    if (!shell || !shell.root.isConnected) return;
    const counts = _counts();

    const chips = {
        running: shell.view.querySelector('#services-chip-running'),
        stopped: shell.view.querySelector('#services-chip-stopped'),
        failed: shell.view.querySelector('#services-chip-failed'),
    };
    if (chips.running) chips.running.textContent = `${counts.running} running`;
    if (chips.stopped) chips.stopped.textContent = `${counts.stopped} stopped`;
    if (chips.failed) {
        chips.failed.textContent = `${counts.failed} failed`;
        chips.failed.hidden = counts.failed === 0;
    }

    shell.view.querySelectorAll('[data-tab]').forEach((button) => {
        button.classList.toggle('primary', button.dataset.tab === state.tab);
    });

    const banners = shell.view.querySelector('#servicesBanners');
    if (banners) {
        const html = [];
        if (state.tab !== 'systemd' && state.dockerError) {
            html.push(`<div class="ui-callout error" data-testid="services-error-docker">Docker unavailable: ${escapeHtml(state.dockerError)}</div>`);
        }
        if (state.tab !== 'docker' && state.systemdError) {
            html.push(`<div class="ui-callout error" data-testid="services-error-systemd">Systemd unavailable: ${escapeHtml(state.systemdError)}</div>`);
        }
        banners.innerHTML = html.join('');
    }

    const table = shell.view.querySelector('#servicesTable');
    const items = _visibleItems();
    if (table) {
        table.innerHTML = `<tbody>${items.length
            ? items.map(_rowHtml).join('')
            : `<tr><td colspan="4">${emptyState({
                title: state.query ? 'No matching services' : 'No services found',
                hint: state.query ? `Nothing matches “${state.query}”.` : 'Docker and systemd returned no entries.',
            })}</td></tr>`}</tbody>`;
    }

    const footer = shell.view.querySelector('#servicesFooter');
    if (footer) {
        const when = state.lastUpdated ? state.lastUpdated.toLocaleTimeString() : '—';
        footer.textContent = `${counts.total} services · ${state.containers.length} containers + ${state.units.length} units · updated ${when}`;
    }
}

async function _load(silent = false) {
    if (state.loading || !shell) return;
    state.loading = true;
    const table = shell.view.querySelector('#servicesTable');
    if (!silent && table) table.innerHTML = `<tbody><tr><td colspan="4">${skeletonRows(8)}</td></tr></tbody>`;

    const [containers, units] = await Promise.allSettled([
        apiRequest('/api/containers'),
        apiRequest('/api/systemd'),
    ]);

    if (containers.status === 'fulfilled' && containers.value) {
        state.containers = Array.isArray(containers.value.containers) ? containers.value.containers : [];
        state.dockerError = null;
    } else {
        const error = containers.reason || {};
        state.dockerError = error.detail || error.message || 'request failed';
        logError('Services: Docker source unavailable', state.dockerError);
    }

    if (units.status === 'fulfilled' && units.value) {
        state.units = Array.isArray(units.value.services) ? units.value.services : [];
        state.systemdError = null;
    } else {
        const error = units.reason || {};
        state.systemdError = error.detail || error.message || 'request failed';
        logError('Services: systemd source unavailable', state.systemdError);
    }

    state.lastUpdated = new Date();
    state.loading = false;
    _render();
    shell.setStatus(state.dockerError && state.systemdError ? 'Both sources unavailable' : 'Service list updated',
        state.dockerError && state.systemdError ? 'error' : 'ok');
}

export async function renderServicesPage(body) {
    if (!body) return;
    if (_autoStop) { _autoStop(); _autoStop = null; }
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'services',
        title: 'Service Manager',
        subtitle: 'Docker containers and systemd units in one table.',
        icon: '⚙️',
        sections: [{ id: 'services', label: 'Services' }],
        rail: false,
        note: 'Restarting a service briefly interrupts its app.',
    });
    shell.view.setAttribute('data-testid', 'services-app');
    shell.view.innerHTML = `
        <div class="ui-toolbar">
            <div class="ui-row">
                <span id="services-chip-running" class="ui-tag is-ok" data-testid="services-chip-running">0 running</span>
                <span id="services-chip-stopped" class="ui-tag is-warn" data-testid="services-chip-stopped">0 stopped</span>
                <span id="services-chip-failed" class="ui-tag is-bad" data-testid="services-chip-failed" hidden>0 failed</span>
            </div>
            <div class="ui-row">
                ${['all', 'docker', 'systemd'].map((tab) => `<button class="ui-btn small${tab === state.tab ? ' primary' : ''}" data-tab="${tab}" data-testid="services-tab-${tab}">${tab === 'all' ? 'All' : tab === 'docker' ? 'Containers' : 'Systemd'}</button>`).join('')}
            </div>
        </div>
        <div class="ui-toolbar">
            <input id="servicesSearch" class="ui-input" data-testid="services-search" type="search" placeholder="Search by name, image or description…" aria-label="Search services">
            <button class="ui-btn small" data-testid="services-refresh-btn">${uiIcon('refresh', 12)} Refresh</button>
        </div>
        <div id="servicesBanners" data-testid="services-banners"></div>
        <section class="ui-panel" style="min-height:180px;">
            <div class="ui-table-wrap">
                <table class="ui-table" id="servicesTable" data-testid="services-table">
                    <thead><tr><th>Service</th><th>Status</th><th>Details</th><th class="actions">Actions</th></tr></thead>
                    <tbody><tr><td colspan="4">${skeletonRows(8)}</td></tr></tbody>
                </table>
            </div>
        </section>
        <p id="servicesFooter" class="ui-muted" data-testid="services-footer">Loading…</p>`;

    const search = shell.view.querySelector('#servicesSearch');
    search.value = state.query;
    search.addEventListener('input', () => {
        state.query = search.value || '';
        _render();
    });
    shell.view.querySelector('[data-testid="services-refresh-btn"]')?.addEventListener('click', () => _load(false));
    shell.view.querySelectorAll('[data-tab]').forEach((button) => {
        button.addEventListener('click', () => {
            state.tab = button.dataset.tab;
            _render();
        });
    });
    shell.view.querySelector('#servicesTable')?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-service-action]');
        if (!button) return;
        _action(button.dataset.kind, button.dataset.name, button.dataset.serviceAction);
    });

    shell.setStatus('Loading services…');
    await _load(false);

    if (_autoStop) _autoStop();
    _autoStop = poll(() => _load(true), AUTO_REFRESH_MS,
        { appId: 'services', immediate: false, name: 'services' });
}

async function _action(kind, name, action) {
    const labels = {
        start: 'Start', stop: 'Stop', restart: 'Restart',
        enable: 'Enable at boot', disable: 'Disable at boot',
    };
    const target = kind === 'docker' ? `container “${name}”` : `service “${name}”`;
    const ok = await showConfirm('Service Manager', `${labels[action] || action} ${target}?`);
    if (!ok) return;

    try {
        const url = kind === 'docker'
            ? `/api/containers/${encodeURIComponent(name)}/action`
            : `/api/systemd/${encodeURIComponent(name)}/action`;
        await apiRequest(url, { method: 'POST', body: { action } });
        notify({ kind: 'success', message: `${labels[action] || action}: ${name}` });
        await _load(true);
    } catch (err) {
        logError(`Services: ${action} ${name} failed`, err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || `Failed to ${action} ${name}` });
    }
}
