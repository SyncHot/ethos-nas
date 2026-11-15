// EthOS NAS — Global Notification Center (single source of truth)
//
// One module owns every notification surface in the product:
//
//   toasts   transient feedback for the action the user just took      (#toastContainer)
//   sheet    persistent history from the backend store + /ws/notifications
//            behind the top-bar bell                                   (#notificationDrawer)
//   badge    one writer (`_setBadge`) so the unread count cannot flicker
//   prefs    sound + desktop notifications (localStorage, per browser)
//
// Rules for app and package code (see docs/notifications.md):
//   1. Use `notify({ kind, title, message, action })` — or the legacy
//      `addNotification(msg, type)` alias, which routes here.
//   2. Never render your own toast, notification panel or event history.
//   3. Backend work publishes with `emit_event(..., source="<domain>")` and the
//      event lands here automatically (badge, sheet, desktop alert).

import { poll } from './poller.js';
import { escapeAttr, escapeHtml } from './utils.js';
import { uiIcon } from './icons.js';
import { apiFetch } from './api.js';
import { connectWs } from './ws.js';

const NOTIFICATION_TYPES = ['success', 'error', 'warning', 'info'];
const MAX_VISIBLE_TOASTS = 3;      // the rest queue behind a "+N more" chip
const DEDUPE_WINDOW_MS = 2500;     // identical toasts collapse into one row with ×N
const SETTINGS_KEY = 'ethos.notif.settings';
const DEFAULT_TIMEOUTS = { success: 4000, info: 5000, warning: 7000, error: 0 };
const HISTORY_LIMIT = 100;

const state = {
    toasts: [],        // mounted cards (never more than MAX_VISIBLE_TOASTS)
    queue: [],         // waiting cards
    unread: 0,
    events: [],        // last loaded history page
    filter: 'all',     // all | unread | error
    search: '',
    open: false,
    loading: false,
    loaded: false,
    nextId: 1,
    ws: null,
    wsOpen: false,
    pollStop: null,
    wired: false,
    settings: { sound: false, desktop: false },
};

const KIND_ICONS = {
    success: () => uiIcon('check', 15),
    error: () => uiIcon('close', 15),
    warning: () => uiIcon('alert', 15),
    info: () => uiIcon('info', 15),
};

const SOURCE_LABELS = {
    system: 'System',
    files: 'Files',
    storage: 'Storage',
    downloads: 'Downloads',
    docker: 'Docker',
    packages: 'Packages',
    backup: 'Backup',
    shares: 'Shares',
    'share-links': 'Share Links',
    firewall: 'Firewall',
    health: 'Health',
    surveillance: 'Surveillance',
};

const byId = (id) => document.getElementById(id);
const kindOf = (value) => (NOTIFICATION_TYPES.includes(value) ? value : 'info');

// ── Badge (one writer) ───────────────────────────────────────────────────────

/**
 * The only function allowed to write the unread badge. Everything else — the
 * WebSocket, the REST fallback, local read/delete — calls this with an absolute
 * value, which is why two writers can no longer fight over the same element.
 */
function _setBadge(value) {
    const unread = Math.max(0, Number.parseInt(value, 10) || 0);
    state.unread = unread;

    const badge = byId('notificationCount');
    if (badge) {
        badge.textContent = unread > 99 ? '99+' : String(unread);
        badge.style.display = unread > 0 ? 'inline-flex' : 'none';
    }
    const header = byId('notifHeaderUnread');
    if (header) {
        header.textContent = String(unread);
        header.hidden = unread === 0;
    }
    const chip = byId('notifCountUnread');
    if (chip) chip.textContent = unread ? String(unread) : '';
    const trigger = byId('notificationDrawerTrigger');
    if (trigger) {
        trigger.setAttribute('aria-label', unread ? `Notifications, ${unread} unread` : 'Notifications');
    }
}

export function getUnreadCount() { return state.unread; }

// ── Public entry point ───────────────────────────────────────────────────────

/**
 * Show a toast. The canonical API for app and package code.
 *
 * @param {Object|string} options
 * @param {'success'|'error'|'warning'|'info'} [options.kind='info']
 * @param {string} [options.title]                bold first line (optional)
 * @param {string} [options.message]              body text
 * @param {{label: string, appId?: string, href?: string}} [options.action]
 * @param {number} [options.autoDismissMs]        override the per-kind timeout
 * @param {boolean} [options.noAutoDismiss]       stick until dismissed
 * @param {Function} [options.onDismiss]          called when the card goes away
 * @returns {number} toast id (-1 when there is nothing to show)
 */
export function notify(options = {}) {
    const opts = typeof options === 'string' ? { message: options } : (options || {});
    const kind = kindOf(opts.kind || opts.type);
    const message = opts.message != null ? String(opts.message) : '';
    const title = opts.title != null ? String(opts.title) : '';
    if (!message && !title) return -1;

    const now = Date.now();
    const duplicate = state.toasts.find(
        (t) => t.kind === kind && t.message === message && t.title === title
            && (now - t.createdAt) < DEDUPE_WINDOW_MS,
    );
    if (duplicate) {
        duplicate.count += 1;
        duplicate.createdAt = now;
        _syncToastCount(duplicate);
        _restartDismissTimer(duplicate);
        return duplicate.id;
    }

    const sticky = Boolean(opts.noAutoDismiss || opts.persistent);
    const toast = {
        id: state.nextId++,
        kind,
        title,
        message,
        count: 1,
        createdAt: now,
        action: opts.action || null,
        onDismiss: typeof opts.onDismiss === 'function' ? opts.onDismiss : null,
        timeout: sticky ? 0 : (opts.autoDismissMs ?? DEFAULT_TIMEOUTS[kind]),
        el: null,
        timer: null,
        startedAt: 0,
        remaining: 0,
    };

    if (state.toasts.length < MAX_VISIBLE_TOASTS) {
        state.toasts.push(toast);
        if (!_mountToast(toast)) {          // toast container not in the DOM yet
            state.toasts.pop();
            state.queue.push(toast);
            _updateMoreChip();
        }
    } else {
        state.queue.push(toast);
        _updateMoreChip();
    }
    return toast.id;
}

/**
 * Legacy signature kept for the ~350 existing call sites.
 * Accepts both argument orders used in the codebase:
 *   addNotification(message, type)   ← preferred
 *   addNotification(type, message)   ← older call sites (docker.js)
 */
export function addNotification(msg, type = 'info', opts = {}) {
    let message = msg;
    let kind = type;
    if (NOTIFICATION_TYPES.includes(msg) && !NOTIFICATION_TYPES.includes(type)) {
        message = type;
        kind = msg;
    }
    return notify({ ...(opts || {}), kind, message });
}

/** Deprecated aliases — both funnel into `notify`. */
export function showUnifiedToast(first, second = 'info', opts = {}) {
    return addNotification(first, second, opts);
}
export const showToast = showUnifiedToast;

// ── Toast rendering ──────────────────────────────────────────────────────────

function _mountToast(toast) {
    const container = byId('toastContainer');
    if (!container) return false;

    const el = document.createElement('div');
    el.className = `notification notification-${toast.kind}`;
    el.setAttribute('data-testid', `notification-${toast.id}`);
    const countChip = toast.count > 1
        ? `<span class="notification-count" data-role="count">×${toast.count}</span>` : '';
    el.innerHTML = `
        <span class="notification-icon">${KIND_ICONS[toast.kind]()}</span>
        <span class="notification-body">
            ${toast.title ? `<span class="notification-title"><span class="notif-event-text">${escapeHtml(toast.title)}</span>${countChip}</span>` : ''}
            <span class="notification-msg">${escapeHtml(toast.message)}${toast.title ? '' : countChip}</span>
            ${toast.action ? `<button class="notification-action" type="button" data-role="action">${escapeHtml(toast.action.label || 'Open')}</button>` : ''}
        </span>
        <button class="notification-dismiss" type="button"
                data-testid="notification-dismiss-${toast.id}" aria-label="Dismiss">&times;</button>
        ${toast.timeout ? '<span class="notification-timer" aria-hidden="true"></span>' : ''}`;

    if (toast.timeout) {
        el.querySelector('.notification-timer').style.setProperty('--notif-ms', `${toast.timeout}ms`);
    }
    el.querySelector('.notification-dismiss')
        .addEventListener('click', () => dismissNotification(toast.id));
    const actionBtn = el.querySelector('[data-role="action"]');
    if (actionBtn) {
        actionBtn.addEventListener('click', () => {
            _runAction(toast.action);
            dismissNotification(toast.id);
        });
    }
    // Reading time is not counted against the auto-dismiss timer.
    el.addEventListener('mouseenter', () => _pauseDismissTimer(toast));
    el.addEventListener('mouseleave', () => _resumeDismissTimer(toast));
    el.addEventListener('focusin', () => _pauseDismissTimer(toast));
    el.addEventListener('focusout', () => _resumeDismissTimer(toast));

    container.appendChild(el);
    toast.el = el;
    requestAnimationFrame(() => el.classList.add('visible'));
    _restartDismissTimer(toast);
    _updateMoreChip();
    return true;
}

function _syncToastCount(toast) {
    if (!toast.el) return;
    let chip = toast.el.querySelector('[data-role="count"]');
    if (!chip) {
        const host = toast.el.querySelector('.notification-title') || toast.el.querySelector('.notification-msg');
        if (!host) return;
        chip = document.createElement('span');
        chip.className = 'notification-count';
        chip.dataset.role = 'count';
        host.appendChild(chip);
    }
    chip.textContent = `×${toast.count}`;
}

function _restartDismissTimer(toast) {
    if (!toast.timeout) return;
    clearTimeout(toast.timer);
    toast.remaining = toast.timeout;
    toast.startedAt = Date.now();
    toast.timer = setTimeout(() => dismissNotification(toast.id), toast.remaining);
}

function _pauseDismissTimer(toast) {
    if (!toast.timer) return;
    clearTimeout(toast.timer);
    toast.timer = null;
    toast.remaining = Math.max(0, toast.remaining - (Date.now() - toast.startedAt));
}

function _resumeDismissTimer(toast) {
    if (toast.timer || !toast.timeout || toast.remaining <= 0) return;
    toast.startedAt = Date.now();
    toast.timer = setTimeout(() => dismissNotification(toast.id), toast.remaining);
}

function _drainQueue() {
    while (state.toasts.length < MAX_VISIBLE_TOASTS && state.queue.length) {
        const toast = state.queue.shift();
        state.toasts.push(toast);
        if (!_mountToast(toast)) {
            state.toasts.pop();
            state.queue.unshift(toast);
            break;
        }
    }
    _updateMoreChip();
}

function _updateMoreChip() {
    const container = byId('toastContainer');
    if (!container) return;
    let chip = container.querySelector('.notification-more');
    if (!state.queue.length) {
        chip?.remove();
        return;
    }
    if (!chip) {
        chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'notification-more';
        chip.addEventListener('click', () => {
            // Reveal everything the user is waiting to see; cards auto-dismiss.
            const waiting = state.queue.splice(0, state.queue.length);
            waiting.forEach((toast) => { state.toasts.push(toast); _mountToast(toast); });
            _updateMoreChip();
        });
    }
    chip.textContent = `+${state.queue.length} more`;
    container.appendChild(chip);   // keep the chip below the newest card
}

/** Dismiss a toast by id. Works for queued cards too. */
export function dismissNotification(id) {
    const index = state.toasts.findIndex((t) => t.id === id);
    if (index === -1) {
        state.queue = state.queue.filter((t) => t.id !== id);
        _updateMoreChip();
        return;
    }
    const [toast] = state.toasts.splice(index, 1);
    clearTimeout(toast.timer);
    if (toast.el) {
        const el = toast.el;
        el.classList.add('dismissing');
        setTimeout(() => el.remove(), 220);
    }
    try {
        toast.onDismiss?.();
    } catch {
        // A consumer callback must never break the notification stack.
    }
    _drainQueue();
}

// ── Notification Center sheet ────────────────────────────────────────────────

export function openNotifications(options = {}) {
    const drawer = byId('notificationDrawer');
    if (!drawer) return;
    _wireOnce();
    if (options.filter) _setFilter(options.filter);

    state.open = true;
    drawer.classList.add('open');
    const trigger = byId('notificationDrawerTrigger');
    if (trigger) {
        trigger.classList.add('active');
        trigger.setAttribute('aria-expanded', 'true');
    }
    document.addEventListener('keydown', _onKeydown, true);
    document.addEventListener('pointerdown', _onOutsidePointer, true);
    _loadEvents();
    setTimeout(() => {
        if (state.open) {
            drawer.querySelector('[data-testid="notification-drawer-close"]')
                ?.focus({ preventScroll: true });
        }
    }, 80);
}

export function closeNotifications(options = {}) {
    const { restoreFocus = true } = options;
    const drawer = byId('notificationDrawer');
    if (!drawer) return;
    state.open = false;
    drawer.classList.remove('open');
    const trigger = byId('notificationDrawerTrigger');
    if (trigger) {
        trigger.classList.remove('active');
        trigger.setAttribute('aria-expanded', 'false');
    }
    document.removeEventListener('keydown', _onKeydown, true);
    document.removeEventListener('pointerdown', _onOutsidePointer, true);
    _toggleSettings(false);
    if (restoreFocus) trigger?.focus({ preventScroll: true });
}

export function toggleNotificationDrawer() {
    if (state.open) closeNotifications();
    else openNotifications();
}

function _onKeydown(ev) {
    if (ev.key === 'Escape') {
        ev.stopPropagation();
        closeNotifications();
        return;
    }
    if (ev.key !== 'Tab') return;
    const drawer = byId('notificationDrawer');
    if (!drawer) return;
    const focusable = drawer.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (ev.shiftKey && document.activeElement === first) {
        ev.preventDefault();
        last.focus();
    } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault();
        first.focus();
    }
}

/** The bell lives in the top bar (index.html) and opens/closes the sheet. */
function _wireBell() {
    const trigger = byId('notificationDrawerTrigger');
    if (!trigger || trigger.dataset.wired === '1') return;
    trigger.dataset.wired = '1';
    trigger.addEventListener('click', () => toggleNotificationDrawer());
}

function _onOutsidePointer(ev) {
    const drawer = byId('notificationDrawer');
    const trigger = byId('notificationDrawerTrigger');
    if (!drawer) return;
    if (drawer.contains(ev.target)) return;
    if (trigger && trigger.contains(ev.target)) return;
    closeNotifications({ restoreFocus: false });
}

/** One-time wiring: the sheet markup is static, the rows are re-rendered. */
function _wireOnce() {
    if (state.wired) return;
    const drawer = byId('notificationDrawer');
    if (!drawer) return;
    state.wired = true;
    _wireBell();

    drawer.querySelector('[data-testid="notification-drawer-close"]')
        ?.addEventListener('click', () => closeNotifications());
    drawer.querySelector('[data-testid="notification-mark-all-read"]')
        ?.addEventListener('click', () => { markAllRead(); });
    drawer.querySelector('[data-testid="notification-clear-all"]')
        ?.addEventListener('click', () => { clearAll(); });
    drawer.querySelector('[data-testid="notification-settings"]')
        ?.addEventListener('click', () => _toggleSettings());

    drawer.querySelectorAll('.notif-chip').forEach((chip) => {
        chip.addEventListener('click', () => _setFilter(chip.dataset.filter || 'all'));
    });

    const search = byId('notifSearch');
    if (search) {
        let debounce = null;
        search.addEventListener('input', () => {
            clearTimeout(debounce);
            debounce = setTimeout(() => {
                state.search = search.value || '';
                _renderList();
            }, 150);
        });
    }

    const list = byId('notificationDrawerList');
    if (list) {
        list.addEventListener('click', (ev) => {
            const row = ev.target.closest('[data-evt-id]');
            if (!row) return;
            const id = row.dataset.evtId;
            if (ev.target.closest('[data-act="delete"]')) {
                ev.stopPropagation();
                _deleteEvent(id);
                return;
            }
            const event = state.events.find((e) => e.id === id);
            if (!event) return;
            if (!event.read) _markRead(id);
            if (event.action) {
                _runAction(event.action);
                closeNotifications({ restoreFocus: false });
            }
        });
        list.addEventListener('keydown', (ev) => {
            if (ev.key !== 'Enter' && ev.key !== ' ') return;
            const row = ev.target.closest('[data-evt-id]');
            if (!row) return;
            ev.preventDefault();
            row.click();
        });
    }

    _loadSettings();
    const sound = byId('notifSoundToggle');
    if (sound) sound.addEventListener('change', () => { _setSetting('sound', sound.checked); });
    const desktop = byId('notifDesktopToggle');
    if (desktop) {
        desktop.addEventListener('change', () => { _setSetting('desktop', desktop.checked); });
    }
}

/** Load the history page and refresh the badge from the authoritative count. */
async function _loadEvents() {
    if (state.loading) return;
    state.loading = true;
    if (!state.loaded) _renderList(true);
    try {
        const resp = await apiFetch(`/api/notifications?limit=${HISTORY_LIMIT}`);
        if (resp.ok) {
            const data = await resp.json();
            state.events = Array.isArray(data.events) ? data.events : [];
            state.loaded = true;
            _setBadge(data.unread);
        }
    } catch {
        // Offline or unauthenticated — show whatever we already had.
    }
    state.loading = false;
    _renderList();
}

function _setFilter(filter) {
    state.filter = ['all', 'unread', 'error'].includes(filter) ? filter : 'all';
    document.querySelectorAll('.notif-chip').forEach((chip) => {
        chip.classList.toggle('active', chip.dataset.filter === state.filter);
    });
    _renderList();
}

function _filtered() {
    const query = state.search.trim().toLowerCase();
    return state.events.filter((event) => {
        if (state.filter === 'unread' && event.read) return false;
        if (state.filter === 'error' && !['error', 'warning'].includes(event.kind)) return false;
        if (!query) return true;
        return `${event.title || ''} ${event.message || ''} ${event.source || ''}`
            .toLowerCase().includes(query);
    });
}

function _renderList(skeleton = false) {
    const list = byId('notificationDrawerList');
    if (!list) return;

    if (skeleton) {
        list.innerHTML = '<span class="notif-sr">Loading…</span>'
            + '<div class="notif-skeleton" aria-hidden="true"></div>'
            + '<div class="notif-skeleton" aria-hidden="true"></div>'
            + '<div class="notif-skeleton" aria-hidden="true"></div>';
        return;
    }

    const allChip = byId('notifCountAll');
    if (allChip) allChip.textContent = state.events.length ? String(state.events.length) : '';

    const rows = _filtered();
    if (!rows.length) {
        const message = state.filter === 'unread' ? 'You are all caught up.'
            : state.filter === 'error' ? 'No errors or warnings.'
                : state.search ? 'Nothing matches that search.' : 'No notifications yet.';
        list.innerHTML = `
            <div class="notification-drawer-empty">
                ${uiIcon('bell', 30)}
                <span>${escapeHtml(message)}</span>
            </div>`;
        return;
    }

    let html = '';
    let lastDay = null;
    for (const event of rows) {
        const day = _dayLabel(event.created_at);
        if (day !== lastDay) {
            html += `<div class="notif-day">${escapeHtml(day)}</div>`;
            lastDay = day;
        }
        html += _rowHtml(event);
    }
    list.innerHTML = html;
}

function _rowHtml(event) {
    const kind = kindOf(event.kind);
    const count = Number(event.count || 1);
    const unread = !event.read;
    const action = event.action && (event.action.appId || event.action.href)
        ? `<button class="notif-row-btn notif-row-open" type="button" data-act="open"
                   title="${escapeAttr(event.action.label || 'Open')}">${escapeHtml(event.action.label || 'Open')}</button>`
        : '';
    return `
        <div class="notification-history-item notification-${kind}" role="listitem" tabindex="0"
             data-evt-id="${escapeAttr(event.id)}" data-read="${unread ? '0' : '1'}"
             data-testid="notification-event-${escapeAttr(event.id)}">
            <span class="notification-icon">${KIND_ICONS[kind]()}</span>
            <div class="notification-history-content">
                <span class="notif-event-title">
                    ${unread ? '<span class="notif-unread-dot" aria-hidden="true"></span>' : ''}
                    <span class="notif-event-text">${escapeHtml(event.title || 'Notification')}</span>
                    ${count > 1 ? `<span class="notification-count">×${count}</span>` : ''}
                </span>
                ${event.message ? `<span class="notification-history-msg">${escapeHtml(event.message)}</span>` : ''}
                <span class="notif-event-meta">
                    <span class="notif-source-chip">${escapeHtml(_sourceLabel(event.source))}</span>
                    <span class="notification-history-time">${escapeHtml(_relTime(event.created_at))}</span>
                </span>
            </div>
            <div class="notif-row-actions">
                ${action}
                <button class="notif-row-btn danger" type="button" data-act="delete"
                        title="Delete" aria-label="Delete notification">${uiIcon('trash', 14)}</button>
            </div>
        </div>`;
}

async function _markRead(id) {
    const event = state.events.find((e) => e.id === id);
    if (event?.read) return;
    if (event) event.read = true;
    _setBadge(state.unread - 1);
    _renderList();
    try {
        await apiFetch(`/api/notifications/${encodeURIComponent(id)}/read`, { method: 'POST' });
    } catch {
        // The WebSocket sync frame or the next poll corrects the badge.
    }
}

async function _deleteEvent(id) {
    const event = state.events.find((e) => e.id === id);
    state.events = state.events.filter((e) => e.id !== id);
    if (event && !event.read) _setBadge(state.unread - 1);
    _renderList();
    try {
        await apiFetch(`/api/notifications/${encodeURIComponent(id)}`, { method: 'DELETE' });
    } catch {
        // Best effort — the row is already gone locally.
    }
}

export async function markAllRead() {
    try {
        await apiFetch('/api/notifications/read-all', { method: 'POST' });
    } catch {
        // Offline — still clear the local view.
    }
    state.events.forEach((event) => { event.read = true; });
    _setBadge(0);
    _renderList();
}

export async function clearAll() {
    try {
        await apiFetch('/api/notifications', { method: 'DELETE' });
    } catch {
        // Offline — still clear the local view.
    }
    state.events = [];
    _setBadge(0);
    _renderList();
}

function _runAction(action) {
    if (!action) return;
    if (action.appId) {
        // app.js wires the RENDERERS-backed global; importing window-manager here
        // would close an import cycle (it imports showUnifiedToast from us).
        if (typeof window.openApp === 'function') window.openApp(action.appId);
        return;
    }
    if (action.href) window.location.assign(action.href);
}

// ── Time + source labels ─────────────────────────────────────────────────────

function _relTime(iso) {
    const ts = Date.parse(iso || '');
    if (Number.isNaN(ts)) return '';
    const seconds = Math.max(0, (Date.now() - ts) / 1000);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return new Date(ts).toLocaleDateString();
}

function _dayKey(iso) {
    const ts = Date.parse(iso || '');
    if (Number.isNaN(ts)) return 'unknown';
    const date = new Date(ts);
    return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function _dayLabel(iso) {
    const ts = Date.parse(iso || '');
    if (Number.isNaN(ts)) return 'Earlier';
    const today = _dayKey(new Date().toISOString());
    const yesterday = _dayKey(new Date(Date.now() - 86400000).toISOString());
    const key = _dayKey(iso);
    if (key === today) return 'Today';
    if (key === yesterday) return 'Yesterday';
    return new Date(ts).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

function _sourceLabel(source) {
    const raw = String(source || 'system');
    if (raw.startsWith('pkg:')) return raw.slice(4);
    return SOURCE_LABELS[raw] || raw.replace(/[-_]/g, ' ');
}

// ── Preferences: sound + desktop notifications ───────────────────────────────

function _toggleSettings(force) {
    const box = byId('notifSettings');
    const button = document.querySelector('[data-testid="notification-settings"]');
    if (!box) return;
    const open = force ?? !box.classList.contains('open');
    box.classList.toggle('open', open);
    button?.setAttribute('aria-expanded', String(open));
}

function _loadSettings() {
    try {
        const stored = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
        if (stored && typeof stored === 'object') {
            state.settings.sound = Boolean(stored.sound);
            state.settings.desktop = Boolean(stored.desktop);
        }
    } catch {
        // Corrupt preference — defaults are fine.
    }
    const sound = byId('notifSoundToggle');
    if (sound) sound.checked = state.settings.sound;
    const desktop = byId('notifDesktopToggle');
    if (desktop) desktop.checked = state.settings.desktop;
}

function _saveSettings() {
    try {
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(state.settings));
    } catch {
        // Private mode / quota — preferences are best effort.
    }
}

async function _setSetting(name, enabled) {
    if (name === 'desktop' && enabled) {
        if (typeof Notification === 'undefined') {
            _revertSetting('notifDesktopToggle');
            notify({ kind: 'warning', message: 'This browser does not support desktop notifications.' });
            return;
        }
        try {
            if (Notification.permission === 'default') await Notification.requestPermission();
        } catch {
            // User dismissed the prompt.
        }
        if (Notification.permission !== 'granted') {
            _revertSetting('notifDesktopToggle');
            notify({ kind: 'warning', message: 'Desktop notifications are blocked for this site.' });
            return;
        }
    }
    if (name === 'sound' && enabled) _beep('info');   // audible confirmation
    state.settings[name] = Boolean(enabled);
    _saveSettings();
}

function _revertSetting(id) {
    const input = byId(id);
    if (input) input.checked = false;
}

let _audioCtx = null;

/** Short two-tone cue — no asset file, respects the Sound toggle. */
function _beep(kind) {
    try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;
        _audioCtx ||= new Ctx();
        if (_audioCtx.state === 'suspended') _audioCtx.resume();
        const now = _audioCtx.currentTime;
        const osc = _audioCtx.createOscillator();
        const gain = _audioCtx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(kind === 'error' ? 420 : 660, now);
        osc.frequency.exponentialRampToValueAtTime(kind === 'error' ? 300 : 520, now + 0.16);
        gain.gain.setValueAtTime(0.0001, now);
        gain.gain.exponentialRampToValueAtTime(0.06, now + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.2);
        osc.connect(gain).connect(_audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.22);
    } catch {
        // Audio blocked by the browser — stay silent.
    }
}

function _maybeDesktopNotification(event) {
    if (!state.settings.desktop) return;
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    if (document.visibilityState === 'visible') return;
    try {
        const note = new Notification(event.title || 'EthOS NAS', {
            body: event.message || '',
            tag: event.id,
        });
        note.onclick = () => {
            window.focus();
            openNotifications();
            note.close();
        };
    } catch {
        // Some browsers require a service worker — ignore.
    }
}

// ── Live updates ─────────────────────────────────────────────────────────────

/**
 * Open the notification WebSocket. Called once after authentication; the
 * socket is global (no `appId`) because the bell outlives every window.
 */
export function connectNotificationStream() {
    if (state.ws) return state.ws;
    if (!localStorage.getItem('token')) return null;
    state.ws = connectWs('/ws/notifications', _onStreamMessage, {
        name: 'notification-stream',
        onOpen: () => { state.wsOpen = true; },
        onClose: () => { state.wsOpen = false; },
        onAuthFailure: () => { state.ws = null; state.wsOpen = false; },
    });
    return state.ws;
}

function _onStreamMessage(message) {
    if (!message || typeof message !== 'object') return;
    if (typeof message.unread === 'number') _setBadge(message.unread);

    switch (message.type) {
        case 'event': {
            if (!message.event || !message.event.id) return;
            const event = message.event;
            state.events = [event, ...state.events.filter((e) => e.id !== event.id)];
            if (state.open) _renderList();
            _alertForEvent(event);
            break;
        }
        case 'read': {
            const event = state.events.find((e) => e.id === message.event?.id);
            if (event) event.read = true;
            if (state.open) _renderList();
            break;
        }
        case 'read-all': {
            state.events.forEach((event) => { event.read = true; });
            if (state.open) _renderList();
            break;
        }
        case 'delete': {
            state.events = state.events.filter((e) => e.id !== message.event?.id);
            if (state.open) _renderList();
            break;
        }
        case 'cleared': {
            state.events = [];
            if (state.open) _renderList();
            break;
        }
        default:
            break;   // "sync" only carries the unread count, handled above
    }
}

/**
 * Backend events do not spawn toasts — a toast means "you just did something".
 * They surface through the badge, the sheet, and (if enabled) a sound or a
 * desktop notification while the tab is in the background.
 */
function _alertForEvent(event) {
    if (state.settings.sound && ['error', 'warning'].includes(event.kind)) _beep(event.kind);
    _maybeDesktopNotification(event);
}

/** REST fallback for the badge (the WebSocket is the primary path). */
export async function pollUnreadCount() {
    if (state.wsOpen) return state.unread;
    try {
        const resp = await apiFetch('/api/notifications/unread-count');
        if (resp.ok) {
            const data = await resp.json();
            _setBadge(data.unread);
        }
    } catch {
        // Not logged in / offline — keep the last known value.
    }
    return state.unread;
}

/**
 * Kept for bootstrap compatibility. Opens the live stream and installs a slow
 * safety-net poll that only runs while the socket is down.
 */
export function startNotificationPolling(intervalMs = 60000) {
    if (!localStorage.getItem('token')) return;
    // Called on bootstrap and again after a login overlay — the second call must
    // reopen the stream (an auth close nulled it) but not install two pollers.
    connectNotificationStream();
    if (state.pollStop) return;
    pollUnreadCount();
    state.pollStop = poll(() => (state.wsOpen ? state.unread : pollUnreadCount()),
        intervalMs, { name: 'notification-badge' });
}

// Keep the bell badge correct even if the sheet markup is added after import.
function _bootstrap() {
    _wireBell();
    _loadSettings();
}
if (typeof document !== 'undefined' && document.readyState !== 'loading') {
    _bootstrap();
} else if (typeof document !== 'undefined') {
    document.addEventListener('DOMContentLoaded', _bootstrap, { once: true });
}
