// EthOS NAS — Logs: one viewer for every diagnostic source (docs/logs.md).
//
// Sources: the unified event store (core / browser / pkg:<id>), host log files,
// journald and Docker containers (grouped by package). History comes from the
// REST API, live updates from /ws/logs via core/log-stream.js. Apps and packages
// never build their own log UI — they call openLogs({ app / path / unit /
// container }) and this module owns the presentation.

import { escapeAttr, escapeHtml, formatBytes } from '../core/utils.js';
import { apiJsonFetch } from '../core/api.js';
import { showConfirm } from '../core/modal.js';
import { emptyState, errorState } from '../core/states.js';
import { openWindows, focusWindow } from '../core/window-manager.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';
import {
    fetchLogs, fetchLogApps, fetchLogStats, fetchLogFiles, fetchLogContainers,
    fetchJournal, tailLogFile, clearLogs, streamLogs,
} from '../core/log-stream.js';

const MAX_ROWS = 2000;
const DEFAULT_TAIL = 200;
const LEVEL_SEVERITY = { debug: 10, info: 20, warning: 30, error: 40 };
const LEVEL_FILTERS = ['', 'info', 'warning', 'error'];
const STATE = new WeakMap();
let _pendingFilter = null;

const PLACEHOLDER = {
    app: 'Waiting for events… Backend records, browser errors and package messages appear here.',
    file: 'Select a log file to stream it.',
    journal: 'Select the journal to stream it.',
    container: 'Select a container to stream it.',
};

// ── public entry points ─────────────────────────────────────────────────────

export async function renderLogsPage(body) {
    _dispose(body);
    const filter = _pendingFilter || {};
    _pendingFilter = null;

    const shell = _buildShell(body);
    const state = {
        body,
        shell,
        isAdmin: false,
        selection: { kind: 'app', app: '' },
        level: '',
        search: '',
        live: true,
        stream: null,
        entries: [],
        lines: [],
        rows: 0,
        counts: {},
        stats: null,
        apps: [],
        onBodyClick: null,
        onBodyInput: null,
    };
    STATE.set(body, state);
    _wireShell(state);

    try {
        const me = await apiJsonFetch('/api/auth/me');
        state.isAdmin = String(me?.role || '') === 'admin';
    } catch (_) {
        state.isAdmin = false;
    }
    if (!state.isAdmin) {
        const rail = body.querySelector('.logs-rail-body');
        if (rail) rail.innerHTML = '<div class="logs-empty">Admin only</div>';
        const list = body.querySelector('[data-testid="logs-list"]');
        if (list) list.innerHTML = errorState({
            detail: 'Logs require an administrator account — the event store and host logs are diagnostic data.',
            retry: false,
        });
        const clearBtn = body.querySelector('[data-action="clear"]');
        if (clearBtn) clearBtn.remove();
        state.shell.setPill('offline', 'Admin only');
        return;
    }

    await _loadTree(state);
    const initial = _selectionFromFilter(filter);
    await _select(state, initial);
}

/** Open the Logs window (or re-target an open one) from another app. */
export function openLogs(filter = {}) {
    _pendingFilter = filter;
    const existing = [...openWindows.values()].find((w) => w.appId === 'logs');
    if (existing) {
        const body = existing.element.querySelector('.window-body');
        if (body) renderLogsPage(body);
        focusWindow(existing.element);
        return;
    }
    if (typeof window.openApp === 'function') window.openApp('logs');
}

// ── shell ───────────────────────────────────────────────────────────────────

function _buildShell(body) {
    const levelButtons = LEVEL_FILTERS.map((level) => {
        const label = level ? level[0].toUpperCase() + level.slice(1) : 'All';
        return `<button class="logs-chip${level === '' ? ' active' : ''}" data-action="level" data-level="${level}" data-testid="logs-level-${level || 'all'}">${label}</button>`;
    }).join('');

    const railBody = document.createElement('div');
    railBody.className = 'logs-rail-body';
    railBody.setAttribute('data-testid', 'logs-rail-body');
    railBody.innerHTML = '<div class="logs-empty">Loading sources…</div>';

    const shell = mountShell(body, {
        idPrefix: 'logs',
        title: 'Logs',
        subtitle: 'One viewer for every diagnostic source.',
        icon: '📜',
        sections: [],
        rail: true,
        railContent: railBody,
        header: false,
        flush: true,
        note: 'Admin only — diagnostics can contain credentials.',
    });

    shell.view.innerHTML = `
        <div class="logs-toolbar" data-testid="logs-toolbar">
            <span>${levelButtons}</span>
            <input class="logs-input" data-testid="logs-search" data-action="search" type="search"
                   placeholder="Filter (message, source…)" aria-label="Filter logs">
            <label class="ui-check"><input type="checkbox" data-action="live" checked> Live</label>
            <button class="logs-chip" data-action="pause" data-testid="logs-pause">Pause</button>
            <button class="logs-chip" data-action="export" data-testid="logs-export">Export</button>
            <button class="logs-chip" data-action="clear" data-testid="logs-clear">Clear</button>
        </div>
        <div class="logs-list" data-testid="logs-list">${emptyState({ title: 'Loading logs…' })}</div>
        <div class="logs-meta" data-testid="logs-meta"></div>`;
    shell.setPill('starting', 'Loading…');
    shell.rail?.setAttribute('data-testid', 'logs-rail');
    return shell;
}

function _dispose(body) {
    const state = STATE.get(body);
    if (!state) return;
    try { state.stream?.close(); } catch (_) { /* already closed */ }
    if (state.onBodyClick) body.removeEventListener('click', state.onBodyClick);
    if (state.onBodyInput) body.removeEventListener('input', state.onBodyInput);
    state.shell?.destroy();
    STATE.delete(body);
}

function _wireShell(state) {
    const { body } = state;
    state.onBodyClick = (event) => {
        const btn = event.target.closest('[data-action]');
        if (!btn || !body.contains(btn)) return;
        const action = btn.dataset.action;
        if (action === 'level') {
            state.level = btn.dataset.level || '';
            body.querySelectorAll('[data-action="level"]').forEach((el) => el.classList.toggle('active', el === btn));
            _reload(state);
        } else if (action === 'pause') {
            state.live = !state.live;
            btn.textContent = state.live ? 'Pause' : 'Resume';
            btn.classList.toggle('active', !state.live);
            _reload(state);
        } else if (action === 'export') {
            _export(state);
        } else if (action === 'clear') {
            _clear(state);
        } else if (action === 'select') {
            _select(state, {
                kind: btn.dataset.kind || 'app',
                app: btn.dataset.app || '',
                path: btn.dataset.path || '',
                unit: btn.dataset.unit || '',
                container: btn.dataset.container || '',
            });
        }
    };
    body.addEventListener('click', state.onBodyClick);
    state.onBodyInput = (event) => {
        const input = event.target.closest('[data-action="search"]');
        if (!input) return;
        clearTimeout(state.searchTimer);
        state.searchTimer = setTimeout(() => {
            state.search = input.value.trim();
            if (state.selection.kind === 'app') _reload(state);
            else _applyLineFilter(state);
        }, 300);
    };
    body.addEventListener('input', state.onBodyInput);
    const live = body.querySelector('[data-action="live"]');
    if (live) live.addEventListener('change', () => {
        state.live = live.checked;
        _reload(state);
    });
}

// ── source tree ─────────────────────────────────────────────────────────────

function _selectionFromFilter(filter) {
    if (filter.container) return { kind: 'container', container: filter.container };
    if (filter.path) return { kind: 'file', path: filter.path };
    if (filter.unit) return { kind: 'journal', unit: filter.unit };
    if (filter.source && String(filter.source).startsWith('container:')) {
        return { kind: 'container', container: String(filter.source).slice('container:'.length) };
    }
    if (filter.source && String(filter.source).startsWith('file:')) {
        return { kind: 'file', path: String(filter.source).slice('file:'.length) };
    }
    return { kind: 'app', app: filter.app || '' };
}

async function _loadTree(state) {
    const [apps, files, containers] = await Promise.all([
        fetchLogApps().catch(() => ({ apps: [] })),
        fetchLogFiles().catch(() => ({ files: [] })),
        fetchLogContainers().catch(() => ({ containers: [] })),
    ]);
    state.apps = apps.apps || [];
    const rail = state.body.querySelector('.logs-rail-body');
    if (!rail) return;

    const appButtons = [
        { app: '', label: 'All logs' },
        { app: 'browser', label: 'Browser' },
        { app: 'core', label: 'EthOS core' },
        ...state.apps
            .filter((item) => item.app.startsWith('pkg:'))
            .map((item) => ({ app: item.app, label: item.app.slice(4), count: item.count })),
    ];

    const section = (title, html) => `<h4>${escapeHtml(title)}</h4>${html}`;
    const appHtml = appButtons.map((item) => {
        const count = item.count ?? state.apps.find((entry) => entry.app === (item.app || 'core'))?.count;
        const badge = count ? `<span class="logs-badge">${count}</span>` : '';
        return `<button class="logs-src-btn" data-action="select" data-kind="app" data-app="${escapeAttr(item.app)}">${escapeHtml(item.label)}${badge}</button>`;
    }).join('');

    const fileHtml = (files.files || []).map((file) =>
        `<button class="logs-src-btn" data-action="select" data-kind="file" data-path="${escapeAttr(file.path)}" title="${escapeAttr(file.path)}">${escapeHtml(file.label)} <span class="logs-badge">${formatBytes(file.size || 0)}</span></button>`
    ).join('') || '<div class="logs-empty">No readable files</div>';

    const containerHtml = (containers.containers || []).map((container) => {
        const label = container.package_id ? `${container.package_id} · ${container.name}` : container.name;
        return `<button class="logs-src-btn" data-action="select" data-kind="container" data-container="${escapeAttr(container.name)}" title="${escapeAttr(container.name)}">${escapeHtml(label)}</button>`;
    }).join('') || '<div class="logs-empty">Docker is not reachable</div>';

    const journalHtml = `
        <button class="logs-src-btn" data-action="select" data-kind="journal" data-unit="">All units</button>
        <button class="logs-src-btn" data-action="select" data-kind="journal" data-unit="ethos-nas">ethos-nas</button>`;

    rail.innerHTML =
        section('Applications', appHtml) +
        section('System files', fileHtml) +
        section('Journal (systemd)', journalHtml) +
        section('Containers', containerHtml);

    _markActive(state);
}

function _markActive(state) {
    const { selection } = state;
    state.body.querySelectorAll('.logs-src-btn').forEach((btn) => {
        const match =
            (selection.kind === 'app' && btn.dataset.kind === 'app' && (btn.dataset.app || '') === (selection.app || '')) ||
            (selection.kind === 'file' && btn.dataset.kind === 'file' && btn.dataset.path === selection.path) ||
            (selection.kind === 'journal' && btn.dataset.kind === 'journal' && (btn.dataset.unit || '') === (selection.unit || '')) ||
            (selection.kind === 'container' && btn.dataset.kind === 'container' && btn.dataset.container === selection.container);
        btn.classList.toggle('active', Boolean(match));
    });
}

// ── loading / streaming ─────────────────────────────────────────────────────

async function _select(state, selection) {
    state.selection = selection;
    _markActive(state);
    const search = state.body.querySelector('[data-action="search"]');
    if (search) search.value = state.search;
    await _reload(state);
}

async function _reload(state) {
    try { state.stream?.close(); } catch (_) { /* noop */ }
    state.stream = null;
    state.rows = 0;
    state.lines = [];
    state.entries = [];
    const list = state.body.querySelector('[data-testid="logs-list"]');
    if (!list) return;
    list.innerHTML = `<div class="logs-empty">Loading…</div>`;
    await _loadHistory(state, list);
    if (state.live) _subscribe(state);
    await _refreshMeta(state);
}

async function _loadHistory(state, list) {
    const { selection } = state;
    try {
        if (selection.kind === 'app') {
            const data = await fetchLogs({
                limit: 500,
                level: state.level,
                app: selection.app || '',
                search: state.search,
            });
            state.entries = [];
            const entries = data.entries || [];
            list.replaceChildren();
            if (!entries.length) {
                list.innerHTML = emptyState({ title: 'No events yet', hint: PLACEHOLDER.app });
                return;
            }
            for (const entry of entries) _appendEntry(state, entry, list, false);
        } else if (selection.kind === 'file') {
            const data = await tailLogFile(selection.path, DEFAULT_TAIL);
            list.replaceChildren();
            for (const line of data.lines || []) _appendLine(state, line, `file:${selection.path}`, list);
        } else if (selection.kind === 'journal') {
            const data = await fetchJournal(DEFAULT_TAIL, selection.unit || '');
            list.replaceChildren();
            for (const line of data.lines || []) _appendLine(state, line, `journal:${selection.unit || 'all'}`, list);
        } else {
            // Containers replay their tail over the socket on connect.
            list.replaceChildren();
            list.innerHTML = `<div class="logs-empty">${escapeHtml(PLACEHOLDER.container)}</div>`;
        }
    } catch (err) {
        list.innerHTML = errorState({
            detail: err?.detail || err?.message || 'Could not load logs',
            retry: true,
        });
        const retry = list.querySelector('[data-state-action="retry"]');
        if (retry) retry.addEventListener('click', () => _reload(state));
    }
}

function _subscribe(state) {
    const { selection } = state;
    const list = state.body.querySelector('[data-testid="logs-list"]');
    if (!list) return;
    const options = {
        appId: 'logs',
        tail: DEFAULT_TAIL,
        onError: (detail) => {
            const note = document.createElement('div');
            note.className = 'logs-empty';
            note.textContent = `[${detail}]`;
            list.appendChild(note);
        },
    };
    try {
        if (selection.kind === 'app') {
            state.stream = streamLogs({
                ...options,
                source: 'app',
                onEntry: (entry) => {
                    if (!_matches(state, entry)) return;
                    _appendEntry(state, entry, list, true);
                    _refreshMeta(state);
                },
            });
        } else if (selection.kind === 'file') {
            state.stream = streamLogs({
                ...options,
                source: `file:${selection.path}`,
                onLine: (line, source) => _appendLine(state, line, source, list),
            });
        } else if (selection.kind === 'journal') {
            state.stream = streamLogs({
                ...options,
                source: 'journal',
                unit: selection.unit || '',
                onLine: (line, source) => _appendLine(state, line, source, list),
            });
        } else if (selection.kind === 'container') {
            state.stream = streamLogs({
                ...options,
                source: `container:${selection.container}`,
                onLine: (line, source) => _appendLine(state, line, source, list),
                onEnd: (detail) => {
                    const note = document.createElement('div');
                    note.className = 'logs-empty';
                    note.textContent = `[${detail || 'stream ended'}]`;
                    list.appendChild(note);
                },
            });
        }
    } catch (err) {
        console.warn('Logs stream failed', err);
    }
}

function _matches(state, entry) {
    if (!entry) return false;
    const level = (entry.level || 'info').toLowerCase();
    if (state.level && (LEVEL_SEVERITY[level] || 0) < (LEVEL_SEVERITY[state.level] || 0)) return false;
    if (state.selection.app) {
        const app = entry.app || '';
        if (app !== state.selection.app && !app.startsWith(`${state.selection.app}:`)) return false;
    }
    if (state.search) {
        const haystack = `${entry.message} ${entry.detail} ${entry.source} ${entry.app}`.toLowerCase();
        if (!haystack.includes(state.search.toLowerCase())) return false;
    }
    return true;
}

// ── rows ────────────────────────────────────────────────────────────────────

function _time(ts) {
    const date = new Date(Number(ts || 0) * 1000);
    return Number.isNaN(date.getTime())
        ? '—'
        : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function _entryRow(entry) {
    const row = document.createElement('div');
    row.className = 'logs-row';
    const ts = document.createElement('span');
    ts.className = 'logs-ts';
    ts.textContent = _time(entry.ts);
    const level = document.createElement('span');
    const name = (entry.level || 'info').toLowerCase();
    level.className = `logs-level logs-level-${name}`;
    level.textContent = name.toUpperCase();
    const source = document.createElement('span');
    source.className = 'logs-src';
    source.textContent = `[${entry.source || entry.app || ''}]`;
    const message = document.createElement('span');
    message.className = 'logs-msg';
    message.textContent = entry.message || '';
    if (Number(entry.count || 1) > 1) {
        const count = document.createElement('span');
        count.className = 'logs-count';
        count.textContent = `×${entry.count}`;
        message.appendChild(count);
    }
    row.append(ts, level, source, message);
    if (entry.detail) {
        const details = document.createElement('details');
        const summary = document.createElement('summary');
        summary.textContent = 'detail';
        summary.className = 'logs-detail-toggle';
        const pre = document.createElement('pre');
        pre.className = 'logs-detail';
        pre.textContent = entry.detail;
        details.append(summary, pre);
        row.appendChild(details);
    }
    return row;
}

function _appendEntry(state, entry, list, prepend) {
    const empty = list.querySelector('[data-state="empty"], .logs-empty');
    if (empty) empty.remove();
    const row = _entryRow(entry);
    if (prepend) list.prepend(row);
    else list.appendChild(row);
    state.entries.push(entry);
    _trimRows(list);
}

function _appendLine(state, line, source, list) {
    const empty = list.querySelector('.logs-empty');
    if (empty) empty.remove();
    const pre = document.createElement('div');
    pre.className = 'logs-line';
    pre.textContent = line;
    if (source) pre.dataset.source = source;
    list.appendChild(pre);
    state.lines.push({ line, source });
    _trimRows(list);
    list.scrollTop = list.scrollHeight;
}

function _trimRows(list) {
    while (list.children.length > MAX_ROWS) list.removeChild(list.firstChild);
}

function _applyLineFilter(state) {
    const list = state.body.querySelector('[data-testid="logs-list"]');
    if (!list) return;
    const needle = state.search.toLowerCase();
    const rows = [...list.children];
    for (const row of rows) {
        if (!row.classList.contains('logs-line') && !row.classList.contains('logs-row')) continue;
        row.style.display = !needle || row.textContent.toLowerCase().includes(needle) ? '' : 'none';
    }
}

// ── toolbar actions ─────────────────────────────────────────────────────────

async function _refreshMeta(state) {
    const meta = state.body.querySelector('[data-testid="logs-meta"]');
    if (!meta) return;
    // Live entries arrive in bursts — do not fetch stats on every row.
    if (state.metaBusy) return;
    state.metaBusy = true;
    try {
        state.stats = await fetchLogStats();
    } catch (_) {
        state.stats = null;
    } finally {
        setTimeout(() => { state.metaBusy = false; }, 1500);
    }
    const counts = state.stats?.counts || {};
    const shown = state.selection.kind === 'app' ? state.entries.length : state.lines.length;
    const parts = [
        `${shown} shown`,
        `${counts.error || 0} errors`,
        `${counts.warning || 0} warnings`,
    ];
    if (state.stats) {
        parts.push(`ring ${state.stats.ring}/${state.stats.ring_size}`);
        parts.push(`disk ${formatBytes(state.stats.disk_bytes || 0)}`);
        if (state.stats.dropped) parts.push(`${state.stats.dropped} dropped`);
    }
    meta.textContent = parts.join(' · ');
}

function _export(state) {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    let text = '';
    if (state.selection.kind === 'app') {
        text = [...state.entries].reverse().map((entry) => JSON.stringify(entry)).join('\n');
    } else {
        text = state.lines.map((item) => item.line).join('\n');
    }
    if (!text) return;
    const blob = new Blob([text], { type: 'application/x-ndjson' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `logs-${stamp}.jsonl`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 2000);
}

async function _clear(state) {
    if (!state.isAdmin) return;
    const ok = await showConfirm(
        'Clear the log store?',
        'Removes the in-memory ring and the active log file. Rotated history stays on disk.',
        { confirmLabel: 'Clear', danger: true },
    );
    if (!ok) return;
    try {
        await clearLogs();
        await _reload(state);
    } catch (err) {
        console.warn('Clearing logs failed', err);
    }
}

// Kept for callers that resolved the renderer from the global scope.
// (App registration lives in app.js; no window globals here.)

