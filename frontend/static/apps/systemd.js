// EthOS NAS — Systemd Service Manager App
// Lists systemd services with start/stop/restart/enable/disable actions
// Plus a dedicated EthOS NAS service management card at the top.

import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiFetch, apiJsonFetch } from '../core/api.js';
import { skeletonRows, emptyState, errorState, wireStateActions } from '../core/states.js';

export async function renderSystemdPage(body) {
    if (!body) return;

    body.innerHTML = `
        <div style="padding:16px;height:100%;display:flex;flex-direction:column;background:var(--bg, #0f172a);font-family:system-ui,sans-serif;color: var(--text-1);">
            <!-- ═══ EthOS NAS Service Card ═══ -->
            <div id="ethosServiceCard" data-testid="ethos-service-card" style="margin-bottom:16px;padding:16px;background:rgba(30,41,59,0.8);border:1px solid var(--border);border-radius:12px;">
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
                    <h3 style="margin:0;font-size:16px;font-weight:700;color: var(--text-1);display:flex;align-items:center;gap:8px;">
                        🖥️ EthOS NAS Service
                    </h3>
                    <button class="btn-ghost" data-testid="ethos-refresh-btn" onclick="window._ethosRefresh()" style="padding:6px 14px;font-size:12px;border-radius:8px;border:1px solid var(--border);background:var(--surface-2);color: var(--text-2);cursor:pointer;display:flex;align-items:center;gap:6px;">
                        🔄 Refresh
                    </button>
                </div>
                <div id="ethosStatusContent" data-testid="ethos-status-content">
                    <div style="display:flex;align-items:center;justify-content:center;height:60px;color: var(--text-3);">
                        ${skeletonRows(8)}
                    </div>
                </div>
            </div>

            <!-- ═══ Services Table ═══ -->
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
                <h2 style="margin:0;font-size:18px;font-weight:700;color: var(--text-1);display:flex;align-items:center;gap:8px;">
                    <span>⚙️</span> System Services
                </h2>
                <button class="btn-ghost" data-testid="systemd-refresh-btn" onclick="window._systemdLoad()" style="padding:6px 14px;font-size:12px;border-radius:8px;border:1px solid var(--border);background:var(--surface-2);color: var(--text-2);cursor:pointer;display:flex;align-items:center;gap:6px;">
                    🔄 Refresh
                </button>
            </div>
            <div id="systemdContent" data-testid="systemd-content" style="flex:1;overflow-y:auto;">
                <div style="display:flex;align-items:center;justify-content:center;height:100px;color: var(--text-3);">
                    ${skeletonRows(8)}
                </div>
            </div>
            <div style="margin-top:8px;padding:8px 12px;background:rgba(30,41,59,0.6);border:1px solid var(--border);border-radius:8px;display:flex;gap:16px;font-size:11px;color: var(--text-3);">
                <span id="systemd-total-count" data-testid="systemd-total">Loading...</span>
                <span id="systemd-active-count" style="color:#22c55e;">-</span>
            </div>
        </div>`;

    // ── Load EthOS NAS service status in parallel with services list ──────────
    await Promise.all([window._ethosRefresh(), window._systemdLoad()]);
}

// ════════════════════════════════════════════
// APP: EthOS NAS Service Card
// ════════════════════════════════════════════

window._ethosRefresh = async function () {
    const el = document.getElementById('ethosStatusContent');
    if (!el) return;
    el.innerHTML = skeletonRows(8);

    try {
        const data = await apiJsonFetch('/api/ethos-nas-service/status');
        const isRunning = data.active_state === 'running';
        const portOpen = data.port_9000_open;
        const scopeLabel = data.scope === 'system' ? 'System' : 'User-level';

        let actionsHtml = '';
        if (isRunning) {
            actionsHtml = `
                <button class="btn-ghost" data-testid="ethos-action-restart" onclick="window._ethosAction('restart')" style="font-size:12px;padding:6px 14px;color:#eab308;border-radius:8px;">↻ Restart</button>
                <button class="btn-ghost" data-testid="ethos-action-stop" onclick="window._ethosAction('stop')" style="font-size:12px;padding:6px 14px;color:#ef4444;border-radius:8px;">⏹ Stop</button>`;
        } else {
            actionsHtml = `
                <button class="btn-ghost" data-testid="ethos-action-start" onclick="window._ethosAction('start')" style="font-size:12px;padding:6px 14px;color:#22c55e;border-radius:8px;">▶ Start</button>`;
        }

        const enabledLabel = data.enabled_state === 'enabled' ? '🟢 Enabled (auto-start)' :
                             data.enabled_state === 'disabled' ? '🔴 Disabled' : '⚪ Unknown';

        el.innerHTML = `
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;flex-wrap:wrap;">
                <span class="${isRunning ? 'badge-green' : 'badge-red'}" data-testid="ethos-status-badge" style="padding:4px 12px;border-radius:6px;font-size:12px;font-weight:600;">
                    ${isRunning ? '● Running' : '○ Stopped'}
                </span>
                <span style="font-size:12px;color: var(--text-2);">${scopeLabel}</span>
                <span style="font-size:12px;color:${portOpen ? '#22c55e' : '#64748b'};">Port 9000: ${portOpen ? '● Open' : '○ Closed'}</span>
            </div>
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;font-size:12px;color: var(--text-2);">
                <span>${enabledLabel}</span>
                ${data.user_service_file_exists ? '<span style="color: var(--text-3);">📄 ~/.config/systemd/user/ethos-nas.service</span>' : ''}
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
                ${actionsHtml}
                <button class="btn-ghost" data-testid="ethos-action-toggle-enable" onclick="window._ethosToggleEnable()" style="font-size:12px;padding:6px 14px;color:#3b82f6;border-radius:8px;">
                    🔛 ${isRunning ? (data.enabled_state === 'enabled' ? 'Disable Boot Start' : 'Enable Boot Start') : ''}
                </button>
            </div>`;
    } catch (e) {
        el.innerHTML = `<div style="text-align:center;padding:20px;color:#ef4444;font-size:13px;">❌ ${escapeHtml(e.message)}</div>`;
    }
};

window._ethosAction = async function (action) {
    if (!await window.showModalConfirm('EthOS NAS Service', `${action.charAt(0).toUpperCase() + action.slice(1)} the EthOS NAS service?`)) return;
    try {
        const resp = await apiFetch('/api/ethos-nas-service/action', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action}),
        });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            window.addNotification?.(d.detail || `Failed to ${action}`, 'error');
            return;
        }
        window.addNotification?.(`EthOS NAS service ${action} — success`, 'success');
        window._ethosRefresh();
    } catch (e) {
        window.addNotification?.(e.message, 'error');
    }
};

window._ethosToggleEnable = async function () {
    if (!await window.showModalConfirm('EthOS NAS Service', 'Toggle auto-start on boot?')) return;
    try {
        const resp = await apiFetch('/api/ethos-nas-service/toggle-enable', { method: 'POST' });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            window.addNotification?.(d.detail || 'Failed to toggle enable', 'error');
            return;
        }
        window.addNotification?.(`Auto-start ${resp.ok ? 'enabled' : 'disabled'} for EthOS NAS`, 'success');
        window._ethosRefresh();
    } catch (e) {
        window.addNotification?.(e.message, 'error');
    }
};

// ════════════════════════════════════════════
// APP: Systemd Services Table
// ════════════════════════════════════════════

window._systemdLoad = async function () {
    const content = document.getElementById('systemdContent');
    if (!content) return;
    content.innerHTML = skeletonRows(8);

    try {
        const resp = await apiFetch('/api/systemd');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();

        // Update counts
        const totalEl = document.getElementById('systemd-total-count');
        const activeEl = document.getElementById('systemd-active-count');
        if (totalEl) totalEl.textContent = `📊 ${data.total} services (${data.loaded} loaded)`;
        if (activeEl) activeEl.textContent = `🟢 ${data.active} active`;

        if (!data.services || data.services.length === 0) {
            content.innerHTML = emptyState({
                icon: 'settings',
                title: 'No services found',
                hint: 'Nothing matches the current filter. Clear the search or switch to the "All" tab.',
                action: { label: 'Show all', id: 'reset', icon: 'refresh' },
            });
            wireStateActions(content, { reset: () => { const el = document.getElementById('systemdSearch'); if (el) el.value = ''; window._systemdLoad?.(true); } });
            return;
        }

        let html = `<table class="app-table" data-testid="systemd-table" style="width:100%;font-size:12px;"><thead><tr>
            <th data-testid="systemd-col-name" style="text-align:left;padding:8px 10px;">Service</th>
            <th data-testid="systemd-col-description" style="text-align:left;padding:8px 10px;">Description</th>
            <th data-testid="systemd-col-status" style="text-align:center;padding:8px 10px;">Status</th>
            <th data-testid="systemd-col-actions" style="text-align:right;padding:8px 10px;">Actions</th>
        </tr></thead><tbody>`;

        for (const svc of data.services) {
            const safeName = escapeAttr(svc.name);
            const isActive = svc.active_state === 'active';
            const statusClass = isActive ? 'badge-green' : svc.active_state === 'failed' ? 'badge-red' : 'badge-yellow';
            const statusText = svc.active_state || svc.sub_state || 'unknown';

            html += `<tr class="fm-list-row" data-testid="systemd-row-${safeName}" style="border-bottom:1px solid var(--border);">
                <td style="padding:8px 10px;font-weight:600;color:white;font-family:monospace;font-size:11px;">
                    ${escapeHtml(svc.name)}
                </td>
                <td style="padding:8px 10px;color: var(--text-2);font-size:11px;max-width:250px;overflow:hidden;text-overflow:ellipsis;">
                    ${escapeHtml(svc.description || '—')}
                </td>
                <td style="padding:8px 10px;text-align:center;">
                    <span class="${statusClass}" data-testid="systemd-status-${safeName}" style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;">
                        ${escapeHtml(statusText)}
                    </span>
                </td>
                <td style="padding:8px 10px;text-align:right;white-space:nowrap;">
                    ${isActive ? `
                        <button class="btn-ghost" data-testid="systemd-action-${safeName}-stop" onclick="window._systemdAction('${safeName}','stop')" style="font-size:10px;padding:3px 8px;color:#ef4444;" title="Stop">⏹ Stop</button>
                        <button class="btn-ghost" data-testid="systemd-action-${safeName}-restart" onclick="window._systemdAction('${safeName}','restart')" style="font-size:10px;padding:3px 8px;color:#eab308;" title="Restart">↻ Restart</button>
                    ` : `
                        <button class="btn-ghost" data-testid="systemd-action-${safeName}-start" onclick="window._systemdAction('${safeName}','start')" style="font-size:10px;padding:3px 8px;color:#22c55e;" title="Start">▶ Start</button>
                    `}
                    <button class="btn-ghost" onclick="window._systemdAction('${safeName}','enable')" style="font-size:10px;padding:3px 8px;color:#3b82f6;" title="Enable">🔛</button>
                    <button class="btn-ghost" onclick="window._systemdAction('${safeName}','disable')" style="font-size:10px;padding:3px 8px;color: var(--text-3);" title="Disable">🔕</button>
                </td>
            </tr>`;
        }

        html += '</tbody></table>';
        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = errorState({ detail: e.message });
        wireStateActions(content, { retry: () => window._systemdLoad?.(true) });
    }
};

window._systemdAction = async function (name, action) {
    if (!await window.showModalConfirm('Systemd Action', `${action.charAt(0).toUpperCase() + action.slice(1)} "${name}"?`)) return;
    try {
        const resp = await apiFetch(`/api/systemd/${encodeURIComponent(name)}/action`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({action}),
        });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            window.addNotification?.(d.detail || `Failed to ${action} ${name}`, 'error');
            return;
        }
        window.addNotification?.(`${action} ${name} — success`, 'success');
        window._systemdLoad();
    } catch (e) {
        window.addNotification?.(e.message, 'error');
    }
};

