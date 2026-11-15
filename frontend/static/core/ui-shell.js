// EthOS NAS — shared core app shell (docs/app-ui.md)
//
// Every desktop app window is built from the same parts as the native package
// windows (ethos-antivirus / domains / backup-manager): a 218 px rail with a
// text-only nav, a scrolling main pane, one page header with a status line, and
// the .ui-* panels/tables/tags from `static/css/ethos-ui.css`.
//
//   const shell = mountShell(body, {
//       idPrefix: 'settings',
//       title: 'Settings',
//       subtitle: 'Server configuration and per-user preferences.',
//       icon: '🔧',
//       sections: [
//           { id: 'overview', label: 'Overview', title: 'System Overview' },
//           { id: 'users', label: 'Users', title: 'User Accounts' },
//       ],
//       note: 'Changes apply immediately.',
//   });
//   shell.onSection((id) => renderSection(id));
//   shell.setActive('overview');
//
// Conventions:
//   • sections ≥ 2 → rail. One-section apps pass `rail: false` and still get the
//     header/status/view contract (Packages, Logs and Files add their own panes).
//   • never set the window title here — the window manager owns it.
//   • all dynamic text goes through textContent/escapeHtml; no window globals.

import { escapeAttr, escapeHtml } from './utils.js';

/**
 * Build the shared shell inside `container` (usually a `.window-body`).
 * Returns handles for the app to fill and update; the markup itself carries the
 * `data-testid` hooks the UI tests use (`<prefix>-rail`, `-nav-<section>`,
 * `-main`, `-title`, `-status`, `-pill`).
 */
export function mountShell(container, {
    idPrefix = 'ui',
    title = 'App',
    subtitle = '',
    icon = '',
    sections = [],
    rail = sections.length > 1,
    railContent = null,
    header = true,
    flush = false,
    note = '',
    status = '',
} = {}) {
    if (!container) throw new Error('mountShell: a container element is required');

    const showRail = rail && (sections.length > 0 || Boolean(railContent));
    const prefix = escapeAttr(idPrefix);
    const railHtml = showRail ? `
        <aside class="ui-rail" data-testid="${prefix}-rail">
            <div class="ui-brand">
                ${icon ? `<span class="ui-brand-icon" aria-hidden="true">${escapeHtml(icon)}</span>` : ''}
                <div><strong></strong><small>EthOS</small></div>
            </div>
            <nav class="ui-nav" aria-label="${escapeAttr(title)} sections" data-testid="${prefix}-nav"></nav>
            <div class="ui-foot">
                <span class="ui-pill" role="status" hidden data-testid="${prefix}-pill"></span>
                <span class="ui-side-note" hidden data-testid="${prefix}-note"></span>
            </div>
        </aside>` : '';

    const headerHtml = header ? `
        <header class="ui-head">
            <h1 data-testid="${prefix}-title"></h1>
            <p class="ui-head-sub" data-testid="${prefix}-subtitle"></p>
            <p class="ui-status" role="status" aria-live="polite" data-testid="${prefix}-status"></p>
        </header>` : '';

    const root = document.createElement('div');
    root.className = 'ui-shell';
    root.setAttribute('data-testid', `${idPrefix}-shell`);
    root.innerHTML = `${railHtml}
        <main class="ui-main${flush ? ' ui-main-flush' : ''}" data-testid="${prefix}-main">
            ${headerHtml}
            <div class="ui-view${flush ? ' ui-view-fill' : ''}" data-testid="${prefix}-view"></div>
        </main>`;
    container.replaceChildren(root);

    const el = (selector) => root.querySelector(selector);
    const navEl = el('.ui-nav');
    const brandTitle = el('.ui-brand strong');
    const titleEl = el('.ui-head h1');
    const subtitleEl = el('.ui-head-sub');
    const statusEl = el('.ui-status');
    const viewEl = el('.ui-view');
    const pillEl = el('.ui-pill');
    const noteEl = el('.ui-side-note');
    const mainEl = el('.ui-main');
    const railEl = el('.ui-rail');

    if (brandTitle) brandTitle.textContent = title;
    if (noteEl && note) {
        noteEl.textContent = note;
        noteEl.hidden = false;
    }

    if (railContent && railEl) {
        if (navEl) railEl.insertBefore(railContent, navEl);
        else railEl.appendChild(railContent);
    }

    const sectionList = sections.map((section) => ({ ...section }));
    const buttons = new Map();
    const sectionHandlers = [];
    let activeId = null;

    const sectionById = (id) => sectionList.find((section) => section.id === id) || null;
    const setHeader = (heading, sub = '') => {
        if (!titleEl || !subtitleEl) return;
        titleEl.textContent = heading || title;
        subtitleEl.textContent = sub || '';
    };
    const setStatus = (text = '', tone = '') => {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.className = `ui-status${tone ? ` ${tone}` : ''}`;
    };
    const setPill = (tone = '', text = '') => {
        if (!pillEl) return;
        pillEl.textContent = text;
        pillEl.className = `ui-pill${tone ? ` ${tone}` : ''}`;
        pillEl.hidden = !text;
    };
    const setNote = (text = '') => {
        if (!noteEl) return;
        noteEl.textContent = text;
        noteEl.hidden = !text;
    };

    function setActive(id, { silent = false } = {}) {
        const section = sectionById(id);
        if (!section) return;
        activeId = id;
        for (const [key, button] of buttons) {
            const isActive = key === id;
            button.classList.toggle('is-active', isActive);
            button.tabIndex = isActive ? 0 : -1;
            if (isActive) button.setAttribute('aria-current', 'page');
            else button.removeAttribute('aria-current');
        }
        setHeader(section.title || section.label || title, section.subtitle || subtitle);
        if (!silent) for (const handler of sectionHandlers) handler(id, section);
    }

    function focusSection(offset) {
        if (!sectionList.length) return;
        const index = sectionList.findIndex((section) => section.id === activeId);
        const next = (index + offset + sectionList.length) % sectionList.length;
        const id = sectionList[next].id;
        setActive(id);
        buttons.get(id)?.focus();
    }

    function onNavKey(event) {
        if (event.key === 'ArrowDown' || event.key === 'ArrowRight') {
            event.preventDefault();
            focusSection(1);
        } else if (event.key === 'ArrowUp' || event.key === 'ArrowLeft') {
            event.preventDefault();
            focusSection(-1);
        } else if (event.key === 'Home') {
            event.preventDefault();
            if (sectionList.length) {
                setActive(sectionList[0].id);
                buttons.get(sectionList[0].id)?.focus();
            }
        } else if (event.key === 'End') {
            event.preventDefault();
            const last = sectionList[sectionList.length - 1];
            if (last) {
                setActive(last.id);
                buttons.get(last.id)?.focus();
            }
        }
    }

    for (const section of sectionList) {
        if (!navEl) break;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'ui-nav-item';
        button.dataset.section = section.id;
        button.setAttribute('data-testid', section.testId || `${idPrefix}-nav-${section.id}`);
        button.tabIndex = -1;
        button.textContent = section.label || section.id;
        if (section.count != null) {
            const count = document.createElement('span');
            count.className = 'ui-nav-count';
            count.textContent = String(section.count);
            button.appendChild(count);
        }
        button.addEventListener('click', () => setActive(section.id));
        button.addEventListener('keydown', onNavKey);
        navEl.appendChild(button);
        buttons.set(section.id, button);
    }

    setHeader(title, subtitle);
    if (status) setStatus(status);

    return {
        root,
        rail: railEl,
        main: mainEl,
        nav: navEl,
        view: viewEl,
        get active() { return activeId; },
        setActive,
        setHeader,
        setStatus,
        setPill,
        setNote,
        /** Subscribe to section changes; returns an unsubscribe function. */
        onSection(handler) {
            if (typeof handler !== 'function') return () => {};
            sectionHandlers.push(handler);
            return () => {
                const index = sectionHandlers.indexOf(handler);
                if (index >= 0) sectionHandlers.splice(index, 1);
            };
        },
        destroy() {
            sectionHandlers.length = 0;
            container.replaceChildren();
        },
    };
}

/**
 * Apply `data-width="42"` attributes to `.ui-bar-fill` elements after a render.
 * Keeps percentage widths out of HTML strings (no inline `style="…"`).
 */
export function applyBarWidths(root) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    root.querySelectorAll('[data-width]').forEach((el) => {
        const value = Number(el.dataset.width);
        if (Number.isFinite(value)) el.style.width = `${Math.max(0, Math.min(100, value))}%`;
    });
}
