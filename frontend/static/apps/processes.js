// EthOS NAS — Process Manager Page (with search, auto-refresh)
import { escapeHtml, escapeAttr } from '../core/utils.js';
import { apiFetch } from '../core/api.js';
import { poll } from '../core/poller.js';
import { skeletonRows } from '../core/states.js';

let _procSort = 'mem', _procSortAsc = false;
let _procFilter = '';
let _procAutoRefresh = false;
const AUTO_REFRESH_MS = 5000;
const HEAVY_MB = 300;      // rows above this get a yellow tint
const CRITICAL_MB = 1000;  // ... and this one a red tint

// ── formatting helpers ───────────────────────────────────────────────

function _mb(v) {
    const n = Number(v) || 0;
    return n >= 1024 ? `${(n / 1024).toFixed(2)} GB` : `${Math.round(n)} MB`;
}

function _bar(percent, color) {
    const p = Math.max(0, Math.min(100, Number(percent) || 0));
    const c = color || (p >= 90 ? '#ef4444' : p >= 75 ? '#eab308' : '#22c55e');
    return `<div style="height:8px;border-radius:999px;background:rgba(255,255,255,0.08);overflow:hidden;">
        <div style="height:100%;width:${p}%;background:${c};transition:width .3s;"></div>
    </div>`;
}

function _statusBadge(status) {
    const cls = status === 'running' ? 'badge-green'
        : status === 'sleeping' ? 'badge-blue'
        : status === 'zombie' ? 'badge-red' : 'badge-yellow';
    return `<span class="${cls}" style="padding:2px 6px;border-radius:4px;font-size:10px;">${escapeHtml(status || '?')}</span>`;
}

/** RAM/swap/load header + the "what actually eats RAM" grouping. */
function _summaryHtml(sys, groups) {
    if (!sys) return '';
    const memPct = sys.mem_percent || 0;
    const swapPct = sys.swap_percent || 0;
    // Swap saturation is the real "my NAS is choking" signal — surface it loudly.
    const swapWarn = swapPct >= 90
        ? `<span style="color:#ef4444;font-weight:700;">⚠ swap almost full</span>` : `${swapPct}%`;

    let html = `
        <div style="display:grid;grid-template-columns:1fr 1fr auto;gap:16px;align-items:center;">
            <div>
                <div style="display:flex;justify-content:space-between;font-size:12px;color: var(--text-2);margin-bottom:4px;">
                    <span><b style="color:var(--text-1);">RAM</b> ${_mb(sys.mem_used_mb)} / ${_mb(sys.mem_total_mb)}</span>
                    <span>${_mb(sys.mem_available_mb)} free · ${memPct}%</span>
                </div>
                ${_bar(memPct)}
            </div>
            <div>
                <div style="display:flex;justify-content:space-between;font-size:12px;color: var(--text-2);margin-bottom:4px;">
                    <span><b style="color:var(--text-1);">Swap</b> ${_mb(sys.swap_used_mb)} / ${_mb(sys.swap_total_mb)}</span>
                    <span>${swapWarn}</span>
                </div>
                ${_bar(swapPct, swapPct >= 90 ? '#ef4444' : undefined)}
            </div>
            <div style="font-size:12px;color: var(--text-3);text-align:right;white-space:nowrap;">
                <div>load <b style="color:${sys.load_1 > sys.cpu_count ? '#ef4444' : 'var(--text-1)'};">${sys.load_1}</b>
                     <span style="opacity:.7;">/ ${sys.cpu_count} cpu</span></div>
                <div style="opacity:.7;">${sys.load_1} · ${sys.load_5} · ${sys.load_15}</div>
            </div>
        </div>`;

    if (groups && groups.length) {
        const max = Math.max(...groups.map(g => g.memory_rss_mb || 0), 1);
        const rows = groups.slice(0, 6).map(g => {
            const pct = Math.round(((g.memory_rss_mb || 0) / max) * 100);
            const isContainer = !!g.container;
            return `<div style="display:flex;align-items:center;gap:10px;font-size:12px;margin-top:6px;">
                <span style="width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-1);"
                      title="${escapeAttr(isContainer ? 'container ' + g.container : g.name)}">
                    ${isContainer ? '🐳' : '⚙️'} ${escapeHtml(g.name)}
                    <span style="color: var(--text-3);">×${g.process_count}</span>
                </span>
                <span style="flex:1;height:8px;border-radius:999px;background:rgba(255,255,255,0.06);overflow:hidden;">
                    <span style="display:block;height:100%;width:${pct}%;background:${isContainer ? '#5b9aff' : '#8b5cf6'};"></span>
                </span>
                <span style="width:82px;text-align:right;color:var(--text-1);font-weight:600;">${_mb(g.memory_rss_mb)}</span>
            </div>`;
        }).join('');
        html += `<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border);">
            <div style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color: var(--text-3);">Top RAM consumers</div>
            ${rows}
        </div>`;
    }
    return html;
}

export async function renderProcessesPage(body) {
    // One timer per window body; re-rendering must not stack intervals.
    if (body._procTimer) { clearInterval(body._procTimer); body._procTimer = null; }
    body.innerHTML = `
        <div style="display:flex;flex-direction:column;height:100%;background:var(--surface-0);">
            <div style="display:flex;justify-content:space-between;align-items:center;padding:16px 20px;border-bottom:1px solid var(--border);flex-shrink:0;">
                <h2 style="margin:0;font-size:18px;font-weight:700;color:white;">📋 Process Manager</h2>
                <div style="display:flex;gap:8px;align-items:center;">
                    <input type="text" id="procSearch" placeholder="🔍 Filter processes..."
                        style="padding:6px 12px;background:rgba(0,0,0,0.2);border:1px solid var(--border);border-radius:8px;color: var(--text-1);font-size:13px;width:180px;outline:none;transition:border-color 0.15s;"
                        onfocus="this.style.borderColor='#3584e4'" onblur="this.style.borderColor='rgba(255,255,255,0.08)'">
                    <button class="btn-ghost" id="procRefreshBtn" data-testid="proc-refresh-btn" style="font-size:12px;padding:5px 12px;" onclick="_refreshProcList(this.closest('.window-body'))">↻ Refresh</button>
                    <label title="Reload every 5 s" style="display:flex;align-items:center;gap:6px;font-size:12px;color: var(--text-3);cursor:pointer;user-select:none;padding:5px 10px;border:1px solid var(--border);border-radius:8px;">
                        <input type="checkbox" id="procAuto" data-testid="proc-auto-refresh" style="cursor:pointer;">
                        auto
                    </label>
                </div>
            </div>
            <div id="procSummary" style="padding:12px 20px;border-bottom:1px solid var(--border);flex-shrink:0;"></div>
            <div id="procTableWrap" style="flex:1;overflow:auto;padding:0;">
                ${skeletonRows(10)}
            </div>
        </div>`;
    // A real listener, not inline oninput: inline handlers run in global scope, so
    // `_procFilter = ...` created a NEW window global and the module's own
    // (shadowing) variable stayed '' — the filter silently never applied.
    const search = body.querySelector('#procSearch');
    if (search) {
        search.value = _procFilter;
        search.addEventListener('input', () => {
            _procFilter = search.value.toLowerCase();
            _refreshProcList(body);
        });
    }
    // Checkbox listener in JS, not inline: an inline `_procAutoRefresh = ...`
    // would set a window global while the module variable stayed false.
    const auto = body.querySelector('#procAuto');
    if (auto) {
        auto.checked = _procAutoRefresh;
        auto.addEventListener('change', () => {
            _procAutoRefresh = auto.checked;
            _refreshProcList(body);
        });
    }
    _refreshProcList(body);
}

async function _refreshProcList(body) {
    const wrap = body.querySelector('#procTableWrap');
    if (!wrap) return;
    wrap.innerHTML = skeletonRows(10);
    
    try {
        // The API only understands cpu|mem|name|pid; other columns are ordered here.
        const serverSort = ['cpu', 'mem', 'name', 'pid'].includes(_procSort) ? _procSort : 'mem';
        const r = await apiFetch(`/api/processes?sort=${serverSort}&limit=300`);
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const d = await r.json();
        const summaryEl = body.querySelector('#procSummary');
        if (summaryEl) summaryEl.innerHTML = _summaryHtml(d.system, d.groups || []);
        let procs = d.processes || [];
        
        // Apply client-side filter
        if (_procFilter) {
            procs = procs.filter(p =>
                (p.name && p.name.toLowerCase().includes(_procFilter)) ||
                (p.display_name && p.display_name.toLowerCase().includes(_procFilter)) ||
                (p.container && p.container.toLowerCase().includes(_procFilter)) ||
                (p.username && p.username.toLowerCase().includes(_procFilter)) ||
                String(p.pid).includes(_procFilter) ||
                (p.cmdline && p.cmdline.toLowerCase().includes(_procFilter))
            );
        }

        const dir = _procSortAsc ? 1 : -1;
        if (_procSort === 'threads') procs = [...procs].sort((a, b) => dir * ((a.threads || 0) - (b.threads || 0)));
        else if (_procSort === 'mem_pct') procs = [...procs].sort((a, b) => dir * ((a.memory_percent || 0) - (b.memory_percent || 0)));
        else if (_procSort === 'user') procs = [...procs].sort((a, b) => dir * String(a.username || '').localeCompare(String(b.username || '')));
        else if (_procSort === 'status') procs = [...procs].sort((a, b) => dir * String(a.status || '').localeCompare(String(b.status || '')));
        
        const cols = [
            { key: 'pid', label: 'PID' },
            { key: 'name', label: 'Process' },
            { key: 'user', label: 'User' },
            { key: 'cpu', label: 'CPU' },
            { key: 'mem', label: 'RAM' },
            { key: 'mem_pct', label: '%MEM' },
            { key: 'threads', label: 'Thr' },
            { key: 'status', label: 'Status' },
            { key: '_actions', label: '' }
        ];
        const thStyle = (key) => `cursor:pointer;user-select:none;padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;text-align:left;${_procSort === key ? 'color:#5b9aff;' : 'color: var(--text-3);'}`;
        const arrow = (key) => _procSort === key ? (_procSortAsc ? ' ↑' : ' ↓') : '';
        const countInfo = _procFilter ? `${procs.length} of ${d.processes?.length || 0}` : `${procs.length}`;

        if (procs.length === 0) {
            wrap.innerHTML = `
                <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color: var(--text-3);padding:40px;min-height:200px;">
                    <div style="font-size:48px;margin-bottom:12px;">📋</div>
                    <p style="font-size:16px;font-weight:600;color: var(--text-2);margin-bottom:4px;">${_procFilter ? 'No matching processes' : 'No processes found'}</p>
                    <p style="font-size:13px;">${_procFilter ? 'Try a different search term' : 'The process list is empty'}</p>
                </div>`;
            return;
        }

        let html = `<div style="padding:8px 20px;border-bottom:1px solid var(--border);font-size:12px;color: var(--text-3);display:flex;justify-content:space-between;">
            <span>${countInfo} process${procs.length !== 1 ? 'es' : ''}</span>
            <span style="color:#22c55e;">CPU: ${(procs.reduce((s,p) => s + (p.cpu_percent||0), 0)).toFixed(1)}%</span>
        </div>`;
        html += `<table class="app-table" data-testid="processes-table" style="width:100%;"><thead><tr>
            ${cols.map(c => c.key === '_actions' ? '<th style="padding:8px 10px;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color: var(--text-3);">Actions</th>' : `<th style="${thStyle(c.key)}" onclick="window._sortProcesses('${c.key}',this.closest('.window-body'))">${c.label}${arrow(c.key)}</th>`).join('')}
        </tr></thead><tbody>`;
        
        for (const p of procs) {
            const cpu = p.cpu_percent || 0;
            const cc = cpu > 50 ? '#ef4444' : cpu > 20 ? '#eab308' : 'var(--text-2)';
            const memMb = p.memory_rss_mb || 0;
            const tint = memMb >= CRITICAL_MB ? 'rgba(239,68,68,0.10)'
                : memMb >= HEAVY_MB ? 'rgba(234,179,8,0.08)' : '';
            const label = p.display_name || p.name;
            html += `<tr style="${tint ? `background:${tint};` : ''}">
                <td style="font-family:monospace;color: var(--text-3);padding:6px 10px;">${p.pid}</td>
                <td style="padding:6px 10px;max-width:260px;">
                    <div style="font-weight:600;color:white;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeAttr(p.cmdline || p.name)}">${escapeHtml(label)}</div>
                    ${p.container ? `<span style="font-size:10px;color:#5b9aff;" title="Docker container">🐳 ${escapeHtml(p.container)}</span>` : ''}
                </td>
                <td style="color: var(--text-3);font-size:12px;padding:6px 10px;white-space:nowrap;">${escapeHtml(p.username || '—')}</td>
                <td style="color:${cc};font-weight:600;padding:6px 10px;white-space:nowrap;">${cpu.toFixed(1)}%</td>
                <td style="padding:6px 10px;white-space:nowrap;font-weight:${memMb >= HEAVY_MB ? 700 : 400};color:${memMb >= HEAVY_MB ? '#eab308' : 'var(--text-1)'};">${_mb(memMb)}</td>
                <td style="padding:6px 10px;color: var(--text-2);white-space:nowrap;">${(p.memory_percent || 0).toFixed(1)}%</td>
                <td style="padding:6px 10px;color: var(--text-3);">${p.threads || 0}</td>
                <td style="padding:6px 10px;">${_statusBadge(p.status)}</td>
                <td style="padding:6px 10px;"><button class="btn-ghost" style="font-size:11px;padding:3px 8px;color:#ef4444;" data-testid="proc-kill-${p.pid}" onclick="window._killProcess(${p.pid},this.closest('.window-body'))">Kill</button></td>
            </tr>`;
        }
        html += '</tbody></table>';
        wrap.innerHTML = html;
    } catch (e) {
        wrap.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:#ef4444;padding:40px;"><div style="font-size:32px;margin-bottom:8px;">❌</div><p>${escapeHtml(e.message)}</p></div>`;
    }

    // Auto-refresh through the shared poller: pauses in a hidden tab, skips ticks
    // while a fetch is in flight, and stops with the window (TODO.md P3-09).
    if (body._procStop) { body._procStop(); body._procStop = null; }
    if (_procAutoRefresh) {
        body._procStop = poll(() => _refreshProcList(body), AUTO_REFRESH_MS,
                              { appId: 'processes', immediate: false, name: 'processes' });
    }
}

window._sortProcesses = function(col, bodyEl) {
    if (_procSort === col) _procSortAsc = !_procSortAsc;
    else { _procSort = col; _procSortAsc = (col === 'name' || col === 'user'); }
    _refreshProcList(bodyEl);
};

async function _killProcess(pid, bodyEl) {
    if (!await window.showModalConfirm('Kill Process', `Kill process PID ${pid}?`)) return;
    try {
        const r = await apiFetch(`/api/processes/${pid}`, { method: 'DELETE' });
        if (r.status === 404) {
            if (window.showToast) window.showToast('error', 'Process not found');
            else window.showAlert('Process not found');
        } else if (r.status === 400) {
            const d = await r.json().catch(() => ({}));
            if (window.showToast) window.showToast('error', d.detail || 'Cannot terminate process');
            else window.showAlert(d.detail || 'Cannot terminate system process');
        } else if (!r.ok) throw new Error(`HTTP ${r.status}`);
        else {
            if (window.showToast) window.showToast('success', `Process ${pid} terminated`);
        }
        _refreshProcList(bodyEl);
    } catch(e) {
        if (window.showToast) window.showToast('error', e.message);
    }
}

window._killProcess = _killProcess;
window.renderProcessesPage = renderProcessesPage;
// Used by the search box and the ↻ Refresh button, both inline onclick/oninput
// attributes — without this the button and filtering were dead (ReferenceError).
window._refreshProcList = _refreshProcList;
