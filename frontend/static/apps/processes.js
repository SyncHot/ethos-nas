// EthOS NAS — Process Manager (single-pane shell)
import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiRequest } from '../core/api.js';
import { poll } from '../core/poller.js';
import { skeletonRows, emptyState, errorState } from '../core/states.js';
import { showConfirm } from '../core/modal.js';
import { notify } from '../core/notification-center.js';
import { uiIcon } from '../core/icons.js';
import { logError } from '../core/logger.js';
import { mountShell, applyBarWidths } from '../core/ui-shell.js';

let shell = null;
let sortKey = 'mem';
let sortAsc = false;
let filter = '';
let autoRefresh = false;

const AUTO_REFRESH_MS = 5000;
const HEAVY_MB = 300;
const CRITICAL_MB = 1000;

function _mb(value) {
    const n = Number(value) || 0;
    return n >= 1024 ? `${(n / 1024).toFixed(2)} GB` : `${Math.round(n)} MB`;
}

function _tone(percent) {
    const value = Number(percent) || 0;
    return value >= 90 ? 'bad' : value >= 75 ? 'warn' : 'ok';
}

function _bar(percent) {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    return `<div class="ui-bar">
        <div class="ui-bar-fill ${_tone(value)}" data-width="${value}"></div></div>`;
}

function _statusTag(status) {
    const tone = status === 'running' ? 'is-ok'
        : status === 'sleeping' ? 'is-info'
            : status === 'zombie' ? 'is-bad' : 'is-warn';
    return `<span class="ui-tag ${tone}">${escapeHtml(status || '?')}</span>`;
}

export async function renderProcessesPage(body) {
    if (body._procStop) { body._procStop(); body._procStop = null; }
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'processes',
        title: 'Process Manager',
        subtitle: 'Running processes and what is eating RAM.',
        icon: '📋',
        sections: [{ id: 'processes', label: 'Processes' }],
        rail: false,
        note: 'Killing a service process may break an app.',
    });
    shell.view.innerHTML = `
        <div class="ui-toolbar">
            <div class="ui-row">
                <input id="procSearch" class="ui-input" type="search" placeholder="Filter processes…" aria-label="Filter processes">
                <button class="ui-btn small" id="procRefreshBtn" data-testid="proc-refresh-btn">${uiIcon('refresh', 12)} Refresh</button>
                <label class="ui-check" title="Reload every 5 s"><input type="checkbox" id="procAuto" data-testid="proc-auto-refresh"> auto</label>
            </div>
            <span class="ui-tag" id="procCount">—</span>
        </div>
        <section class="ui-panel" id="procSummary">${skeletonRows(2)}</section>
        <section class="ui-panel" style="min-height:180px;">
            <div id="procTableWrap">${skeletonRows(10)}</div>
        </section>`;

    const search = shell.view.querySelector('#procSearch');
    search.value = filter;
    search.addEventListener('input', () => {
        filter = search.value.toLowerCase();
        refresh();
    });
    shell.view.querySelector('#procRefreshBtn')?.addEventListener('click', () => refresh());
    const auto = shell.view.querySelector('#procAuto');
    auto.checked = autoRefresh;
    auto.addEventListener('change', () => {
        autoRefresh = auto.checked;
        refresh();
    });

    shell.setStatus('Loading processes…');
    refresh();

    async function refresh() {
        await _refreshProcList(body);
    }
}

async function _refreshProcList(body) {
    if (!shell || !shell.root.isConnected) return;
    const wrap = shell.view.querySelector('#procTableWrap');
    const summary = shell.view.querySelector('#procSummary');
    if (!wrap) return;
    wrap.innerHTML = skeletonRows(10);

    try {
        const serverSort = ['cpu', 'mem', 'name', 'pid'].includes(sortKey) ? sortKey : 'mem';
        const data = await apiRequest(`/api/processes?sort=${serverSort}&limit=300`);
        if (!shell.root.isConnected) return;

        if (summary) {
            summary.innerHTML = _summaryHtml(data.system, data.groups || []);
            applyBarWidths(summary);
        }

        let procs = data.processes || [];
        if (filter) {
            procs = procs.filter((proc) =>
                (proc.name && proc.name.toLowerCase().includes(filter))
                || (proc.display_name && proc.display_name.toLowerCase().includes(filter))
                || (proc.container && proc.container.toLowerCase().includes(filter))
                || (proc.username && proc.username.toLowerCase().includes(filter))
                || String(proc.pid).includes(filter)
                || (proc.cmdline && proc.cmdline.toLowerCase().includes(filter)));
        }

        const direction = sortAsc ? 1 : -1;
        if (sortKey === 'threads') procs = [...procs].sort((a, b) => direction * ((a.threads || 0) - (b.threads || 0)));
        else if (sortKey === 'mem_pct') procs = [...procs].sort((a, b) => direction * ((a.memory_percent || 0) - (b.memory_percent || 0)));
        else if (sortKey === 'user') procs = [...procs].sort((a, b) => direction * String(a.username || '').localeCompare(String(b.username || '')));
        else if (sortKey === 'status') procs = [...procs].sort((a, b) => direction * String(a.status || '').localeCompare(String(b.status || '')));

        const count = shell.view.querySelector('#procCount');
        if (count) count.textContent = `${procs.length} process${procs.length === 1 ? '' : 'es'}`;

        if (!procs.length) {
            wrap.innerHTML = emptyState({
                icon: 'list',
                title: filter ? 'No matching processes' : 'No processes found',
                hint: filter ? 'Try a different search term.' : 'The process list is empty.',
            });
            shell.setStatus(filter ? 'No matches' : 'No processes', 'warn');
            return;
        }

        const columns = [
            ['pid', 'PID'], ['name', 'Process'], ['user', 'User'], ['cpu', 'CPU'], ['mem', 'RAM'],
            ['mem_pct', '%MEM'], ['threads', 'Thr'], ['status', 'Status'], ['_actions', ''],
        ];
        const arrow = (key) => (sortKey === key ? (sortAsc ? ' ↑' : ' ↓') : '');
        const rows = procs.map((proc) => {
            const cpu = Number(proc.cpu_percent) || 0;
            const memMb = Number(proc.memory_rss_mb) || 0;
            const tone = memMb >= CRITICAL_MB ? 'is-bad' : memMb >= HEAVY_MB ? 'is-warn' : '';
            const label = proc.display_name || proc.name;
            return `<tr class="${tone ? `row-${tone.slice(3)}` : ''}">
                <td class="path">${proc.pid}</td>
                <td>
                    <div title="${escapeAttr(proc.cmdline || proc.name)}">${escapeHtml(label)}</div>
                    ${proc.container ? `<span class="ui-tag is-info">🐳 ${escapeHtml(proc.container)}</span>` : ''}
                </td>
                <td>${escapeHtml(proc.username || '—')}</td>
                <td><span class="ui-tag ${cpu > 50 ? 'is-bad' : cpu > 20 ? 'is-warn' : ''}">${cpu.toFixed(1)}%</span></td>
                <td class="num">${_mb(memMb)}</td>
                <td class="num">${(Number(proc.memory_percent) || 0).toFixed(1)}%</td>
                <td class="num">${Number(proc.threads) || 0}</td>
                <td>${_statusTag(proc.status)}</td>
                <td class="actions"><button class="ui-btn small danger" data-kill="${proc.pid}" data-testid="proc-kill-${proc.pid}">Kill</button></td>
            </tr>`;
        }).join('');

        wrap.innerHTML = `<div class="ui-table-wrap"><table class="ui-table" data-testid="processes-table">
            <thead><tr>${columns.map(([key, label]) => `<th data-sort="${key}" style="cursor:pointer;">${escapeHtml(label)}${arrow(key)}</th>`).join('')}</tr></thead>
            <tbody>${rows}</tbody></table></div>`;

        wrap.onclick = (event) => {
            const header = event.target.closest('[data-sort]');
            if (header) {
                const key = header.dataset.sort;
                if (key === '_actions') return;
                if (sortKey === key) sortAsc = !sortAsc;
                else { sortKey = key; sortAsc = key === 'name' || key === 'user'; }
                _refreshProcList(body);
                return;
            }
            const kill = event.target.closest('[data-kill]');
            if (kill) _killProcess(Number(kill.dataset.kill), body);
        };
        shell.setStatus(`CPU ${procs.reduce((sum, proc) => sum + (Number(proc.cpu_percent) || 0), 0).toFixed(1)}%`, 'ok');
    } catch (err) {
        if (!shell.root.isConnected) return;
        wrap.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read processes', 'error');
        logError('Processes: list unavailable', err?.detail || err?.message);
    }

    if (body._procStop) { body._procStop(); body._procStop = null; }
    if (autoRefresh) {
        body._procStop = poll(() => _refreshProcList(body), AUTO_REFRESH_MS,
            { appId: 'processes', immediate: false, name: 'processes' });
    }
}

function _summaryHtml(system, groups) {
    if (!system) return emptyState({ title: 'System summary unavailable', hint: 'The process API did not return system data.' });
    const memPercent = Number(system.mem_percent) || 0;
    const swapPercent = Number(system.swap_percent) || 0;
    const loadHigh = Number(system.load_1) > Number(system.cpu_count);

    const consumers = (groups || []).slice(0, 6).map((group) => {
        const max = Math.max(...groups.map((item) => Number(item.memory_rss_mb) || 0), 1);
        const percent = Math.round(((Number(group.memory_rss_mb) || 0) / max) * 100);
        return `<div class="ui-list-row">
            <span class="ui-list-row-main">${group.container ? '🐳' : '⚙️'} ${escapeHtml(group.name)}
                <span class="ui-muted">×${Number(group.process_count) || 0}</span></span>
            <span class="ui-bar" style="width:120px;"><span class="ui-bar-fill ${group.container ? 'ok' : 'warn'}" data-width="${percent}" style="display:block;"></span></span>
            <span class="ui-tag">${_mb(group.memory_rss_mb)}</span>
        </div>`;
    }).join('');

    return `
        <div class="ui-grid ui-grid-2">
            <div>
                <p class="ui-muted">RAM ${_mb(system.mem_used_mb)} / ${_mb(system.mem_total_mb)} · ${_mb(system.mem_available_mb)} free · ${memPercent}%</p>
                ${_bar(memPercent)}
            </div>
            <div>
                <p class="ui-muted">Swap ${_mb(system.swap_used_mb)} / ${_mb(system.swap_total_mb)} · ${swapPercent}%${swapPercent >= 90 ? ' — almost full' : ''}</p>
                ${_bar(swapPercent)}
            </div>
        </div>
        <div class="ui-kv ui-panel-body">
            <div><dt class="ui-kv-key">Load (1 / 5 / 15)</dt>
                <dd class="ui-kv-value">${escapeHtml(String(system.load_1))} · ${escapeHtml(String(system.load_5))} · ${escapeHtml(String(system.load_15))}
                    <span class="ui-tag ${loadHigh ? 'is-bad' : ''}">${escapeHtml(String(system.cpu_count))} cpu</span></dd></div>
        </div>
        ${consumers ? `<div class="ui-panel-body">
            <p class="ui-label">Top RAM consumers</p>
            <div class="ui-list">${consumers}</div>
        </div>` : ''}`;
}

async function _killProcess(pid, body) {
    const ok = await showConfirm('Kill process', `Kill process PID ${pid}?`, { danger: true });
    if (!ok) return;
    try {
        await apiRequest(`/api/processes/${encodeURIComponent(pid)}`, { method: 'DELETE' });
        notify({ kind: 'success', message: `Process ${pid} terminated` });
    } catch (err) {
        logError(`Processes: kill ${pid} failed`, err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Could not terminate the process' });
    }
    _refreshProcList(body);
}
