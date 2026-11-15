// EthOS NAS — Shared Links (public tokenized share URLs for files/folders)
import { escapeAttr, escapeHtml } from '../core/utils.js';
import { apiFetch } from '../core/api.js';
import { skeletonRows } from '../core/states.js';

let _links = [];
let _busy = false;

const STATUS_META = {
    active:          { label: 'Active',  cls: 'sl-chip-green' },
    expired:         { label: 'Expired', cls: 'sl-chip-gray' },
    limit_reached:   { label: 'Limit reached', cls: 'sl-chip-yellow' },
};

function _linkUrl(token) {
    return `${location.origin}/api/share-links/public/${token}`;
}

function _fmtDate(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    } catch (_) { return iso; }
}

export async function renderShareLinksPage(body) {
    // Pre-fill path when opened from Files' "Share link…" context menu.
    const ctx = window.__fmShareCtx || null;
    if (ctx && ctx.path) delete window.__fmShareCtx;
    const prefillPath = ctx ? escapeHtml(ctx.path) : '';
    const prefillName = ctx && ctx.name ? escapeHtml(ctx.name) : '';

    body.innerHTML = `
        <div style="display:flex;flex-direction:column;height:100%;background:var(--surface-0);color: var(--text-1);font-family:system-ui,sans-serif;">
            <style>
                .sl-chip { display:inline-block;padding:2px 8px;border-radius:9px;font-size:11px;font-weight:600; }
                .sl-chip-green  { background:rgba(46,160,67,.15);color:#3fb950; }
                .sl-chip-yellow { background:rgba(210,153,34,.18);color:#d29922; }
                .sl-chip-gray   { background:rgba(110,118,129,.18);color:var(--text-3); }
                .sl-input { background:var(--surface-2);border:1px solid var(--border);color: var(--text-1);border-radius:6px;padding:7px 10px;font-size:13px;width:100%;box-sizing:border-box; }
                .sl-btn { border:none;border-radius:6px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer; }
                .sl-btn-primary { background:var(--accent);color:#fff; }
                .sl-btn-ghost { background:transparent;color: var(--text-2);border:1px solid var(--border)!important; }
                .sl-row-btn { background:none;border:none;color:var(--accent);cursor:pointer;font-size:12px;padding:2px 6px;border-radius:4px; }
                .sl-row-btn:hover { background:rgba(255,255,255,.08); }
                .sl-table { width:100%;border-collapse:collapse;font-size:13px; }
                .sl-table th { text-align:left;padding:8px 10px;color: var(--text-3);font-size:11px;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--surface-0); }
                .sl-table td { padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:middle; }
            </style>

            <div style="padding:16px 20px 12px;border-bottom:1px solid var(--border);" data-testid="sl-create-panel">
                <div style="font-size:14px;font-weight:700;margin-bottom:10px;">🔗 New share link</div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;">
                    <div style="flex:3;min-width:260px;">
                        <label style="font-size:11px;color: var(--text-3);display:block;margin-bottom:4px;">Path (file or folder)</label>
                        <input id="slNewPath" class="sl-input" placeholder="/home/user/photos/vacation.jpg" value="${prefillPath}" data-testid="sl-new-path">
                    </div>
                    <div style="flex:1;min-width:140px;">
                        <label style="font-size:11px;color: var(--text-3);display:block;margin-bottom:4px;">Label (optional)</label>
                        <input id="slNewName" class="sl-input" placeholder="vacation.jpg" value="${prefillName}" data-testid="sl-new-name">
                    </div>
                    <div style="width:120px;">
                        <label style="font-size:11px;color: var(--text-3);display:block;margin-bottom:4px;">Expires</label>
                        <select aria-label="Slnewexpiry" id="slNewExpiry" class="sl-input" data-testid="sl-new-expiry">
                            <option value="">Never</option>
                            <option value="1">1 day</option>
                            <option value="7">7 days</option>
                            <option value="30">30 days</option>
                        </select>
                    </div>
                    <div style="width:120px;">
                        <label style="font-size:11px;color: var(--text-3);display:block;margin-bottom:4px;">Max downloads</label>
                        <input id="slNewMaxDl" class="sl-input" type="number" min="1" placeholder="∞" data-testid="sl-new-maxdl">
                    </div>
                    <button id="slCreateBtn" class="sl-btn sl-btn-primary" data-testid="sl-create-btn">Create link</button>
                </div>
            </div>

            <div style="flex:1;overflow-y:auto;padding:8px 20px 20px;">
                <table class="sl-table" data-testid="sl-table">
                    <thead><tr>
                        <th>Name</th><th>Path</th><th>Created</th><th>Expires</th><th>Downloads</th><th>Status</th><th style="text-align:right;">Actions</th>
                    </tr></thead>
                    <tbody id="slTableBody">
                        <tr><td colspan="7" style="padding:0;">${skeletonRows(5)}</td></tr>
                    </tbody>
                </table>
            </div>
        </div>`;

    body.querySelector('#slCreateBtn').addEventListener('click', () => _createLink(body));
    await loadLinks(body);
}

async function loadLinks(body) {
    const tbody = body.querySelector('#slTableBody');
    if (!tbody) return;
    try {
        const resp = await apiFetch('/api/share-links');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        _links = (await resp.json()).links || [];
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="7" style="color:#f85149;text-align:center;padding:30px;">Failed to load links: ${escapeHtml(e.message)}</td></tr>`;
        return;
    }

    if (!_links.length) {
        tbody.innerHTML = `<tr><td colspan="7" style="color: var(--text-3);text-align:center;padding:40px;">No share links yet — create one above.</td></tr>`;
        return;
    }

    tbody.innerHTML = _links.map(l => {
        const st = STATUS_META[l.status] || STATUS_META.active;
        const dl = l.max_downloads ? `${l.downloads}/${l.max_downloads}` : String(l.downloads);
        // Multi-file links (Photo Station multi-share) have no single path —
        // show the file count; hover reveals every shared path.
        const isMulti = Array.isArray(l.files) && l.files.length > 0;
        const pathCell = isMulti
            ? `<span title="${escapeAttr(l.files.join('\n'))}" style="cursor:help;">📷 ${l.files.length} photos</span>`
            : escapeHtml(l.path);
        return `<tr data-testid="sl-row-${l.id}">
            <td style="font-weight:600;">${escapeHtml(l.name)}</td>
            <td style="color: var(--text-2);font-family:monospace;font-size:12px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${pathCell}</td>
            <td style="color: var(--text-2);">${_fmtDate(l.created_at)}</td>
            <td style="color: var(--text-2);">${l.expires_at ? _fmtDate(l.expires_at) : 'Never'}</td>
            <td style="font-family:monospace;">${dl}</td>
            <td><span class="sl-chip ${st.cls}">${st.label}</span></td>
            <td style="text-align:right;white-space:nowrap;">
                <button class="sl-row-btn" data-act="copy" data-token="${escapeAttr(l.token)}">Copy link</button>
                <button class="sl-row-btn" data-act="open" data-token="${escapeAttr(l.token)}">Open</button>
                <button class="sl-row-btn" style="color:#f85149;" data-act="del" data-id="${escapeAttr(l.id)}">Delete</button>
            </td>
        </tr>`;
    }).join('');

    tbody.querySelectorAll('[data-act]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const act = btn.dataset.act;
            if (act === 'copy') {
                await _copyText(_linkUrl(btn.dataset.token));
            } else if (act === 'open') {
                window.open(_linkUrl(btn.dataset.token), '_blank');
            } else if (act === 'del') {
                const ok = await window.showConfirm('Delete share link', 'Anyone holding this URL will lose access. Continue?');
                if (!ok) return;
                try {
                    const resp = await apiFetch(`/api/share-links/${btn.dataset.id}`, { method: 'DELETE' });
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    window.addNotification('Share link deleted', 'success');
                    loadLinks(body);
                } catch (e) {
                    window.addNotification(e.message, 'error');
                }
            }
        });
    });
}

async function _createLink(body) {
    if (_busy) return;
    const pathEl = body.querySelector('#slNewPath');
    const path = (pathEl.value || '').trim();
    if (!path) { window.addNotification('Enter a file or folder path first', 'warning'); return; }

    _busy = true;
    const btn = body.querySelector('#slCreateBtn');
    btn.disabled = true; btn.textContent = 'Creating…';
    try {
        const payload = { path };
        const name = (body.querySelector('#slNewName').value || '').trim();
        if (name) payload.name = name;
        const exp = body.querySelector('#slNewExpiry').value;
        if (exp) payload.expires_in_days = Number(exp);
        const maxDl = parseInt(body.querySelector('#slNewMaxDl').value, 10);
        if (maxDl > 0) payload.max_downloads = maxDl;

        const resp = await apiFetch('/api/share-links', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!resp.ok) {
            let detail = `HTTP ${resp.status}`;
            try { detail = (await resp.json()).detail || detail; } catch (_) {}
            throw new Error(detail);
        }
        const link = await resp.json();
        pathEl.value = ''; body.querySelector('#slNewName').value = '';
        window.addNotification(`Share link created for "${link.name}"`, 'success');
        loadLinks(body);
    } catch (e) {
        window.addNotification(e.message, 'error');
    } finally {
        _busy = false;
        btn.disabled = false; btn.textContent = 'Create link';
    }
}

async function _copyText(text) {
    try {
        await navigator.clipboard.writeText(text);
        window.addNotification('Link copied to clipboard', 'success');
    } catch (_) {
        const ta = document.createElement('textarea');
        ta.value = text; ta.style.position = 'fixed'; ta.style.left = '-9999px';
        document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); window.addNotification('Link copied to clipboard', 'success'); }
        catch (e2) { window.addNotification('Failed to copy link', 'error'); }
        ta.remove();
    }
}
