// EthOS NAS — Settings window
//
// Rail shell: Health · Updates · Maintenance · Users · Security · Appearance ·
// About (docs/app-ui.md). Every section loads lazily into the shared
// `core/ui-shell.js` view; no inline styles, no window globals, no raw fetch.
import { escapeAttr, escapeHtml, formatBytes } from '../core/utils.js';
import { uiIcon } from '../core/icons.js';
import { apiRequest } from '../core/api.js';
import { notify } from '../core/notification-center.js';
import { showAlert, showConfirm } from '../core/modal.js';
import { showRestartOverlay } from '../core/window-manager.js';
import { startTask } from '../core/progress.js';
import { skeletonRows, emptyState, errorState } from '../core/states.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';
import { openLogs } from './logs.js';
import {
    WALLPAPER_IDS, CUSTOM_ID, DEFAULT_WALLPAPER,
    saveWallpaper, uploadCustomWallpaper, resetWallpaper, customImageUrl,
} from '../core/wallpaper.js';

const SECTION_KEY = 'ethos-settings-section';

const SECTIONS = [
    { id: 'overview', label: 'Health', title: 'System Health', subtitle: 'Health checks and system alerts at a glance.' },
    { id: 'updates', label: 'Updates', title: 'Updates', subtitle: 'Core releases and the Android apps.' },
    { id: 'maintenance', label: 'Maintenance', title: 'Maintenance', subtitle: 'Caches, health thresholds and the danger zone.' },
    { id: 'users', label: 'Users', title: 'User Accounts', subtitle: 'Accounts imported from Linux through PAM.' },
    { id: 'security', label: 'Security', title: 'Security', subtitle: 'Your session, API key and hardening notes.' },
    { id: 'appearance', label: 'Appearance', title: 'Appearance', subtitle: 'Desktop wallpaper for this account.' },
    { id: 'about', label: 'About', title: 'About', subtitle: 'Build identity and environment self-check.' },
];

// Android APK downloads: the server resolves the file and exposes an
// app-scoped URL (`/api/app/download?app=…`), so no host path is baked in.
const APK_APPS = [
    { id: 'moments', label: 'EthOS Photos', icon: 'phone', linkTestId: 'apk-link-moments' },
    { id: 'music', label: 'EthOS Music', icon: 'music', linkTestId: 'apk-link-music' },
    { id: 'video', label: 'EthOS Video', icon: 'video', linkTestId: 'apk-link-video' },
];

const CACHE_LABELS = {
    temp_chunks: 'Abandoned uploads',
    transcodes: 'Playback transcodes',
    trash: 'Recycle bin',
    thumbnails: 'Image thumbnails',
    video_thumbs: 'Video thumbnails',
};

let shell = null;
let navToken = 0;

// ════════════════════════════════════════════════════════════════════════════
// Shell + section routing
// ════════════════════════════════════════════════════════════════════════════

export async function renderSettingsPage(body) {
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'settings',
        title: 'Settings',
        subtitle: SECTIONS[0].subtitle,
        icon: '🔧',
        sections: SECTIONS,
        note: 'EthOS core configuration. Changes apply immediately.',
    });
    shell.setPill('starting', 'Loading…');
    shell.onSection((id) => { renderSection(id); });
    const remembered = _rememberedSection();
    shell.setActive(SECTIONS.some((section) => section.id === remembered) ? remembered : 'overview');
}

async function renderSection(id) {
    const section = SECTIONS.find((item) => item.id === id);
    if (!shell || !section) return;
    _rememberSection(id);

    const token = ++navToken;
    const isCurrent = () => token === navToken && shell && shell.root.isConnected;

    shell.view.innerHTML = skeletonRows(5);
    shell.setStatus('Loading…');
    shell.setPill('starting', 'Loading…');

    try {
        await RENDERERS[id](isCurrent);
        if (!isCurrent()) return;
        shell.setPill('ready', 'Ready');
    } catch (err) {
        if (!isCurrent()) return;
        const detail = err?.detail || err?.message || 'Unknown error';
        shell.view.innerHTML = errorState({ detail });
        shell.setStatus('Could not load this section', 'error');
        shell.setPill('offline', 'Error');
        logError(`Settings: could not load ${id}`, detail);
    }
}

function _rememberSection(id) {
    try { sessionStorage.setItem(SECTION_KEY, id); } catch (err) {
        logError('Settings: could not persist the active section', err?.message || String(err));
    }
}

function _rememberedSection() {
    try { return sessionStorage.getItem(SECTION_KEY); } catch (err) {
        logError('Settings: could not read the active section', err?.message || String(err));
        return null;
    }
}

// ════════════════════════════════════════════════════════════════════════════
// Overview
// ════════════════════════════════════════════════════════════════════════════

async function renderHealth(isCurrent) {
    const health = await apiRequest('/api/health/status').catch((err) => {
        logError('Settings: health status unavailable', err?.detail || err?.message);
        return null;
    });
    if (!isCurrent()) return;

    shell.view.innerHTML = `
        <section class="ui-panel" data-testid="settings-health-panel">
            <div class="ui-toolbar">
                <div>
                    <h2>${uiIcon('activity', 13)} Health monitor</h2>
                    <p class="ui-muted">${_healthSummary(health)}</p>
                </div>
                <button class="ui-btn small" data-action="health-run">${uiIcon('play', 12)} Run check now</button>
            </div>
            <div class="ui-panel-body ui-list">${_healthRows(health)}</div>
        </section>`;

    shell.view.querySelector('[data-action="health-run"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const result = await apiRequest('/api/health/check-now', { method: 'POST' });
            const count = (result?.emitted || []).length;
            notify({
                kind: count ? 'warning' : 'success',
                title: 'Health check',
                message: count ? `${count} new alert(s)` : 'All checks passed',
            });
            renderSection('overview');
        } catch (err) {
            logError('Settings: manual health check failed', err?.detail || err?.message);
            notify({ kind: 'error', title: 'Health check failed', message: err?.detail || err?.message || 'Unknown error' });
            button.disabled = false;
        }
    });

    shell.setStatus(`Updated ${_clock()}`, 'ok');
}

function _healthSummary(health) {
    if (!health) return 'Health monitor is not reporting.';
    const monitor = health.monitor || {};
    return `Checks every ${monitor.interval_seconds ?? '—'} s · last check ${_relTime(monitor.last_check_at)} · `
        + `${monitor.checks_run ?? 0} run(s)`;
}

function _healthRows(health) {
    if (!health) {
        return `<div class="ui-list-row"><span class="ui-tag is-warn">offline</span>`
            + `<span class="ui-list-row-main">No health data — the monitor did not answer.</span></div>`;
    }
    const monitor = health.monitor || {};
    const rows = [];

    for (const unit of health.failed_units || []) {
        rows.push(_healthRow('bad', 'unit', `${unit.name} — ${unit.description || 'failed'}`));
    }

    const hot = (health.disks || []).filter((item) => Number(item.percent) >= (monitor.disk_warn_percent ?? 90));
    for (const disk of hot) {
        rows.push(_healthRow('warn', 'disk',
            `${disk.mount} — ${Math.round(Number(disk.percent) || 0)}% used (${formatBytes(disk.free || 0)} free)`));
    }

    if (!rows.length) {
        rows.push(_healthRow('ok', 'all', 'All disks below the warning threshold · no failed services'));
    }

    const memory = health.memory;
    if (memory && memory.total > 0) {
        const warn = monitor.memory_warn_percent ?? 90;
        const tone = Number(memory.percent) >= warn ? 'bad' : 'ok';
        rows.push(_healthRow(tone, 'memory',
            `${Math.round(Number(memory.percent) || 0)}% used (${formatBytes(memory.used || 0)} of ${formatBytes(memory.total || 0)})`));
    }

    const temps = health.temps || [];
    if (temps.length) {
        const hottest = temps.reduce((a, b) => (b.current > a.current ? b : a));
        const warn = monitor.temp_warn_celsius ?? 85;
        rows.push(_healthRow(hottest.current >= warn ? 'bad' : 'ok', 'temp',
            `${hottest.label} — ${hottest.current}°C (hottest of ${temps.length} sensors)`));
    }

    const smart = health.smart;
    if (smart && Array.isArray(smart.results)) {
        for (const entry of smart.results) {
            if (!entry.ok) rows.push(_healthRow('warn', 'smart', `${entry.device} — SMART unavailable`));
            else if (entry.passed) rows.push(_healthRow('ok', 'smart', `${entry.device} — SMART OK${entry.model ? ` (${entry.model})` : ''}`));
            else rows.push(_healthRow('bad', 'smart', `${entry.device} — SMART FAILING`));
        }
    }

    return rows.join('');
}

function _healthRow(tone, kind, text) {
    return `<div class="ui-list-row"><span class="ui-tag is-${tone}">${escapeHtml(kind)}</span>`
        + `<span class="ui-list-row-main">${escapeHtml(text)}</span></div>`;
}

// ════════════════════════════════════════════════════════════════════════════
// Updates
// ════════════════════════════════════════════════════════════════════════════

async function renderUpdates(isCurrent) {
    const isAdmin = _isAdmin();
    shell.view.innerHTML = `
        <section class="ui-panel" data-testid="core-update-card">
            <div class="ui-toolbar">
                <div>
                    <h2>${uiIcon('download', 13)} EthOS NAS <span class="ui-tag" data-testid="core-version">…</span></h2>
                    <p class="ui-muted" data-testid="core-update-status">Loading update status…</p>
                </div>
                <div class="ui-row">
                    ${isAdmin
                        ? `<button class="ui-btn small" data-action="update-check" data-testid="core-update-check">${uiIcon('refresh', 12)} Check for updates</button>
                           <button class="ui-btn small primary" data-action="update-apply" data-testid="core-update-apply" hidden>${uiIcon('download', 12)} Update now</button>`
                        : '<span class="ui-tag">Admin required</span>'}
                </div>
            </div>
            <div class="ui-callout info" data-testid="core-update-notes" hidden></div>
            <p class="ui-muted" data-testid="core-update-result" hidden></p>
        </section>

        <section class="ui-panel">
            <div class="ui-toolbar">
                <div>
                    <h2>${uiIcon('phone', 13)} Android apps</h2>
                    <p class="ui-muted">APK versions come from the same endpoint the apps poll for self-updates.</p>
                </div>
                <span class="ui-tag is-info">APK</span>
            </div>
            <div class="ui-panel-body ui-table-wrap">
                <table class="ui-table">
                    <thead><tr><th>App</th><th>Version</th><th class="actions">Download</th></tr></thead>
                    <tbody id="settings-apk-rows"><tr><td colspan="3" class="ui-muted">Loading…</td></tr></tbody>
                </table>
            </div>
        </section>`;

    await Promise.all([_fillCoreUpdate(isCurrent), _fillApkRows(isCurrent)]);

    shell.view.querySelector('[data-action="update-check"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            await apiRequest('/api/system/update/check', { method: 'POST' });
            renderSection('updates');
        } catch (err) {
            logError('Settings: update check failed', err?.detail || err?.message);
            notify({ kind: 'error', title: 'Update check failed', message: err?.detail || err?.message || 'Unknown error' });
            button.disabled = false;
        }
    });
    shell.view.querySelector('[data-action="update-apply"]')?.addEventListener('click', () => { _applyUpdate(); });

    shell.setStatus('Release status loaded', 'ok');
}

async function _fillCoreUpdate(isCurrent) {
    const versionEl = shell.view.querySelector('[data-testid="core-version"]');
    const statusEl = shell.view.querySelector('[data-testid="core-update-status"]');
    const notesEl = shell.view.querySelector('[data-testid="core-update-notes"]');
    const resultEl = shell.view.querySelector('[data-testid="core-update-result"]');
    const applyBtn = shell.view.querySelector('[data-action="update-apply"]');
    if (!versionEl || !statusEl) return;

    const status = await apiRequest('/api/system/update/status').catch((err) => {
        logError('Settings: update status unavailable', err?.detail || err?.message);
        return null;
    });
    const data = await apiRequest('/api/system/update');
    if (!isCurrent()) return;

    const current = data.current || {};
    versionEl.textContent = `v${current.version || '?'}${current.git_sha ? ` · ${current.git_sha}` : ''}`;

    const latest = data.latest || null;
    const checked = data.checked_at_iso ? `last checked ${_fmtDate(data.checked_at_iso)}` : 'never checked';
    let tone = '';
    if (data.enabled === false) {
        statusEl.textContent = 'Update checks are disabled on this server';
    } else if (data.error) {
        statusEl.textContent = `Check failed — ${data.error} (${checked})`;
        tone = 'error';
    } else if (data.update_available === true && latest) {
        statusEl.textContent = `Update available: v${latest.version || '?'}${latest.git_sha ? ` · ${latest.git_sha}` : ''} — ${checked}`;
        tone = 'warn';
    } else if (data.update_available === false) {
        statusEl.textContent = `Up to date — ${checked}`;
    } else {
        statusEl.textContent = `Not checked yet — ${checked}`;
    }
    statusEl.className = `ui-muted${tone ? ` ${tone}` : ''}`;

    notesEl.textContent = (data.update_available === true && latest && latest.notes) ? latest.notes : '';
    notesEl.hidden = !notesEl.textContent;

    if (applyBtn) {
        applyBtn.hidden = !(data.enabled !== false && data.update_available === true);
    }

    const result = status?.result;
    if (result && result.status) {
        const when = result.at ? ` (${_fmtDate(result.at)})` : '';
        const labels = {
            ok: 'Last update succeeded',
            up_to_date: 'Already up to date',
            rolled_back: 'Last update failed — previous version restored',
            failed: 'Last update failed',
        };
        resultEl.textContent = `${labels[result.status] || `Last update: ${result.status}`}`
            + `${result.version ? ` → ${result.version}` : ''}${when}`
            + `${result.detail ? ` — ${result.detail}` : ''}`;
        resultEl.hidden = false;
    }
}

async function _fillApkRows(isCurrent) {
    const tbody = shell.view.querySelector('#settings-apk-rows');
    if (!tbody) return;

    const results = await Promise.all(APK_APPS.map(async (app) => {
        try {
            return { app, data: await apiRequest(`/api/app/version?app=${app.id}`) };
        } catch (err) {
            logError(`Settings: version for ${app.id} unavailable`, err?.detail || err?.message);
            return { app, data: null };
        }
    }));
    if (!isCurrent()) return;

    tbody.innerHTML = results.map(({ app, data }) => {
        const version = data
            ? `<span class="ui-tag is-ok">v${escapeHtml(data.version_name || '?')} · code ${escapeHtml(String(data.version_code ?? '?'))}</span>`
            : '<span class="ui-tag is-warn">No build info</span>';
        const link = data
            ? `<a class="ui-btn small" data-testid="${escapeAttr(app.linkTestId)}"
                    href="${escapeAttr(_withToken(data.url || `/api/app/download?app=${app.id}`))}">${uiIcon('download', 11)} Download</a>`
            : `<span class="ui-btn small" aria-disabled="true" data-testid="${escapeAttr(app.linkTestId)}">${uiIcon('download', 11)} Download</span>`;
        return `<tr>
            <td>${uiIcon(app.icon, 13)} ${escapeHtml(app.label)}</td>
            <td>${version}</td>
            <td class="actions">${link}</td>
        </tr>`;
    }).join('');
}

async function _applyUpdate() {
    const latest = (await apiRequest('/api/system/update').catch((err) => {
        logError('Settings: could not read the target version', err?.detail || err?.message);
        return {};
    })).latest || {};
    const target = `v${latest.version || '?'}${latest.git_sha ? ` · ${latest.git_sha}` : ''}`;
    const ok = await showConfirm(
        'Update EthOS NAS',
        `Install ${target} now?\n\nA data snapshot is taken first, the service restarts and this page reloads. `
        + 'If the new version does not come up, the previous version is restored automatically.',
    );
    if (!ok) return;

    try {
        await apiRequest('/api/system/update', { method: 'POST' });
    } catch (err) {
        logError('Settings: update could not start', err?.detail || err?.message);
        showAlert('Update failed to start', err?.detail || err?.message || 'Unknown error');
        return;
    }

    const task = startTask({
        appId: 'system',
        title: 'Updating EthOS NAS',
        detail: 'starting the helper…',
        percent: null,
        testIdPrefix: 'sys-update',
    });
    const stepPct = { fetch: 10, apply: 30, deps: 55, restart: 75, verify: 90, done: 100 };

    const tick = async () => {
        let state;
        try {
            state = await apiRequest('/api/system/update/status');
        } catch (err) {
            logError('Settings: update status lost during restart', err?.detail || err?.message);
            task.update({ percent: 95, detail: 'service restarting…' });
            showRestartOverlay();
            return;
        }
        const job = state.job || {};
        if (job.status === 'running') {
            task.update({ percent: stepPct[job.step] ?? 20, detail: `${job.step}${job.detail ? ` — ${job.detail}` : ''}` });
            setTimeout(tick, 2000);
            return;
        }
        if (job.status === 'ok' || job.status === 'up_to_date') {
            task.finish('done', { percent: 100, detail: job.detail || job.status });
            showRestartOverlay();
        } else if (job.status === 'rolled_back' || job.status === 'failed') {
            task.finish('failed', { percent: 100, detail: job.detail || job.status });
            showAlert('Update failed', job.detail || 'The previous version was restored.');
        } else {
            setTimeout(tick, 2000);
        }
    };
    setTimeout(tick, 1500);
}

// ════════════════════════════════════════════════════════════════════════════
// Maintenance
// ════════════════════════════════════════════════════════════════════════════

async function renderMaintenance(isCurrent) {
    const [usage, health] = await Promise.all([
        apiRequest('/api/system/storage-usage').catch((err) => {
            logError('Settings: cache usage unavailable', err?.detail || err?.message);
            return null;
        }),
        apiRequest('/api/health/status').catch((err) => {
            logError('Settings: health thresholds unavailable', err?.detail || err?.message);
            return null;
        }),
    ]);
    if (!isCurrent()) return;

    const isAdmin = _isAdmin();
    const monitor = health?.monitor || {};
    const areas = Object.entries(usage?.areas || {})
        .sort((a, b) => (Number(b[1]?.bytes) || 0) - (Number(a[1]?.bytes) || 0));

    const cacheRows = areas.length
        ? areas.map(([name, area]) => `<tr>
                <td>${escapeHtml(CACHE_LABELS[name] || name)}</td>
                <td class="num">${escapeHtml(formatBytes(area.bytes || 0))}</td>
                <td class="num">${Number(area.files) || 0}</td>
            </tr>`).join('')
        : `<tr><td colspan="3">${emptyState({ title: 'Nothing cached', hint: 'Caches and the recycle bin are empty.' })}</td></tr>`;

    shell.view.innerHTML = `
        <section class="ui-panel" data-testid="settings-caches-card">
            <div class="ui-toolbar">
                <div>
                    <h2>${uiIcon('disk', 13)} Caches &amp; recycle bin</h2>
                    <p class="ui-muted">${usage
                        ? `${escapeHtml(formatBytes(usage.total_bytes || 0))} in ${Number(usage.total_files) || 0} file(s) — swept automatically`
                        : 'Cache usage is unavailable.'}</p>
                </div>
                <div class="ui-row">
                    ${isAdmin
                        ? `<button class="ui-btn small" data-action="cache-sweep">${uiIcon('trash', 12)} Clean now</button>`
                        : '<span class="ui-tag">Admin required</span>'}
                </div>
            </div>
            <div class="ui-panel-body ui-table-wrap">
                <table class="ui-table">
                    <thead><tr><th>Area</th><th class="num">Size</th><th class="num">Files</th></tr></thead>
                    <tbody>${usage ? cacheRows : `<tr><td colspan="3">${emptyState({ title: 'Storage usage unavailable', hint: 'Check the Logs app for the backend error.' })}</td></tr>`}</tbody>
                </table>
            </div>
        </section>

        <section class="ui-panel" data-testid="health-config-row">
            <div class="ui-toolbar">
                <div>
                    <h2>${uiIcon('activity', 13)} Health thresholds</h2>
                    <p class="ui-muted">Warnings are raised and cleared at these values. Cooldown suppresses repeats.</p>
                </div>
            </div>
            <div class="ui-panel-body">
                <div class="ui-fields">
                    ${_numberField('healthCfgWarn', 'Disk warn %', monitor.disk_warn_percent, 1, 100)}
                    ${_numberField('healthCfgRecover', 'Disk recover %', monitor.disk_recover_percent, 0, 100)}
                    ${_numberField('healthCfgMemWarn', 'Memory warn %', monitor.memory_warn_percent, 1, 100)}
                    ${_numberField('healthCfgMemRecover', 'Memory recover %', monitor.memory_recover_percent, 0, 100)}
                    ${_numberField('healthCfgTempWarn', 'Temperature warn °C', monitor.temp_warn_celsius, 40, 125)}
                    ${_numberField('healthCfgCooldown', 'Alert cooldown (s)', monitor.alert_cooldown_seconds, 60, 604800, 60)}
                    ${_numberField('healthCfgInterval', 'Check interval (s)', monitor.interval_seconds, 30, 86400, 30)}
                </div>
                <div class="ui-form-actions">
                    <button class="ui-btn" data-action="health-reset">${uiIcon('refresh', 12)} Reset to defaults</button>
                    <button class="ui-btn primary" data-testid="health-cfg-save" data-action="health-save">${uiIcon('check', 12)} Save</button>
                </div>
            </div>
        </section>

        <section class="ui-panel">
            <div class="ui-toolbar">
                <div>
                    <h2>${uiIcon('alert', 13)} Danger zone</h2>
                    <p class="ui-muted">These actions interrupt everyone using the NAS. Settings stay saved.</p>
                </div>
                ${isAdmin ? '' : '<span class="ui-tag">Admin required</span>'}
            </div>
            <div class="ui-panel-body ui-row">
                <button class="ui-btn" data-action="restart" ${isAdmin ? '' : 'disabled'}>${uiIcon('refresh', 12)} Restart application</button>
                <button class="ui-btn danger" data-action="reboot" ${isAdmin ? '' : 'disabled'}>${uiIcon('refresh', 12)} Reboot server</button>
            </div>
        </section>`;

    shell.view.querySelector('[data-action="cache-sweep"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            const summary = await apiRequest('/api/system/retention/sweep', { method: 'POST' });
            notify({
                kind: 'success',
                title: 'Caches cleaned',
                message: `Freed ${formatBytes(summary?.freed_bytes || 0)} (${summary?.removed_files || 0} item(s))`,
            });
            renderSection('maintenance');
        } catch (err) {
            logError('Settings: cache sweep failed', err?.detail || err?.message);
            notify({ kind: 'error', title: 'Cleanup failed', message: err?.detail || err?.message || 'Unknown error' });
            button.disabled = false;
        }
    });

    shell.view.querySelector('[data-action="health-save"]')?.addEventListener('click', async (event) => {
        const body = _healthConfigBody();
        if (!Object.keys(body).length) {
            notify({ kind: 'info', message: 'Nothing to save' });
            return;
        }
        const button = event.currentTarget;
        button.disabled = true;
        try {
            await apiRequest('/api/health/config', { method: 'PUT', body });
            notify({ kind: 'success', title: 'Health monitor', message: 'Thresholds saved' });
            renderSection('maintenance');
        } catch (err) {
            logError('Settings: health thresholds save failed', err?.detail || err?.message);
            notify({ kind: 'error', title: 'Save failed', message: err?.detail || err?.message || 'Unknown error' });
            button.disabled = false;
        }
    });

    shell.view.querySelector('[data-action="health-reset"]')?.addEventListener('click', async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
            await apiRequest('/api/health/config', { method: 'DELETE' });
            notify({ kind: 'success', title: 'Health monitor', message: 'Reset to defaults' });
            renderSection('maintenance');
        } catch (err) {
            logError('Settings: health thresholds reset failed', err?.detail || err?.message);
            notify({ kind: 'error', title: 'Reset failed', message: err?.detail || err?.message || 'Unknown error' });
            button.disabled = false;
        }
    });

    shell.view.querySelector('[data-action="restart"]')?.addEventListener('click', async () => {
        const ok = await showConfirm('Restart EthOS NAS', 'Restart the web server? This page reconnects automatically.');
        if (!ok) return;
        try {
            await apiRequest('/api/system/restart', { method: 'POST' });
            showRestartOverlay();
        } catch (err) {
            logError('Settings: restart failed', err?.detail || err?.message);
            showAlert('Restart failed', err?.detail || err?.message || 'Could not reach the server');
        }
    });

    shell.view.querySelector('[data-action="reboot"]')?.addEventListener('click', async () => {
        const ok = await showConfirm('Reboot server', 'Reboot the entire server? This disconnects all users.', { danger: true });
        if (!ok) return;
        try {
            await apiRequest('/api/system/shutdown', { method: 'POST', body: { action: 'reboot', confirm: true } });
            notify({ kind: 'info', title: 'Reboot', message: 'The server is rebooting…' });
        } catch (err) {
            logError('Settings: reboot failed', err?.detail || err?.message);
            showAlert('Reboot failed', err?.detail || err?.message || 'Could not reach the server');
        }
    });

    shell.setStatus(`Caches are ${usage ? 'reported' : 'unavailable'} · thresholds ${health ? 'loaded' : 'unavailable'}`,
        usage && health ? 'ok' : 'warn');
}

function _numberField(id, label, value, min, max, step = 1) {
    const text = value == null ? '' : escapeAttr(String(value));
    return `<div class="ui-field">
        <label for="${escapeAttr(id)}">${escapeHtml(label)}</label>
        <input class="ui-input" id="${escapeAttr(id)}" type="number" min="${min}" max="${max}" step="${step}" value="${text}">
    </div>`;
}

function _healthConfigBody() {
    const read = (id, field) => {
        const input = document.getElementById(id);
        if (!input || input.value === '') return null;
        const value = Number(input.value);
        return Number.isFinite(value) ? { [field]: value } : null;
    };
    const body = {};
    for (const part of [
        read('healthCfgWarn', 'disk_warn_percent'),
        read('healthCfgRecover', 'disk_recover_percent'),
        read('healthCfgMemWarn', 'memory_warn_percent'),
        read('healthCfgMemRecover', 'memory_recover_percent'),
        read('healthCfgTempWarn', 'temp_warn_celsius'),
        read('healthCfgCooldown', 'alert_cooldown_seconds'),
        read('healthCfgInterval', 'check_interval_seconds'),
    ]) {
        if (part) Object.assign(body, part);
    }
    return body;
}

// ════════════════════════════════════════════════════════════════════════════
// Users
// ════════════════════════════════════════════════════════════════════════════

async function renderUsers(isCurrent) {
    const data = await apiRequest('/api/users');
    if (!isCurrent()) return;

    const users = data.users || [];
    const rows = users.length
        ? users.map((user) => {
            const roleTone = user.role === 'admin' ? 'is-warn' : 'is-info';
            return `<tr>
                <td>${uiIcon('users', 13)} ${escapeHtml(user.username || '—')}</td>
                <td><span class="ui-tag ${roleTone}">${escapeHtml(user.role || 'user')}</span></td>
                <td>${user.created_at ? escapeHtml(new Date(user.created_at).toLocaleDateString()) : '—'}</td>
                <td>${user.last_login ? escapeHtml(new Date(user.last_login).toLocaleString()) : '—'}</td>
            </tr>`;
        }).join('')
        : `<tr><td colspan="4">${emptyState({ title: 'No users configured', hint: 'Accounts appear after the first PAM login.' })}</td></tr>`;

    shell.view.innerHTML = `
        <section class="ui-panel">
            <div class="ui-toolbar">
                <div>
                    <h2>${uiIcon('users', 13)} Accounts</h2>
                    <p class="ui-muted">${users.length} user${users.length === 1 ? '' : 's'} registered</p>
                </div>
            </div>
            <div class="ui-panel-body ui-table-wrap">
                <table class="ui-table">
                    <thead><tr><th>Username</th><th>Role</th><th>Created</th><th>Last login</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </section>

        <section class="ui-panel">
            <h2>${uiIcon('info', 13)} How accounts work</h2>
            <p class="ui-muted ui-panel-body">Users are imported automatically from Linux system accounts through PAM.
                The first successful login registers the account with the <code class="ui-code">user</code> role;
                promote it to <code class="ui-code">admin</code> in the users store if it needs privileged actions.</p>
        </section>`;

    shell.setStatus(`${users.length} account(s)`, 'ok');
}

// ════════════════════════════════════════════════════════════════════════════
// Security
// ════════════════════════════════════════════════════════════════════════════

async function renderSecurity(isCurrent) {
    const session = _tokenInfo();
    shell.view.innerHTML = `
        <div class="ui-grid ui-grid-2">
            <section class="ui-panel">
                <h2>${uiIcon('lock', 13)} Session</h2>
                <div class="ui-panel-body">
                    ${_kv([
                        ['Authenticated', `<span class="ui-tag ${session.valid ? 'is-ok' : 'is-bad'}">${session.valid ? 'Yes' : 'No'}</span>`],
                        ['User', escapeHtml(session.username)],
                        ['Role', escapeHtml(session.role)],
                        ['Token expires', escapeHtml(session.expires)],
                    ])}
                </div>
            </section>

            <section class="ui-panel">
                <h2>${uiIcon('phone', 13)} API key</h2>
                <div class="ui-panel-body" data-testid="api-key-card">
                    <p class="ui-muted">Loading…</p>
                </div>
            </section>
        </div>

        <section class="ui-panel">
            <h2>${uiIcon('alert', 13)} Security notes</h2>
            <div class="ui-panel-body ui-list">
                <div class="ui-list-row"><span class="ui-tag is-warn">secret</span><span class="ui-list-row-main">Set <code class="ui-code">ETHOS_JWT_SECRET</code> to a strong random value in production.</span></div>
                <div class="ui-list-row"><span class="ui-tag is-info">cors</span><span class="ui-list-row-main">Restrict origins with <code class="ui-code">ETHOS_CORS_ORIGINS</code>.</span></div>
                <div class="ui-list-row"><span class="ui-tag is-info">tls</span><span class="ui-list-row-main">Never expose the NAS to the public internet without TLS.</span></div>
                <div class="ui-list-row"><span class="ui-tag is-ok">paths</span><span class="ui-list-row-main">File operations are validated by <code class="ui-code">path_scope.sanitize()</code>.</span></div>
            </div>
        </section>`;

    await _fillApiKey(isCurrent);
    shell.setStatus(`Signed in as ${session.username}`, session.valid ? 'ok' : 'warn');
}

async function _fillApiKey(isCurrent) {
    const card = shell.view.querySelector('[data-testid="api-key-card"]');
    if (!card) return;

    let data;
    try {
        data = await apiRequest('/api/auth/api-key');
    } catch (err) {
        logError('Settings: API key unavailable', err?.detail || err?.message);
        if (isCurrent()) card.innerHTML = `<p class="ui-muted">Could not load the API key — ${escapeHtml(err?.detail || err?.message || 'unknown error')}</p>`;
        return;
    }
    if (!isCurrent()) return;

    card.innerHTML = `
        <p class="ui-muted">Use this key in the EthOS Photos or EthOS Music Android app
            (Settings → Server → API key). It belongs to your account.</p>
        <div class="ui-row ui-panel-body">
            <input class="ui-input mono" readonly aria-label="API key" value="${escapeAttr(data.api_key || '')}">
        </div>
        <div class="ui-row ui-panel-body">
            <button class="ui-btn small" data-action="api-copy">${uiIcon('edit', 12)} Copy</button>
            <button class="ui-btn small danger" data-action="api-regen">${uiIcon('refresh', 12)} Regenerate</button>
        </div>`;

    const input = card.querySelector('input');
    card.querySelector('[data-action="api-copy"]')?.addEventListener('click', async () => {
        try {
            await navigator.clipboard.writeText(input?.value || '');
            notify({ kind: 'success', message: 'API key copied to the clipboard' });
        } catch (err) {
            logError('Settings: clipboard copy failed', err?.message || String(err));
            showAlert('Could not copy', 'Select the key and copy it manually.');
        }
    });

    card.querySelector('[data-action="api-regen"]')?.addEventListener('click', async () => {
        const ok = await showConfirm('Regenerate API key',
            'The current key stops working immediately. Update the Android app afterwards.', { danger: true });
        if (!ok) return;
        try {
            await apiRequest('/api/auth/api-key/regenerate', { method: 'POST' });
            notify({ kind: 'success', title: 'API key', message: 'A new key was generated' });
            renderSection('security');
        } catch (err) {
            logError('Settings: API key regeneration failed', err?.detail || err?.message);
            showAlert('Regenerate failed', err?.detail || err?.message || 'Unknown error');
        }
    });
}

function _tokenInfo() {
    const token = localStorage.getItem('token') || '';
    const fallback = { valid: false, username: '—', role: '—', expires: '—' };
    if (!token) return fallback;
    try {
        const payload = JSON.parse(atob(token.split('.')[1] || ''));
        return {
            valid: true,
            username: payload.username || '—',
            role: payload.role || 'user',
            expires: payload.exp ? new Date(payload.exp * 1000).toLocaleString() : '—',
        };
    } catch (err) {
        logError('Settings: could not decode the session token', err?.message || String(err));
        return fallback;
    }
}

// ════════════════════════════════════════════════════════════════════════════
// Appearance
// ════════════════════════════════════════════════════════════════════════════

async function renderAppearance(isCurrent) {
    let current = DEFAULT_WALLPAPER;
    let hasCustom = false;
    try {
        const data = await apiRequest('/api/appearance/wallpaper');
        current = data?.wallpaper || DEFAULT_WALLPAPER;
        hasCustom = Boolean(data?.has_custom);
    } catch (err) {
        // 404 means "never saved" for older installs — keep the local default.
        if (err?.status !== 404) logError('Settings: wallpaper preference unavailable', err?.detail || err?.message);
    }
    if (!isCurrent()) return;

    const tiles = WALLPAPER_IDS.map((id) => `
        <button type="button" class="ui-wp-tile${current === id ? ' is-active' : ''}" data-wp="${escapeAttr(id)}"
                aria-pressed="${current === id ? 'true' : 'false'}">
            <span class="ui-wp-art wallpaper-${escapeAttr(id)}"></span>
            <span class="ui-wp-label">${escapeHtml(id[0].toUpperCase() + id.slice(1))}</span>
        </button>`).join('');

    shell.view.innerHTML = `
        <div class="ui-grid ui-grid-2">
            <section class="ui-panel">
                <h2>${uiIcon('palette', 13)} Built-in wallpapers</h2>
                <p class="ui-muted">Saved per user — each account keeps its own background.</p>
                <div class="ui-panel-body ui-wp-grid">${tiles}</div>
            </section>

            <section class="ui-panel">
                <h2>${uiIcon('plus', 13)} Custom image</h2>
                <p class="ui-muted">JPEG, PNG or WebP · up to 8 MB.</p>
                <div class="ui-panel-body ui-row">
                    <div class="ui-wp-preview${current === CUSTOM_ID ? ' is-active' : ''}" data-testid="wp-custom-preview" data-wp-preview>
                        ${hasCustom ? '' : '<span class="ui-wp-preview-note">No image uploaded</span>'}
                    </div>
                    <div class="ui-list">
                        <input type="file" id="wpUploadInput" accept="image/jpeg,image/png,image/webp" hidden>
                        <button class="ui-btn small" data-action="wp-upload">${uiIcon('plus', 12)} Upload image…</button>
                        ${hasCustom ? `<button class="ui-btn small danger" data-testid="wp-remove-custom" data-action="wp-remove">${uiIcon('trash', 12)} Remove custom image</button>` : ''}
                    </div>
                </div>
            </section>
        </div>`;

    if (hasCustom) {
        const preview = shell.view.querySelector('[data-wp-preview]');
        if (preview) preview.style.backgroundImage = `url(${JSON.stringify(customImageUrl())})`;
    }

    shell.view.querySelectorAll('.ui-wp-tile').forEach((tile) => {
        tile.addEventListener('click', () => {
            saveWallpaper(tile.dataset.wp);
            notify({ kind: 'success', message: 'Wallpaper updated' });
            renderSection('appearance');
        });
    });

    shell.view.querySelector('[data-action="wp-upload"]')?.addEventListener('click', () => {
        document.getElementById('wpUploadInput')?.click();
    });

    const fileInput = document.getElementById('wpUploadInput');
    fileInput?.addEventListener('change', async () => {
        const file = fileInput.files?.[0];
        if (!file) return;
        try {
            await uploadCustomWallpaper(file);
            notify({ kind: 'success', message: 'Custom wallpaper uploaded' });
            renderSection('appearance');
        } catch (err) {
            logError('Settings: wallpaper upload failed', err?.detail || err?.message);
            notify({ kind: 'error', title: 'Upload failed', message: err?.detail || err?.message || 'Unknown error' });
        }
    });

    shell.view.querySelector('[data-action="wp-remove"]')?.addEventListener('click', async () => {
        const ok = await showConfirm('Remove custom image',
            'Your uploaded wallpaper is deleted and the default background restored.', { danger: true });
        if (!ok) return;
        try {
            await resetWallpaper();
            notify({ kind: 'success', message: 'Custom image removed' });
            renderSection('appearance');
        } catch (err) {
            logError('Settings: wallpaper reset failed', err?.detail || err?.message);
            notify({ kind: 'error', title: 'Remove failed', message: err?.detail || err?.message || 'Unknown error' });
        }
    });

    shell.setStatus(hasCustom ? 'Custom wallpaper active' : 'Built-in wallpaper active', 'ok');
}

// ════════════════════════════════════════════════════════════════════════════
// About
// ════════════════════════════════════════════════════════════════════════════

async function renderAbout(isCurrent) {
    const [version, doctor] = await Promise.all([
        apiRequest('/api/system/version').catch((err) => {
            logError('Settings: build identity unavailable', err?.detail || err?.message);
            return null;
        }),
        apiRequest('/api/system/doctor').catch((err) => {
            logError('Settings: doctor self-check unavailable', err?.detail || err?.message);
            return null;
        }),
    ]);
    if (!isCurrent()) return;

    const checks = doctor?.checks || [];
    const checkRows = checks.length
        ? checks.map((item) => `<tr>
                <td class="path">${escapeHtml(item.name)}</td>
                <td><span class="ui-tag ${item.ok ? 'is-ok' : (item.required ? 'is-bad' : 'is-warn')}">${item.ok ? 'ok' : (item.required ? 'fail' : 'optional')}</span></td>
                <td>${escapeHtml(item.detail || '—')}</td>
                <td class="ui-muted">${escapeHtml(item.hint || '')}</td>
            </tr>`).join('')
        : `<tr><td colspan="4">${emptyState({ title: 'Self-check unavailable', hint: 'The doctor endpoint did not answer.' })}</td></tr>`;

    shell.view.innerHTML = `
        <div class="ui-summary">
            <div><span class="ui-summary-label">Core version</span><span class="ui-summary-value small">${escapeHtml(version?.version || '—')}</span></div>
            <div><span class="ui-summary-label">Git</span><span class="ui-summary-value small">${escapeHtml(version?.git_sha || '—')}</span></div>
            <div><span class="ui-summary-label">Frontend hash</span><span class="ui-summary-value small">${escapeHtml(version?.frontend_hash || '—')}</span></div>
            <div><span class="ui-summary-label">Self-check</span><span class="ui-summary-value small">${doctor ? (doctor.ok ? 'Healthy' : 'Attention') : 'Unavailable'}</span></div>
        </div>

        <section class="ui-panel">
            <div class="ui-toolbar">
                <div>
                    <h2>${uiIcon('shield', 13)} Environment self-check</h2>
                    <p class="ui-muted">Read-only probes: data directory, secrets, tools and permissions.</p>
                </div>
                <div class="ui-row">
                    <button class="ui-btn small" data-action="open-logs">${uiIcon('info', 12)} Open Logs</button>
                    <button class="ui-btn small" data-action="goto-updates">${uiIcon('download', 12)} Updates</button>
                </div>
            </div>
            <div class="ui-panel-body ui-table-wrap">
                <table class="ui-table">
                    <thead><tr><th>Check</th><th>Status</th><th>Detail</th><th>Hint</th></tr></thead>
                    <tbody>${checkRows}</tbody>
                </table>
            </div>
        </section>`;

    shell.view.querySelector('[data-action="open-logs"]')?.addEventListener('click', () => openLogs({}));
    shell.view.querySelector('[data-action="goto-updates"]')?.addEventListener('click', () => shell.setActive('updates'));

    shell.setStatus(version ? `EthOS ${version.version || ''} · built ${_fmtDate(version.backend_built_at)}` : 'Build identity unavailable',
        version ? 'ok' : 'warn');
}

// ════════════════════════════════════════════════════════════════════════════
// Shared helpers
// ════════════════════════════════════════════════════════════════════════════

const RENDERERS = {
    overview: renderHealth,
    updates: renderUpdates,
    maintenance: renderMaintenance,
    users: renderUsers,
    security: renderSecurity,
    appearance: renderAppearance,
    about: renderAbout,
};

function _kv(rows) {
    return `<dl class="ui-kv">${rows.map(([key, value, mono]) => `<div>
            <dt class="ui-kv-key">${escapeHtml(key)}</dt>
            <dd class="ui-kv-value${mono ? ' mono' : ''}">${value}</dd>
        </div>`).join('')}</dl>`;
}

function _relTime(iso) {
    if (!iso) return 'never';
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return String(iso);
    const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (seconds < 5) return 'just now';
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} h ${minutes % 60} min ago`;
    return `${Math.floor(hours / 24)} d ago`;
}

function _clock() {
    return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/** ISO string or epoch seconds → local date-time; never "Invalid Date". */
function _fmtDate(value) {
    if (!value) return '—';
    const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return `${date.toLocaleDateString()} ${date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
}

function _jwtRole() {
    try {
        const parts = (localStorage.getItem('token') || '').split('.');
        if (parts.length < 2) return 'user';
        return JSON.parse(atob(parts[1])).role || 'user';
    } catch (err) {
        logError('Settings: could not read the role from the token', err?.message || String(err));
        return 'user';
    }
}

function _isAdmin() {
    return _jwtRole() === 'admin';
}

function _withToken(url) {
    const token = encodeURIComponent(localStorage.getItem('token') || '');
    return `${url}${url.includes('?') ? '&' : '?'}token=${token}`;
}
