// EthOS NAS — Cron Jobs (single-pane shell)
import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiRequest } from '../core/api.js';
import { skeletonRows, emptyState, errorState, wireStateActions } from '../core/states.js';
import { showConfirm, showFormModal } from '../core/modal.js';
import { notify } from '../core/notification-center.js';
import { uiIcon } from '../core/icons.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';

let shell = null;
let jobs = [];

export async function renderCronPage(body) {
    if (!body) return;
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'cron',
        title: 'Cron Jobs',
        subtitle: 'Scheduled commands from the user crontab.',
        icon: '⏰',
        sections: [{ id: 'cron', label: 'Jobs' }],
        rail: false,
        note: 'Jobs run with the EthOS service user’s permissions.',
    });
    shell.view.innerHTML = `
        <div class="ui-toolbar">
            <span class="ui-tag" id="cron-total-count" data-testid="cron-total">Loading…</span>
            <div class="ui-row">
                <button class="ui-btn small" data-testid="cron-refresh-btn">${uiIcon('refresh', 12)} Refresh</button>
                <button class="ui-btn small primary" data-testid="cron-add-btn">${uiIcon('plus', 12)} Add job</button>
            </div>
        </div>
        <section class="ui-panel" style="min-height:180px;">
            <div class="ui-table-wrap">
                <table class="ui-table" data-testid="cron-table">
                    <thead><tr><th data-testid="cron-col-schedule">Schedule</th><th data-testid="cron-col-command">Command</th>
                        <th data-testid="cron-col-comment">Comment</th><th data-testid="cron-col-status">Status</th>
                        <th class="actions" data-testid="cron-col-actions">Actions</th></tr></thead>
                    <tbody id="cronContent" data-testid="cron-content"><tr><td colspan="5">${skeletonRows(5)}</td></tr></tbody>
                </table>
            </div>
        </section>`;

    shell.view.querySelector('[data-testid="cron-refresh-btn"]')?.addEventListener('click', () => loadJobs());
    shell.view.querySelector('[data-testid="cron-add-btn"]')?.addEventListener('click', () => openJobForm());
    shell.view.querySelector('#cronContent')?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-cron-action]');
        if (!button) return;
        const { cronId, cronAction } = button.dataset;
        if (cronAction === 'toggle') toggleJob(cronId);
        else if (cronAction === 'edit') openJobForm(cronId);
        else if (cronAction === 'delete') deleteJob(cronId);
    });

    await loadJobs();
}

async function loadJobs() {
    if (!shell) return;
    const content = shell.view.querySelector('#cronContent');
    if (!content) return;
    content.innerHTML = `<tr><td colspan="5">${skeletonRows(5)}</td></tr>`;

    try {
        jobs = await apiRequest('/api/cron');
        if (!shell.root.isConnected) return;
        const total = shell.view.querySelector('#cron-total-count');
        if (total) total.textContent = `${jobs.length} job(s) configured`;

        if (!jobs.length) {
            content.innerHTML = `<tr><td colspan="5">${emptyState({
                icon: 'activity',
                title: 'No cron jobs yet',
                hint: 'Scheduled commands appear here with their schedule and state.',
                action: { label: 'Add job', id: 'create', icon: 'plus' },
            })}</td></tr>`;
            wireStateActions(content, { create: () => openJobForm() });
            shell.setStatus('No jobs configured', 'warn');
            return;
        }

        content.innerHTML = jobs.map((job) => {
            const safe = escapeAttr(job.id);
            const enabled = Boolean(job.enabled);
            return `<tr data-testid="cron-row-${safe}">
                <td class="path">${escapeHtml(job.schedule)}</td>
                <td>${escapeHtml(job.command || '—')}</td>
                <td class="ui-muted">${escapeHtml(job.comment || '—')}</td>
                <td><span class="ui-tag ${enabled ? 'is-ok' : 'is-warn'}" data-testid="cron-status-${safe}">${enabled ? 'Active' : 'Disabled'}</span></td>
                <td class="actions"><div class="ui-row" style="justify-content:flex-end;">
                    <button class="ui-btn small" data-cron-action="toggle" data-cron-id="${safe}" data-testid="cron-action-${safe}-toggle">${enabled ? '⏸ Disable' : '▶ Enable'}</button>
                    <button class="ui-btn small" data-cron-action="edit" data-cron-id="${safe}" data-testid="cron-action-${safe}-edit">Edit</button>
                    <button class="ui-btn small danger" data-cron-action="delete" data-cron-id="${safe}" data-testid="cron-action-${safe}-delete">Delete</button>
                </div></td>
            </tr>`;
        }).join('');

        shell.setStatus(`${jobs.length} job(s) loaded`, 'ok');
    } catch (err) {
        if (!shell.root.isConnected) return;
        content.innerHTML = `<tr><td colspan="5">${errorState({ detail: err?.detail || err?.message || 'Unknown error' })}</td></tr>`;
        wireStateActions(content, { retry: () => loadJobs() });
        shell.setStatus('Could not read cron jobs', 'error');
        logError('Cron: job list unavailable', err?.detail || err?.message);
    }
}

async function openJobForm(editId) {
    const existing = editId ? jobs.find((job) => job.id === editId) : null;
    const values = await showFormModal({
        title: existing ? `Edit cron job` : 'Add cron job',
        confirmLabel: existing ? 'Update job' : 'Add job',
        bodyHtml: `
            <div class="ui-fields">
                <div class="ui-field"><label for="cronFormSchedule">Schedule *</label>
                    <input id="cronFormSchedule" name="schedule" class="ui-input mono" data-testid="cron-form-schedule"
                           placeholder="0 2 * * *" value="${escapeAttr(existing?.schedule || '')}"></div>
                <div class="ui-field"><label for="cronFormCommand">Command *</label>
                    <input id="cronFormCommand" name="command" class="ui-input mono" data-testid="cron-form-command"
                           placeholder="/usr/bin/backup.sh" value="${escapeAttr(existing?.command || '')}"></div>
                <div class="ui-field full"><label for="cronFormComment">Comment (optional)</label>
                    <input id="cronFormComment" name="comment" class="ui-input" data-testid="cron-form-comment"
                           placeholder="Daily backup" value="${escapeAttr(existing?.comment || '')}"></div>
                <div class="ui-field full"><label class="ui-check">
                    <input type="checkbox" name="enabled" data-testid="cron-form-enabled" ${existing ? (existing.enabled ? 'checked' : '') : 'checked'}> Enabled
                </label></div>
            </div>`,
    });
    if (!values) return;

    const schedule = String(values.schedule || '').trim();
    const command = String(values.command || '').trim();
    if (!schedule) { notify({ kind: 'error', message: 'Schedule is required' }); return; }
    if (!command) { notify({ kind: 'error', message: 'Command is required' }); return; }
    const body = {
        schedule,
        command,
        comment: String(values.comment || '').trim(),
        enabled: values.enabled === true,
    };

    try {
        if (existing) await apiRequest(`/api/cron/${encodeURIComponent(existing.id)}`, { method: 'PUT', body });
        else await apiRequest('/api/cron', { method: 'POST', body });
        notify({ kind: 'success', message: existing ? 'Cron job updated' : 'Cron job added' });
        await loadJobs();
    } catch (err) {
        logError('Cron: save failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Could not save the job' });
    }
}

async function toggleJob(jobId) {
    const job = jobs.find((item) => item.id === jobId);
    if (!job) return;
    try {
        await apiRequest(`/api/cron/${encodeURIComponent(jobId)}`, {
            method: 'PUT',
            body: { enabled: !job.enabled },
        });
        notify({ kind: 'success', message: `Cron job ${job.enabled ? 'disabled' : 'enabled'}` });
        await loadJobs();
    } catch (err) {
        logError('Cron: toggle failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Could not toggle the job' });
    }
}

async function deleteJob(jobId) {
    const ok = await showConfirm('Delete cron job', 'Delete this cron job?', { danger: true });
    if (!ok) return;
    try {
        await apiRequest(`/api/cron/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
        notify({ kind: 'success', message: 'Cron job deleted' });
        await loadJobs();
    } catch (err) {
        logError('Cron: delete failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Could not delete the job' });
    }
}
