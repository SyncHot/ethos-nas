// EthOS NAS — shared rendering for user-to-user grants.
//
// Used by the Sharing app and Settings → Sharing so both show the same tables,
// labels and empty states. No local styles, no client-side permission logic:
// the server already filtered incoming/outgoing grants for the caller.

import { apiRequest } from './api.js';
import { openPathInFiles } from './app-links.js';
import { emptyState } from './states.js';
import { escapeAttr, escapeHtml } from './utils.js';

export const KIND_META = {
    path: { label: 'File / folder', app: 'Files', appId: 'files' },
    album: { label: 'Photo album', app: 'Photos', appId: 'gallery' },
    playlist: { label: 'Playlist', app: 'Music', appId: 'music' },
};

export function kindMeta(kind) {
    return KIND_META[kind] || { label: kind || 'Item', app: 'App', appId: '' };
}

export function formatWhen(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleDateString(undefined, {
            year: 'numeric', month: 'short', day: 'numeric',
        });
    } catch {
        return String(iso);
    }
}

export function audienceLabel(grant) {
    if (grant.all) return '<span class="ui-tag is-info">All users</span>';
    const names = grant.recipients || [];
    if (!names.length) return '<span class="ui-muted">Nobody</span>';
    return names.map((name) => `<span class="ui-chip">${escapeHtml(name)}</span>`).join(' ');
}

export async function fetchIncoming(kind = '') {
    const query = kind ? `?kind=${encodeURIComponent(kind)}` : '';
    const data = await apiRequest(`/api/sharing/incoming${query}`);
    return data?.grants || [];
}

export async function fetchOutgoing(kind = '') {
    const query = kind ? `?kind=${encodeURIComponent(kind)}` : '';
    const data = await apiRequest(`/api/sharing/outgoing${query}`);
    return data?.grants || [];
}

export function incomingTableHtml(grants) {
    if (!grants.length) {
        return emptyState({
            icon: 'files',
            title: 'Nothing shared with you',
            hint: 'Items another account shares with you appear here.',
        });
    }
    const rows = grants.map((grant) => {
        const meta = kindMeta(grant.kind);
        return `<tr data-share-grant="${escapeAttr(grant.id)}">
            <td>
                <div class="ui-list-row-main">${escapeHtml(grant.label || grant.resource)}</div>
                <div class="ui-muted">${escapeHtml(meta.label)}</div>
            </td>
            <td>${escapeHtml(grant.owner)}</td>
            <td>${escapeHtml(meta.app)}</td>
            <td>${escapeHtml(formatWhen(grant.updated_at || grant.created_at))}</td>
            <td class="actions">
                <button class="ui-btn small" data-share-open="${escapeAttr(grant.id)}"
                        data-testid="sharing-open-${escapeAttr(grant.id)}">Open</button>
            </td>
        </tr>`;
    }).join('');
    return `<div class="ui-table-wrap"><table class="ui-table" data-testid="sharing-incoming-table">
        <thead><tr><th>Shared item</th><th>Owner</th><th>App</th><th>Shared</th><th class="actions">Actions</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
}

export function outgoingTableHtml(grants) {
    if (!grants.length) {
        return emptyState({
            icon: 'files',
            title: 'You have not shared anything',
            hint: 'Use “Share with users…” in Files or the Sharing panel inside an app.',
        });
    }
    const rows = grants.map((grant) => {
        const meta = kindMeta(grant.kind);
        return `<tr data-share-grant="${escapeAttr(grant.id)}">
            <td>
                <div class="ui-list-row-main">${escapeHtml(grant.label || grant.resource)}</div>
                <div class="ui-muted">${escapeHtml(meta.label)}</div>
            </td>
            <td>${audienceLabel(grant)}</td>
            <td>${escapeHtml(formatWhen(grant.updated_at || grant.created_at))}</td>
            <td class="actions">
                <button class="ui-btn small" data-share-edit="${escapeAttr(grant.id)}"
                        data-testid="sharing-edit-${escapeAttr(grant.id)}">Edit</button>
                <button class="ui-btn small danger" data-share-revoke="${escapeAttr(grant.id)}"
                        data-testid="sharing-revoke-${escapeAttr(grant.id)}">Revoke</button>
            </td>
        </tr>`;
    }).join('');
    return `<div class="ui-table-wrap"><table class="ui-table" data-testid="sharing-outgoing-table">
        <thead><tr><th>Shared item</th><th>Shared with</th><th>Updated</th><th class="actions">Actions</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`;
}

export function wireSharingActions(root, { grants = [], onOpen, onEdit, onRevoke } = {}) {
    if (!root) return;
    const byId = new Map(grants.map((grant) => [grant.id, grant]));
    root.addEventListener('click', (event) => {
        const open = event.target.closest('[data-share-open]');
        if (open && onOpen) return onOpen(byId.get(open.dataset.shareOpen) || null);
        const edit = event.target.closest('[data-share-edit]');
        if (edit && onEdit) return onEdit(byId.get(edit.dataset.shareEdit) || null);
        const revoke = event.target.closest('[data-share-revoke]');
        if (revoke && onRevoke) return onRevoke(byId.get(revoke.dataset.shareRevoke) || null);
    });
}

/** Open the owning app for a grant. Paths ask Files to navigate once mounted. */
export function openGrantApp(grant) {
    if (!grant) return;
    if (grant.kind === 'path') {
        openPathInFiles(grant.resource);
        return;
    }
    window.openApp?.(kindMeta(grant.kind).appId);
}
