// EthOS NAS — Sharing (core app)
//
// “Shared with me” / “Shared by me” for view-only user grants. One place to
// discover incoming resources and manage outgoing grants; the per-app panels
// and Files context menu reuse the same backend and dialog.

import { apiRequest } from '../core/api.js';
import { showConfirm } from '../core/modal.js';
import { notify } from '../core/notification-center.js';
import { skeletonRows, errorState } from '../core/states.js';
import { mountShell } from '../core/ui-shell.js';
import { uiIcon } from '../core/icons.js';
import { logError } from '../core/logger.js';
import { pickFile, pickFolder } from '../core/file-picker.js';
import { openShareDialog } from '../core/share-dialog.js';
import {
    fetchIncoming, fetchOutgoing, incomingTableHtml, outgoingTableHtml,
    wireSharingActions, openGrantApp,
} from '../core/sharing-ui.js';

const SECTIONS = [
    {
        id: 'incoming', label: 'Shared with me', title: 'Shared with me',
        subtitle: 'Resources other accounts have made available to you.',
    },
    {
        id: 'outgoing', label: 'Shared by me', title: 'Shared by me',
        subtitle: 'Resources you have shared, and who can view them.',
    },
];

let shell = null;
let navToken = 0;
let incoming = [];
let outgoing = [];

export async function renderSharingPage(body) {
    if (!body) return;
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'sharing',
        title: 'Sharing',
        subtitle: SECTIONS[0].subtitle,
        icon: '🤝',
        sections: SECTIONS,
        note: 'Recipients can view and stream shared items — never edit or re-share them.',
    });
    shell.onSection((id) => renderSection(id));
    shell.setActive('incoming');
}

async function renderSection(id) {
    if (!shell) return;
    const token = ++navToken;
    const isCurrent = () => token === navToken && shell?.root.isConnected;
    shell.view.innerHTML = skeletonRows(5);
    shell.setStatus('Loading…');
    shell.setPill('starting', 'Loading…');
    try {
        if (id === 'outgoing') await renderOutgoing(isCurrent);
        else await renderIncoming(isCurrent);
        if (!isCurrent()) return;
        shell.setPill('ready', 'Ready');
    } catch (err) {
        if (!isCurrent()) return;
        const detail = err?.detail || err?.message || 'Unknown error';
        shell.view.innerHTML = errorState({ detail, retry: false });
        shell.setStatus('Could not load sharing', 'error');
        shell.setPill('offline', 'Error');
        logError('Sharing: could not load the section', detail);
    }
}

function _toolbar(title, count, testid, actions = '') {
    return `<div class="ui-toolbar">
        <div>
            <h2>${title}</h2>
            <p class="ui-muted">${count} item${count === 1 ? '' : 's'}</p>
        </div>
        <div class="ui-row">
            ${actions}
            <button class="ui-btn small" data-refresh data-testid="${testid}">Refresh</button>
        </div>
    </div>`;
}

async function renderIncoming(isCurrent) {
    incoming = await fetchIncoming();
    if (!isCurrent()) return;
    shell.view.innerHTML = `<section class="ui-panel" data-testid="sharing-incoming-panel">
        ${_toolbar('Shared with me', incoming.length, 'sharing-incoming-refresh')}
        <div data-sharing-table>${incomingTableHtml(incoming)}</div>
    </section>`;
    shell.setStatus(incoming.length ? `${incoming.length} shared with you` : 'Nothing shared with you');
    const panel = shell.view.querySelector('.ui-panel');
    wireSharingActions(panel, {
        grants: incoming,
        onOpen: (grant) => openGrantApp(grant),
    });
    panel?.querySelector('[data-refresh]')?.addEventListener('click', () => renderSection('incoming'));
}

async function renderOutgoing(isCurrent) {
    outgoing = await fetchOutgoing();
    if (!isCurrent()) return;
    shell.view.innerHTML = `<section class="ui-panel" data-testid="sharing-outgoing-panel">
        ${_toolbar('Shared by me', outgoing.length, 'sharing-outgoing-refresh', `
            <button class="ui-btn small primary" data-new-share="folder" data-testid="sharing-new-folder">${uiIcon('folder', 12)} Share folder…</button>
            <button class="ui-btn small" data-new-share="file" data-testid="sharing-new-file">${uiIcon('plus', 12)} Share file…</button>`)}
        <div data-sharing-table>${outgoingTableHtml(outgoing)}</div>
    </section>`;
    shell.setStatus(outgoing.length ? `${outgoing.length} active share${outgoing.length === 1 ? '' : 's'}` : 'You have not shared anything');
    const panel = shell.view.querySelector('.ui-panel');
    wireSharingActions(panel, {
        grants: outgoing,
        onEdit: async (grant) => {
            if (!grant) return;
            await openShareDialog({ kind: grant.kind, resource: grant.resource, label: grant.label });
            await renderSection('outgoing');
        },
        onRevoke: async (grant) => {
            if (!grant) return;
            const ok = await showConfirm(
                'Revoke sharing?',
                `“${grant.label || grant.resource}” will stop being available to ${grant.all ? 'all users' : 'its recipients'}.`,
                { danger: true, confirmLabel: 'Revoke' },
            );
            if (!ok) return;
            try {
                await apiRequest(`/api/sharing/grants/${encodeURIComponent(grant.id)}`, { method: 'DELETE' });
                notify({ kind: 'success', title: 'Sharing revoked', message: grant.label || grant.resource });
                await renderSection('outgoing');
            } catch (err) {
                notify({
                    kind: 'error', title: 'Could not revoke sharing',
                    message: err?.detail || err?.message || 'Unknown error',
                });
            }
        },
    });
    panel?.querySelector('[data-refresh]')?.addEventListener('click', () => renderSection('outgoing'));
    panel?.querySelectorAll('[data-new-share]').forEach((button) => {
        button.addEventListener('click', () => startShare(button.dataset.newShare));
    });
}

/** Pick a folder or file, then reuse the one shared dialog (Files/Photos/Music use it too). */
async function startShare(mode) {
    const picked = mode === 'folder'
        ? await pickFolder({ title: 'Select a folder to share' })
        : await pickFile({ title: 'Select a file to share' });
    if (!picked) return;
    const label = String(picked).split('/').filter(Boolean).pop() || String(picked);
    try {
        const saved = await openShareDialog({ kind: 'path', resource: picked, label });
        if (saved) await renderSection('outgoing');
    } catch (err) {
        logError('Sharing: could not open the share dialog', err?.detail || err?.message || String(err));
        notify({
            kind: 'error', title: 'Could not share this item',
            message: err?.detail || err?.message || 'The share dialog could not be opened.',
        });
    }
}
