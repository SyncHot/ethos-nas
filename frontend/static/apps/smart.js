// EthOS NAS — SMART Disk Health (single-pane shell)
import { escapeAttr, escapeHtml, formatBytes } from '../core/utils.js';
import { apiRequest } from '../core/api.js';
import { poll } from '../core/poller.js';
import { skeletonRows, emptyState, errorState, wireStateActions } from '../core/states.js';
import { uiIcon } from '../core/icons.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';

let shell = null;
let _autoStop = null;

export async function renderSmartPage(body) {
    if (!body) return;
    if (_autoStop) { _autoStop(); _autoStop = null; }
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'smart',
        title: 'SMART Disk Health',
        subtitle: 'Drive health, temperature and power-on time.',
        icon: '💿',
        sections: [{ id: 'smart', label: 'Disks' }],
        rail: false,
        note: 'SMART data may require root privileges.',
    });
    shell.view.setAttribute('data-testid', 'smart-content');
    shell.view.innerHTML = `
        <div class="ui-toolbar">
            <span class="ui-tag" id="smartCount">—</span>
            <button class="ui-btn small" data-testid="smart-refresh-btn">${uiIcon('refresh', 12)} Refresh</button>
        </div>
        <div id="smartDisks">${skeletonRows(4)}</div>`;

    shell.view.querySelector('[data-testid="smart-refresh-btn"]')?.addEventListener('click', () => loadDisks());
    await loadDisks();
    // Temperatures and power-on hours drift while the window is open; a slow
    // refresh keeps the view honest without a loading flash (poll pauses while
    // the tab is hidden and stops when the window closes).
    _autoStop = poll(() => loadDisks({ silent: true }), 60_000,
        { appId: 'smart', immediate: false, name: 'smart' });
}

async function loadDisks({ silent = false } = {}) {
    if (!shell) return;
    const content = shell.view.querySelector('#smartDisks');
    const count = shell.view.querySelector('#smartCount');
    if (!content) return;
    if (!silent) {
        content.innerHTML = skeletonRows(4);
        shell.setStatus('Loading SMART data…');
    }

    try {
        const data = await apiRequest('/api/smart');
        if (!shell.root.isConnected) return;
        const disks = data.disks || [];
        if (count) count.textContent = `${data.total ?? disks.length} disk(s) detected`;

        if (!disks.length) {
            content.innerHTML = emptyState({
                icon: 'disk',
                title: 'No disks detected',
                hint: 'SMART data may require root privileges.',
            });
            shell.setStatus('No disks reported', 'warn');
            return;
        }

        content.innerHTML = `<div class="ui-grid">${disks.map((disk) => {
            const healthy = disk.healthy === true;
            const failing = disk.healthy === false;
            const tone = healthy ? 'is-ok' : failing ? 'is-bad' : 'is-warn';
            const label = healthy ? 'Healthy' : failing ? 'FAILING' : 'Unknown';
            return `<section class="ui-panel" data-testid="smart-disk-${escapeAttr(disk.name)}">
                <div class="ui-toolbar">
                    <div>
                        <h2>${escapeHtml(disk.name)}</h2>
                        ${disk.model ? `<p class="ui-muted">${escapeHtml(disk.model)}</p>` : ''}
                    </div>
                    <div class="ui-row">
                        ${disk.temperature != null ? `<span class="ui-tag" title="Temperature">${escapeHtml(String(disk.temperature))}°C</span>` : ''}
                        <span class="ui-tag ${tone}" data-testid="smart-status-${escapeAttr(disk.name)}">${label}</span>
                    </div>
                </div>
                <div class="ui-panel-body">
                    <dl class="ui-kv">
                        <div><dt class="ui-kv-key">Size</dt><dd class="ui-kv-value mono">${escapeHtml(formatBytes(disk.size_bytes || 0))}</dd></div>
                        ${disk.power_on_hours != null ? `<div><dt class="ui-kv-key">Power-on</dt><dd class="ui-kv-value">${escapeHtml(String(disk.power_on_hours))} h</dd></div>` : ''}
                        ${disk.serial ? `<div><dt class="ui-kv-key">Serial</dt><dd class="ui-kv-value mono">${escapeHtml(disk.serial)}</dd></div>` : ''}
                    </dl>
                    ${disk.note ? `<p class="ui-callout">${escapeHtml(disk.note)}</p>` : ''}
                </div>
            </section>`;
        }).join('')}</div>`;

        shell.setStatus(`${disks.length} disk(s) checked · updated ${new Date().toLocaleTimeString()}`, failing ? 'warn' : 'ok');
    } catch (err) {
        if (!shell.root.isConnected) return;
        content.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        wireStateActions(content, { retry: () => loadDisks() });
        shell.setStatus('Could not read SMART data', 'error');
        logError('SMART: disk data unavailable', err?.detail || err?.message);
    }
}
