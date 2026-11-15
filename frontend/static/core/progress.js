// EthOS NAS — Unified task/progress center
//
// ONE component for every long-running action: uploads, copy/move, delete,
// trash/purge, package install/update, Docker engine install, backups, …
// Apps must never hand-roll progress bars or their own floating panels — call
// startTask() and drive it with update()/finish().
//
// Two hosts, one design:
//   * default            → row in the floating panel (#taskProgressPanel,
//                          bottom-right, stacks every app's running tasks)
//   * `host: <element>`  → inline row with the same markup/CSS/a11y inside an
//                          app window (Packages banner, Docker box …)
//
// Lifecycle: running → done | failed | cancelled. Finished rows stay visible
// until the user clears them (✓) or closes the panel (×) — failed work never
// disappears on a timer. `retry` adds a Retry button on failed tasks.
//
// Events: window `ethos:tasks-changed` with {running, done, failed, cancelled,
// total, percent, byApp} (throttled; immediate on status changes).

import { formatBytes } from './utils.js';

const EVENT_THROTTLE_MS = 150;
const MAX_FINISHED_TASKS = 12;

/** @type {Map<string, object>} */
const _tasks = new Map();
let _seq = 0;
let _lastEventAt = 0;
let _eventTimer = null;

/**
 * Start a task and render its first state.
 *
 * @param {object} opts
 * @param {string} [opts.appId]          owning app id ('files', 'uploads', …)
 * @param {string} opts.title            primary label (file name, job name…)
 * @param {number} [opts.total]          item count for item-based progress
 * @param {string} [opts.detail]         secondary line (bytes, step, current item)
 * @param {Element} [opts.host]          inline host element (default: floating panel)
 * @param {string} [opts.testIdPrefix]   data-testid prefix (default 'task')
 * @param {Function} [opts.retry]        called when the user clicks Retry on a failure
 * @param {Function} [opts.onDismiss]    called when the row is cleared/hidden
 * @returns {object} task handle: {id, update, finish, dismiss, snapshot}
 */
export function startTask(opts = {}) {
    const {
        appId = 'system', title = 'Working…', total = 0, detail = '',
        host = null, testIdPrefix = 'task', retry = null, onDismiss = null,
    } = opts;
    _seq += 1;
    const task = {
        id: `task-${Date.now().toString(36)}-${_seq.toString(36)}`,
        appId, title: String(title), detail: String(detail || ''),
        total: Math.max(0, Number(total) || 0), index: 0, percent: null,
        status: 'running', log: '', startedAt: Date.now(),
        bytesDone: 0, bytesTotal: 0,
        host: host || null, testIdPrefix, retry, onDismiss,
        el: null, dismissed: false,
    };
    _tasks.set(task.id, task);
    _render(task);
    _emit(true);
    return _handle(task);
}

/** Update a running (or finished) task. Unknown fields are left untouched. */
export function updateTask(taskId, patch = {}) {
    const task = _tasks.get(taskId);
    if (task) _update(task, patch);
}

/** Finish a task: status 'done' | 'failed' | 'cancelled'. */
export function finishTask(taskId, status = 'done', patch = {}) {
    const task = _tasks.get(taskId);
    if (task) _finish(task, status, patch);
}

/**
 * Remove finished tasks (all apps, or one app).
 * `globalOnly` keeps inline rows of running apps untouched.
 */
export function clearFinishedTasks(appId = null, { globalOnly = false } = {}) {
    for (const [taskId, task] of [..._tasks]) {
        if (task.status === 'running') continue;
        if (appId && task.appId !== appId) continue;
        if (globalOnly && task.host) continue;
        _dismiss(taskId);
    }
}

/** Re-open the floating panel (also used by the File Manager upload chip). */
export function showTaskPanel() {
    _ensurePanel();
}

/** Aggregate counters — used by status bars/chips. */
export function getTaskStatus() {
    return _snapshot();
}

/** Current task count for one app (e.g. uploads) and their aggregate state. */
export function getAppTaskStatus(appId) {
    const snap = _snapshot();
    return { count: snap.byApp[appId] || 0, ...snap };
}

// ── Task handle ─────────────────────────────────────────────────────────────

function _handle(task) {
    return {
        get id() { return task.id; },
        update(patch = {}) { _update(task, patch); return this; },
        finish(status = 'done', patch = {}) { _finish(task, status, patch); return this; },
        dismiss() { _dismiss(task.id); },
        get snapshot() { return _taskSnapshot(task); },
    };
}

function _update(task, patch) {
    if (!task || task.dismissed) return;
    if (patch.title != null) task.title = String(patch.title);
    if (patch.detail != null) task.detail = String(patch.detail);
    if (patch.log != null) task.log = String(patch.log);
    if (patch.total != null) task.total = Math.max(0, Number(patch.total) || 0);
    if (patch.index != null) task.index = Math.max(0, Number(patch.index) || 0);
    if (patch.bytesDone != null) task.bytesDone = Number(patch.bytesDone) || 0;
    if (patch.bytesTotal != null) task.bytesTotal = Number(patch.bytesTotal) || 0;
    if (patch.percent !== undefined) {
        task.percent = patch.percent == null ? null
            : Math.max(0, Math.min(100, Number(patch.percent) || 0));
    } else if (patch.index != null && task.total > 0) {
        task.percent = Math.min(100, (task.index / task.total) * 100);
    }
    if (patch.status) task.status = patch.status;
    _render(task);
    _emit(false);
}

function _finish(task, status, patch = {}) {
    if (!task || task.dismissed) return;
    const final = status === 'done' ? 'done' : status === 'cancelled' ? 'cancelled' : 'failed';
    if (patch.title != null) task.title = String(patch.title);
    if (patch.detail != null) task.detail = String(patch.detail);
    if (patch.log != null) task.log = String(patch.log);
    if (patch.bytesDone != null) task.bytesDone = Number(patch.bytesDone) || 0;
    if (patch.bytesTotal != null) task.bytesTotal = Number(patch.bytesTotal) || 0;
    if (patch.percent != null) {
        task.percent = Math.max(0, Math.min(100, Number(patch.percent) || 0));
    } else if (final === 'done') {
        task.percent = 100;
    }
    task.status = final;
    _render(task);
    _emit(true);
    _pruneFinished();
}

function _dismiss(taskId) {
    const task = _tasks.get(taskId);
    if (!task) return;
    task.dismissed = true;
    if (task.el && task.el.parentNode) task.el.remove();
    if (task.host) task.host.innerHTML = '';
    _tasks.delete(taskId);
    _maybeHidePanel();
    _emit(true);
    try { if (typeof task.onDismiss === 'function') task.onDismiss(task); } catch { /* non-fatal */ }
}

function _pruneFinished() {
    const finished = [..._tasks.values()].filter(t => t.status !== 'running');
    for (const task of finished.slice(0, Math.max(0, finished.length - MAX_FINISHED_TASKS))) {
        _dismiss(task.id);
    }
}

// ── Rendering (one markup for every host) ───────────────────────────────────

function _render(task) {
    if (task.host) _renderInline(task);
    else _renderPanelRow(task);
}

function _buildRow(task) {
    const p = task.testIdPrefix;
    const row = document.createElement('div');
    row.className = 'task-progress-item';
    row.id = `task-entry-${task.id}`;
    row.dataset.app = task.appId;
    row.setAttribute('data-testid', `${p}-item`);
    row.innerHTML =
        '<div class="task-progress-head">' +
            `<span class="task-progress-name" data-testid="${p}-title"></span>` +
            `<span class="task-progress-pct" data-testid="${p}-percent"></span>` +
        '</div>' +
        `<div class="task-progress-bar-track" data-testid="${p}-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100">` +
            `<div class="task-progress-bar" data-testid="${p}-fill"></div>` +
        '</div>' +
        `<span class="task-progress-label" data-testid="${p}-step"></span>` +
        `<span class="task-progress-log" data-testid="${p}-log"></span>` +
        `<div class="task-progress-error" data-testid="${p}-error"></div>` +
        `<button type="button" class="task-progress-retry" data-testid="${p}-retry">Retry</button>`;
    row.querySelector('.task-progress-retry').addEventListener('click', () => {
        if (typeof task.retry === 'function') task.retry();
    });
    return row;
}

function _applyRow(task) {
    const row = task.el;
    if (!row) return;
    const pct = task.percent;
    const running = task.status === 'running';

    const nameEl = row.querySelector('.task-progress-name');
    nameEl.textContent = task.title;
    nameEl.title = task.title;

    const pctEl = row.querySelector('.task-progress-pct');
    pctEl.textContent = pct == null
        ? (running ? '' : (task.status === 'done' ? '100%' : ''))
        : `${Math.round(pct)}%`;

    const track = row.querySelector('.task-progress-bar-track');
    const bar = row.querySelector('.task-progress-bar');
    const indeterminate = pct == null && running;
    track.classList.toggle('is-indeterminate', indeterminate);
    bar.style.width = indeterminate ? '' : `${pct == null ? 0 : pct}%`;
    if (pct == null) track.removeAttribute('aria-valuenow');
    else track.setAttribute('aria-valuenow', String(Math.round(pct)));
    track.setAttribute('aria-label', task.title);

    let detail = task.detail || '';
    if (task.total > 1) {
        const current = Math.min(task.index + 1, task.total);
        detail = detail ? `${detail} · ${current}/${task.total}` : `${current}/${task.total}`;
    }
    if (task.bytesTotal > 0) {
        const bytes = `${formatBytes(task.bytesDone)} / ${formatBytes(task.bytesTotal)}`;
        detail = detail ? `${bytes} · ${detail}` : bytes;
    }
    const labelEl = row.querySelector('.task-progress-label');
    labelEl.textContent = detail;
    labelEl.style.display = detail ? '' : 'none';

    const logEl = row.querySelector('.task-progress-log');
    logEl.textContent = task.log || '';
    logEl.title = task.log || '';
    logEl.style.display = task.log ? '' : 'none';

    const errorEl = row.querySelector('.task-progress-error');
    const errorText = task.status === 'failed' ? (task.detail || '') : '';
    errorEl.textContent = errorText;
    errorEl.style.display = errorText ? '' : 'none';
    if (errorText) labelEl.style.display = 'none';  // no duplicated message

    const retryEl = row.querySelector('.task-progress-retry');
    const canRetry = task.status === 'failed' && typeof task.retry === 'function';
    retryEl.style.display = canRetry ? '' : 'none';

    row.classList.toggle('is-done', task.status === 'done');
    row.classList.toggle('is-failed', task.status === 'failed');
    row.classList.toggle('is-cancelled', task.status === 'cancelled');
}

function _renderInline(task) {
    const host = task.host;
    if (!host) return;
    if (!task.el || !host.contains(task.el)) {
        host.innerHTML = '';
        task.el = _buildRow(task);
        task.el.classList.add('task-progress-item-inline');
        host.appendChild(task.el);
    }
    _applyRow(task);
}

// ── Floating panel ──────────────────────────────────────────────────────────

function _ensurePanel() {
    let panel = document.getElementById('taskProgressPanel');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'taskProgressPanel';
        panel.className = 'task-progress-panel';
        document.body.appendChild(panel);
    }

    if (!panel.querySelector('.task-progress-header')) {
        panel.innerHTML = '';
        const header = document.createElement('div');
        header.className = 'task-progress-header';
        header.innerHTML =
            '<span class="task-progress-title">\u{1F5C2} Tasks</span>' +
            '<span class="task-progress-summary" data-testid="task-progress-summary"></span>' +
            '<span class="task-progress-header-actions">' +
                '<button type="button" class="task-progress-clear" data-testid="task-progress-clear" title="Clear finished tasks">&#10003;</button>' +
                '<button type="button" class="task-progress-close" data-testid="task-progress-close" title="Hide">\u00d7</button>' +
            '</span>';

        const body = document.createElement('div');
        body.className = 'task-progress-body';
        body.id = 'taskProgressBody';
        body.setAttribute('role', 'status');
        body.setAttribute('aria-live', 'polite');

        panel.appendChild(header);
        panel.appendChild(body);

        header.querySelector('.task-progress-close').addEventListener('click', _hidePanel);
        header.querySelector('.task-progress-clear').addEventListener('click', () => {
            clearFinishedTasks(null, { globalOnly: true });
            _updateSummary();
        });
    }

    // The placeholder in index.html ships display:none — always unhide here.
    panel.style.display = '';
    panel.setAttribute('data-testid', 'task-progress-panel');
    return panel;
}

function _renderPanelRow(task) {
    const panel = _ensurePanel();
    const body = panel.querySelector('.task-progress-body');
    if (!task.el || !body.contains(task.el)) {
        task.el = _buildRow(task);
        body.appendChild(task.el);
    }
    _applyRow(task);
    _updateSummary();
}

function _hidePanel() {
    const panel = document.getElementById('taskProgressPanel');
    if (panel) panel.style.display = 'none';
    clearFinishedTasks(null, { globalOnly: true });
}

function _maybeHidePanel() {
    const hasGlobal = [..._tasks.values()].some(t => !t.host);
    if (hasGlobal) return;
    const panel = document.getElementById('taskProgressPanel');
    if (panel) panel.style.display = 'none';
}

function _updateSummary() {
    const el = document.querySelector('#taskProgressPanel [data-testid="task-progress-summary"]');
    if (!el) return;
    const snap = _snapshot();
    const parts = [];
    if (snap.running > 0) parts.push(`\u2191 ${snap.running}`);
    if (snap.failed > 0) parts.push(`\u26a0 ${snap.failed}`);
    if (snap.cancelled > 0) parts.push(`\u23f9 ${snap.cancelled}`);
    if (snap.done > 0) parts.push(`\u2713 ${snap.done}`);
    el.textContent = parts.join(' · ');
}

// ── Events ──────────────────────────────────────────────────────────────────

function _snapshot() {
    const detail = { running: 0, done: 0, failed: 0, cancelled: 0, total: 0, percent: 0, byApp: {} };
    let sum = 0;
    let counted = 0;
    for (const task of _tasks.values()) {
        detail.total += 1;
        detail.byApp[task.appId] = (detail.byApp[task.appId] || 0) + 1;
        if (task.status === 'running') {
            detail.running += 1;
            if (task.percent != null) { sum += task.percent; counted += 1; }
        } else if (task.status === 'failed') detail.failed += 1;
        else if (task.status === 'cancelled') detail.cancelled += 1;
        else detail.done += 1;
    }
    detail.percent = counted > 0
        ? Math.round(sum / counted)
        : (detail.running > 0 ? 0 : 100);
    return detail;
}

function _taskSnapshot(task) {
    return {
        id: task.id, appId: task.appId, title: task.title, status: task.status,
        percent: task.percent, detail: task.detail, index: task.index, total: task.total,
    };
}

function _emit(immediate = false) {
    const fire = () => {
        _lastEventAt = Date.now();
        _eventTimer = null;
        _updateSummary();
        try {
            window.dispatchEvent(new CustomEvent('ethos:tasks-changed', { detail: _snapshot() }));
        } catch { /* non-fatal */ }
    };
    if (immediate) {
        if (_eventTimer) { clearTimeout(_eventTimer); _eventTimer = null; }
        fire();
        return;
    }
    if (_eventTimer) return;
    const wait = EVENT_THROTTLE_MS - (Date.now() - _lastEventAt);
    if (wait <= 0) fire();
    else _eventTimer = setTimeout(fire, wait);
}
