// EthOS NAS — SMART Disk Health (single-pane shell)
// Disk cards open an inline detail: full attribute table, 24 h temperature
// series (persisted by the health monitor) and drive-side self-tests.
import { escapeAttr, escapeHtml, formatBytes } from '../core/utils.js';
import { apiRequest } from '../core/api.js';
import { poll } from '../core/poller.js';
import { skeletonRows, emptyState, errorState, wireStateActions } from '../core/states.js';
import { showConfirm } from '../core/modal.js';
import { notify } from '../core/notification-center.js';
import { uiIcon } from '../core/icons.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';

let shell = null;
let _autoStop = null;
let _selectedDevice = null;

export async function renderSmartPage(body) {
    if (!body) return;
    if (_autoStop) { _autoStop(); _autoStop = null; }
    _selectedDevice = null;
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
        <div id="smartDisks">${skeletonRows(4)}</div>
        <div id="smartDetail" data-testid="smart-detail" style="display:none;"></div>`;

    shell.view.querySelector('[data-testid="smart-refresh-btn"]')?.addEventListener('click', () => loadDisks());
    shell.view.addEventListener('click', (event) => {
        const detailBtn = event.target.closest('[data-smart-detail]');
        if (detailBtn) { toggleDetail(detailBtn.dataset.smartDetail); return; }
        if (event.target.closest('[data-smart-close]')) { closeDetail(); return; }
        const testBtn = event.target.closest('[data-selftest]');
        if (testBtn) { selfTest(testBtn.dataset.selftestDevice, testBtn.dataset.selftest); return; }
        if (event.target.closest('[data-selftest-log]')) selfTestLog();
    });
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

        content.innerHTML = `<div class="ui-grid">${disks.map(_diskCard).join('')}</div>`;
        const anyFailing = disks.some((disk) => disk.healthy === false);
        shell.setStatus(`${disks.length} disk(s) checked · updated ${new Date().toLocaleTimeString()}`,
            anyFailing ? 'warn' : 'ok');

        // Keep an open detail panel in sync with a background refresh.
        if (_selectedDevice) {
            const stillPresent = disks.some((disk) => disk.name === _selectedDevice);
            if (stillPresent) openDetail(_selectedDevice, { silent: true });
            else closeDetail();
        }
    } catch (err) {
        if (!shell.root.isConnected) return;
        content.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        wireStateActions(content, { retry: () => loadDisks() });
        shell.setStatus('Could not read SMART data', 'error');
        logError('SMART: disk data unavailable', err?.detail || err?.message);
    }
}

function _diskCard(disk) {
    const healthy = disk.healthy === true;
    const failing = disk.healthy === false;
    const tone = healthy ? 'is-ok' : failing ? 'is-bad' : 'is-warn';
    const label = healthy ? 'Healthy' : failing ? 'FAILING' : 'Unknown';
    const safe = escapeAttr(disk.name);
    return `<section class="ui-panel" data-testid="smart-disk-${safe}">
        <div class="ui-toolbar">
            <div>
                <h2>${escapeHtml(disk.name)}</h2>
                ${disk.model ? `<p class="ui-muted">${escapeHtml(disk.model)}</p>` : ''}
            </div>
            <div class="ui-row">
                ${disk.temperature != null ? `<span class="ui-tag" title="Temperature">${escapeHtml(String(disk.temperature))}°C</span>` : ''}
                <span class="ui-tag ${tone}" data-testid="smart-status-${safe}">${label}</span>
            </div>
        </div>
        <div class="ui-panel-body">
            <dl class="ui-kv">
                <div><dt class="ui-kv-key">Size</dt><dd class="ui-kv-value mono">${escapeHtml(formatBytes(disk.size_bytes || 0))}</dd></div>
                ${disk.power_on_hours != null ? `<div><dt class="ui-kv-key">Power-on</dt><dd class="ui-kv-value">${escapeHtml(String(disk.power_on_hours))} h</dd></div>` : ''}
                ${disk.serial ? `<div><dt class="ui-kv-key">Serial</dt><dd class="ui-kv-value mono">${escapeHtml(disk.serial)}</dd></div>` : ''}
            </dl>
            ${disk.note ? `<p class="ui-callout">${escapeHtml(disk.note)}</p>` : ''}
            <div class="ui-row">
                <button class="ui-btn small" data-smart-detail="${safe}" data-testid="smart-detail-${safe}">
                    ${uiIcon('chart', 12)} Details, trend &amp; tests
                </button>
            </div>
        </div>
    </section>`;
}

function closeDetail() {
    _selectedDevice = null;
    const host = shell?.view.querySelector('#smartDetail');
    if (host) {
        host.style.display = 'none';
        host.innerHTML = '';
    }
}

function toggleDetail(name) {
    if (_selectedDevice === name) { closeDetail(); return; }
    openDetail(name);
}

async function openDetail(name, { silent = false } = {}) {
    if (!shell || !name) return;
    const host = shell.view.querySelector('#smartDetail');
    if (!host) return;
    _selectedDevice = name;
    host.style.display = '';
    if (!silent) host.innerHTML = skeletonRows(5);

    const devPath = `/dev/${name}`;
    const [detail, history, health] = await Promise.all([
        apiRequest(`/api/smart/${encodeURIComponent(name)}`).catch((err) => ({ error: err?.detail || err?.message })),
        apiRequest(`/api/smart/history?hours=24&device=${encodeURIComponent(devPath)}`).catch(() => null),
        apiRequest('/api/health/status').catch(() => null),
    ]);
    if (!shell.root.isConnected || _selectedDevice !== name) return;

    if (detail?.error) {
        host.innerHTML = errorState({ detail: detail.error });
        wireStateActions(host, { retry: () => openDetail(name) });
        return;
    }
    if (!silent) host.innerHTML = _detailHtml(name, detail, history, health);
    else {
        // Silent refresh: keep the panel but update the body in place.
        host.innerHTML = _detailHtml(name, detail, history, health);
    }
    _drawTempChart('smartTempChart', _samplesFor(history, devPath));
}

function _samplesFor(history, devPath) {
    const devices = history?.devices || [];
    return devices.find((entry) => entry.device === devPath)?.samples || [];
}

function _trendFor(health, devPath) {
    return (health?.smart_trends?.devices || []).find((entry) => entry.device === devPath) || null;
}

function _attributesTable(detail, trend) {
    const attributes = Array.isArray(detail?.attributes) ? detail.attributes : [];
    if (!attributes.length) {
        return `<p class="ui-callout">No attribute table available (${escapeHtml(detail?.note || 'smartctl returned a summary only')}).</p>`;
    }
    const current = trend?.attrs || {};
    const previous = trend?.prev_attrs || {};
    const changed = new Set(trend?.changes || []);
    const rows = attributes.map((attr) => {
        const name = attr.name || '';
        const now = current[name];
        const before = previous[name];
        let delta = '';
        if (typeof before === 'number' && typeof now === 'number' && before !== now) {
            const bad = changed.has(name);
            delta = `<span class="ui-tag ${bad ? 'is-bad' : 'is-warn'}" title="Change since the previous sample">${escapeHtml(String(before))} → ${escapeHtml(String(now))}</span>`;
        }
        const critical = attr.critical || attr.when_failed
            ? '<span class="ui-tag is-bad">critical</span>' : '';
        return `<tr data-testid="smart-attr-${escapeAttr(String(attr.id ?? name))}">
            <td class="num">${escapeHtml(String(attr.id ?? '—'))}</td>
            <td class="path">${escapeHtml(name || '—')}</td>
            <td class="num">${escapeHtml(String(attr.value ?? '—'))}</td>
            <td class="num">${escapeHtml(String(attr.worst ?? '—'))}</td>
            <td class="num">${escapeHtml(String(attr.thresh ?? '—'))}</td>
            <td class="num">${escapeHtml(String(attr.raw ?? '—'))}</td>
            <td>${critical} ${delta}</td>
        </tr>`;
    }).join('');
    return `<div class="ui-table-wrap"><table class="ui-table" data-testid="smart-attributes">
        <thead><tr><th class="num">ID</th><th>Attribute</th><th class="num">Value</th>
            <th class="num">Worst</th><th class="num">Thresh</th><th class="num">Raw</th><th>Notes</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
}

function _detailHtml(name, detail, history, health) {
    const devPath = `/dev/${name}`;
    const trend = _trendFor(health, devPath);
    const samples = _samplesFor(history, devPath);
    const changes = trend?.changes || [];
    const trendCallout = changes.length
        ? `<div class="ui-callout error" data-testid="smart-trend-alert">Error counters grew since the last sample:
             <strong>${escapeHtml(changes.join(', '))}</strong> — back up important data and plan a replacement.</div>`
        : '';
    const tempNow = detail?.temperature ?? (samples.length ? samples[samples.length - 1].temp : null);
    const temps = samples.map((sample) => sample.temp).filter((value) => typeof value === 'number');
    const tempSummary = temps.length
        ? `${samples.length} sample(s) · min ${Math.min(...temps)} °C · max ${Math.max(...temps)} °C`
        : 'No samples yet — the health monitor records one per check.';
    return `<section class="ui-panel" data-testid="smart-detail-panel">
        <div class="ui-toolbar">
            <div>
                <h2>${escapeHtml(name)} — details</h2>
                <p class="ui-muted">${escapeHtml(detail?.model || '')}${detail?.serial ? ` · ${escapeHtml(detail.serial)}` : ''}${tempNow != null ? ` · ${escapeHtml(String(tempNow))} °C` : ''}</p>
            </div>
            <div class="ui-row">
                <button class="ui-btn small" data-selftest="short" data-selftest-device="${escapeAttr(name)}" data-testid="smart-selftest-short">Short test</button>
                <button class="ui-btn small" data-selftest="long" data-selftest-device="${escapeAttr(name)}" data-testid="smart-selftest-long">Long test</button>
                <button class="ui-btn small danger" data-selftest="abort" data-selftest-device="${escapeAttr(name)}" data-testid="smart-selftest-abort">Abort</button>
                <button class="ui-btn small" data-smart-close data-testid="smart-detail-close">Close</button>
            </div>
        </div>
        <div class="ui-panel-body">
            ${trendCallout}
            <h3>Temperature (last 24 h)</h3>
            <canvas id="smartTempChart" class="ui-canvas" data-testid="smart-temp-chart"></canvas>
            <p class="ui-muted" data-testid="smart-temp-summary">${tempSummary}</p>
            <h3>Attributes</h3>
            ${_attributesTable(detail, trend)}
            <details>
                <summary>Self-test log</summary>
                <div class="ui-row" style="margin:8px 0;">
                    <button class="ui-btn small" data-selftest-log data-testid="smart-selftest-log">Load log</button>
                </div>
                <pre id="smartSelfTestLog" class="ui-code" data-testid="smart-selftest-log-output" style="max-height:220px;overflow:auto;"></pre>
            </details>
        </div>
    </section>`;
}

function _drawTempChart(canvasId, samples) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const points = samples.filter((sample) => typeof sample.temp === 'number');
    const root = getComputedStyle(document.documentElement);
    const accent = root.getPropertyValue('--ui-accent').trim();
    const border = root.getPropertyValue('--ui-border').trim();
    const muted = root.getPropertyValue('--text-3').trim();
    const dpr = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 600;
    const height = canvas.clientHeight || 120;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const context = canvas.getContext('2d');
    context.scale(dpr, dpr);
    context.clearRect(0, 0, width, height);

    if (!points.length) {
        context.fillStyle = muted;
        context.font = '12px system-ui, sans-serif';
        context.textAlign = 'center';
        context.fillText('No temperature samples yet', width / 2, height / 2);
        return;
    }
    const padL = 8, padR = 8, padT = 10, padB = 18;
    const values = points.map((point) => point.temp);
    const maxValue = Math.max(...values) + 1;
    const minValue = Math.min(...values) - 1;
    const span = Math.max(1, maxValue - minValue);
    const x = (index) => points.length === 1
        ? (padL + width - padR) / 2
        : padL + (width - padL - padR) * index / (points.length - 1);
    const y = (value) => height - padB - (height - padT - padB) * ((value - minValue) / span);

    context.strokeStyle = border;
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(padL, height - padB);
    context.lineTo(width - padR, height - padB);
    context.stroke();

    context.strokeStyle = accent;
    context.lineWidth = 1.5;
    context.beginPath();
    points.forEach((point, index) => {
        if (index === 0) context.moveTo(x(index), y(point.temp));
        else context.lineTo(x(index), y(point.temp));
    });
    context.stroke();

    context.fillStyle = muted;
    context.font = '10px system-ui, sans-serif';
    context.textAlign = 'left';
    context.fillText(`${maxValue.toFixed(0)} °C`, padL, 8);
    context.fillText(new Date(points[0].ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }), padL, height - 4);
    context.textAlign = 'right';
    context.fillText(new Date(points[points.length - 1].ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }), width - padR, height - 4);
}

async function selfTest(name, mode) {
    const labels = {
        short: 'short self-test (about 2 minutes)',
        long: 'long self-test (can take hours; the drive stays usable)',
        abort: 'abort the running self-test',
    };
    const ok = await showConfirm('SMART self-test',
        `Run the ${labels[mode] || mode} on /dev/${name}?`, { danger: mode === 'abort' });
    if (!ok) return;
    try {
        const result = await apiRequest(`/api/smart/${encodeURIComponent(name)}/self-test`, {
            method: 'POST',
            body: { mode },
        });
        notify({ kind: 'success', title: 'SMART self-test', message: result?.output || `${mode} test started` });
    } catch (err) {
        logError(`SMART: ${mode} self-test failed`, err?.detail || err?.message);
        notify({ kind: 'error', title: 'SMART self-test failed', message: err?.detail || err?.message || 'Unknown error' });
    }
}

async function selfTestLog() {
    if (!_selectedDevice || !shell) return;
    const output = shell.view.querySelector('#smartSelfTestLog');
    if (!output) return;
    output.textContent = 'Loading…';
    try {
        const result = await apiRequest(`/api/smart/${encodeURIComponent(_selectedDevice)}/selftest-log`);
        if (!shell.root.isConnected) return;
        output.textContent = result?.output || 'The drive returned an empty self-test log.';
    } catch (err) {
        output.textContent = err?.detail || err?.message || 'Could not read the self-test log.';
        logError('SMART: self-test log unavailable', err?.detail || err?.message);
    }
}
