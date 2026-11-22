// EthOS NAS — Packages (signed catalog, jobs, sources)
//
// Available / Installed / Updates, a detail modal, async install jobs with a
// live log, and a source manager that shows the trust level of each repository.

import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiFetch, apiJsonFetch } from '../core/api.js';
import { addNotification } from '../core/notification-center.js';
import { skeletonRows, emptyState, errorState, wireStateActions } from '../core/states.js';
import { showConfirm, showFormModal } from '../core/modal.js';
import { startTask } from '../core/progress.js';
import { logError } from '../core/logger.js';
import { uiIcon } from '../core/icons.js';
import { mountShell } from '../core/ui-shell.js';
import { openLogs } from './logs.js';

const _pkg = {
    tab: 'available',
    catalog: [],
    installed: [],
    sources: [],
    updates: [],
    statuses: {},
    search: '',
    category: '',
    isAdmin: false,
    jobTimer: null,
    job: null,
    jobHideTimer: null,
    task: null,          // shared progress row (core/progress.js)
    updateAll: null,     // { total, done, name } while "Update all" runs
    loading: false,
};

// jobId → { promise, resolve }: lets callers (`Update all`) await a job result.
const _jobWaiters = new Map();

const TABS = [
    { id: 'available', label: 'Available', title: 'Available Packages', subtitle: 'Signed catalog from every enabled source.' },
    { id: 'installed', label: 'Installed', title: 'Installed Packages', subtitle: 'Services, native windows and engine packages on this NAS.' },
    { id: 'updates', label: 'Updates', title: 'Package Updates', subtitle: 'Versions newer than the installed ones.' },
    { id: 'sources', label: 'Sources', title: 'Package Sources', subtitle: 'Signed repositories and their trust level.' },
];

let shell = null;

const JOB_TITLES = { install: 'Installing', update: 'Updating', uninstall: 'Uninstalling' };

const STEP_LABELS = {
    queued: 'Queued',
    verify: 'Verifying manifest',
    download: 'Downloading files',
    policy: 'Checking compose policy',
    deps: 'Preparing system dependencies',
    deploy: 'Deploying files',
    backup: 'Backing up current version',
    start: 'Starting service',
    stop: 'Stopping service',
    images: 'Removing unused images',
    cleanup: 'Removing package files',
    done: 'Complete',
};

const OFFICIAL_SOURCE = {
    name: 'EthOS Community Packages',
    url: 'https://raw.githubusercontent.com/SyncHot/ethos-packages/main/index.json',
    public_key: 'sXiL7v0droY2BVyttuDt0Bqq6jB7MpknylJo+/iavqE=',
    trust: 'pinned',
};

function _icon(pkg, size = 28) {
    if (pkg.icon_url) {
        return `<img src="${escapeAttr(pkg.icon_url)}" alt="" style="width:${size}px;height:${size}px;border-radius:8px;object-fit:cover;">`;
    }
    return `<span style="font-size:${Math.round(size * 0.8)}px;line-height:1;">${escapeHtml(pkg.icon || '📦')}</span>`;
}

function _badge(text, cls = 'badge-blue') {
    const tone = cls.includes('green') ? 'is-ok'
        : cls.includes('red') ? 'is-bad'
            : cls.includes('yellow') ? 'is-warn' : 'is-info';
    return `<span class="ui-tag ${tone}">${escapeHtml(text)}</span>`;
}

function _trustBadge(pkg) {
    return pkg.trust === 'pinned'
        ? _badge('signed', 'badge-green')
        : _badge('unverified', 'badge-yellow');
}

function _capabilities(pkg) {
    const caps = pkg.capabilities || (pkg.manifest && pkg.manifest.capabilities) || [];
    return Array.isArray(caps) ? caps : [];
}

function _systemRequires(pkg) {
    const list = pkg.system_requires || (pkg.manifest && pkg.manifest.system_requires) || [];
    return Array.isArray(list) ? list : [];
}

// Host software a package needs (manifest `system_requires`). Docker is the only
// one Packages window may provision; the rest must already be on the NAS.
const SYSTEM_DEP_LABELS = { docker: 'Docker Engine', samba: 'Samba' };

function _systemLabel(id) {
    return SYSTEM_DEP_LABELS[id] || String(id);
}

// Requirements the host cannot satisfy and EthOS will not install itself —
// computed by the backend probe (`system_status` on the catalog detail).
function _blockedDeps(pkg) {
    const status = pkg && pkg.system_status;
    if (!Array.isArray(status)) return [];
    return status.filter((item) => item && item.ok === false && !item.provisionable);
}

function _systemWarning(pkg) {
    const blocked = _blockedDeps(pkg);
    if (!blocked.length) return '';
    const rows = blocked.map((item) => {
        const missing = (item.missing_tools || []).join(', ');
        const hint = item.hint ? ` · <code>${escapeHtml(item.hint)}</code>` : '';
        return `<li>${escapeHtml(item.label || _systemLabel(item.id))} — missing <code>${escapeHtml(missing)}</code>${hint}</li>`;
    }).join('');
    return `<div data-testid="pkg-missing-deps" class="ui-callout error">
        <strong>Cannot install yet — host software is missing.</strong>
        <ul>${rows}</ul>
        EthOS never installs OS packages on its own. Run the command above on the NAS, then reopen this dialog.
    </div>`;
}

// Dependencies a `runtime: venv` package installs into its own virtualenv
// (offline wheelhouse — never the core Python environment).
function _pythonRequires(pkg) {
    const cfg = pkg.python || (pkg.manifest && pkg.manifest.python) || null;
    const list = cfg && cfg.requirements;
    return Array.isArray(list) ? list : [];
}

function _capabilityBadges(pkg) {
    const caps = _capabilities(pkg);
    const badges = [];
    if (caps.includes('kvm')) badges.push(_badge('/dev/kvm', 'badge-blue'));
    for (const dep of _systemRequires(pkg)) badges.push(_badge(_systemLabel(dep), 'badge-blue'));
    return badges.join(' ');
}

function _statusBadge(status) {
    const cls = status === 'running' ? 'badge-green'
        : status === 'ready' ? 'badge-green'
        : status === 'error' ? 'badge-red'
            : status === 'not_configured' ? 'badge-blue' : 'badge-yellow';
    return _badge(status || 'unknown', cls);
}

// ── Active job state ────────────────────────────────────────────────────────

function _jobBusy(pkgId) {
    return !!_pkg.job && _pkg.job.packageId === pkgId;
}

function _jobBlocks(pkgId) {
    return !!_pkg.job && _pkg.job.packageId !== pkgId;
}

function _disabledAttr(pkgId) {
    return _jobBusy(pkgId) || _jobBlocks(pkgId)
        ? ' disabled title="Another package operation is running"'
        : '';
}

function _busyLabel(pkgId) {
    if (!_jobBusy(pkgId)) return '';
    const title = JOB_TITLES[_pkg.job.kind] || 'Working';
    return `<span data-testid="pkg-busy-${escapeAttr(pkgId)}" class="ui-tag is-warn">${escapeHtml(title)}…</span>`;
}

export async function renderPackagesPage(body) {
    // A re-render destroys the banner element hosting the inline task row.
    if (_pkg.task) { _pkg.task.dismiss(); _pkg.task = null; }
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'packages',
        title: 'Packages',
        subtitle: 'Signed catalog, installed apps and sources.',
        icon: '📦',
        sections: TABS.map(({ id, label, title, subtitle }) => ({
            id, label, title, subtitle, testId: `pkg-tab-${id}`,
        })),
        note: 'Native packages run with EthOS privileges — install only signed sources.',
    });

    const toolbar = document.createElement('div');
    toolbar.className = 'ui-toolbar';
    toolbar.id = 'pkgToolbar';
    toolbar.innerHTML = `
        <div class="pkg-search" id="pkgSearchWrap">
            ${uiIcon('search', 14)}
            <input id="pkgSearch" class="ui-input pkg-search-input" data-testid="pkg-search-input" type="search"
                   placeholder="Search packages…" aria-label="Search packages" value="${escapeAttr(_pkg.search)}">
        </div>
        <div class="ui-row">
            <span id="pkgTabCount" class="ui-tag" data-testid="pkg-tab-count" hidden></span>
            <button class="ui-btn small primary" id="pkgUpdateAllBtn" data-testid="pkg-update-all"
                    title="Update every package with a newer version" hidden>${uiIcon('arrowUp', 12)} Update all</button>
            <button class="ui-btn small" id="pkgRefreshBtn" data-testid="pkg-refresh-btn">${uiIcon('refresh', 12)} Refresh</button>
        </div>`;

    const filterBar = document.createElement('div');
    filterBar.id = 'pkgFilterBar';
    filterBar.className = 'ui-row';

    const banner = document.createElement('div');
    banner.id = 'pkgJobBanner';
    banner.className = 'ui-callout info';
    banner.hidden = true;
    banner.setAttribute('role', 'status');
    banner.setAttribute('aria-live', 'polite');

    shell.main.insertBefore(toolbar, shell.view);
    shell.main.insertBefore(filterBar, shell.view);
    shell.main.insertBefore(banner, shell.view);

    toolbar.querySelector('#pkgSearch')?.addEventListener('input', (ev) => {
        _pkg.search = ev.target.value;
        _renderContent();
    });
    toolbar.querySelector('#pkgRefreshBtn')?.addEventListener('click', () => _loadAll(true));
    toolbar.querySelector('#pkgUpdateAllBtn')?.addEventListener('click', () => _updateAll());

    shell.onSection((id) => {
        _pkg.tab = id;
        _syncToolbar();
        _renderContent();
    });

    try {
        const me = await apiJsonFetch('/api/auth/me');
        _pkg.isAdmin = me?.role === 'admin';
    } catch (err) {
        logError('Packages: could not read the current role', err?.detail || err?.message);
    }

    await _loadAll();
    await _resumeJob();
    shell.setActive(TABS.some((tab) => tab.id === _pkg.tab) ? _pkg.tab : 'available');
}

async function _loadAll(force = false) {
    if (_pkg.loading) return;
    _pkg.loading = true;
    const content = shell?.view;
    const refresh = shell?.root.querySelector('#pkgRefreshBtn');
    if (refresh) refresh.disabled = true;
    try {
        const [catalog, installed, sources] = await Promise.all([
            apiJsonFetch(
                force && _pkg.isAdmin ? '/api/packages/registry/refresh' : '/api/packages/registry/browse',
                force && _pkg.isAdmin ? { method: 'POST' } : undefined,
            ),
            apiJsonFetch('/api/packages'),
            apiJsonFetch('/api/packages/registry/sources'),
        ]);
        _pkg.catalog = catalog.packages || [];
        _pkg.installed = installed.packages || installed.items || [];
        _pkg.sources = sources.sources || [];
        _pkg.updates = _pkg.catalog.filter((p) => p.update_available);

        const statuses = {};
        await Promise.all(_pkg.installed.map(async (pkg) => {
            try {
                const status = await apiJsonFetch(`/api/packages/${encodeURIComponent(pkg.id)}/status`);
                statuses[pkg.id] = status.status;
            } catch (_) {
                statuses[pkg.id] = 'unknown';
            }
        }));
        _pkg.statuses = statuses;

        const count = shell?.root.querySelector('#pkgTabCount');
        if (count) {
            count.textContent = _pkg.updates.length
                ? `${_pkg.updates.length} update${_pkg.updates.length === 1 ? '' : 's'}`
                : '';
            count.hidden = !_pkg.updates.length;
        }
        _renderFilterBar();
        _syncToolbar();
        _syncUpdateAllButton();
        _renderContent();
    } catch (err) {
        logError('Packages: catalog load failed', err?.detail || err?.message);
        if (content) {
            content.innerHTML = errorState({ detail: err.detail || err.message });
            wireStateActions(content, { retry: () => _loadAll(true) });
        }
    } finally {
        _pkg.loading = false;
        if (refresh) refresh.disabled = false;
    }
}

function _categories() {
    return [...new Set(_pkg.catalog.map((p) => p.category).filter(Boolean))].sort();
}

function _renderFilterBar() {
    const bar = shell?.root.querySelector('#pkgFilterBar');
    if (!bar) return;
    const cats = _categories();
    bar.innerHTML = cats.length
        ? `<button class="ui-chip${_pkg.category === '' ? ' is-active' : ''}" data-cat="">All</button>`
            + cats.map((cat) => `<button class="ui-chip${_pkg.category === cat ? ' is-active' : ''}" data-cat="${escapeAttr(cat)}">${escapeHtml(cat)}</button>`).join('')
        : '';
    bar.querySelectorAll('[data-cat]').forEach((btn) => btn.addEventListener('click', () => {
        _pkg.category = btn.dataset.cat;
        _renderFilterBar();
        _syncToolbar();
        _renderContent();
    }));
}

// Search and category chips describe the catalog only; on Sources they would
// just add noise. Keep the refresh action reachable from every tab.
function _syncToolbar() {
    const root = shell?.root;
    if (!root) return;
    const catalogTab = _pkg.tab !== 'sources';
    const search = root.querySelector('#pkgSearchWrap');
    if (search) search.hidden = !catalogTab;
    const filters = root.querySelector('#pkgFilterBar');
    if (filters) filters.hidden = !catalogTab || !filters.childElementCount;
}

// The "Update all" entry point sits in the toolbar so it is reachable from
// every tab; it disappears when nothing is pending (or for non-admins).
function _syncUpdateAllButton() {
    const btn = shell?.root.querySelector('#pkgUpdateAllBtn');
    if (!btn) return;
    const total = _pkg.updates.length;
    const queue = _pkg.updateAll;
    btn.hidden = !_pkg.isAdmin || (!total && !queue);
    btn.disabled = !!_pkg.job || !!queue;
    if (queue) {
        const index = Math.min(queue.done + 1, queue.total);
        btn.innerHTML = `${uiIcon('refresh', 12)} Updating ${index}/${queue.total}…`;
        btn.title = queue.name ? `Updating ${queue.name}` : 'Updating packages';
    } else {
        btn.innerHTML = `${uiIcon('arrowUp', 12)} Update all${total ? ` (${total})` : ''}`;
        btn.title = 'Update every package with a newer version';
    }
}

function _matches(pkg) {
    if (_pkg.category && pkg.category !== _pkg.category) return false;
    const needle = _pkg.search.trim().toLowerCase();
    if (!needle) return true;
    return ['id', 'name', 'description', 'category', 'author'].some(
        (key) => String(pkg[key] || '').toLowerCase().includes(needle),
    );
}

function _renderContent() {
    const content = shell?.view;
    if (!content) return;
    if (_pkg.tab === 'installed') {
        _renderInstalled(content);
    } else if (_pkg.tab === 'updates') {
        _renderUpdates(content);
    } else if (_pkg.tab === 'sources') {
        _renderSources(content);
    } else {
        _renderCatalog(content);
    }
}

function _renderCatalog(content) {
    const packages = _pkg.catalog.filter(_matches);
    if (!_pkg.sources.length) {
        content.innerHTML = emptyState({
            icon: 'package',
            title: 'No package sources yet',
            hint: 'Add a signed repository to browse packages.',
            action: { label: 'Add source', id: 'add-source' },
        });
        wireStateActions(content, { 'add-source': () => _addSourceDialog() });
        return;
    }
    if (!packages.length) {
        content.innerHTML = emptyState({ icon: 'search', title: 'No packages match', hint: 'Try another search or category.' });
        return;
    }
    content.innerHTML = `<div id="pkgAvailableGrid" data-testid="pkg-available-grid" class="ui-grid">
        ${packages.map(_card).join('')}</div>`;
    content.querySelectorAll('[data-card-id]').forEach((card) => {
        card.addEventListener('click', (ev) => {
            if (ev.target.closest('button')) return;
            _openDetail(card.dataset.cardId);
        });
    });
    content.querySelectorAll('[data-install]').forEach((btn) => {
        btn.addEventListener('click', () => _install(btn.dataset.install));
    });
    content.querySelectorAll('[data-update]').forEach((btn) => {
        btn.addEventListener('click', () => _update(btn.dataset.update));
    });
    content.querySelectorAll('[data-uninstall]').forEach((btn) => {
        btn.addEventListener('click', () => _uninstall(btn.dataset.uninstall));
    });
}

function _card(pkg) {
    const installed = !!pkg.installed;
    const busy = _jobBusy(pkg.id);
    const disabled = _disabledAttr(pkg.id);
    const uninstall = _pkg.isAdmin
        ? `<button class="ui-btn small danger" data-uninstall="${escapeAttr(pkg.id)}" data-testid="pkg-card-uninstall-${escapeAttr(pkg.id)}"${disabled}>Uninstall</button>`
        : '';
    let action;
    if (busy) {
        action = `<div class="ui-row">${_busyLabel(pkg.id)}${uninstall}</div>`;
    } else if (installed) {
        action = pkg.update_available && _pkg.isAdmin
            ? `<button class="ui-btn small primary" data-update="${escapeAttr(pkg.id)}" data-testid="pkg-update-${escapeAttr(pkg.id)}"${disabled}>Update → v${escapeHtml(pkg.version)}</button>`
            : `<div class="ui-row"><span class="ui-tag is-ok">Installed v${escapeHtml(pkg.installed_version || '')}</span>${uninstall}</div>`;
    } else {
        action = _pkg.isAdmin
            ? `<button class="ui-btn small primary" data-install="${escapeAttr(pkg.id)}" data-testid="pkg-install-${escapeAttr(pkg.id)}"${disabled}>Install</button>`
            : '<span class="ui-tag">Admin required</span>';
    }
    const caps = _capabilityBadges(pkg);
    const meta = `${pkg.source_name || pkg.source || ''}${pkg.author ? ` · ${pkg.author}` : ''}`;
    return `
        <article data-card-id="${escapeAttr(pkg.id)}" data-testid="pkg-card-${escapeAttr(pkg.id)}" class="ui-panel ui-card pkg-card">
            <div class="pkg-card-head">
                <div class="ui-card-icon">${_icon(pkg, 28)}</div>
                <div class="pkg-card-title">
                    <h2>${escapeHtml(pkg.name)}</h2>
                    <p class="ui-muted">v${escapeHtml(pkg.version)} · ${escapeHtml(pkg.category || 'other')}</p>
                    <div class="pkg-card-caps">${caps}</div>
                </div>
                ${_trustBadge(pkg)}
            </div>
            <p class="ui-muted pkg-card-desc">${escapeHtml(pkg.description || '')}</p>
            <p class="ui-muted pkg-card-meta" title="${escapeAttr(meta)}">${escapeHtml(meta)}</p>
            <div class="pkg-card-foot">${action}</div>
        </article>`;
}

function _renderUpdates(content) {
    const updates = _pkg.updates.filter(_matches);
    if (!updates.length) {
        content.innerHTML = emptyState({ icon: 'refresh', title: 'Everything is up to date', hint: 'New versions will appear here after a catalog refresh.' });
        return;
    }
    content.innerHTML = `<div class="ui-list">
        ${updates.map((pkg) => `
            <div class="ui-panel" style="display:flex;align-items:center;gap:12px;">
                <div>${_icon(pkg, 26)}</div>
                <div style="flex:1;min-width:0;">
                    <h2>${escapeHtml(pkg.name)}</h2>
                    <p class="ui-muted">${escapeHtml(pkg.installed_version || '')} → ${escapeHtml(pkg.version)} · ${escapeHtml(pkg.source_name || '')}</p>
                </div>
                ${_pkg.isAdmin ? `<button class="ui-btn small primary" data-update="${escapeAttr(pkg.id)}" data-testid="pkg-update-${escapeAttr(pkg.id)}"${_disabledAttr(pkg.id)}>Update</button>` : ''}
            </div>`).join('')}
    </div>`;
    content.querySelectorAll('[data-update]').forEach((btn) => btn.addEventListener('click', () => _update(btn.dataset.update)));
}

function _renderInstalled(content) {
    if (!_pkg.installed.length) {
        content.innerHTML = emptyState({ icon: 'package', title: 'No packages installed', hint: 'Install one from the Available tab.' });
        return;
    }
    content.innerHTML = `<div class="ui-table-wrap"><table class="ui-table" data-testid="pkg-installed-list">
        <thead><tr><th>Package</th><th>Version</th><th>Status</th><th>Source</th><th class="actions">Actions</th></tr></thead>
        <tbody>${_pkg.installed.map(_installedRow).join('')}</tbody></table></div>`;
    content.querySelectorAll('[data-action]').forEach((btn) => btn.addEventListener('click', () => _action(btn.dataset.id, btn.dataset.action)));
    content.querySelectorAll('[data-uninstall]').forEach((btn) => btn.addEventListener('click', () => _uninstall(btn.dataset.uninstall)));
    content.querySelectorAll('[data-update]').forEach((btn) => btn.addEventListener('click', () => _update(btn.dataset.update)));
    content.querySelectorAll('[data-open-ui]').forEach((btn) => btn.addEventListener('click', () => _openUi(btn.dataset.openUi)));
    content.querySelectorAll('[data-open-logs]').forEach((btn) => btn.addEventListener('click', () => openLogs({ app: `pkg:${btn.dataset.openLogs}` })));
}

function _installedRow(pkg) {
    const status = _pkg.statuses[pkg.id] || 'unknown';
    const disabled = _disabledAttr(pkg.id);
    const busyLabel = _busyLabel(pkg.id);
    // Files-only packages (native UI, no service) have nothing to start or stop.
    const hasService = (pkg.runtime || 'compose') !== 'files';
    const serviceButtons = hasService ? `
        <button class="ui-btn small" data-action="start" data-id="${escapeAttr(pkg.id)}" data-testid="pkg-action-${escapeAttr(pkg.id)}-start"${disabled} title="Start ${escapeAttr(pkg.name)}" aria-label="Start ${escapeAttr(pkg.name)}">${uiIcon('play', 11)}</button>
        <button class="ui-btn small" data-action="restart" data-id="${escapeAttr(pkg.id)}" data-testid="pkg-action-${escapeAttr(pkg.id)}-restart"${disabled} title="Restart ${escapeAttr(pkg.name)}" aria-label="Restart ${escapeAttr(pkg.name)}">${uiIcon('refresh', 11)}</button>
        <button class="ui-btn small" data-action="stop" data-id="${escapeAttr(pkg.id)}" data-testid="pkg-action-${escapeAttr(pkg.id)}-stop"${disabled} title="Stop ${escapeAttr(pkg.name)}" aria-label="Stop ${escapeAttr(pkg.name)}">${uiIcon('stop', 11)}</button>` : '';
    const actions = _pkg.isAdmin ? `
        ${serviceButtons}
        <button class="ui-btn small danger" data-uninstall="${escapeAttr(pkg.id)}" data-testid="pkg-uninstall-${escapeAttr(pkg.id)}"${disabled}>Uninstall</button>` : '';
    const update = _pkg.isAdmin && _pkg.updates.some((u) => u.id === pkg.id)
        ? `<button class="ui-btn small primary" data-update="${escapeAttr(pkg.id)}" data-testid="pkg-update-${escapeAttr(pkg.id)}"${disabled}>${uiIcon('arrowUp', 11)} Update</button>` : '';
    const openUi = pkg.ui && (pkg.ui.mode === 'native' ? pkg.ui.entry : pkg.ui.port)
        ? `<button class="ui-btn small" data-open-ui="${escapeAttr(pkg.id)}">Open UI</button>` : '';
    const openLogsBtn = _pkg.isAdmin
        ? `<button class="ui-btn small" data-open-logs="${escapeAttr(pkg.id)}" data-testid="pkg-logs-${escapeAttr(pkg.id)}" title="Open the unified Logs app filtered to this package">Logs</button>` : '';
    return `
        <tr data-testid="pkg-row-${escapeAttr(pkg.id)}">
            <td><div>${_icon(pkg, 20)} ${escapeHtml(pkg.name)}</div><div class="path">${escapeHtml(pkg.id)}</div></td>
            <td class="path">${escapeHtml(pkg.version)}</td>
            <td data-testid="pkg-status-${escapeAttr(pkg.id)}">${_statusBadge(status)}</td>
            <td>${escapeHtml(pkg.source_name || pkg.source || 'local')}</td>
            <td class="actions"><div class="ui-row" style="justify-content:flex-end;">${busyLabel}${openUi}${openLogsBtn}${update}${actions}</div></td>
        </tr>`;
}

async function _openDetail(pkgId) {
    const pkg = _pkg.catalog.find((p) => p.id === pkgId);
    if (!pkg) return;
    let detail = pkg;
    try {
        detail = { ...pkg, ...(await apiJsonFetch(`/api/packages/catalog/${encodeURIComponent(pkgId)}`)) };
    } catch (_) { /* card data is enough when the detail fetch fails */ }
    const canInstall = _pkg.isAdmin && !detail.installed && _blockedDeps(detail).length === 0;
    const canUpdate = _pkg.isAdmin && detail.update_available && _blockedDeps(detail).length === 0;
    const canUninstall = _pkg.isAdmin && detail.installed && !canUpdate;
    const confirmLabel = canUpdate ? `Update to v${detail.version}` : (canInstall ? 'Install' : (canUninstall ? 'Uninstall' : 'Close'));
    const body = `
        <div style="display:flex;gap:12px;align-items:center;margin-bottom:12px;">
            <div style="width:48px;height:48px;display:flex;align-items:center;justify-content:center;background:var(--surface-2);border-radius:12px;">${_icon(detail, 34)}</div>
            <div>
                <div style="font-size:16px;font-weight:700;">${escapeHtml(detail.name)}</div>
                <div style="font-size:12px;color:var(--text-3);">v${escapeHtml(detail.version)} · ${escapeHtml(detail.author || 'unknown')} · ${_trustBadge(detail)}</div>
                ${_capabilities(detail).includes('kvm') ? `<div style="font-size:11px;color:var(--text-3);margin-top:4px;">${_capabilityBadges(detail)} Requires /dev/kvm (hardware virtualization); falls back to slow software emulation without it.</div>` : ''}
            </div>
        </div>
        <p style="font-size:13px;line-height:1.55;color:var(--text-2);">${escapeHtml(detail.description || '')}</p>
        ${canUninstall ? '<p class="ui-callout error">Uninstalling stops the containers and deletes the package directory. You choose next whether its data volumes and unused images go too.</p>' : ''}
        ${detail.changelog ? `<p style="font-size:12px;color:var(--text-2);white-space:pre-wrap;">${escapeHtml(detail.changelog)}</p>` : ''}
        <div style="font-size:12px;color:var(--text-3);">Source: ${escapeHtml(detail.source_name || detail.source || '')}${detail.key_id ? ` · key ${escapeHtml(detail.key_id)}` : ''}</div>
        ${detail.homepage ? `<div class="ui-panel-body"><a href="${escapeAttr(detail.homepage)}" target="_blank" rel="noopener" style="color:var(--ui-accent);">Homepage</a></div>` : ''}
        ${(detail.requires || []).length ? `<div style="font-size:12px;margin-top:6px;">Requires: ${detail.requires.map(escapeHtml).join(', ')}</div>` : ''}
        ${_systemRequires(detail).length ? `<div style="font-size:12px;margin-top:6px;">System requirements: ${_systemRequires(detail).map((item) => escapeHtml(_systemLabel(item))).join(', ')}${_systemRequires(detail).includes('docker') ? ' — Docker is installed automatically when missing.' : ''}</div>` : ''}
        ${_systemWarning(detail)}
        ${_pythonRequires(detail).length ? `<div style="font-size:12px;margin-top:6px;">Package environment: ${_pythonRequires(detail).map(escapeHtml).join(', ')} — installed into a private virtualenv from bundled wheels, offline.</div>` : ''}
        ${(detail.screenshots || []).length ? `<div style="display:flex;gap:6px;margin-top:10px;overflow-x:auto;">${detail.screenshots.map((url) => `<a href="${escapeAttr(url)}" target="_blank" rel="noopener"><img src="${escapeAttr(url)}" alt="" style="height:90px;border-radius:8px;"></a>`).join('')}</div>` : ''}
        ${detail.installed && _pkg.isAdmin ? `<div style="margin-top:12px;"><button class="btn-ghost" data-testid="pkg-detail-logs" style="font-size:12px;padding:4px 10px;">📋 Open logs for ${escapeHtml(detail.name)}</button></div>` : ''}`;
    await showFormModal({
        title: detail.name,
        width: '620px',
        confirmLabel,
        danger: canUninstall,
        bodyHtml: body,
        onReady: (modalBody) => {
            const logsBtn = modalBody?.querySelector('[data-testid="pkg-detail-logs"]');
            if (logsBtn) logsBtn.addEventListener('click', () => openLogs({ app: `pkg:${detail.id}` }));
        },
        onSubmit: canUpdate || canInstall || canUninstall
            ? async (_values, { setError, close }) => {
                try {
                    if (canUninstall) {
                        // The options dialog needs the screen; close this one first.
                        close();
                        setTimeout(() => { _uninstall(detail.id); }, 0);
                        return false;
                    }
                    await _startJob(canUpdate ? 'update' : 'install', detail);
                    return true;
                } catch (err) {
                    setError(err.detail || err.message);
                    return false;
                }
            }
            : null,
    });
}

async function _install(pkgId) {
    const pkg = _pkg.catalog.find((p) => p.id === pkgId);
    if (!pkg) return;
    try {
        await _startJob('install', pkg);
    } catch (err) {
        addNotification(err.detail || err.message, 'error');
    }
}

async function _update(pkgId) {
    const pkg = _pkg.catalog.find((p) => p.id === pkgId) || _pkg.installed.find((p) => p.id === pkgId);
    if (!pkg) return null;
    try {
        return await _startJob('update', pkg);
    } catch (err) {
        addNotification(err.detail || err.message, 'error');
        return null;
    }
}

/**
 * Update every package with a newer version, one job at a time so a failing
 * update cannot take the others down (the backend rolls each one back).
 */
async function _updateAll() {
    if (!_pkg.isAdmin || _pkg.job || _pkg.updateAll) return;
    const pending = _pkg.updates.map((p) => ({ id: p.id, name: p.name || p.id }));
    if (!pending.length) return;
    const names = pending.map((p) => p.name).join(', ');
    const okay = await showConfirm(
        'Update all packages',
        `${pending.length === 1 ? 'One package' : `${pending.length} packages`} will be updated one at a time`
        + ` (${names}). If an update fails, its previous version is restored and the remaining packages still update.`,
        { confirmLabel: pending.length === 1 ? 'Update' : `Update ${pending.length}` },
    );
    if (!okay) return;

    _pkg.updateAll = { total: pending.length, done: 0, name: '' };
    _syncUpdateAllButton();
    let failed = 0;
    try {
        for (let i = 0; i < pending.length; i += 1) {
            const pkg = pending[i];
            _pkg.updateAll.done = i;
            _pkg.updateAll.name = pkg.name;
            _syncUpdateAllButton();
            const finished = await _update(pkg.id);
            if (!finished || finished.status !== 'success') failed += 1;
        }
    } finally {
        const total = pending.length;
        _pkg.updateAll = null;
        addNotification(
            failed
                ? `Updated ${total - failed} of ${total} packages — ${failed} failed`
                : `All ${total} package${total === 1 ? '' : 's'} updated`,
            failed ? 'error' : 'success',
        );
        await _loadAll();
        _syncUpdateAllButton();
    }
}

async function _startJob(kind, pkg) {
    if (_pkg.job) {
        const current = JOB_TITLES[_pkg.job.kind] || 'Operation';
        throw new Error(`${current} ${_pkg.job.name} is still running`);
    }
    const response = kind === 'install'
        ? await apiJsonFetch('/api/packages/install', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ package_id: pkg.id, source: pkg.source }),
        })
        : await apiJsonFetch(`/api/packages/${encodeURIComponent(pkg.id)}/update`, { method: 'POST' });
    const jobId = response.job_id;
    if (!jobId) throw new Error('Server did not return a job id');
    addNotification(`${kind === 'install' ? 'Installing' : 'Updating'} ${pkg.name}…`, 'info');
    return _watchJob({ id: jobId, kind, package_id: pkg.id, status: 'queued' }, pkg.name);
}

function _renderJobPanel(job, name) {
    const banner = document.getElementById('pkgJobBanner');
    if (!banner) return;
    const kind = job.kind || (_pkg.job ? _pkg.job.kind : 'install');
    const status = job.status || 'running';
    const running = status === 'queued' || status === 'running';
    const pct = typeof job.progress === 'number' && Number.isFinite(job.progress)
        ? Math.max(0, Math.min(100, Math.round(job.progress)))
        : null;
    const total = Number(job.step_total) || 0;
    const index = Math.min(Number(job.step_index) || 0, total);
    const stepLabel = STEP_LABELS[job.step] || job.step || (status === 'queued' ? STEP_LABELS.queued : 'Working');
    const lastLog = Array.isArray(job.log) && job.log.length ? String(job.log[job.log.length - 1]) : '';
    const title = `${JOB_TITLES[kind] || 'Working on'} ${name}`;

    banner.hidden = false;
    banner.className = status === 'failed' ? 'ui-callout error'
        : status === 'success' ? 'ui-callout ok' : 'ui-callout info';

    // Same shared row as uploads and file operations — inline host, so the
    // progress stays inside Packages. No hand-rolled bar here.
    if (!_pkg.task) {
        _pkg.task = startTask({
            appId: 'packages',
            title,
            total,
            detail: stepLabel,
            log: lastLog,
            host: banner,
            testIdPrefix: 'pkg-job',
        });
    }
    _pkg.task.update({
        title,
        detail: stepLabel,
        log: lastLog,
        percent: running ? pct : 100,
        index,
        total,
    });
}

// One promise per job id, so a re-render or `_resumeJob` re-attaching to the
// same job still lets a caller ("Update all") await its outcome.
function _jobDonePromise(jobId) {
    const existing = _jobWaiters.get(jobId);
    if (existing) return existing.promise;
    let resolve;
    const promise = new Promise((res) => { resolve = res; });
    _jobWaiters.set(jobId, { promise, resolve });
    return promise;
}

function _watchJob(job, name) {
    if (_pkg.jobTimer) {
        clearTimeout(_pkg.jobTimer);
        _pkg.jobTimer = null;
    }
    if (_pkg.jobHideTimer) {
        clearTimeout(_pkg.jobHideTimer);
        _pkg.jobHideTimer = null;
    }
    const jobId = job.id;
    const label = name || job.package_id || jobId;
    const done = _jobDonePromise(jobId);
    if (_pkg.task) { _pkg.task.dismiss(); _pkg.task = null; }
    _pkg.job = { id: jobId, kind: job.kind || 'install', packageId: job.package_id || '', name: label };
    _renderJobPanel(job, label);
    _renderContent();  // disable conflicting buttons while the job runs
    _syncUpdateAllButton();

    const tick = async () => {
        let finished = null;
        try {
            const current = await apiJsonFetch(`/api/packages/jobs/${encodeURIComponent(jobId)}`);
            _renderJobPanel(current, label);
            if (current.status === 'success' || current.status === 'failed' || current.status === 'cancelled') {
                finished = current;
            }
        } catch (err) {
            const logEl = document.querySelector('[data-testid="pkg-job-log"]');
            if (logEl) logEl.textContent = `status check failed — ${err.message}`;
        }
        if (finished) {
            _pkg.jobTimer = null;
            _pkg.job = null;
            const task = _pkg.task;
            if (task) {
                const ok = finished.status === 'success';
                const lastLog = Array.isArray(finished.log) && finished.log.length
                    ? String(finished.log[finished.log.length - 1]) : '';
                task.finish(ok ? 'done' : finished.status === 'cancelled' ? 'cancelled' : 'failed', {
                    percent: 100,
                    log: lastLog,
                    detail: finished.error || (ok ? STEP_LABELS.done : finished.status),
                });
            }
            addNotification(`${label}: ${finished.status}${finished.error ? ' — ' + finished.error : ''}`,
                finished.status === 'success' ? 'success' : finished.status === 'cancelled' ? 'info' : 'error');
            if (typeof window.refreshPackageIcons === 'function') window.refreshPackageIcons();
            await _loadAll();
            _syncUpdateAllButton();
            const waiter = _jobWaiters.get(jobId);
            if (waiter) {
                _jobWaiters.delete(jobId);
                waiter.resolve(finished);
            }
            _pkg.jobHideTimer = setTimeout(() => {
                const banner = document.getElementById('pkgJobBanner');
                if (banner) banner.hidden = true;
                if (task) task.dismiss();
                if (_pkg.task === task) _pkg.task = null;
                _pkg.jobHideTimer = null;
            }, 5000);
            return;
        }
        _pkg.jobTimer = setTimeout(tick, 600);
    };
    _pkg.jobTimer = setTimeout(tick, 300);
    return done;
}

/** Re-attach to a running job after the window was reopened or re-rendered. */
async function _resumeJob() {
    if (_pkg.job) {
        if (_pkg.jobTimer) {
            clearTimeout(_pkg.jobTimer);
            _pkg.jobTimer = null;
        }
        _watchJob({ id: _pkg.job.id, kind: _pkg.job.kind, package_id: _pkg.job.packageId, status: 'running' }, _pkg.job.name);
        return;
    }
    try {
        const data = await apiJsonFetch('/api/packages/jobs');
        const active = (data.jobs || []).find((j) => j.status === 'queued' || j.status === 'running');
        if (!active) return;
        const pkg = _pkg.installed.find((p) => p.id === active.package_id)
            || _pkg.catalog.find((p) => p.id === active.package_id);
        _watchJob(active, pkg ? pkg.name : active.package_id);
    } catch (_) { /* the job list is optional; polling still works after a new action */ }
}

async function _action(pkgId, action) {
    try {
        await apiJsonFetch(`/api/packages/${encodeURIComponent(pkgId)}/action`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action }),
        });
        addNotification(`Package ${action} requested`, 'success');
        await _loadAll();
    } catch (err) {
        addNotification(err.detail || err.message, 'error');
    }
}

async function _uninstall(pkgId) {
    const pkg = _pkg.installed.find((p) => p.id === pkgId);
    const name = pkg ? pkg.name : pkgId;
    let inventory = { volumes: [], images: [], shared_images: [] };
    try {
        inventory = await apiJsonFetch(`/api/packages/${encodeURIComponent(pkgId)}/uninstall-preview`);
    } catch (_) { /* the dialog still works without an inventory */ }
    const volumes = inventory.volumes || [];
    const images = inventory.images || [];
    const shared = inventory.shared_images || [];
    const sharedOwners = [...new Set(shared.flatMap((img) => (img.in_use_by || [])
        .map((owner) => owner.project || owner.container || '')))].filter(Boolean);
    const bodyHtml = `
        <p style="margin:0 0 12px;font-size:13px;line-height:1.55;color:var(--text-2);">
            Removing <strong>${escapeHtml(name)}</strong> always stops its containers, deletes its network and
            the package directory. The options below are permanent.
        </p>
        <label style="display:flex;gap:9px;align-items:flex-start;font-size:13px;line-height:1.45;cursor:${volumes.length ? 'pointer' : 'not-allowed'};">
            <input type="checkbox" name="remove_volumes" data-testid="pkg-uninstall-remove-volumes"
                   style="margin-top:2px;"${volumes.length ? '' : ' disabled'}>
            <div>Delete package data${volumes.length ? ` — ${volumes.length} volume${volumes.length === 1 ? '' : 's'}` : ''}
                <div style="font-size:11px;color:var(--text-3);font-family:monospace;margin-top:3px;word-break:break-all;">${volumes.length ? volumes.map(escapeHtml).join(', ') : 'no volumes'}</div>
                <div style="font-size:11px;color:var(--text-3);margin-top:2px;">Settings, logs, certificates and similar state. Cannot be recovered.</div>
            </div>
        </label>
        <label style="display:flex;gap:9px;align-items:flex-start;font-size:13px;line-height:1.45;margin-top:12px;cursor:${images.length ? 'pointer' : 'not-allowed'};">
            <input type="checkbox" name="remove_images" data-testid="pkg-uninstall-remove-images"
                   style="margin-top:2px;"${images.length ? '' : ' disabled'}>
            <div>Remove unused images${images.length ? ` — ${images.length}` : ''}
                <div style="font-size:11px;color:var(--text-3);font-family:monospace;margin-top:3px;word-break:break-all;">${images.length ? images.map((img) => escapeHtml(img.ref)).join(', ') : 'no images'}</div>
                ${shared.length ? `<div style="font-size:11px;color:var(--text-3);margin-top:2px;">Kept — still used by ${escapeHtml(sharedOwners.join(', ') || 'another container')}: ${shared.map((img) => escapeHtml(img.ref)).join(', ')}</div>` : ''}
            </div>
        </label>`;
    const values = await showFormModal({
        title: `Uninstall ${name}`,
        width: '560px',
        danger: true,
        confirmLabel: 'Uninstall',
        bodyHtml,
        onReady: (body, overlay) => {
            const okBtn = overlay.querySelector('[data-testid="modal-form-ok"]');
            const sync = () => {
                const deleting = body.querySelector('[name="remove_volumes"]')?.checked
                    || body.querySelector('[name="remove_images"]')?.checked;
                if (okBtn) {
                    okBtn.textContent = deleting ? 'Delete everything' : 'Uninstall';
                    okBtn.classList.toggle('ethos-modal-btn-danger', deleting);
                }
            };
            body.querySelectorAll('input[type="checkbox"]').forEach((el) => el.addEventListener('change', sync));
            sync();
        },
    });
    if (!values) return;
    await _uninstallDirect(pkgId, name, {
        removeVolumes: values.remove_volumes === true,
        removeImages: values.remove_images === true,
    });
}

async function _uninstallDirect(pkgId, name, { removeVolumes = false, removeImages = false } = {}) {
    try {
        const params = new URLSearchParams();
        if (removeVolumes) params.set('remove_volumes', '1');
        if (removeImages) params.set('remove_images', '1');
        const query = params.toString() ? `?${params.toString()}` : '';
        const response = await apiJsonFetch(`/api/packages/${encodeURIComponent(pkgId)}${query}`, { method: 'DELETE' });
        if (response.job_id) {
            _watchJob({ id: response.job_id, kind: 'uninstall', package_id: pkgId, status: 'queued' }, name || pkgId);
        } else {
            await _loadAll();
        }
    } catch (err) {
        addNotification(err.detail || err.message, 'error');
    }
}

function _openUi(pkgId) {
    const pkg = _pkg.installed.find((p) => p.id === pkgId);
    if (!pkg || !pkg.ui) return;
    if (pkg.ui.mode === 'native' || typeof window.openApp === 'function') {
        window.openApp(`pkg-${pkgId}`);
        return;
    }
    if (!pkg.ui.port) return;
    const scheme = pkg.ui.scheme || 'http';
    const path = pkg.ui.path || '/';
    const loopback = ['127.0.0.1', '::1', '[::1]', '0.0.0.0'];
    const uiHost = loopback.includes(location.hostname) ? 'localhost' : location.hostname;
    window.open(`${scheme}://${uiHost}:${pkg.ui.port}${path}`, '_blank', 'noopener');
}

// ── Sources ─────────────────────────────────────────────────────────────────

async function _toggleSources(body) {
    if (shell) _renderSources(shell.view);
}

function _renderSources(panel) {
    if (!_pkg.isAdmin) {
        panel.innerHTML = '<div class="ui-callout">Only administrators can manage package sources.</div>';
        return;
    }
    const official = _pkg.sources.find((s) => s.url === OFFICIAL_SOURCE.url);
    const officialLabel = official ? 'Fix official source' : '+ Official source';
    panel.innerHTML = `
        <div class="ui-toolbar pkg-sources-toolbar">
            <p class="ui-muted">Native UI packages require a signed source with its public key.</p>
            <div class="ui-row">
                ${!official || official.trust !== 'pinned' ? `<button class="ui-btn small" id="pkgAddOfficialSource" data-testid="pkg-source-add-official">${officialLabel}</button>` : ''}
                <button class="ui-btn small primary" id="pkgAddSource" data-testid="pkg-source-add-btn">+ Add source</button>
            </div>
        </div>
        <div class="ui-list">
            ${_pkg.sources.map((source, index) => `
                <div class="ui-panel pkg-source">
                    <div class="ui-toolbar">
                        <div class="pkg-source-info">
                            <strong>${escapeHtml(source.name || source.url)}</strong>
                            <p class="ui-muted pkg-source-url" title="${escapeAttr(source.url)}">${escapeHtml(source.url)}</p>
                        </div>
                        <div class="ui-row">
                            ${source.trust === 'pinned' ? _badge('signed', 'badge-green') : _badge('unverified', 'badge-yellow')}
                            <button class="ui-btn small" data-edit-source="${index}" data-testid="pkg-source-edit-${index}">Edit</button>
                            <button class="ui-btn small" data-toggle-source="${index}" data-testid="pkg-source-toggle-${index}">${source.enabled === false ? 'Enable' : 'Disable'}</button>
                            <button class="ui-btn small danger" data-remove-source="${index}" data-testid="pkg-source-remove-${index}">Remove</button>
                        </div>
                    </div>
                </div>`).join('') || emptyState({ icon: 'package', title: 'No sources configured', hint: 'Add the official signed repository.' })}
        </div>`;
    panel.querySelector('#pkgAddSource')?.addEventListener('click', () => _addSourceDialog());
    panel.querySelector('#pkgAddOfficialSource')?.addEventListener('click', () => _addSourceDialog(OFFICIAL_SOURCE));
    panel.querySelectorAll('[data-edit-source]').forEach((btn) => btn.addEventListener('click', () => _editSourceDialog(Number(btn.dataset.editSource))));
    panel.querySelectorAll('[data-remove-source]').forEach((btn) => btn.addEventListener('click', () => _removeSource(Number(btn.dataset.removeSource))));
    panel.querySelectorAll('[data-toggle-source]').forEach((btn) => btn.addEventListener('click', () => _toggleSource(Number(btn.dataset.toggleSource))));
}

async function _addSourceDialog(preset = null) {
    const p = preset || {};
    const values = await showFormModal({
        title: preset ? 'Add official package source' : 'Add package source',
        width: '560px',
        confirmLabel: 'Add source',
        bodyHtml: `
            <div style="display:flex;flex-direction:column;gap:10px;font-size:12px;">
                ${preset ? '<p style="margin:0;color:var(--text-3);line-height:1.5;">This adds the official repository as a <strong>Signed</strong> source with its pinned public key.</p>' : ''}
                <label for="pkg-source-name">Name</label>
                <input id="pkg-source-name" name="name" class="ethos-modal-input" placeholder="Community packages" value="${escapeAttr(p.name || '')}">
                <label for="pkg-source-url">Repository URL (GitHub repo or index.json)</label>
                <input id="pkg-source-url" name="url" data-testid="pkg-source-input" class="ethos-modal-input" placeholder="https://github.com/owner/repo" value="${escapeAttr(p.url || '')}">
                <label for="pkg-source-key">Ed25519 public key (base64, optional for unverified)</label>
                <input id="pkg-source-key" name="public_key" class="ethos-modal-input" placeholder="base64…" value="${escapeAttr(p.public_key || '')}">
                <label for="pkg-source-trust">Trust</label>
                <select id="pkg-source-trust" name="trust" class="ethos-modal-input" aria-label="Source trust">
                    <option value="pinned"${p.trust !== 'unverified' ? ' selected' : ''}>Signed (pinned key)</option>
                    <option value="unverified"${p.trust === 'unverified' ? ' selected' : ''}>Unverified (accept unsigned)</option>
                </select>
            </div>`,
        onSubmit: async (values, { setError }) => {
            try {
                await apiJsonFetch('/api/packages/registry/sources', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(values),
                });
                addNotification('Package source added', 'success');
                await _loadAll();
                if (_pkg.tab === 'sources' && shell) _renderSources(shell.view);
                return true;
            } catch (err) {
                setError(err.detail || err.message);
                return false;
            }
        },
    });
    return values;
}

async function _editSourceDialog(index) {
    const source = _pkg.sources[index];
    if (!source) return;
    await showFormModal({
        title: 'Edit package source',
        width: '560px',
        confirmLabel: 'Save',
        bodyHtml: `
            <div style="display:flex;flex-direction:column;gap:10px;font-size:12px;">
                <p style="margin:0;color:var(--text-3);line-height:1.5;">Native UI packages require <strong>Trust: Signed</strong> and the repository public key. Unverified sources can still host window/port packages.</p>
                <label for="pkg-edit-url">Repository URL</label>
                <input id="pkg-edit-url" value="${escapeAttr(source.url)}" class="ethos-modal-input" readonly>
                <label for="pkg-edit-name">Name</label>
                <input id="pkg-edit-name" name="name" value="${escapeAttr(source.name || '')}" class="ethos-modal-input">
                <label for="pkg-edit-key">Ed25519 public key (base64)</label>
                <input id="pkg-edit-key" name="public_key" value="${escapeAttr(source.public_key || '')}" class="ethos-modal-input" placeholder="base64…">
                <label for="pkg-edit-trust">Trust</label>
                <select id="pkg-edit-trust" name="trust" class="ethos-modal-input" aria-label="Source trust">
                    <option value="pinned"${source.trust === 'pinned' ? ' selected' : ''}>Signed (pinned key)</option>
                    <option value="unverified"${source.trust !== 'pinned' ? ' selected' : ''}>Unverified (accept unsigned)</option>
                </select>
            </div>`,
        onSubmit: async (values, { setError }) => {
            try {
                await apiJsonFetch(`/api/packages/registry/sources/${index}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: String(values.name || '').trim(),
                        public_key: String(values.public_key || '').trim(),
                        trust: values.trust,
                    }),
                });
                addNotification('Package source updated', 'success');
                await _loadAll();
                if (_pkg.tab === 'sources' && shell) _renderSources(shell.view);
                return true;
            } catch (err) {
                setError(err.detail || err.message);
                return false;
            }
        },
    });
}

async function _removeSource(index) {
    const source = _pkg.sources[index];
    if (!await showConfirm('Remove source', `Remove "${source?.name || source?.url}"? Installed packages stay installed.`, { danger: true, confirmLabel: 'Remove' })) return;
    try {
        await apiJsonFetch(`/api/packages/registry/sources/${index}`, { method: 'DELETE' });
        addNotification('Source removed', 'success');
        await _loadAll();
        if (_pkg.tab === 'sources' && shell) _renderSources(shell.view);
    } catch (err) {
        addNotification(err.detail || err.message, 'error');
    }
}

async function _toggleSource(index) {
    const source = _pkg.sources[index];
    try {
        await apiJsonFetch(`/api/packages/registry/sources/${index}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: source.enabled === false }),
        });
        await _loadAll();
        if (_pkg.tab === 'sources' && shell) _renderSources(shell.view);
    } catch (err) {
        addNotification(err.detail || err.message, 'error');
    }
}

// Compatibility hooks used by app.js.
window._renderInstalledPackages = () => _loadAll();
window._renderAvailablePackages = () => _loadAll();
window._uninstallPkg = (pkgId) => _uninstall(pkgId);
window._pkgWatchJob = _watchJob;
window._pkgAction = (pkgId, action, btn) => {
    if (btn) btn.disabled = true;
    _action(pkgId, action).finally(() => { if (btn) btn.disabled = false; });
};
