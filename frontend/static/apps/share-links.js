// EthOS NAS — Shared Links (single-pane shell)
// Public tokenised share URLs for files and folders.
import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiRequest } from '../core/api.js';
import { skeletonRows, emptyState, errorState } from '../core/states.js';
import { showConfirm } from '../core/modal.js';
import { notify } from '../core/notification-center.js';
import { uiIcon } from '../core/icons.js';
import { logError } from '../core/logger.js';
import { mountShell } from '../core/ui-shell.js';

const STATUS_META = {
    active: { label: 'Active', tone: 'is-ok' },
    expired: { label: 'Expired', tone: '' },
    limit_reached: { label: 'Limit reached', tone: 'is-warn' },
};

let shell = null;
let links = [];
let busy = false;

function _linkUrl(token) {
    return `${location.origin}/api/share-links/public/${token}`;
}

function _fmtDate(iso) {
    if (!iso) return '—';
    try {
        return new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    } catch (err) {
        logError('Shared links: could not format a date', err?.message || String(err));
        return String(iso);
    }
}

export async function renderShareLinksPage(body) {
    if (!body) return;
    // Pre-fill path when opened from Files' "Share link…" context menu.
    const ctx = window.__fmShareCtx || null;
    if (ctx && ctx.path) delete window.__fmShareCtx;
    const prefillPath = ctx ? escapeAttr(ctx.path) : '';
    const prefillName = ctx && ctx.name ? escapeAttr(ctx.name) : '';

    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'share-links',
        title: 'Shared Links',
        subtitle: 'Public, tokenised URLs for files and folders.',
        icon: '🔗',
        sections: [{ id: 'links', label: 'Links' }],
        rail: false,
        note: 'Anyone with the URL can download until it expires.',
    });

    shell.view.innerHTML = `
        <section class="ui-panel" data-testid="sl-create-panel">
            <div class="ui-toolbar">
                <h2>New share link</h2>
                <span class="ui-tag is-info">public URL</span>
            </div>
            <div class="ui-fields ui-panel-body">
                <div class="ui-field full">
                    <label for="slNewPath">Path (file or folder)</label>
                    <input id="slNewPath" class="ui-input mono" data-testid="sl-new-path" placeholder="/home/user/photos/vacation.jpg" value="${prefillPath}">
                </div>
                <div class="ui-field">
                    <label for="slNewName">Label (optional)</label>
                    <input id="slNewName" class="ui-input" data-testid="sl-new-name" placeholder="vacation.jpg" value="${prefillName}">
                </div>
                <div class="ui-field">
                    <label for="slNewExpiry">Expires</label>
                    <select id="slNewExpiry" class="ui-select" data-testid="sl-new-expiry" aria-label="Expiry">
                        <option value="">Never</option>
                        <option value="1">1 day</option>
                        <option value="7">7 days</option>
                        <option value="30">30 days</option>
                    </select>
                </div>
                <div class="ui-field">
                    <label for="slNewMaxDl">Max downloads</label>
                    <input id="slNewMaxDl" class="ui-input" data-testid="sl-new-maxdl" type="number" min="1" placeholder="∞">
                </div>
            </div>
            <div class="ui-form-actions">
                <button id="slCreateBtn" class="ui-btn primary" data-testid="sl-create-btn">${uiIcon('plus', 12)} Create link</button>
            </div>
        </section>

        <section class="ui-panel">
            <div class="ui-table-wrap">
                <table class="ui-table" data-testid="sl-table">
                    <thead><tr><th>Name</th><th>Path</th><th>Created</th><th>Expires</th><th>Downloads</th><th>Status</th><th class="actions">Actions</th></tr></thead>
                    <tbody id="slTableBody"><tr><td colspan="7">${skeletonRows(5)}</td></tr></tbody>
                </table>
            </div>
        </section>`;

    shell.view.querySelector('#slCreateBtn')?.addEventListener('click', () => createLink());
    shell.view.querySelector('#slTableBody')?.addEventListener('click', (event) => {
        const button = event.target.closest('[data-act]');
        if (button) handleRowAction(button);
    });
    shell.view.querySelector('#slNewPath')?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') createLink();
    });

    await loadLinks();
}

async function loadLinks() {
    if (!shell) return;
    const tbody = shell.view.querySelector('#slTableBody');
    if (!tbody) return;
    try {
        const data = await apiRequest('/api/share-links');
        if (!shell.root.isConnected) return;
        links = data.links || [];

        if (!links.length) {
            tbody.innerHTML = `<tr><td colspan="7">${emptyState({ icon: 'folder', title: 'No share links yet', hint: 'Create one above to share a file or folder.' })}</td></tr>`;
            shell.setStatus('No links', 'warn');
            return;
        }

        tbody.innerHTML = links.map((link) => {
            const status = STATUS_META[link.status] || STATUS_META.active;
            const downloads = link.max_downloads ? `${link.downloads}/${link.max_downloads}` : String(link.downloads);
            const multi = Array.isArray(link.files) && link.files.length > 0;
            const pathCell = multi
                ? `<span title="${escapeAttr(link.files.join('\n'))}">${link.files.length} photos</span>`
                : escapeHtml(link.path);
            return `<tr data-testid="sl-row-${escapeAttr(link.id)}">
                <td>${escapeHtml(link.name)}</td>
                <td class="path">${pathCell}</td>
                <td>${escapeHtml(_fmtDate(link.created_at))}</td>
                <td>${escapeHtml(link.expires_at ? _fmtDate(link.expires_at) : 'Never')}</td>
                <td class="num">${escapeHtml(downloads)}</td>
                <td><span class="ui-tag ${status.tone}">${status.label}</span></td>
                <td class="actions"><div class="ui-row" style="justify-content:flex-end;">
                    <button class="ui-btn small" data-act="copy" data-token="${escapeAttr(link.token)}">Copy</button>
                    <button class="ui-btn small" data-act="open" data-token="${escapeAttr(link.token)}">Open</button>
                    <button class="ui-btn small danger" data-act="del" data-id="${escapeAttr(link.id)}">Delete</button>
                </div></td>
            </tr>`;
        }).join('');

        shell.setStatus(`${links.length} link(s)`, 'ok');
    } catch (err) {
        if (!shell.root.isConnected) return;
        tbody.innerHTML = `<tr><td colspan="7">${errorState({ detail: err?.detail || err?.message || 'Unknown error' })}</td></tr>`;
        shell.setStatus('Could not read share links', 'error');
        logError('Shared links: list unavailable', err?.detail || err?.message);
    }
}

async function createLink() {
    if (busy || !shell) return;
    const pathEl = shell.view.querySelector('#slNewPath');
    const path = (pathEl?.value || '').trim();
    if (!path) {
        notify({ kind: 'warning', message: 'Enter a file or folder path first' });
        return;
    }

    busy = true;
    const button = shell.view.querySelector('#slCreateBtn');
    button.disabled = true;
    button.textContent = 'Creating…';
    try {
        const payload = { path };
        const name = (shell.view.querySelector('#slNewName')?.value || '').trim();
        if (name) payload.name = name;
        const expires = shell.view.querySelector('#slNewExpiry')?.value;
        if (expires) payload.expires_in_days = Number(expires);
        const maxDownloads = parseInt(shell.view.querySelector('#slNewMaxDl')?.value || '', 10);
        if (maxDownloads > 0) payload.max_downloads = maxDownloads;

        const link = await apiRequest('/api/share-links', { method: 'POST', body: payload });
        pathEl.value = '';
        shell.view.querySelector('#slNewName').value = '';
        notify({ kind: 'success', message: `Share link created for “${link.name}”` });
        await loadLinks();
    } catch (err) {
        logError('Shared links: create failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Could not create the link' });
    } finally {
        busy = false;
        if (button.isConnected) {
            button.disabled = false;
            button.innerHTML = `${uiIcon('plus', 12)} Create link`;
        }
    }
}

async function handleRowAction(button) {
    const action = button.dataset.act;
    if (action === 'copy') {
        await copyText(_linkUrl(button.dataset.token));
    } else if (action === 'open') {
        window.open(_linkUrl(button.dataset.token), '_blank', 'noopener');
    } else if (action === 'del') {
        const ok = await showConfirm('Delete share link', 'Anyone holding this URL will lose access. Continue?', { danger: true });
        if (!ok) return;
        try {
            await apiRequest(`/api/share-links/${encodeURIComponent(button.dataset.id)}`, { method: 'DELETE' });
            notify({ kind: 'success', message: 'Share link deleted' });
            await loadLinks();
        } catch (err) {
            logError('Shared links: delete failed', err?.detail || err?.message);
            notify({ kind: 'error', message: err?.detail || err?.message || 'Could not delete the link' });
        }
    }
}

async function copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
        notify({ kind: 'success', message: 'Link copied to clipboard' });
    } catch (err) {
        logError('Shared links: clipboard unavailable', err?.message || String(err));
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        document.body.appendChild(textarea);
        textarea.select();
        try {
            document.execCommand('copy');
            notify({ kind: 'success', message: 'Link copied to clipboard' });
        } catch (fallbackError) {
            logError('Shared links: fallback copy failed', fallbackError?.message || String(fallbackError));
            notify({ kind: 'error', message: 'Failed to copy link' });
        }
        textarea.remove();
    }
}
