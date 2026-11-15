// EthOS NAS — Reusable File/Folder Picker (overlay)
// A modal overlay for browsing directories and selecting a file or folder.
// Use cases: "Open file" in Code Editor, save/destination folders in Downloads, etc.
//
// Usage:
//   const p = await pickFile({ title: 'Select file to edit', startPath: '/home/user' });
//   const d = await pickFolder({ title: 'Save to folder' });
// Options (all optional):
//   title        — overlay header text (default: 'Select file' / 'Select folder')
//   startPath    — directory to open first; may also be a file path in pickFile
//                  (its parent is opened and the file pre-selected). Default: last used, or '/'
//   extensions   — pickFile only: ['.txt', '.md'] → other files are dimmed & not selectable
//   roots        — restrict browsing to these absolute roots (e.g. ['/home','/mnt','/media']);
//                  quick-jump chips are shown and navigation cannot escape the roots
//   acceptLabel  — OK button label (default: 'Open' / 'Select')
// Returns the selected absolute path (string), or null on cancel.

import { escapeHtml, escapeAttr, getFileIcon, formatBytes } from './utils.js';
import { apiFetch } from './api.js';

let _active = null;    // currently open picker state (only one at a time)
let _lastPath = '/';   // remember the last browsed directory across pickers

const _join = (base, name) => base === '/' ? '/' + name : base.replace(/\/+$/, '') + '/' + name;
const _parentOf = (p) => { if (!p || p === '/') return '/'; const i = p.lastIndexOf('/'); return i <= 0 ? '/' : p.substring(0, i); };
const _extOf = (name) => { const i = name.lastIndexOf('.'); return i > 0 ? name.substring(i).toLowerCase() : ''; };

function _normRoot(p) {
    const raw = String(p || '').trim();
    if (!raw.startsWith('/')) return '';
    return raw.replace(/\/+$/, '') || '/';
}

function _rootFor(dir, roots) {
    let best = '';
    for (const root of roots || []) {
        if (dir === root || dir.startsWith(root === '/' ? '/' : root + '/')) {
            if (root.length > best.length) best = root;
        }
    }
    return best;
}

async function _listDir(p) {
    // Directory listing endpoint (same one Files uses): GET /api/files?path=…
    const resp = await apiFetch(`/api/files?path=${encodeURIComponent(p)}`);
    if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
            const payload = await resp.json();
            detail = payload?.detail || payload?.error || detail;
        } catch { /* non-JSON error body */ }
        throw new Error(detail);
    }
    return resp.json();
}

/* ── Render + lifecycle (one overlay at a time) ─────────────────────────── */

function _render(opts, resolve) {
    const mode = opts.mode; // 'file' | 'folder'
    const exts = Array.isArray(opts.extensions)
        ? opts.extensions.map(e => e.startsWith('.') ? e.toLowerCase() : '.' + e.toLowerCase())
        : null;
    const roots = Array.isArray(opts.roots)
        ? opts.roots.map(_normRoot).filter(Boolean)
        : null;

    let dir = (opts.startPath && typeof opts.startPath === 'string') ? opts.startPath : _lastPath;
    if (!dir.startsWith('/')) dir = '/' + dir;
    if (roots && roots.length && !_rootFor(dir, roots)) dir = roots[0];

    const st = { mode, exts, roots, dir, entries: [], hl: null, pendingHl: null, loading: false, error: '', done: false };
    st.resolve = resolve;

    /* DOM */
    const overlay = document.createElement('div');
    overlay.className = 'ethos-modal-overlay';
    overlay.setAttribute('data-testid', 'file-picker');
    const box = document.createElement('div');
    box.className = 'ethos-modal-box fp-box';
    box.innerHTML = `
        <div class="ethos-modal-header">
            <span class="ethos-modal-title" data-testid="fp-title">${escapeHtml(opts.title || (mode === 'file' ? 'Select file' : 'Select folder'))}</span>
            <button class="ethos-modal-close" data-testid="fp-close">&times;</button>
        </div>
        <div class="fp-pathbar">
            <button type="button" class="fp-parent" title="Go to parent folder" data-testid="fp-parent"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="18,15 12,9 6,15"/></svg></button>
            <span class="fp-crumbs" data-testid="fp-crumbs"></span>
        </div>
        <div class="fp-roots" data-testid="fp-roots" hidden></div>
        <div class="ethos-modal-body fp-list" data-testid="fp-list" role="listbox" aria-label="Folder contents"></div>
        <div class="ethos-modal-footer">
            <span class="fp-sel" data-testid="fp-selected"></span>
            <button type="button" class="ethos-modal-btn ethos-modal-btn-ghost" data-testid="fp-cancel">Cancel</button>
            <button type="button" class="ethos-modal-btn ethos-modal-btn-primary" data-testid="fp-ok">${escapeHtml(opts.acceptLabel || (mode === 'file' ? 'Open' : 'Select'))}</button>
        </div>`;
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    st.dom = {
        overlay, box,
        parent: box.querySelector('[data-testid="fp-parent"]'),
        crumbs: box.querySelector('[data-testid="fp-crumbs"]'),
        roots: box.querySelector('[data-testid="fp-roots"]'),
        list: box.querySelector('[data-testid="fp-list"]'),
        sel: box.querySelector('[data-testid="fp-selected"]'),
    };

    /* Finish (called exactly once) */
    const finish = (sel) => {
        if (st.done) return;
        st.done = true;
        document.removeEventListener('keydown', st.kd);
        overlay.remove();
        if (_active === st) _active = null;
        resolve(sel || null);
    };

    /* Accept: folder mode picks the highlighted subfolder, or the current dir (Windows semantics). */
    const accept = () => {
        let sel = null;
        if (st.mode === 'folder') sel = st.hl ? _join(st.dir, st.hl) : st.dir;
        else if (st.hl) sel = _join(st.dir, st.hl);
        finish(sel);
    };

    /* Cancel handlers */
    box.querySelector('[data-testid="fp-close"]').onclick = () => finish(null);
    box.querySelector('[data-testid="fp-cancel"]').onclick = () => finish(null);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) finish(null); });

    /* OK button + keyboard */
    box.querySelector('[data-testid="fp-ok"]').onclick = accept;
    st.kd = (e) => {
        if (st.done) return;
        if (e.key === 'Escape') { e.preventDefault(); finish(null); }
        else if (e.key === 'Enter' && !e.defaultPrevented) { e.preventDefault(); accept(); }
    };
    document.addEventListener('keydown', st.kd);

    /* Parent button */
    st.dom.parent.onclick = () => _load(st, _parentOf(st.dir));

    /* Root chips */
    st.dom.roots.onclick = (e) => {
        const chip = e.target.closest('.fp-root');
        if (chip && !chip.classList.contains('cur')) _load(st, chip.dataset.path);
    };

    /* Breadcrumb clicks */
    st.dom.crumbs.onclick = (e) => {
        const c = e.target.closest('.fp-crumb');
        if (!c || c.classList.contains('fp-crumb-cur')) return;
        _load(st, c.dataset.path);
    };

    /* Row interactions: click highlights, double-click navigates / selects.
       File mode: only files are selectable (folders navigate). Folder mode:
       only folders are selectable (files are ignored). */
    st.dom.list.onclick = (e) => {
        const row = e.target.closest('.fp-row');
        if (!row || row.classList.contains('dim')) return;
        const isDir = row.dataset.isdir === '1';
        if ((st.mode === 'file' && isDir) || (st.mode === 'folder' && !isDir)) return;
        st.hl = (st.hl === row.dataset.name) ? null : row.dataset.name;
        // Toggle the highlight in place — a full re-render between the two
        // clicks of a double-click detaches the row nodes, so the browser
        // dispatches dblclick on the list container and folder navigation
        // (double-click to enter) silently stopped working.
        _updateHl(st); _renderSel(st);
    };
    st.dom.list.ondblclick = (e) => {
        const row = e.target.closest('.fp-row');
        if (!row || row.classList.contains('dim')) return;
        const name = row.dataset.name;
        if (row.dataset.isdir === '1') _load(st, _join(st.dir, name));
        else if (st.mode === 'file') finish(_join(st.dir, name));
    };

    /* Keyboard: arrows move focus, Space highlights, Enter opens/selects. */
    st.dom.list.onkeydown = (e) => {
        const row = e.target.closest('.fp-row');
        if (!row || row.classList.contains('dim')) return;
        const rows = Array.from(st.dom.list.querySelectorAll('.fp-row:not(.dim)'));
        const index = rows.indexOf(row);
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
            e.preventDefault();
            const step = e.key === 'ArrowDown' ? 1 : -1;
            rows[(index + step + rows.length) % rows.length]?.focus();
            return;
        }
        if (e.key === 'Home' || e.key === 'End') {
            e.preventDefault();
            rows[e.key === 'Home' ? 0 : rows.length - 1]?.focus();
            return;
        }
        if (e.key === ' ' || e.key === 'Spacebar') {
            e.preventDefault();
            st.hl = st.hl === row.dataset.name ? null : row.dataset.name;
            _updateHl(st); _renderSel(st);
            return;
        }
        if (e.key === 'Enter') {
            e.preventDefault();  // keep the global Enter handler from accepting immediately
            if (row.dataset.isdir === '1') _load(st, _join(st.dir, row.dataset.name));
            else if (st.mode === 'file') { st.hl = row.dataset.name; finish(_join(st.dir, row.dataset.name)); }
        }
    };

    /* Initial load + focus the OK button so keyboard state is unambiguous */
    _active = st;
    setTimeout(() => { if (!st.done) box.querySelector('[data-testid="fp-ok"]').focus(); }, 50);
    _load(st, dir);
}

/* ── Directory loading + sub-renders ────────────────────────────────────── */

async function _load(st, dir) {
    if (st.done) return;
    if (st.roots && st.roots.length && !_rootFor(dir, st.roots)) dir = st.roots[0];
    st.loading = true; st.error = ''; st.hl = null;
    _renderCrumbs(st); _renderList(st); _renderSel(st);
    try {
        const data = await _listDir(dir);
        if (st.done) return;
        st.dir = data.path;
        st.entries = data.files || [];
        _lastPath = st.dir;
    } catch (e) {
        if (st.done) return;
        // startPath may have pointed at a file → open its parent and pre-select it.
        const base = dir.split('/').filter(Boolean).pop() || '';
        if (!st.pendingHl && base.includes('.')) {
            st.pendingHl = base;
            _load(st, _parentOf(dir));
            return;
        }
        // partial/invalid startPath → walk up to the nearest existing ancestor
        // (recursion terminates at '/', which either loads or shows the error).
        if (dir !== '/') {
            _load(st, _parentOf(dir));
            return;
        }
        st.entries = [];
        st.error = `Cannot open folder (${e.message})`;
    }
    st.loading = false;
    if (st.pendingHl && st.entries.some(f => f.name === st.pendingHl)) {
        st.hl = st.pendingHl;
    }
    st.pendingHl = null;
    _renderCrumbs(st); _renderList(st); _renderSel(st);
}

function _renderCrumbs(st) {
    const base = st.roots && st.roots.length ? (_rootFor(st.dir, st.roots) || st.roots[0]) : '/';
    const suffix = base === '/' ? st.dir : st.dir.slice(base.length);
    const parts = suffix.split('/').filter(Boolean);
    let html = `<span class="fp-crumb ${st.dir === base ? 'fp-crumb-cur' : ''}" data-path="${escapeAttr(base)}" title="${escapeAttr(base)}">${escapeHtml(base)}</span>`;
    let acc = base === '/' ? '' : base;
    parts.forEach((p, i) => {
        acc += '/' + p;
        const last = i === parts.length - 1;
        html += `<span class="fp-sep">›</span><span class="fp-crumb ${last ? 'fp-crumb-cur' : ''}" data-path="${escapeAttr(acc)}" title="${escapeAttr(acc)}">${escapeHtml(p)}</span>`;
    });
    st.dom.crumbs.innerHTML = html;
    st.dom.parent.style.visibility = st.dir === base ? 'hidden' : 'visible';
    _renderRoots(st);
}

function _renderRoots(st) {
    const el = st.dom.roots;
    if (!el) return;
    if (!st.roots || st.roots.length < 2) {
        el.hidden = true;
        el.replaceChildren();
        return;
    }
    const current = _rootFor(st.dir, st.roots);
    el.hidden = false;
    el.innerHTML = st.roots.map((root) =>
        `<button type="button" class="fp-root${root === current ? ' cur' : ''}" data-path="${escapeAttr(root)}">${escapeHtml(root)}</button>`).join('');
}

function _updateHl(st) {
    st.dom.list.querySelectorAll('.fp-row').forEach((r) => {
        const selected = r.dataset.name === st.hl;
        r.classList.toggle('hl', selected);
        r.setAttribute('aria-selected', selected ? 'true' : 'false');
    });
}

function _renderList(st) {
    const el = st.dom.list;
    if (st.loading) { el.innerHTML = `<div class="fp-status">Loading…</div>`; return; }
    if (st.error) { el.innerHTML = `<div class="fp-status fp-err">${escapeHtml(st.error)}</div>`; return; }
    if (!st.entries.length) { el.innerHTML = `<div class="fp-status">Empty folder</div>`; return; }
    let html = '';
    for (const f of st.entries) {
        const isDir = !!f.is_dir;
        // Folder mode ignores files; file mode dims extensions outside the filter.
        const dim = !isDir && (st.mode === 'folder'
            || (st.exts !== null && !st.exts.includes(_extOf(f.name))));
        html += `<div class="fp-row ${st.hl === f.name ? 'hl' : ''} ${dim ? 'dim' : ''}" role="option" tabindex="${dim ? '-1' : '0'}" aria-selected="${st.hl === f.name ? 'true' : 'false'}" data-name="${escapeAttr(f.name)}" data-isdir="${isDir ? 1 : 0}" title="${escapeAttr(_join(st.dir, f.name))}">
            <span class="fp-ic">${getFileIcon(f.name, isDir ? 'dir' : 'file')}</span>
            <span class="fp-name">${escapeHtml(f.name)}</span>
            <span class="fp-meta">${isDir ? '' : formatBytes(f.size || 0)}</span>
        </div>`;
    }
    el.innerHTML = html;
}

function _renderSel(st) {
    const p = st.mode === 'folder'
        ? (st.hl ? _join(st.dir, st.hl) : st.dir)   // folder mode: OK without selection picks the current dir
        : (st.hl ? _join(st.dir, st.hl) : '');
    st.dom.sel.textContent = p;
}

/* ── Public API ─────────────────────────────────────────────────────────── */

export function pickFile(opts = {}) {
    return new Promise((resolve) => { if (_active && !_active.done) _finishActive(null); _render({ ...opts, mode: 'file' }, resolve); });
}

export function pickFolder(opts = {}) {
    return new Promise((resolve) => { if (_active && !_active.done) _finishActive(null); _render({ ...opts, mode: 'folder' }, resolve); });
}

function _finishActive(sel) {
    const st = _active;
    if (!st || st.done) return;
    st.done = true;
    document.removeEventListener('keydown', st.kd);
    st.dom.overlay.remove();
    if (_active === st) _active = null;
    st.resolve(sel || null);
}
