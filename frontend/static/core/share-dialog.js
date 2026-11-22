// EthOS NAS — reusable “Share with other users” dialog
//
// One implementation for core apps and native packages (via ctx.share.open).
// View-only grants: the dialog only manages recipients; the backend enforces
// read scope. Uses the global modal + .ui-* classes, never local styles.

import { apiRequest } from './api.js';
import { showFormModal } from './modal.js';
import { notify } from './notification-center.js';
import { escapeAttr, escapeHtml } from './utils.js';
import { logError } from './logger.js';

const KIND_NOUN = {
    path: 'file or folder',
    album: 'album',
    playlist: 'playlist',
};

function _kindNoun(kind) {
    return KIND_NOUN[kind] || 'item';
}

function _userRow(user, checked) {
    const role = user.role === 'admin' ? ' <span class="ui-tag">admin</span>' : '';
    return `<label class="ui-list-row" data-share-row data-username="${escapeAttr(user.username)}">
        <input type="checkbox" name="share_user_${escapeAttr(user.username)}"
               data-share-user="${escapeAttr(user.username)}"
               data-testid="share-user-${escapeAttr(user.username)}"
               ${checked ? 'checked' : ''}>
        <span class="ui-list-row-main">${escapeHtml(user.username)}${role}</span>
    </label>`;
}

/**
 * Open the share dialog for one resource.
 *
 * @param {object} opts
 * @param {'path'|'album'|'playlist'} opts.kind
 * @param {string} opts.resource  path / album id / playlist id
 * @param {string} [opts.label]   display name shown in the dialog
 * @returns {Promise<object|null>} saved grant, or null when the dialog closed
 */
export async function openShareDialog({ kind, resource, label = '' } = {}) {
    if (!kind || !resource) throw new Error('openShareDialog needs a kind and a resource');

    let directory;
    let current = null;
    try {
        [directory, current] = await Promise.all([
            apiRequest('/api/sharing/users'),
            apiRequest(`/api/sharing/resource?kind=${encodeURIComponent(kind)}&resource=${encodeURIComponent(resource)}`)
                .catch((err) => {
                    logError('Sharing: could not load the current grant', err?.detail || err?.message);
                    return { grant: null };
                }),
        ]);
    } catch (err) {
        notify({
            kind: 'error',
            title: 'Could not open sharing',
            message: err?.detail || err?.message || 'The sharing directory is unavailable.',
        });
        return null;
    }

    const existing = current?.grant || null;
    const selected = new Set(existing?.recipients || []);
    const self = directory?.self || '';
    const users = (directory?.users || []).filter((user) => user.username !== self);
    const noun = _kindNoun(kind);
    const name = label || resource;
    const shareAll = Boolean(existing?.all);

    const bodyHtml = `
        <div class="ui-fields">
            <div class="ui-field full">
                <div class="ui-callout">Sharing <strong>${escapeHtml(name)}</strong> — recipients can view and stream it, but cannot change it.</div>
            </div>
            <div class="ui-field full">
                <label class="ui-check">
                    <input type="checkbox" name="share_all" data-testid="share-all" ${shareAll ? 'checked' : ''}>
                    <span>All users</span>
                </label>
                <p class="ui-muted">Every active account gets access, including accounts created later.</p>
            </div>
            <div class="ui-field full">
                <label for="shareFilter">Users</label>
                <input id="shareFilter" class="ui-input" data-testid="share-filter"
                       placeholder="Filter users…" autocomplete="off">
            </div>
            <div class="ui-field full">
                <div class="ui-list" data-testid="share-user-list">
                    ${users.length
                        ? users.map((user) => _userRow(user, selected.has(user.username))).join('')
                        : `<div class="ui-empty" data-testid="share-no-users">No other accounts yet. Create one in Settings → Users.</div>`}
                </div>
            </div>
        </div>`;

    let modalBody = null;
    const saved = await showFormModal({
        title: existing ? `Sharing ${noun}` : `Share ${noun}`,
        confirmLabel: existing ? 'Update sharing' : 'Share',
        bodyHtml,
        onReady(body) {
            modalBody = body;
            const allToggle = body.querySelector('[data-testid="share-all"]');
            const filter = body.querySelector('[data-testid="share-filter"]');
            const rows = [...body.querySelectorAll('[data-share-row]')];
            const checkboxes = [...body.querySelectorAll('[data-share-user]')];

            const syncAll = () => {
                const on = Boolean(allToggle?.checked);
                for (const box of checkboxes) {
                    box.disabled = on;
                    if (on) box.checked = false;
                }
            };
            allToggle?.addEventListener('change', syncAll);
            syncAll();

            filter?.addEventListener('input', () => {
                const query = filter.value.trim().toLowerCase();
                for (const row of rows) {
                    row.hidden = Boolean(query) && !row.dataset.username.includes(query);
                }
            });
            filter?.addEventListener('keydown', (event) => {
                // The modal submits on Enter; filtering should never close it.
                if (event.key === 'Enter') {
                    event.preventDefault();
                    event.stopPropagation();
                }
            });
        },
        async onSubmit(values, { setError }) {
            const all = Boolean(modalBody?.querySelector('[data-testid="share-all"]')?.checked);
            const recipients = all ? [] : [...(modalBody?.querySelectorAll('[data-share-user]:checked') || [])]
                .map((box) => box.dataset.shareUser);

            if (!all && recipients.length === 0) {
                if (existing) {
                    await apiRequest(`/api/sharing/grants/${encodeURIComponent(existing.id)}`, { method: 'DELETE' });
                    notify({ kind: 'success', title: 'Sharing removed', message: `${name} is private again.` });
                    return true;
                }
                setError('Select at least one user, or enable “All users”.');
                return false;
            }

            try {
                const response = await apiRequest('/api/sharing/grants', {
                    method: 'POST',
                    body: { kind, resource, label, recipients, all },
                });
                notify({
                    kind: 'success',
                    title: existing ? 'Sharing updated' : 'Shared',
                    message: all ? `${name} is shared with all users.` : `${name} is shared with ${recipients.length} user${recipients.length === 1 ? '' : 's'}.`,
                });
                return response?.grant || true;
            } catch (err) {
                setError(err?.detail || err?.message || 'Could not save the share.');
                return false;
            }
        },
    });
    return saved === null ? null : true;
}
