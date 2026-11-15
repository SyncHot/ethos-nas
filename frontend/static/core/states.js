// EthOS NAS — shared loading / empty / error states (TODO.md P3-12)
//
// Before this, every list app invented its own: bare `Loading…` text (cron,
// smart, systemd, settings), a bespoke table skeleton (files), a poster shelf
// skeleton (video-station), and empty states written ad hoc — so the same
// situation looked different in every window.
//
// All three helpers return an HTML string (apps insert it into their container)
// and mark their button with `data-state-action`, so `wireStateActions(root, {retry})`
// can bind handlers after insertion without inline `onclick`.
//
//   import { skeletonRows, emptyState, errorState, wireStateActions } from '../core/states.js';
//
//   table.innerHTML = skeletonRows(6);
//   ...
//   table.innerHTML = emptyState({ icon: 'cron', title: 'No jobs yet',
//                                  hint: 'Scheduled tasks will appear here.',
//                                  action: { label: 'New job', id: 'create' } });
//   wireStateActions(table, { create: () => openForm(), retry: () => load() });

import { escapeAttr, escapeHtml } from './utils.js';
import { uiIcon } from './icons.js';

/** Unknown glyph names render as an empty string — fall back so a state is never blank. */
const glyph = (name, size) => uiIcon(name, size) || uiIcon('info', size);

const SURFACE = 'display:flex;flex-direction:column;align-items:center;justify-content:center;'
    + 'gap:10px;padding:38px 20px;text-align:center;color:var(--text-3);';

/**
 * Placeholder rows for a list/table while the first fetch is in flight.
 * `variant`: 'rows' (default) or 'tiles' (grid cards, e.g. photo/album views).
 */
export function skeletonRows(count = 5, { variant = 'rows', ariaLabel = 'Loading' } = {}) {
    const n = Math.max(1, Math.min(50, Number(count) || 5));
    const bar = (w, h = 12) =>
        `<div class="ethos-skel" style="height:${h}px;width:${w};border-radius:6px;
            background:linear-gradient(90deg,var(--surface-2) 25%,var(--surface-1) 50%,var(--surface-2) 75%);
            background-size:200% 100%;animation:ethos-skel-shimmer 1.4s ease-in-out infinite;"></div>`;
    if (variant === 'tiles') {
        const tile = `<div style="display:flex;flex-direction:column;gap:8px;">${bar('100%', 96)}${bar('70%')}</div>`;
        return `<div class="ethos-skeleton" role="status" aria-label="${escapeAttr(ariaLabel)}"
            aria-busy="true" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px;padding:4px;">
            ${tile.repeat(n)}</div>`;
    }
    const widths = ['42%', '61%', '35%', '55%', '48%', '66%'];
    const row = (i) => `<div class="ethos-skeleton-row" style="display:flex;align-items:center;gap:14px;padding:9px 4px;border-bottom:1px solid var(--border);">
        ${bar('18px', 18)}${bar(widths[i % widths.length])}${bar('14%', 12)}${bar('22%', 12)}</div>`;
    return `<div class="ethos-skeleton" role="status" aria-label="${escapeAttr(ariaLabel)}" aria-busy="true">
        ${Array.from({ length: n }, (_, i) => row(i)).join('')}</div>`;
}

/**
 * "Nothing here yet" — icon, title, optional hint and one primary action.
 * `action: { label, id, icon }` renders a button tagged `data-state-action="id"`.
 */
export function emptyState({ icon = 'folder', title = 'Nothing here yet', hint = '', action = null } = {}) {
    const button = action
        ? `<button class="btn-ghost btn-sm" data-state-action="${escapeAttr(action.id || 'action')}"
                style="margin-top:6px;">${action.icon ? glyph(action.icon, 13) + ' ' : ''}${escapeHtml(action.label || 'Continue')}</button>`
        : '';
    return `<div class="ethos-empty" role="status" style="${SURFACE}">
        <div style="opacity:.6;">${glyph(icon, 26)}</div>
        <div style="font-size:13px;font-weight:600;color:var(--text-1);">${escapeHtml(title)}</div>
        ${hint ? `<div style="font-size:12px;max-width:44ch;line-height:1.5;">${escapeHtml(hint)}</div>` : ''}
        ${button}</div>`;
}

/**
 * Error state — shows the server's `detail` verbatim (a permission explanation
 * is more useful than "HTTP 403") plus an optional Retry button.
 */
export function errorState({ detail = 'Something went wrong', retry = true, retryLabel = 'Retry' } = {}) {
    return `<div class="ethos-error" role="alert" style="${SURFACE}">
        <div style="color:#ef4444;">${glyph('alert', 24)}</div>
        <div style="font-size:13px;font-weight:600;color:var(--text-1);">Could not load this view</div>
        <div style="font-size:12px;max-width:52ch;line-height:1.5;font-family:ui-monospace,monospace;">${escapeHtml(String(detail))}</div>
        ${retry ? `<button class="btn-ghost btn-sm" data-state-action="retry" style="margin-top:6px;">${glyph('refresh', 13)} ${escapeHtml(retryLabel)}</button>` : ''}
    </div>`;
}

/**
 * Bind `data-state-action` buttons inside `root` (call right after inserting the
 * markup). `handlers` maps action ids to callbacks; the `retry` id is optional.
 */
export function wireStateActions(root, handlers = {}) {
    if (!root) return;
    root.querySelectorAll('[data-state-action]').forEach((btn) => {
        const id = btn.getAttribute('data-state-action');
        const fn = handlers[id];
        if (typeof fn !== 'function') return;
        btn.addEventListener('click', (ev) => {
            ev.preventDefault();
            btn.disabled = true;
            try { fn(ev); } finally { setTimeout(() => { btn.disabled = false; }, 400); }
        });
    });
}

/** One-line list footer used by paged tables: "Showing 50 of 1 204". */
export function resultCount(shown, total) {
    const fmt = (n) => Number(n || 0).toLocaleString('pl-PL');
    return `<div style="font-size:11px;color:var(--text-3);padding:6px 4px;">Showing ${fmt(shown)} of ${fmt(total)}</div>`;
}
