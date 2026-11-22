// EthOS NAS — System Services (single-pane shell)
// Dedicated EthOS NAS service panel + the full systemd unit table.
import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiRequest } from '../core/api.js';
import { skeletonRows, emptyState, errorState, wireStateActions } from '../core/states.js';
import { showConfirm } from '../core/modal.js';
import { notify } from '../core/notification-center.js';
import { uiIcon } from '../core/icons.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';

let shell = null;

export async function renderSystemdPage(body) {
    if (!body) return;
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'systemd',
        title: 'System Services',
        subtitle: 'The EthOS NAS service and every systemd unit.',
        icon: '⚙️',
        sections: [{ id: 'systemd', label: 'Services' }],
        rail: false,
        note: 'Stopping the EthOS service ends the web UI.',
    });
    shell.view.innerHTML = `
        <section class="ui-panel" data-testid="ethos-service-card">
            <div class="ui-toolbar">
                <div>
                    <h2>EthOS NAS Service</h2>
                    <p class="ui-muted">State, port and boot behaviour.</p>
                </div>
                <button class="ui-btn small" data-testid="ethos-refresh-btn">${uiIcon('refresh', 12)} Refresh</button>
            </div>
            <div class="ui-panel-body" id="ethosStatusContent" data-testid="ethos-status-content">${skeletonRows(3)}</div>
        </section>

        <div class="ui-toolbar">
            <div class="ui-row">
                <span class="ui-tag" id="systemd-total-count" data-testid="systemd-total">Loading…</span>
                <span class="ui-tag is-ok" id="systemd-active-count">—</span>
            </div>
            <button class="ui-btn small" data-testid="systemd-refresh-btn">${uiIcon('refresh', 12)} Refresh</button>
        </div>
        <section class="ui-panel" style="min-height:180px;">
            <div class="ui-table-wrap">
                <table class="ui-table" id="systemdTable" data-testid="systemd-table">
                    <thead><tr><th data-testid="systemd-col-name">Service</th><th data-testid="systemd-col-description">Description</th>
                        <th data-testid="systemd-col-status">Status</th><th class="actions" data-testid="systemd-col-actions">Actions</th></tr></thead>
                    <tbody id="systemdContent" data-testid="systemd-content"><tr><td colspan="4">${skeletonRows(8)}</td></tr></tbody>
                </table>
            </div>
        </section>`;

    shell.view.querySelector('[data-testid="ethos-refresh-btn"]')?.addEventListener('click', () => loadEthosService());
    shell.view.querySelector('[data-testid="systemd-refresh-btn"]')?.addEventListener('click', () => loadUnits());
    shell.view.querySelector('#systemdTable')?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-unit-action]');
        if (!button) return;
        unitAction(button.dataset.name, button.dataset.unitAction);
    });
    shell.view.querySelector('#ethosStatusContent')?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-ethos-action]');
        if (!button) return;
        ethosAction(button.dataset.ethosAction);
    });

    await Promise.all([loadEthosService(), loadUnits()]);
}

async function loadEthosService() {
    if (!shell) return;
    const content = shell.view.querySelector('#ethosStatusContent');
    if (!content) return;
    content.innerHTML = skeletonRows(3);
    shell.setStatus('Loading EthOS service…');

    try {
        const data = await apiRequest('/api/ethos-nas-service/status');
        if (!shell.root.isConnected) return;
        const running = data.active_state === 'running';
        const portOpen = data.port_9000_open;
        const scope = data.scope === 'system' ? 'System' : 'User-level';

        content.innerHTML = `
            <div class="ui-row">
                <span class="ui-tag ${running ? 'is-ok' : 'is-bad'}" data-testid="ethos-status-badge">${running ? 'Running' : 'Stopped'}</span>
                <span class="ui-tag">${escapeHtml(scope)}</span>
                <span class="ui-tag ${portOpen ? 'is-ok' : 'is-warn'}">port 9000 ${portOpen ? 'open' : 'closed'}</span>
                <span class="ui-tag ${data.enabled_state === 'enabled' ? 'is-ok' : data.enabled_state === 'disabled' ? 'is-bad' : ''}">
                    boot: ${escapeHtml(data.enabled_state || 'unknown')}
                </span>
            </div>
            ${data.user_service_file_exists ? '<p class="ui-muted ui-panel-body">Unit file: ~/.config/systemd/user/ethos-nas.service</p>' : ''}
            <div class="ui-row ui-panel-body">
                ${running
                    ? `<button class="ui-btn small" data-ethos-action="restart" data-testid="ethos-action-restart">${uiIcon('refresh', 12)} Restart</button>
                       <button class="ui-btn small danger" data-ethos-action="stop" data-testid="ethos-action-stop">⏹ Stop</button>`
                    : `<button class="ui-btn small primary" data-ethos-action="start" data-testid="ethos-action-start">▶ Start</button>`}
                <button class="ui-btn small" data-ethos-action="toggle-enable" data-testid="ethos-action-toggle-enable">
                    ${data.enabled_state === 'enabled' ? 'Disable boot start' : 'Enable boot start'}
                </button>
            </div>`;
        shell.setStatus(running ? 'EthOS service running' : 'EthOS service stopped', running ? 'ok' : 'warn');
    } catch (err) {
        if (!shell.root.isConnected) return;
        content.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        wireStateActions(content, { retry: () => loadEthosService() });
        shell.setStatus('Could not read the EthOS service', 'error');
        logError('Systemd: EthOS service status unavailable', err?.detail || err?.message);
    }
}

async function ethosAction(action) {
    if (action === 'toggle-enable') {
        const ok = await showConfirm('EthOS NAS Service', 'Toggle auto-start on boot?');
        if (!ok) return;
        try {
            await apiRequest('/api/ethos-nas-service/toggle-enable', { method: 'POST' });
            notify({ kind: 'success', message: 'Auto-start preference updated' });
            await loadEthosService();
        } catch (err) {
            logError('Systemd: toggling auto-start failed', err?.detail || err?.message);
            notify({ kind: 'error', message: err?.detail || err?.message || 'Could not toggle auto-start' });
        }
        return;
    }
    const ok = await showConfirm('EthOS NAS Service',
        `${action.charAt(0).toUpperCase()}${action.slice(1)} the EthOS NAS service?`,
        { danger: action === 'stop' });
    if (!ok) return;
    try {
        await apiRequest('/api/ethos-nas-service/action', { method: 'POST', body: { action } });
        notify({ kind: 'success', message: `EthOS NAS service ${action} — success` });
        await loadEthosService();
    } catch (err) {
        logError(`Systemd: EthOS service ${action} failed`, err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || `Failed to ${action}` });
    }
}

async function loadUnits() {
    if (!shell) return;
    const content = shell.view.querySelector('#systemdContent');
    if (!content) return;
    content.innerHTML = `<tr><td colspan="4">${skeletonRows(8)}</td></tr>`;

    try {
        const data = await apiRequest('/api/systemd');
        if (!shell.root.isConnected) return;
        const total = shell.view.querySelector('#systemd-total-count');
        const active = shell.view.querySelector('#systemd-active-count');
        if (total) total.textContent = `${data.total} services (${data.loaded} loaded)`;
        if (active) active.textContent = `${data.active} active`;

        const services = data.services || [];
        if (!services.length) {
            content.innerHTML = `<tr><td colspan="4">${emptyState({ icon: 'settings', title: 'No services found', hint: 'systemd returned no units.' })}</td></tr>`;
            shell.setStatus('No units reported', 'warn');
            return;
        }

        content.innerHTML = services.map((service) => {
            const safe = escapeAttr(service.name);
            const running = service.active_state === 'active';
            const tone = running ? 'is-ok' : service.active_state === 'failed' ? 'is-bad' : 'is-warn';
            return `<tr data-testid="systemd-row-${safe}">
                <td class="path">${escapeHtml(service.name)}</td>
                <td>${escapeHtml(service.description || '—')}</td>
                <td data-testid="systemd-status-${safe}"><span class="ui-tag ${tone}">${escapeHtml(service.active_state || service.sub_state || 'unknown')}</span></td>
                <td class="actions"><div class="ui-row" style="justify-content:flex-end;">
                    ${service.protected
                        ? '<span class="ui-tag is-info">Protected</span>'
                        : `${running
                            ? `<button class="ui-btn small" data-unit-action="stop" data-name="${safe}" data-testid="systemd-action-${safe}-stop">⏹ Stop</button>
                               <button class="ui-btn small" data-unit-action="restart" data-name="${safe}" data-testid="systemd-action-${safe}-restart">↻ Restart</button>`
                            : `<button class="ui-btn small primary" data-unit-action="start" data-name="${safe}" data-testid="systemd-action-${safe}-start">▶ Start</button>`}
                        <button class="ui-btn small" data-unit-action="enable" data-name="${safe}" title="Enable at boot">Enable</button>
                        <button class="ui-btn small" data-unit-action="disable" data-name="${safe}" title="Disable at boot">Disable</button>`}
                </div></td>
            </tr>`;
        }).join('');

        shell.setStatus(`${services.length} unit(s) listed`, 'ok');
    } catch (err) {
        if (!shell.root.isConnected) return;
        content.innerHTML = `<tr><td colspan="4">${errorState({ detail: err?.detail || err?.message || 'Unknown error' })}</td></tr>`;
        wireStateActions(content, { retry: () => loadUnits() });
        shell.setStatus('Could not read systemd units', 'error');
        logError('Systemd: unit list unavailable', err?.detail || err?.message);
    }
}

async function unitAction(name, action) {
    const ok = await showConfirm('Systemd action',
        `${action.charAt(0).toUpperCase()}${action.slice(1)} “${name}”?`,
        { danger: action === 'stop' });
    if (!ok) return;
    try {
        await apiRequest(`/api/systemd/${encodeURIComponent(name)}/action`, { method: 'POST', body: { action } });
        notify({ kind: 'success', message: `${action} ${name} — success` });
        await loadUnits();
    } catch (err) {
        logError(`Systemd: ${action} ${name} failed`, err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || `Failed to ${action} ${name}` });
    }
}
