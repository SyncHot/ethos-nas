// EthOS NAS — Cron Job Manager App
// Lists, adds, edits, and deletes cron jobs via the user crontab

import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiFetch } from '../core/api.js';
import { skeletonRows, emptyState, errorState, wireStateActions } from '../core/states.js';

export async function renderCronPage(body) {
    if (!body) return;

    body.innerHTML = `
        <div style="padding:16px;height:100%;display:flex;flex-direction:column;background:var(--bg, #0f172a);font-family:system-ui,sans-serif;color: var(--text-1);">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
                <h2 style="margin:0;font-size:18px;font-weight:700;color: var(--text-1);display:flex;align-items:center;gap:8px;">
                    <span>⏰</span> Cron Jobs
                </h2>
                <div style="display:flex;gap:8px;">
                    <button class="btn-ghost" data-testid="cron-refresh-btn" onclick="window._cronLoad()" style="padding:6px 14px;font-size:12px;border-radius:8px;border:1px solid var(--border);background:var(--surface-2);color: var(--text-2);cursor:pointer;display:flex;align-items:center;gap:6px;">
                        🔄 Refresh
                    </button>
                    <button class="btn-ghost" data-testid="cron-add-btn" onclick="window._cronShowForm()" style="padding:6px 14px;font-size:12px;border-radius:8px;background:#2563eb;color:white;border:none;cursor:pointer;display:flex;align-items:center;gap:6px;">
                        + Add Job
                    </button>
                </div>
            </div>
            <div id="cronFormContainer" style="display:none;margin-bottom:12px;"></div>
            <div id="cronContent" data-testid="cron-content" style="flex:1;overflow-y:auto;">
                ${skeletonRows(5)}
            </div>
            <div style="margin-top:8px;padding:8px 12px;background:rgba(30,41,59,0.6);border:1px solid var(--border);border-radius:8px;display:flex;gap:16px;font-size:11px;color: var(--text-3);">
                <span id="cron-total-count" data-testid="cron-total">Loading...</span>
            </div>
        </div>`;

    // Expose load function globally
    window._cronLoad = async function () {
        const content = document.getElementById('cronContent');
        if (!content) return;
        content.innerHTML = skeletonRows(5);

        try {
            const resp = await apiFetch('/api/cron');
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const jobs = await resp.json();

            const totalEl = document.getElementById('cron-total-count');
            if (totalEl) totalEl.textContent = `📊 ${jobs.length} job(s) configured`;

            if (!jobs || jobs.length === 0) {
                content.innerHTML = emptyState({
                    icon: 'activity',
                    title: 'No cron jobs yet',
                    hint: 'Scheduled commands will run on the NAS and appear here with their schedule and last exit code.',
                    action: { label: 'Add job', id: 'create', icon: 'plus' },
                });
                wireStateActions(content, { create: () => window._cronShowForm?.() });
                return;
            }

            let html = `<table class="app-table" data-testid="cron-table" style="width:100%;font-size:12px;"><thead><tr>
                <th data-testid="cron-col-schedule" style="text-align:left;padding:8px 10px;">Schedule</th>
                <th data-testid="cron-col-command" style="text-align:left;padding:8px 10px;">Command</th>
                <th data-testid="cron-col-comment" style="text-align:left;padding:8px 10px;">Comment</th>
                <th data-testid="cron-col-status" style="text-align:center;padding:8px 10px;">Status</th>
                <th data-testid="cron-col-actions" style="text-align:right;padding:8px 10px;">Actions</th>
            </tr></thead><tbody>`;

            for (const job of jobs) {
                const safeId = escapeAttr(job.id);
                const isEnabled = job.enabled;
                const statusClass = isEnabled ? 'badge-green' : 'badge-yellow';
                const statusText = isEnabled ? 'Active' : 'Disabled';

                html += `<tr class="fm-list-row" data-testid="cron-row-${safeId}" style="border-bottom:1px solid var(--border);">
                    <td style="padding:8px 10px;font-weight:600;color:white;font-family:monospace;font-size:11px;">
                        ${escapeHtml(job.schedule)}
                    </td>
                    <td style="padding:8px 10px;color: var(--text-2);font-size:11px;max-width:250px;overflow:hidden;text-overflow:ellipsis;">
                        ${escapeHtml(job.command || '—')}
                    </td>
                    <td style="padding:8px 10px;color: var(--text-3);font-size:11px;">
                        ${escapeHtml(job.comment || '—')}
                    </td>
                    <td style="padding:8px 10px;text-align:center;">
                        <span class="${statusClass}" data-testid="cron-status-${safeId}" style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;">
                            ${escapeHtml(statusText)}
                        </span>
                    </td>
                    <td style="padding:8px 10px;text-align:right;white-space:nowrap;">
                        <button class="btn-ghost" data-testid="cron-action-${safeId}-toggle" onclick="window._cronToggle('${safeId}')" style="font-size:10px;padding:3px 8px;color:${isEnabled ? '#eab308' : '#22c55e'};" title="${isEnabled ? 'Disable' : 'Enable'}">
                            ${isEnabled ? '⏸' : '▶'}
                        </button>
                        <button class="btn-ghost" data-testid="cron-action-${safeId}-edit" onclick="window._cronShowForm('${safeId}')" style="font-size:10px;padding:3px 8px;color:#3b82f6;" title="Edit">✏️</button>
                        <button class="btn-ghost" data-testid="cron-action-${safeId}-delete" onclick="window._cronDelete('${safeId}')" style="font-size:10px;padding:3px 8px;color:#ef4444;" title="Delete">🗑</button>
                    </td>
                </tr>`;
            }

            html += '</tbody></table>';
            content.innerHTML = html;
        } catch (e) {
            content.innerHTML = errorState({ detail: e.message });
            wireStateActions(content, { retry: () => window._cronLoad?.() });
        }
    };

    // ── Show Add / Edit form ──

    window._cronShowForm = async function (editId) {
        const container = document.getElementById('cronFormContainer');
        if (!container) return;

        let schedule = '', command = '', comment = '', enabled = true;
        let isEdit = false;

        if (editId) {
            try {
                const resp = await apiFetch('/api/cron');
                if (resp.ok) {
                    const jobs = await resp.json();
                    const job = jobs.find(j => j.id === editId);
                    if (job) {
                        schedule = job.schedule;
                        command = job.command;
                        comment = job.comment || '';
                        enabled = job.enabled;
                        isEdit = true;
                    }
                }
            } catch (_) {}
        }

        container.style.display = 'block';
        container.innerHTML = `
            <div style="background:rgba(30,41,59,0.8);border:1px solid var(--border);border-radius:10px;padding:16px;">
                <div style="font-size:14px;font-weight:600;color: var(--text-1);margin-bottom:12px;">${isEdit ? '✏️ Edit Cron Job' : '➕ Add New Cron Job'}</div>
                <div style="display:grid;grid-template-columns:1fr 2fr;gap:12px;">
                    <div style="display:flex;flex-direction:column;gap:4px;">
                        <label style="font-size:11px;color: var(--text-2);">Schedule *</label>
                        <input id="cronFormSchedule" data-testid="cron-form-schedule" value="${escapeAttr(schedule)}" placeholder="0 2 * * *" style="padding:8px 12px;background:var(--surface-1);border:1px solid var(--border);border-radius:6px;color:var(--text-1);font-family:monospace;font-size:12px;outline:none;">
                    </div>
                    <div style="display:flex;flex-direction:column;gap:4px;">
                        <label style="font-size:11px;color: var(--text-2);">Command *</label>
                        <input id="cronFormCommand" data-testid="cron-form-command" value="${escapeAttr(command)}" placeholder="/usr/bin/backup.sh" style="padding:8px 12px;background:var(--surface-1);border:1px solid var(--border);border-radius:6px;color:var(--text-1);font-family:monospace;font-size:12px;outline:none;">
                    </div>
                </div>
                <div style="display:flex;gap:12px;margin-top:12px;align-items:center;">
                    <div style="flex:1;display:flex;flex-direction:column;gap:4px;">
                        <label style="font-size:11px;color: var(--text-2);">Comment (optional)</label>
                        <input id="cronFormComment" data-testid="cron-form-comment" value="${escapeAttr(comment)}" placeholder="Daily backup" style="padding:8px 12px;background:var(--surface-1);border:1px solid var(--border);border-radius:6px;color:var(--text-1);font-size:12px;outline:none;">
                    </div>
                    <label style="display:flex;align-items:center;gap:6px;font-size:12px;color: var(--text-2);margin-top:16px;">
                        <input type="checkbox" id="cronFormEnabled" data-testid="cron-form-enabled" ${enabled ? 'checked' : ''} style="width:16px;height:16px;accent-color:#3b82f6;cursor:pointer;"> Enabled
                    </label>
                </div>
                <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
                    <button class="btn-ghost" data-testid="cron-form-cancel" onclick="document.getElementById('cronFormContainer').style.display='none'" style="padding:6px 14px;font-size:12px;border-radius:6px;border:1px solid var(--border);color: var(--text-2);cursor:pointer;">Cancel</button>
                    <button class="btn-ghost" data-testid="cron-form-submit" onclick="window._cronSave('${editId || ''}')" style="padding:6px 14px;font-size:12px;border-radius:6px;background:#2563eb;color:white;border:none;cursor:pointer;">${isEdit ? 'Update' : 'Add'} Job</button>
                </div>
            </div>`;
    };

    // ── Save cron job (create or update) ──

    window._cronSave = async function (editId) {
        const schedule = document.getElementById('cronFormSchedule')?.value?.trim();
        const command = document.getElementById('cronFormCommand')?.value?.trim();
        const comment = document.getElementById('cronFormComment')?.value?.trim() || '';
        const enabled = document.getElementById('cronFormEnabled')?.checked ?? true;

        if (!schedule) { window.addNotification?.('Schedule is required', 'error'); return; }
        if (!command) { window.addNotification?.('Command is required', 'error'); return; }

        try {
            let resp;
            if (editId) {
                resp = await apiFetch(`/api/cron/${encodeURIComponent(editId)}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({schedule, command, comment, enabled}),
                });
            } else {
                resp = await apiFetch('/api/cron', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({schedule, command, comment, enabled}),
                });
            }

            if (!resp.ok) {
                const d = await resp.json().catch(() => ({}));
                window.addNotification?.(d.detail || `HTTP ${resp.status}`, 'error');
                return;
            }

            window.addNotification?.(editId ? 'Cron job updated' : 'Cron job added', 'success');
            document.getElementById('cronFormContainer').style.display = 'none';
            window._cronLoad();
        } catch (e) {
            window.addNotification?.(e.message, 'error');
        }
    };

    // ── Toggle enable/disable ──

    window._cronToggle = async function (jobId) {
        try {
            const resp = await apiFetch('/api/cron');
            if (!resp.ok) return;
            const jobs = await resp.json();
            const job = jobs.find(j => j.id === jobId);
            if (!job) return;

            const resp2 = await apiFetch(`/api/cron/${encodeURIComponent(jobId)}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: !job.enabled}),
            });

            if (!resp2.ok) {
                const d = await resp2.json().catch(() => ({}));
                window.addNotification?.(d.detail || 'Failed to toggle', 'error');
                return;
            }

            window.addNotification?.(`Cron job ${job.enabled ? 'disabled' : 'enabled'}`, 'success');
            window._cronLoad();
        } catch (e) {
            window.addNotification?.(e.message, 'error');
        }
    };

    // ── Delete cron job ──

    window._cronDelete = async function (jobId) {
        if (!await window.showModalConfirm('Delete Job', 'Delete this cron job?')) return;
        try {
            const resp = await apiFetch(`/api/cron/${encodeURIComponent(jobId)}`, {
                method: 'DELETE',
            });
            if (!resp.ok) {
                const d = await resp.json().catch(() => ({}));
                window.addNotification?.(d.detail || 'Failed to delete', 'error');
                return;
            }
            window.addNotification?.('Cron job deleted', 'success');
            window._cronLoad();
        } catch (e) {
            window.addNotification?.(e.message, 'error');
        }
    };

    await window._cronLoad();
}

