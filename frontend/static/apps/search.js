// EthOS NAS — Universal Search (single-pane shell)
// Cross-volume file search: GET /api/files/universal-search, scopes from
// /api/files/search-roots.
import { escapeAttr, escapeHtml, formatBytes, getFileIcon } from '../core/utils.js';
import { apiRequest, getToken } from '../core/api.js';
import { errorState } from '../core/states.js';
import { uiIcon } from '../core/icons.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';
import { fmOpenInNewWindow } from './files.js';

const TYPE_CHIPS = [
    { id: 'all', label: 'All' },
    { id: 'images', label: 'Images' },
    { id: 'video', label: 'Video' },
    { id: 'audio', label: 'Audio' },
    { id: 'docs', label: 'Docs' },
    { id: 'archives', label: 'Archives' },
    { id: 'code', label: 'Code' },
];

const state = {
    roots: [],
    scope: 'auto',
    type: 'all',
    minSizeMb: 0,
    query: '',
    results: null,
    elapsedMs: 0,
};

let shell = null;
let searchTimer = null;

export async function renderSearchPage(body) {
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'search',
        title: 'Universal Search',
        subtitle: 'Find files across every mounted volume.',
        icon: '🔍',
        sections: [{ id: 'search', label: 'Search' }],
        rail: false,
        note: 'Search walks mounted roots in parallel and reports truncation.',
    });

    shell.view.innerHTML = `
        <div class="ui-toolbar">
            <div class="ui-row" style="flex:1;">
                <input id="usQuery" class="ui-input" data-testid="us-query-input" type="search" autocomplete="off" spellcheck="false"
                       placeholder="Search files across all volumes…" aria-label="Search files" value="${escapeAttr(state.query)}">
                <button id="usGoBtn" class="ui-btn primary" data-testid="us-search-btn">${uiIcon('folder', 12)} Search</button>
            </div>
        </div>
        <div class="ui-toolbar">
            <div class="ui-row">
                ${TYPE_CHIPS.map((chip) => `<button class="ui-btn small${state.type === chip.id ? ' primary' : ''}"
                    data-type="${chip.id}" data-testid="us-chip-${chip.id}">${chip.label}</button>`).join('')}
            </div>
            <div class="ui-row">
                <label class="ui-field" style="flex-direction:row;align-items:center;gap:6px;">
                    <span class="ui-label">Min size (MB)</span>
                    <input id="usMinSize" class="ui-input" data-testid="us-minsize-input" type="number" min="0" step="1"
                           value="${state.minSizeMb}" style="width:80px;">
                </label>
                <select aria-label="Search scope" id="usScope" class="ui-select" data-testid="us-scope-select" style="width:auto;">
                    <option value="auto"${state.scope === 'auto' ? ' selected' : ''}>All volumes</option>
                </select>
            </div>
        </div>
        <div id="usResults" data-testid="us-results">
            ${state.results ? renderResults() : '<div class="ui-empty">Type a name (or part of it) and press Enter.<br>Search runs across all mounted volumes in parallel.</div>'}
        </div>`;

    const input = shell.view.querySelector('#usQuery');
    input.focus();
    input.addEventListener('keydown', (event) => { if (event.key === 'Enter') doSearch(); });
    input.addEventListener('input', () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { if ((input.value || '').trim().length >= 2) doSearch(); }, 500);
    });
    shell.view.querySelector('#usGoBtn')?.addEventListener('click', () => doSearch());

    const scopeSelect = shell.view.querySelector('#usScope');
    _appendRoots(scopeSelect);
    scopeSelect.addEventListener('change', () => {
        state.scope = scopeSelect.value;
        doSearch();
    });

    shell.view.querySelectorAll('[data-type]').forEach((chip) => {
        chip.addEventListener('click', () => {
            if (state.type === chip.dataset.type) return;
            state.type = chip.dataset.type;
            shell.view.querySelectorAll('[data-type]').forEach((other) => {
                other.classList.toggle('primary', other.dataset.type === state.type);
            });
            if (state.query.trim()) doSearch();
        });
    });

    shell.view.querySelector('#usResults')?.addEventListener('click', (event) => {
        if (event.target.closest('[data-download]')) return;
        const row = event.target.closest('.ui-result-row[data-open]');
        if (row) openAt(row.dataset.open);
    });

    await loadRoots(scopeSelect);
    shell.setStatus('Ready', 'ok');
}

function _appendRoots(select) {
    for (const root of state.roots) {
        if ([...select.options].some((option) => option.value === root.path)) continue;
        const option = document.createElement('option');
        option.value = root.path;
        option.textContent = root.label || root.path;
        if (state.scope === root.path) option.selected = true;
        select.appendChild(option);
    }
}

async function loadRoots(select) {
    try {
        const data = await apiRequest('/api/files/search-roots');
        if (!shell?.root.isConnected) return;
        state.roots = data.roots || [];
        _appendRoots(select);
    } catch (err) {
        // Non-fatal: automatic scopes still work server-side.
        logError('Search: search roots unavailable', err?.detail || err?.message);
    }
}

async function doSearch() {
    if (!shell) return;
    const input = shell.view.querySelector('#usQuery');
    const query = (input?.value || '').trim();
    if (!query) return;
    state.query = query;
    state.minSizeMb = Math.max(0, parseFloat(shell.view.querySelector('#usMinSize')?.value || '0') || 0);

    const resultsEl = shell.view.querySelector('#usResults');
    if (resultsEl) resultsEl.innerHTML = '<div class="ui-empty">Searching all volumes…</div>';
    shell.setStatus('Searching…');

    const params = new URLSearchParams({ q: query, type: state.type });
    if (state.scope !== 'auto') params.set('roots', state.scope);
    if (state.minSizeMb > 0) params.set('min_size_mb', String(state.minSizeMb));

    const started = performance.now();
    try {
        state.results = await apiRequest(`/api/files/universal-search?${params.toString()}`);
        state.elapsedMs = Math.round(performance.now() - started);
        if (!shell.root.isConnected) return;
        shell.view.querySelector('#usResults').innerHTML = renderResults();
        shell.setStatus(`${state.results?.total ?? 0} result(s) in ${(state.elapsedMs / 1000).toFixed(1)}s`, 'ok');
    } catch (err) {
        state.results = null;
        if (!shell.root.isConnected) return;
        shell.view.querySelector('#usResults').innerHTML = errorState({ detail: err?.detail || err?.message || 'Search failed' });
        shell.setStatus('Search failed', 'error');
        logError('Search: request failed', err?.detail || err?.message);
    }
}

function renderResults() {
    const result = state.results;
    if (!result) return '<div class="ui-empty">No results yet.</div>';
    if (result.total === 0) {
        return `<div class="ui-empty">No files matching “${escapeHtml(result.query)}” in the selected scope.</div>`;
    }

    const skipped = (result.skipped_roots || []).map((entry) => escapeHtml(entry.root)).join(', ');
    let html = `<div class="ui-toolbar">
        <p class="ui-muted">${result.total} result${result.total === 1 ? '' : 's'} for “${escapeHtml(result.query)}”
            in ${(state.elapsedMs / 1000).toFixed(1)}s across ${(result.roots || []).length} scope${(result.roots || []).length === 1 ? '' : 's'}
            ${result.truncated_any ? ' · some scopes hit the per-root limit' : ''}</p>
    </div>`;
    if (skipped) html += `<div class="ui-callout">Skipped unavailable scope(s): ${skipped}</div>`;

    const groups = new Map();
    for (const stat of result.roots || []) {
        if (stat.count > 0) groups.set(stat.root, { stat, items: [] });
    }
    for (const item of result.results || []) {
        let group = groups.get(item.root);
        if (!group) { group = { stat: { root: item.root, count: 0, truncated: false }, items: [] }; groups.set(item.root, group); }
        group.items.push(item);
    }

    for (const [root, group] of groups) {
        html += `<section class="ui-panel">
            <div class="ui-toolbar">
                <h2 class="path">${escapeHtml(root)}</h2>
                <span class="ui-tag">${group.stat.count} result${group.stat.count === 1 ? '' : 's'}${group.stat.truncated ? ' · truncated' : ''}</span>
            </div>
            <div class="ui-panel-body ui-list">${group.items.map(renderRow).join('')}</div>
        </section>`;
    }
    return html;
}

function renderRow(item) {
    const isDir = Boolean(item.is_dir);
    const icon = getFileIcon(item.name, isDir ? 'dir' : 'file');
    const parent = item.path.includes('/') ? item.path.substring(0, item.path.lastIndexOf('/')) || '/' : '/';
    const openPath = isDir ? item.path : parent;
    const downloadUrl = isDir ? '' : `/api/files/download?path=${encodeURIComponent(item.path)}&token=${encodeURIComponent(getToken() || '')}`;
    return `<div class="ui-result-row" data-open="${escapeAttr(openPath)}" data-testid="us-result-row">
        <span class="ui-result-icon">${icon}</span>
        <div style="flex:1;min-width:0;">
            <div style="font-weight:500;">${escapeHtml(item.name)}</div>
            <p class="ui-muted path">${escapeHtml(isDir ? item.path : parent)}</p>
        </div>
        ${isDir ? '' : `<span class="ui-tag">${escapeHtml(formatBytes(item.size || 0))}</span>`}
        <span class="ui-muted">${escapeHtml(shortDate(item.modified))}</span>
        ${isDir ? '' : `<a href="${escapeAttr(downloadUrl)}" download data-download class="ui-btn small" title="Download">⬇</a>`}
    </div>`;
}

function shortDate(ts) {
    if (!ts) return '';
    try {
        return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
    } catch (err) {
        logError('Search: could not format a date', err?.message || String(err));
        return '';
    }
}

function openAt(path) {
    if (!path) return;
    if (typeof fmOpenInNewWindow === 'function') { fmOpenInNewWindow(path); return; }
    if (typeof window.openApp === 'function') window.openApp('files');
    setTimeout(() => { if (typeof window.fmNavigateTo === 'function') window.fmNavigateTo(path); }, 350);
}
