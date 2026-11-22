// EthOS Sticky Notes — standalone mobile PWA at /notes.
//
// Quick capture + light management for the `ethos-sticky-notes` package:
//   /api/packages/ethos-sticky-notes/proxy/api/sticky-notes/…
// Same origin as the desktop, so it reuses `localStorage.token`. Writes are
// optimistic and survive an offline NAS through a bounded localStorage outbox
// (client-generated ids make the retries idempotent).

import { apiFetch, apiRequest, getToken, setToken, getCurrentClaims } from '../core/api.js';
import { notify, requestNotificationPermission } from '../core/notification-center.js';
import { log } from '../core/logger.js';
import '../core/error-reporter.js';

const BASE = '/api/packages/ethos-sticky-notes/proxy/api/sticky-notes';
const CHANNEL_NAME = 'ethos-sticky-notes';
const CACHE_PREFIX = 'ethos-notes-cache:';
const OUTBOX_PREFIX = 'ethos-notes-outbox:';
const MAX_OUTBOX = 200;
const SAVE_DEBOUNCE_MS = 700;
const COLORS = ['yellow', 'pink', 'blue', 'green', 'orange'];
const DEFAULT_SETTINGS = { ribbon_enabled: false, ribbon_show_completed: true };

const $ = (id) => document.getElementById(id);
const els = {
    login: $('notes-login'),
    main: $('notes-main'),
    loginForm: $('notes-login-form'),
    loginError: $('notes-login-error'),
    pill: $('notes-pill'),
    menuBtn: $('notes-menu-btn'),
    menu: $('notes-menu'),
    install: $('notes-install'),
    push: $('notes-push'),
    theme: $('notes-theme'),
    logout: $('notes-logout'),
    offline: $('notes-offline'),
    quickInput: $('notes-quick-input'),
    add: $('notes-add'),
    status: $('notes-status'),
    search: $('notes-search'),
    list: $('notes-list'),
    modeButtons: [...document.querySelectorAll('[data-mode]')],
    filterButtons: [...document.querySelectorAll('[data-filter]')],
};

const state = {
    notes: [],
    settings: { ...DEFAULT_SETTINGS },
    search: '',
    filter: 'all',
    mode: 'note',
    outbox: [],
    syncing: false,
    offline: !navigator.onLine,
    push: { supported: false, subscription: null },
    installPrompt: null,
    highlight: '',
    timers: new Map(),
    dirty: new Map(),
};

function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

function errorText(error) {
    if (error?.status === 401) return 'Your session expired — sign in again.';
    return error?.detail || error?.message || 'Request failed';
}

function isNetworkError(error) {
    return !error?.status || error.status >= 500;
}

// ── auth ────────────────────────────────────────────────────────────────────
function claims() {
    return getCurrentClaims() || null;
}

function authed() {
    const token = getToken();
    if (!token) return false;
    const payload = claims();
    if (!payload) return false;
    if (payload.exp && payload.exp * 1000 <= Date.now() + 5000) {
        try { localStorage.removeItem('token'); } catch { /* ignore */ }
        return false;
    }
    return true;
}

function username() {
    return String(claims()?.username || '').slice(0, 64) || 'anon';
}

function cacheKey() { return CACHE_PREFIX + username(); }
function outboxKey() { return OUTBOX_PREFIX + username(); }

function showLogin() {
    els.login.hidden = false;
    els.main.hidden = true;
    setPill('Signed out', 'offline');
    els.loginError.textContent = '';
    setTimeout(() => els.loginForm?.querySelector('input[name="username"]')?.focus(), 50);
}

function showApp() {
    els.login.hidden = true;
    els.main.hidden = false;
}

// ── API ─────────────────────────────────────────────────────────────────────
async function api(path = '', options = {}) {
    let response;
    try {
        response = await apiFetch(`${BASE}${path}`, options);
    } catch (error) {
        const wrapped = new Error('Network error — the NAS is unreachable');
        wrapped.kind = 'network';
        throw wrapped;
    }
    let body = null;
    try { body = await response.json(); } catch { body = null; }
    if (!response.ok) {
        const detail = body?.detail || `HTTP ${response.status}`;
        const error = new Error(typeof detail === 'string' ? detail : 'Request failed');
        error.status = response.status;
        throw error;
    }
    return body || {};
}

const send = (path, method, body) => api(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
});

// ── feedback ────────────────────────────────────────────────────────────────
function setPill(text, kind = '') {
    if (!els.pill) return;
    els.pill.textContent = text;
    els.pill.className = `notes-pill ${kind}`.trim();
}

function setStatus(text, kind = '') {
    if (!els.status) return;
    els.status.textContent = text || '';
    els.status.style.color = kind === 'error' ? 'var(--notes-red)' : '';
}

function toast(message, kind = 'info') {
    try { notify({ kind, message }); } catch { /* toast host missing */ }
}

// ── cache + outbox ──────────────────────────────────────────────────────────
function loadCache() {
    try {
        const raw = localStorage.getItem(cacheKey());
        if (!raw) return false;
        const data = JSON.parse(raw);
        if (Array.isArray(data.notes)) state.notes = data.notes;
        if (data.settings && typeof data.settings === 'object') {
            state.settings = { ...DEFAULT_SETTINGS, ...data.settings };
        }
        return true;
    } catch {
        return false;
    }
}

function saveCache() {
    try {
        localStorage.setItem(cacheKey(), JSON.stringify({
            notes: state.notes,
            settings: state.settings,
            savedAt: Date.now(),
        }));
    } catch { /* quota / private mode */ }
}

function loadOutbox() {
    try {
        const raw = localStorage.getItem(outboxKey());
        const parsed = raw ? JSON.parse(raw) : [];
        state.outbox = Array.isArray(parsed) ? parsed.slice(-MAX_OUTBOX) : [];
    } catch {
        state.outbox = [];
    }
    updateOfflineBanner();
}

function saveOutbox() {
    try {
        localStorage.setItem(outboxKey(), JSON.stringify(state.outbox.slice(-MAX_OUTBOX)));
    } catch { /* quota */ }
    updateOfflineBanner();
}

function queue(op) {
    state.outbox.push({ ...op, ts: Date.now() });
    if (state.outbox.length > MAX_OUTBOX) state.outbox.splice(0, state.outbox.length - MAX_OUTBOX);
    saveOutbox();
}

function updateOfflineBanner() {
    if (!els.offline) return;
    const pending = state.outbox.length;
    if (state.offline) {
        els.offline.hidden = false;
        els.offline.textContent = pending
            ? `Offline — ${pending} change${pending === 1 ? '' : 's'} queued; will sync automatically.`
            : 'Offline — new changes are queued and will sync automatically.';
        setPill('Offline', 'offline');
        return;
    }
    if (pending) {
        els.offline.hidden = false;
        els.offline.textContent = state.syncing
            ? `Syncing ${pending} queued change${pending === 1 ? '' : 's'}…`
            : `${pending} queued change${pending === 1 ? '' : 's'} waiting to sync.`;
        setPill('Queued', 'offline');
        return;
    }
    els.offline.hidden = true;
}

async function runOp(op) {
    if (op.op === 'create') return send('', 'POST', op.body);
    if (op.op === 'update') return send(`/${encodeURIComponent(op.id)}`, 'PUT', op.body);
    if (op.op === 'item') return send(`/${encodeURIComponent(op.noteId)}/items/${encodeURIComponent(op.itemId)}`, 'PUT', op.body);
    if (op.op === 'add-item') return send(`/${encodeURIComponent(op.noteId)}/items`, 'POST', { text: op.text, client_id: op.itemId });
    if (op.op === 'delete') return api(`/${encodeURIComponent(op.id)}`, { method: 'DELETE' });
    if (op.op === 'delete-item') return api(`/${encodeURIComponent(op.noteId)}/items/${encodeURIComponent(op.itemId)}`, { method: 'DELETE' });
    return null;
}

async function syncOutbox() {
    if (state.syncing || state.offline || !state.outbox.length) return;
    state.syncing = true;
    updateOfflineBanner();
    try {
        while (state.outbox.length) {
            const op = state.outbox[0];
            try {
                await runOp(op);
                state.outbox.shift();
                saveOutbox();
            } catch (error) {
                if (isNetworkError(error)) break;
                // 4xx: the operation can never succeed (deleted note, bad text).
                log('warning', 'Dropped a queued Sticky Notes change',
                    `${op.op}: ${errorText(error)}`);
                state.outbox.shift();
                saveOutbox();
            }
        }
    } finally {
        state.syncing = false;
        updateOfflineBanner();
    }
    if (!state.outbox.length) await loadRemote();
}

// ── data ────────────────────────────────────────────────────────────────────
async function loadRemote() {
    if (state.offline) return false;
    try {
        const [notesData, settingsData] = await Promise.all([api(''), api('/settings')]);
        state.notes = Array.isArray(notesData.notes) ? notesData.notes : [];
        state.settings = { ...DEFAULT_SETTINGS, ...(settingsData.settings || {}) };
        saveCache();
        setStatus('');
        setPill('Synced', 'ready');
        updateOfflineBanner();
        render();
        return true;
    } catch (error) {
        if (error?.status === 404 || error?.status === 403) {
            setPill('Package missing', 'offline');
            setStatus('Install Sticky Notes in Package Center, then reload.', 'error');
        } else if (isNetworkError(error)) {
            state.offline = true;
            updateOfflineBanner();
        } else {
            setPill('Error', 'offline');
            setStatus(errorText(error), 'error');
        }
        log('error', 'Sticky Notes PWA: could not load notes', String(error?.message || error));
        render();
        return false;
    }
}

const findNote = (id) => state.notes.find((note) => note.id === id) || null;
const openItems = (note) => (note.items || []).filter((item) => !item.done).length;
const isComplete = (note) => note.kind === 'checklist' && Boolean(note.items?.length)
    && !(note.items || []).some((item) => !item.done);
const today = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
};
const isOverdue = (note) => Boolean(note.due_at) && note.due_at < today() && !isComplete(note);

function genId(prefix) {
    const bytes = new Uint8Array(6);
    crypto.getRandomValues(bytes);
    return prefix + Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

function visibleNotes() {
    const query = state.search.trim().toLowerCase();
    const archived = state.filter === 'archive';
    return state.notes
        .filter((note) => (archived ? note.archived : !note.archived))
        .filter((note) => {
            if (archived || state.filter === 'all') return true;
            if (state.filter === 'tasks') return note.kind === 'checklist';
            return note.kind === 'note';
        })
        .filter((note) => !query || `${note.text || ''} ${(note.items || []).map((item) => item.text).join(' ')}`
            .toLowerCase().includes(query))
        .sort((a, b) => Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)));
}

// ── rendering ───────────────────────────────────────────────────────────────
function autoGrowAll() {
    document.querySelectorAll('.notes-note-text').forEach((ta) => {
        ta.style.height = 'auto';
        ta.style.height = `${Math.min(ta.scrollHeight, 280)}px`;
    });
}

function badgesHtml(note) {
    const badges = [];
    if (note.pinned) badges.push('<span class="notes-badge">📌 Pinned</span>');
    if (note.due_at) badges.push(`<span class="notes-badge${isOverdue(note) ? ' overdue' : ''}">Due ${esc(note.due_at)}</span>`);
    if (note.reminder_at) badges.push(`<span class="notes-badge">⏰ ${esc(new Date(note.reminder_at).toLocaleString())}</span>`);
    if (isComplete(note)) badges.push('<span class="notes-badge done">✓ Done</span>');
    return badges.join('');
}

function noteBodyHtml(note) {
    if (note.kind === 'checklist') {
        const items = (note.items || []).map((item) => `<li class="notes-item${item.done ? ' is-done' : ''}">
            <input type="checkbox" data-action="toggle-item" data-note="${esc(note.id)}" data-item="${esc(item.id)}"
                ${item.done ? 'checked' : ''} aria-label="Toggle item">
            <input type="text" data-field="item-text" data-note="${esc(note.id)}" data-item="${esc(item.id)}"
                value="${esc(item.text)}" aria-label="Item text" maxlength="500">
            <button type="button" class="notes-action del" data-action="delete-item" data-note="${esc(note.id)}"
                data-item="${esc(item.id)}" title="Remove item" aria-label="Remove item">×</button>
        </li>`).join('');
        return `<input class="notes-note-title" data-field="title" data-note="${esc(note.id)}"
                value="${esc(note.text || '')}" placeholder="Task list title" aria-label="Task list title" maxlength="2000">
            <ul class="notes-items">${items}</ul>
            <form class="notes-item-add" data-note="${esc(note.id)}">
                <input type="text" placeholder="Add item…" aria-label="Add checklist item" maxlength="500">
                <button type="submit">＋</button>
            </form>`;
    }
    return `<textarea class="notes-note-text" data-field="text" data-note="${esc(note.id)}"
        rows="2" aria-label="Note text" placeholder="Write something…">${esc(note.text || '')}</textarea>`;
}

function noteCardHtml(note) {
    const color = COLORS.includes(note.color) ? note.color : 'yellow';
    const action = note.archived ? 'restore' : 'archive';
    return `<article class="notes-note c-${color}${note.pinned ? ' is-pinned' : ''}${state.highlight === note.id ? ' is-highlight' : ''}"
            data-testid="note-${esc(note.id)}">
        <div class="notes-note-head">
            <div class="notes-badges">${badgesHtml(note)}</div>
            <div class="notes-note-actions">
                <button type="button" class="notes-action" data-action="pin" data-note="${esc(note.id)}"
                    title="${note.pinned ? 'Unpin' : 'Pin'}" aria-label="${note.pinned ? 'Unpin note' : 'Pin note'}">${note.pinned ? '📌' : '📍'}</button>
                <button type="button" class="notes-action" data-action="${action}" data-note="${esc(note.id)}"
                    title="${note.archived ? 'Restore' : 'Archive'}" aria-label="${note.archived ? 'Restore note' : 'Archive note'}">${note.archived ? '↩' : '🗄'}</button>
                <button type="button" class="notes-action del" data-action="delete" data-note="${esc(note.id)}"
                    title="Delete" aria-label="Delete note">×</button>
            </div>
        </div>
        ${noteBodyHtml(note)}
    </article>`;
}

function emptyHtml() {
    if (state.search) {
        return '<div class="notes-empty"><strong>Nothing matches</strong>Try another search.</div>';
    }
    if (state.filter === 'archive') {
        return '<div class="notes-empty"><strong>Archive is empty</strong>Archived notes and tasks appear here.</div>';
    }
    if (state.filter === 'tasks') {
        return '<div class="notes-empty"><strong>No tasks yet</strong>Switch the composer to Task and add one.</div>';
    }
    if (state.filter === 'notes') {
        return '<div class="notes-empty"><strong>No notes yet</strong>Write one in the composer above.</div>';
    }
    return '<div class="notes-empty"><strong>No notes yet</strong>Write one in the composer above — it saves to the NAS.</div>';
}

function renderList() {
    const notes = visibleNotes();
    els.list.innerHTML = notes.length
        ? notes.map(noteCardHtml).join('')
        : emptyHtml();
    autoGrowAll();
    els.filterButtons.forEach((button) => {
        button.classList.toggle('is-active', button.dataset.filter === state.filter);
    });
    if (state.highlight) {
        const target = els.list.querySelector(`[data-testid="note-${CSS.escape(state.highlight)}"]`);
        if (target) target.scrollIntoView({ block: 'center' });
        state.highlight = '';
    }
}

function render() {
    els.modeButtons.forEach((button) => {
        button.classList.toggle('is-active', button.dataset.mode === state.mode);
    });
    els.quickInput.placeholder = state.mode === 'checklist' ? 'Add a task…' : 'Write a note…';
    renderList();
}

function setMode(mode) {
    state.mode = mode === 'checklist' ? 'checklist' : 'note';
    render();
    els.quickInput.focus();
}

// ── note mutations ──────────────────────────────────────────────────────────
async function quickAdd() {
    const text = els.quickInput.value.trim();
    if (!text) return;
    els.quickInput.value = '';
    const now = new Date().toISOString();
    if (state.mode === 'checklist') {
        const noteId = genId('sn_');
        const itemId = genId('ci_');
        const body = {
            client_id: noteId, kind: 'checklist', text: '',
            items: [{ client_id: itemId, text }],
        };
        const note = {
            id: noteId, kind: 'checklist', text: '', items: [{ id: itemId, text, done: false }],
            color: COLORS[state.notes.length % COLORS.length], pinned: false, archived: false,
            due_at: null, reminder_at: null, reminder_fired_at: null,
            created_at: now, updated_at: now,
        };
        state.notes.push(note);
        saveCache(); render();
        await submitCreate(body, noteId, text);
        return;
    }
    const noteId = genId('sn_');
    const body = { client_id: noteId, kind: 'note', text };
    const note = {
        id: noteId, kind: 'note', text, items: [],
        color: COLORS[state.notes.length % COLORS.length], pinned: false, archived: false,
        due_at: null, reminder_at: null, reminder_fired_at: null,
        created_at: now, updated_at: now,
    };
    state.notes.push(note);
    saveCache(); render();
    await submitCreate(body, noteId, text);
}

async function submitCreate(body, noteId, originalText) {
    const op = { op: 'create', id: noteId, body };
    if (state.offline) {
        queue(op);
        setStatus('Queued offline.', '');
        return;
    }
    try {
        const data = await api('', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (Array.isArray(data.notes)) state.notes = data.notes;
        saveCache();
        setStatus('Saved.', '');
        setPill('Synced', 'ready');
        render();
    } catch (error) {
        if (isNetworkError(error)) {
            queue(op);
            state.offline = true;
            updateOfflineBanner();
            setStatus('Queued — will sync.', '');
            return;
        }
        state.notes = state.notes.filter((note) => note.id !== noteId);
        saveCache(); render();
        els.quickInput.value = originalText;
        toast(errorText(error), 'error');
        log('error', 'Sticky Notes PWA: quick add failed', String(error?.message || error));
    }
}

function optimisticUpdate(noteId, patch) {
    const note = findNote(noteId);
    if (!note) return;
    Object.assign(note, patch, { updated_at: new Date().toISOString() });
    saveCache();
}

async function patchNote(noteId, patch, opBody = patch) {
    await flushAll();
    optimisticUpdate(noteId, patch);
    render();
    const op = { op: 'update', id: noteId, body: opBody };
    if (state.offline) { queue(op); setStatus('Queued offline.', ''); return; }
    try {
        const data = await send(`/${encodeURIComponent(noteId)}`, 'PUT', opBody);
        if (Array.isArray(data.notes)) state.notes = data.notes;
        saveCache(); render();
    } catch (error) {
        if (isNetworkError(error)) {
            queue(op); state.offline = true; updateOfflineBanner(); setStatus('Queued — will sync.', '');
        } else {
            toast(errorText(error), 'error');
            log('error', `Sticky Notes PWA: could not update ${noteId}`, String(error?.message || error));
            await loadRemote();
        }
    }
}

async function deleteNote(noteId) {
    await flushAll();
    state.notes = state.notes.filter((note) => note.id !== noteId);
    saveCache(); render();
    const op = { op: 'delete', id: noteId };
    if (state.offline) { queue(op); setStatus('Queued offline.', ''); return; }
    try {
        const data = await api(`/${encodeURIComponent(noteId)}`, { method: 'DELETE' });
        if (Array.isArray(data.notes)) state.notes = data.notes;
        saveCache(); render();
    } catch (error) {
        if (isNetworkError(error)) {
            queue(op); state.offline = true; updateOfflineBanner(); setStatus('Queued — will sync.', '');
        } else if (error?.status !== 404) {
            toast(errorText(error), 'error');
            log('error', `Sticky Notes PWA: could not delete ${noteId}`, String(error?.message || error));
            await loadRemote();
        }
    }
}

async function addItem(noteId, text) {
    const value = String(text || '').trim();
    if (!value) return;
    await flushAll();
    const itemId = genId('ci_');
    const note = findNote(noteId);
    if (!note) return;
    note.kind = 'checklist';
    note.items = [...(note.items || []), { id: itemId, text: value, done: false }];
    note.updated_at = new Date().toISOString();
    saveCache(); render();
    const op = { op: 'add-item', noteId, itemId, text: value };
    if (state.offline) { queue(op); setStatus('Queued offline.', ''); return; }
    try {
        const data = await send(`/${encodeURIComponent(noteId)}/items`, 'POST', { text: value, client_id: itemId });
        if (Array.isArray(data.notes)) state.notes = data.notes;
        saveCache(); render();
    } catch (error) {
        if (isNetworkError(error)) {
            queue(op); state.offline = true; updateOfflineBanner(); setStatus('Queued — will sync.', '');
        } else {
            toast(errorText(error), 'error');
            log('error', `Sticky Notes PWA: could not add item to ${noteId}`, String(error?.message || error));
            await loadRemote();
        }
    }
}

async function toggleItem(noteId, itemId, done) {
    const note = findNote(noteId);
    const item = (note?.items || []).find((entry) => entry.id === itemId);
    if (!item) return;
    item.done = done;
    note.updated_at = new Date().toISOString();
    saveCache(); render();
    const op = { op: 'item', noteId, itemId, body: { done } };
    if (state.offline) { queue(op); setStatus('Queued offline.', ''); return; }
    try {
        const data = await send(`/${encodeURIComponent(noteId)}/items/${encodeURIComponent(itemId)}`, 'PUT', { done });
        if (Array.isArray(data.notes)) state.notes = data.notes;
        saveCache(); render();
    } catch (error) {
        if (isNetworkError(error)) {
            queue(op); state.offline = true; updateOfflineBanner(); setStatus('Queued — will sync.', '');
        } else {
            toast(errorText(error), 'error');
            log('error', `Sticky Notes PWA: could not toggle ${itemId}`, String(error?.message || error));
            await loadRemote();
        }
    }
}

async function saveItemText(noteId, itemId, text) {
    const note = findNote(noteId);
    const item = (note?.items || []).find((entry) => entry.id === itemId);
    if (!item) return;
    item.text = text;
    saveCache();
    const op = { op: 'item', noteId, itemId, body: { text } };
    if (state.offline) { queue(op); setStatus('Queued offline.', ''); return; }
    try {
        const data = await send(`/${encodeURIComponent(noteId)}/items/${encodeURIComponent(itemId)}`, 'PUT', { text });
        if (Array.isArray(data.notes)) state.notes = data.notes;
        saveCache();
    } catch (error) {
        if (isNetworkError(error)) {
            queue(op); state.offline = true; updateOfflineBanner();
        } else {
            toast(errorText(error), 'error');
            log('error', `Sticky Notes PWA: could not save item ${itemId}`, String(error?.message || error));
        }
    }
}

async function deleteItem(noteId, itemId) {
    const note = findNote(noteId);
    if (!note) return;
    note.items = (note.items || []).filter((item) => item.id !== itemId);
    note.updated_at = new Date().toISOString();
    saveCache(); render();
    const op = { op: 'delete-item', noteId, itemId };
    if (state.offline) { queue(op); setStatus('Queued offline.', ''); return; }
    try {
        const data = await api(`/${encodeURIComponent(noteId)}/items/${encodeURIComponent(itemId)}`, { method: 'DELETE' });
        if (Array.isArray(data.notes)) state.notes = data.notes;
        saveCache(); render();
    } catch (error) {
        if (isNetworkError(error)) {
            queue(op); state.offline = true; updateOfflineBanner();
        } else if (error?.status !== 404) {
            toast(errorText(error), 'error');
            log('error', `Sticky Notes PWA: could not remove item ${itemId}`, String(error?.message || error));
            await loadRemote();
        }
    }
}

// ── text saves ──────────────────────────────────────────────────────────────
async function flushSave(noteId) {
    const timer = state.timers.get(noteId);
    if (timer) { clearTimeout(timer); state.timers.delete(noteId); }
    const text = state.dirty.get(noteId);
    if (text === undefined) return;
    state.dirty.delete(noteId);
    const op = { op: 'update', id: noteId, body: { text } };
    if (state.offline) { queue(op); setStatus('Queued offline.', ''); return; }
    try {
        const data = await send(`/${encodeURIComponent(noteId)}`, 'PUT', { text });
        if (Array.isArray(data.notes)) state.notes = data.notes;
        saveCache();
    } catch (error) {
        if (isNetworkError(error)) {
            queue(op); state.offline = true; updateOfflineBanner(); setStatus('Queued — will sync.', '');
        } else {
            state.dirty.set(noteId, text);
            toast(errorText(error), 'error');
            log('error', `Sticky Notes PWA: could not save ${noteId}`, String(error?.message || error));
        }
    }
}

async function flushAll() {
    for (const id of [...state.dirty.keys()]) await flushSave(id);
}

function scheduleSave(noteId, text) {
    const note = findNote(noteId);
    if (note) note.text = text;
    state.dirty.set(noteId, text);
    const previous = state.timers.get(noteId);
    if (previous) clearTimeout(previous);
    state.timers.set(noteId, setTimeout(() => { flushSave(noteId); }, SAVE_DEBOUNCE_MS));
}

// ── push ────────────────────────────────────────────────────────────────────
function pushSupported() {
    return Boolean(window.isSecureContext && 'serviceWorker' in navigator
        && 'PushManager' in window && 'Notification' in window);
}

async function serviceWorkerRegistration() {
    return Promise.race([
        navigator.serviceWorker.ready,
        new Promise((_, reject) => setTimeout(() => reject(new Error('no service worker')), 2500)),
    ]);
}

async function refreshPushState() {
    state.push.supported = pushSupported();
    state.push.subscription = null;
    if (state.push.supported) {
        try {
            const registration = await serviceWorkerRegistration();
            state.push.subscription = await registration.pushManager.getSubscription();
        } catch {
            state.push.subscription = null;
        }
    }
    if (els.push) {
        els.push.disabled = !state.push.supported;
        els.push.textContent = state.push.subscription ? 'Disable notifications' : 'Enable notifications';
    }
}

function urlBase64ToUint8Array(value) {
    const padded = (String(value || '') + '='.repeat((4 - String(value || '').length % 4) % 4))
        .replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(padded);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
    return bytes;
}

async function enablePush() {
    if (!state.push.supported) {
        toast('Push requires HTTPS and a browser with service workers.', 'warning');
        return;
    }
    try {
        const permission = await requestNotificationPermission();
        if (permission !== 'granted') {
            toast('Notification permission was not granted.', 'warning');
            return;
        }
        const keyData = await api('/push/key');
        if (!keyData.public_key) throw new Error('Push key is unavailable');
        const registration = await serviceWorkerRegistration();
        let subscription = await registration.pushManager.getSubscription();
        if (!subscription) {
            subscription = await registration.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: urlBase64ToUint8Array(keyData.public_key),
            });
        }
        await send('/push/subscribe', 'POST', { subscription: subscription.toJSON() });
        await refreshPushState();
        toast('Notifications enabled on this device.', 'success');
    } catch (error) {
        toast(errorText(error), 'error');
        log('error', 'Sticky Notes PWA: could not enable push', String(error?.message || error));
    }
}

async function disablePush() {
    try {
        const subscription = state.push.subscription;
        if (subscription) {
            await send('/push/unsubscribe', 'POST', { endpoint: subscription.endpoint }).catch(() => {});
            await subscription.unsubscribe().catch(() => {});
        }
        await refreshPushState();
        toast('Notifications disabled on this device.', 'info');
    } catch (error) {
        toast(errorText(error), 'error');
    }
}

async function emitReminder(reminder) {
    const body = reminder?.body || 'Reminder';
    toast(body, 'info');
    try {
        await apiRequest('/api/notifications', {
            method: 'POST',
            body: {
                kind: 'info',
                title: 'Sticky Notes',
                message: body,
                action: { appId: 'pkg-ethos-sticky-notes', href: '/notes' },
                dedupe_key: reminder?.tag,
            },
        });
    } catch { /* history is best-effort */ }
}

async function pollReminders() {
    if (document.visibilityState !== 'visible' || state.push.subscription) return;
    try {
        const data = await api('/reminders/due');
        for (const reminder of data.reminders || []) await emitReminder(reminder);
    } catch { /* best-effort */ }
}

// ── install / theme / menu ──────────────────────────────────────────────────
function isStandalone() {
    return window.matchMedia?.('(display-mode: standalone)').matches || navigator.standalone === true;
}

function updateInstallButton() {
    if (!els.install) return;
    els.install.hidden = isStandalone();
}

async function installApp() {
    if (state.installPrompt) {
        state.installPrompt.prompt();
        const choice = await state.installPrompt.userChoice.catch(() => ({ outcome: 'dismissed' }));
        if (choice.outcome === 'accepted') {
            state.installPrompt = null;
            updateInstallButton();
        }
        return;
    }
    toast('Use the browser menu → “Add to Home screen”.', 'info');
}

function toggleTheme() {
    const dark = document.documentElement.classList.contains('dark');
    document.documentElement.className = dark ? 'light' : 'dark';
    try { localStorage.setItem('ethos-theme', dark ? 'light' : 'dark'); } catch { /* ignore */ }
}

function toggleMenu(force) {
    const open = force === undefined ? els.menu.hidden : force;
    els.menu.hidden = !open;
    els.menuBtn.setAttribute('aria-expanded', String(open));
    updateInstallButton();
    if (open) refreshPushState();
}

// ── events ──────────────────────────────────────────────────────────────────
function bindEvents() {
    els.loginForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        els.loginError.textContent = '';
        const data = new FormData(els.loginForm);
        const usernameValue = String(data.get('username') || '').trim();
        const password = String(data.get('password') || '');
        if (!usernameValue || !password) {
            els.loginError.textContent = 'Username and password are required.';
            return;
        }
        const button = els.loginForm.querySelector('button[type="submit"]');
        button.disabled = true;
        try {
            const response = await fetch('/api/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username: usernameValue, password }),
            });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || !payload.token) {
                els.loginError.textContent = payload.detail || 'Sign in failed.';
                return;
            }
            setToken(payload.token);
            localStorage.setItem('token', payload.token);
            localStorage.setItem('user', JSON.stringify(payload.user || {}));
            window.location.reload();
        } catch {
            els.loginError.textContent = 'The NAS is unreachable.';
        } finally {
            button.disabled = false;
        }
    });

    els.add.addEventListener('click', quickAdd);
    els.quickInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            quickAdd();
        }
    });
    els.modeButtons.forEach((button) => {
        button.addEventListener('click', () => setMode(button.dataset.mode));
    });
    els.search.addEventListener('input', () => {
        state.search = els.search.value;
        renderList();
    });
    els.search.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            els.search.value = '';
            state.search = '';
            renderList();
        }
    });
    els.filterButtons.forEach((button) => {
        button.addEventListener('click', () => {
            state.filter = button.dataset.filter;
            renderList();
        });
    });

    els.list.addEventListener('click', async (event) => {
        const action = event.target.closest('[data-action]');
        if (!action) return;
        const name = action.dataset.action;
        const noteId = action.dataset.note;
        if (name === 'pin') {
            const note = findNote(noteId);
            await patchNote(noteId, { pinned: !note?.pinned });
            return;
        }
        if (name === 'archive') { await patchNote(noteId, { archived: true }); return; }
        if (name === 'restore') { await patchNote(noteId, { archived: false }); return; }
        if (name === 'delete') { await deleteNote(noteId); return; }
        if (name === 'delete-item') { await deleteItem(noteId, action.dataset.item); }
    });
    els.list.addEventListener('input', (event) => {
        const target = event.target;
        const noteId = target.dataset?.note;
        if (!noteId) return;
        if (target.dataset.field === 'text') {
            target.style.height = 'auto';
            target.style.height = `${Math.min(target.scrollHeight, 280)}px`;
            scheduleSave(noteId, target.value);
            return;
        }
        const note = findNote(noteId);
        if (!note) return;
        if (target.dataset.field === 'title') note.text = target.value;
        if (target.dataset.field === 'item-text') {
            const item = (note.items || []).find((entry) => entry.id === target.dataset.item);
            if (item) item.text = target.value;
        }
    });
    els.list.addEventListener('focusout', (event) => {
        if (event.target.classList?.contains('notes-note-text')) flushSave(event.target.dataset.note);
    });
    els.list.addEventListener('change', async (event) => {
        const target = event.target;
        const noteId = target.dataset?.note;
        if (!noteId) return;
        if (target.dataset.action === 'toggle-item') {
            await toggleItem(noteId, target.dataset.item, target.checked);
            return;
        }
        if (target.dataset.field === 'title') {
            const note = findNote(noteId);
            if (note && note.text !== target.value) await patchNote(noteId, { text: target.value });
            return;
        }
        if (target.dataset.field === 'item-text') {
            await saveItemText(noteId, target.dataset.item, target.value);
        }
    });
    els.list.addEventListener('submit', async (event) => {
        const form = event.target.closest('.notes-item-add');
        if (!form) return;
        event.preventDefault();
        const input = form.querySelector('input');
        const text = input?.value.trim();
        if (!text) return;
        input.value = '';
        await addItem(form.dataset.note, text);
    });

    els.menuBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        toggleMenu();
    });
    els.menu.addEventListener('click', (event) => event.stopPropagation());
    document.addEventListener('click', () => { if (!els.menu.hidden) toggleMenu(false); });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && !els.menu.hidden) toggleMenu(false);
    });
    els.install.addEventListener('click', installApp);
    els.push.addEventListener('click', () => {
        if (state.push.subscription) disablePush();
        else enablePush();
        toggleMenu(false);
    });
    els.theme.addEventListener('click', toggleTheme);
    els.logout.addEventListener('click', async () => {
        if (state.push.subscription) {
            await send('/push/unsubscribe', 'POST', { endpoint: state.push.subscription.endpoint }).catch(() => {});
            await state.push.subscription.unsubscribe().catch(() => {});
        }
        localStorage.removeItem('token');
        localStorage.removeItem('user');
        location.href = '/notes';
    });

    window.addEventListener('online', async () => {
        state.offline = false;
        updateOfflineBanner();
        await syncOutbox();
        await loadRemote();
        pollReminders();
    });
    window.addEventListener('offline', () => {
        state.offline = true;
        updateOfflineBanner();
    });
    document.addEventListener('visibilitychange', async () => {
        if (document.visibilityState !== 'visible') return;
        if (!state.offline) {
            await syncOutbox();
            await loadRemote();
        }
        pollReminders();
    });
    window.addEventListener('beforeinstallprompt', (event) => {
        event.preventDefault();
        state.installPrompt = event;
        updateInstallButton();
    });
    window.addEventListener('appinstalled', () => {
        state.installPrompt = null;
        updateInstallButton();
        toast('Sticky Notes installed.', 'success');
    });
    navigator.serviceWorker?.addEventListener('message', (event) => {
        const data = event.data || {};
        if (data.type === 'notes-reminder' && data.reminder) emitReminder(data.reminder);
    });
}

// ── launch params / sync channel ────────────────────────────────────────────
function applyLaunchParams() {
    const params = new URLSearchParams(window.location.search);
    const mode = params.get('new');
    if (mode === 'note' || mode === 'checklist') state.mode = mode;
    const shared = [params.get('title'), params.get('text'), params.get('url')]
        .filter((value) => value !== null && String(value).trim())
        .join('\n');
    if (shared) els.quickInput.value = shared.slice(0, 2000);
    const note = params.get('note');
    if (note) state.highlight = note;
    if (params.toString()) {
        try { history.replaceState(null, '', '/notes'); } catch { /* ignore */ }
    }
}

let channel = null;
function connectChannel() {
    try { channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel(CHANNEL_NAME) : null; } catch { channel = null; }
    channel?.addEventListener('message', (event) => {
        const type = event.data?.type;
        if ((type === 'notes' || type === 'settings') && !state.offline && navigator.onLine) {
            loadRemote();
        }
    });
}

function registerWorker() {
    if ('serviceWorker' in navigator && window.isSecureContext) {
        window.addEventListener('load', () => {
            navigator.serviceWorker.register('/sw.js').catch((error) => {
                log('warning', 'Sticky Notes PWA: service worker not registered', String(error?.message || error));
            });
        });
    }
}

// ── boot ────────────────────────────────────────────────────────────────────
async function boot() {
    bindEvents();
    registerWorker();
    connectChannel();
    applyLaunchParams();
    updateInstallButton();

    if (!authed()) {
        showLogin();
        return;
    }
    showApp();
    loadOutbox();
    loadCache();
    render();
    updateOfflineBanner();

    await refreshPushState();
    if (state.offline) {
        setPill('Offline', 'offline');
        updateOfflineBanner();
    } else {
        await syncOutbox();
        await loadRemote();
    }
    pollReminders();

    setInterval(() => {
        if (document.visibilityState === 'visible' && !state.offline) {
            loadRemote();
            pollReminders();
        }
    }, 60000);
    setInterval(refreshPushState, 5 * 60000);
    setTimeout(() => els.quickInput.focus(), 200);
}

boot().catch((error) => {
    log('error', 'Sticky Notes PWA failed to start', String(error?.message || error));
    showLogin();
});
