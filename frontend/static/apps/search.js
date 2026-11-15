// EthOS NAS — Universal Search App
// Global, cross-volume file search. Backend: GET /api/files/universal-search
// (parallel multi-root walk) + GET /api/files/search-roots for scopes.

import { escapeAttr, escapeHtml, formatBytes, getFileIcon } from '../core/utils.js';
import { apiFetch, getToken } from '../core/api.js';
import { fmOpenInNewWindow } from './files.js';

const TYPE_CHIPS = [
    { id: 'all',      label: 'All' },
    { id: 'images',   label: 'Images' },
    { id: 'video',    label: 'Video' },
    { id: 'audio',    label: 'Audio' },
    { id: 'docs',     label: 'Docs' },
    { id: 'archives', label: 'Archives' },
    { id: 'code',     label: 'Code' },
];

const _state = {
    roots: [],        // [{path, label}] from /api/files/search-roots
    scope: 'auto',    // 'auto' or a specific root path
    type: 'all',
    minSizeMb: 0,
    query: '',
    results: null,    // last response payload
    elapsedMs: 0,
};

let _searchTimer = null;

export async function renderSearchPage(body) {
    body.innerHTML = `
        <div style="display:flex;flex-direction:column;height:100%;background:var(--surface-0);color: var(--text-1);font-family:system-ui,sans-serif;">
            <div style="padding:14px 18px 10px;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--surface-0);">
                <div style="display:flex;gap:8px;align-items:center;">
                    <input id="usQuery" data-testid="us-query-input" type="search" placeholder="Search files across all volumes…"
                        autocomplete="off" spellcheck="false" value="${escapeAttr(_state.query)}"
                        style="flex:1;padding:9px 14px;border-radius:10px;border:1.5px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:14px;outline:none;" />
                    <button id="usGoBtn" class="btn-primary" data-testid="us-search-btn" style="padding:9px 18px;">🔍 Search</button>
                </div>
                <div style="display:flex;gap:6px;align-items:center;margin-top:10px;flex-wrap:wrap;">
                    ${TYPE_CHIPS.map(c => `
                        <button class="us-chip" data-type="${c.id}" data-testid="us-chip-${c.id}"
                            style="padding:4px 12px;border-radius:999px;font-size:12px;cursor:pointer;border:1px solid ${_state.type === c.id ? 'var(--accent)' : 'var(--border)'};background:${_state.type === c.id ? 'var(--accent)' : 'transparent'};color:${_state.type === c.id ? 'white' : 'var(--text-2)'};">${c.label}</button>`).join('')}
                    <div style="margin-left:auto;display:flex;gap:8px;align-items:center;">
                        <label style="font-size:12px;color: var(--text-3);display:flex;align-items:center;gap:6px;">Min size (MB)
                            <input id="usMinSize" data-testid="us-minsize-input" type="number" min="0" step="1" value="${_state.minSizeMb}"
                                style="width:70px;padding:4px 8px;border-radius:6px;border:1px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:12px;outline:none;" />
                        </label>
                        <select aria-label="Usscope" id="usScope" data-testid="us-scope-select" title="Search scope"
                            style="padding:5px 8px;border-radius:8px;border:1px solid var(--border);background:var(--surface-1);color:var(--text-1);font-size:12px;outline:none;">
                            <option value="auto"${_state.scope === 'auto' ? ' selected' : ''}>All volumes</option>
                        </select>
                    </div>
                </div>
            </div>
            <div id="usResults" style="flex:1;overflow-y:auto;padding:14px 18px;">
                ${_state.results ? renderResults() : '<div style="color: var(--text-3);text-align:center;padding:60px 20px;font-size:13px;">Type a name (or part of it) and press Enter.<br>Search runs across all mounted volumes in parallel.</div>'}
            </div>
        </div>`;

    const input = body.querySelector('#usQuery');
    input.focus();
    input.addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
    input.addEventListener('input', () => {
        clearTimeout(_searchTimer);
        _searchTimer = setTimeout(() => { if ((input.value || '').trim().length >= 2) doSearch(); }, 500);
    });
    body.querySelector('#usGoBtn').addEventListener('click', doSearch);

    const scopeSel = body.querySelector('#usScope');
    for (const r of _state.roots) {
        if ([...scopeSel.options].some(o => o.value === r.path)) continue;
        const opt = document.createElement('option');
        opt.value = r.path;
        opt.textContent = r.label || r.path;
        if (_state.scope === r.path) opt.selected = true;
        scopeSel.appendChild(opt);
    }
    scopeSel.addEventListener('change', () => { _state.scope = scopeSel.value; doSearch(); });

    body.querySelectorAll('.us-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            if (_state.type === chip.dataset.type) return;
            _state.type = chip.dataset.type;
            // Restyle chips in place (no full re-render — keeps input focus).
            body.querySelectorAll('.us-chip').forEach(c => {
                const active = c.dataset.type === _state.type;
                c.style.borderColor = active ? 'var(--accent)' : 'var(--border)';
                c.style.background = active ? 'var(--accent)' : 'transparent';
                c.style.color = active ? 'white' : 'var(--text-2)';
            });
            if (_state.query.trim()) doSearch();
        });
    });

    // Delegated row clicks — paths live in data attributes, never inline handlers.
    const resultsEl = body.querySelector('#usResults');
    resultsEl.addEventListener('click', e => {
        const row = e.target.closest('.us-row[data-open]');
        if (!row) return;
        _openAt(row.dataset.open);
    });

    await loadRoots(scopeSel);
}

async function loadRoots(scopeSel) {
    try {
        const resp = await apiFetch('/api/files/search-roots');
        if (!resp.ok) return;
        const data = await resp.json();
        _state.roots = data.roots || [];
        for (const r of _state.roots) {
            if ([...scopeSel.options].some(o => o.value === r.path)) continue;
            const opt = document.createElement('option');
            opt.value = r.path;
            opt.textContent = r.label || r.path;
            if (_state.scope === r.path) opt.selected = true;
            scopeSel.appendChild(opt);
        }
    } catch (_) { /* non-fatal — auto scopes still work server-side */ }
}

async function doSearch() {
    const input = document.getElementById('usQuery');
    const q = (input?.value || '').trim();
    if (!q) return;
    _state.query = q;
    const minEl = document.getElementById('usMinSize');
    _state.minSizeMb = Math.max(0, parseFloat(minEl?.value || '0') || 0);

    const resultsEl = document.getElementById('usResults');
    if (resultsEl) resultsEl.innerHTML = '<div style="color: var(--text-3);text-align:center;padding:60px;font-size:13px;">Searching all volumes…</div>';

    const params = new URLSearchParams({ q, type: _state.type });
    if (_state.scope !== 'auto') params.set('roots', _state.scope);
    if (_state.minSizeMb > 0) params.set('min_size_mb', String(_state.minSizeMb));

    const t0 = performance.now();
    try {
        const resp = await apiFetch(`/api/files/universal-search?${params.toString()}`);
        _state.elapsedMs = Math.round(performance.now() - t0);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        _state.results = await resp.json();
    } catch (e) {
        _state.results = null;
        const el2 = document.getElementById('usResults');
        if (el2) el2.innerHTML = `<div style="color:#ef4444;text-align:center;padding:60px;font-size:13px;">Search failed: ${escapeHtml(e.message)}</div>`;
    }

    const el3 = document.getElementById('usResults');
    if (el3) el3.innerHTML = renderResults();
}

function renderResults() {
    const r = _state.results;
    if (!r) return '<div style="color: var(--text-3);text-align:center;padding:60px;font-size:13px;">No results yet.</div>';

    if (r.total === 0) {
        return `<div style="color: var(--text-3);text-align:center;padding:60px;font-size:13px;">No files matching “${escapeHtml(r.query)}” in the selected scope.</div>`;
    }

    const skipped = (r.skipped_roots || []).map(s => escapeHtml(s.root)).join(', ');
    let html = `
        <div style="font-size:12px;color: var(--text-3);margin-bottom:10px;">
            ${r.total} result${r.total !== 1 ? 's' : ''} for “${escapeHtml(r.query)}” in ${(_state.elapsedMs / 1000).toFixed(1)}s across ${(r.roots || []).length} scope${(r.roots || []).length !== 1 ? 's' : ''}
            ${r.truncated_any ? ' · <span style="color:#eab308;">some scopes truncated at the per-root limit</span>' : ''}
        </div>`;
    if (skipped) {
        html += `<div style="font-size:12px;color:#eab308;margin-bottom:10px;">⚠ Skipped unavailable scope(s): ${skipped}</div>`;
    }

    // Group results by root, preserving the server's per-root stats order.
    const groups = new Map();
    for (const stat of r.roots || []) {
        if (stat.count > 0) groups.set(stat.root, { stat, items: [] });
    }
    for (const item of r.results || []) {
        let g = groups.get(item.root);
        if (!g) { g = { stat: { root: item.root, count: 0, truncated: false }, items: [] }; groups.set(item.root, g); }
        g.items.push(item);
    }

    for (const [root, g] of groups) {
        html += `
            <div style="margin-bottom:14px;">
                <div style="display:flex;align-items:center;gap:8px;padding:6px 10px;border-radius:8px;background:var(--surface-1);font-size:12px;font-weight:600;color: var(--text-2);margin-bottom:4px;">
                    <span>📁</span><span style="font-family:monospace;">${escapeHtml(root)}</span>
                    <span style="color: var(--text-3);font-weight:400;">${g.stat.count} result${g.stat.count !== 1 ? 's' : ''}${g.stat.truncated ? ' (truncated)' : ''}</span>
                </div>`;
        for (const item of g.items) html += renderRow(item);
        html += '</div>';
    }
    return html;
}

function renderRow(item) {
    const isDir = !!item.is_dir;
    const icon = getFileIcon(item.name, isDir ? 'dir' : 'file');
    const parent = item.path.includes('/') ? item.path.substring(0, item.path.lastIndexOf('/')) || '/' : '/';
    const openPath = isDir ? item.path : parent;
    const dlUrl = isDir ? '' : `/api/files/download?path=${encodeURIComponent(item.path)}&token=${encodeURIComponent(getToken() || '')}`;
    return `
        <div class="us-row" data-open="${escapeAttr(openPath)}" data-testid="us-result-row" style="display:flex;align-items:center;gap:10px;padding:7px 10px;border-radius:8px;cursor:pointer;transition:background .12s;"
             onmouseenter="this.style.background='var(--surface-1)'" onmouseleave="this.style.background=''">
            <span style="width:26px;height:26px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;">${icon}</span>
            <div style="flex:1;min-width:0;">
                <div style="font-size:13px;font-weight:500;color:white;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(item.name)}</div>
                <div style="font-size:11px;color: var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:monospace;">${escapeHtml(isDir ? item.path : parent)}</div>
            </div>
            ${isDir ? '' : `<span style="font-size:12px;color: var(--text-2);flex-shrink:0;width:70px;text-align:right;">${formatBytes(item.size || 0)}</span>`}
            <span style="font-size:11px;color: var(--text-3);flex-shrink:0;width:90px;text-align:right;">${_shortDate(item.modified)}</span>
            ${isDir ? '' : `<a href="${dlUrl}" download title="Download" onclick="event.stopPropagation()" style="color: var(--text-2);font-size:15px;flex-shrink:0;padding:2px 6px;border-radius:6px;text-decoration:none;">⬇</a>`}
        </div>`;
}

function _shortDate(ts) {
    if (!ts) return '';
    try { return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' }); } catch (_) { return ''; }
}

// Deep-link into Files at the given path (new window, deterministic target).
function _openAt(path) {
    if (!path) return;
    if (typeof fmOpenInNewWindow === 'function') { fmOpenInNewWindow(path); return; }
    // Fallback for builds where the files.js export is unavailable.
    if (typeof window.openApp === 'function') window.openApp('files');
    setTimeout(() => { if (typeof window.fmNavigateTo === 'function') window.fmNavigateTo(path); }, 350);
}
