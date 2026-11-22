import { connectWs } from '../core/ws.js';
import { apiJsonFetch } from '../core/api.js';
import { registerCloseGuard } from '../core/window-manager.js';
import { resolveDefaultTerminalTarget } from '../core/terminal-target.js';

const FONT_KEY = 'ethos-terminal-fontsize';
const SCROLLBACK = 5000;
const _VALID_CONTAINER = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$/;

// Multi-tab state: each tab is an independent PTY session (own xterm + WS).
let _sessions = [];      // session objects in tab order
let _activeId = null;    // id of the active session
let _lastTarget = null;  // null = first open, default target not resolved yet
let _body = null;        // app body element for this window
let _tabBar = null;
let _termArea = null;
let _themeObserver = null;
let _sessionIdSeq = 0;

// Status colours are theme tokens — the terminal follows light/dark like every
// other window (docs/app-ui.md → Theming).
const STATUS_COLORS = {
    connecting: 'var(--ui-warn)',
    connected: 'var(--ui-ok)',
    reconnecting: 'var(--ui-warn)',
    error: 'var(--ui-bad)',
    disconnected: 'var(--text-3)',
};

function _el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
        if (key === 'style') node.style.cssText = value;
        else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
        else node.setAttribute(key, value);
    }
    for (const child of children) {
        if (child != null) node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
}

export async function renderTerminalPage(body) {
    _disposeAll(); // fresh state per window instance
    _body = body;

    const badge = _el('button', {
        id: 'termTargetBadge', 'data-testid': 'term-target-btn', title: 'Change target (active tab)',
        style: 'padding:2px 8px;border-radius:4px;font-size:10px;cursor:pointer;background:var(--accent-glow);color:var(--accent);border:1px solid var(--border);font-family:monospace;',
        onclick: () => window._termShowTargetPicker(),
    }, _lastTarget || '…');

    const statusDot = _el('span', {
        id: 'termStatusDot',
        style: 'width:8px;height:8px;border-radius:50%;background:var(--text-3);display:inline-block;flex-shrink:0;',
    });
    const statusLabel = _el('span', {
        id: 'termStatusLabel', 'data-testid': 'terminal-status',
        style: 'font-size:11px;color:var(--text-3);white-space:nowrap;',
    }, 'disconnected');

    _tabBar = _el('div', {
        style: 'display:flex;align-items:center;gap:4px;padding:6px 10px;background:var(--surface-1);border-bottom:1px solid var(--border);flex-shrink:0;',
    });
    _termArea = _el('div', {
        'data-testid': 'terminal-output', style: 'flex:1;min-height:0;padding:4px 6px;background:var(--surface-0);',
    });

    body.append(
        _el('div', { style: 'display:flex;flex-direction:column;height:100%;min-height:0;' },

            _el('div', {
                style: 'display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:var(--surface-1);border-bottom:1px solid var(--border);flex-shrink:0;gap:8px;',
            },
                _el('div', { style: 'display:flex;align-items:center;gap:8px;min-width:0;' },
                    _el('span', { style: 'font-size:14px;' }, '⌨️'),
                    _el('span', { style: 'font-weight:600;font-size:13px;color:var(--text-1);white-space:nowrap;' }, 'Terminal'),
                    badge,
                ),
                _el('div', { style: 'display:flex;align-items:center;gap:6px;flex-shrink:0;' },
                    _el('button', { class: 'btn-ghost', title: 'Decrease font size (all tabs)', style: 'font-size:11px;padding:4px 8px;', onclick: () => window._termFont(-1) }, 'A−'),
                    _el('button', { class: 'btn-ghost', title: 'Increase font size (all tabs)', style: 'font-size:11px;padding:4px 8px;', onclick: () => window._termFont(1) }, 'A+'),
                    _el('button', { class: 'btn-ghost', 'data-testid': 'term-clear-btn', title: 'Clear screen (Ctrl+L)', style: 'font-size:11px;padding:4px 10px;', onclick: () => window._termClear() }, '🗑 Clear'),
                    statusDot,
                    statusLabel,
                ),
            ),

            _tabBar,
            _termArea,
        ),
    );

    // One observer on the (always-visible) area; hidden tabs are refit on activation.
    let raf = 0;
    const ro = new ResizeObserver(() => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => {
            const s = _activeSession();
            if (!s) return;
            try { s.fit.fit(); } catch {}
            _sendResize(s);
        });
    });
    ro.observe(_termArea);

    _ensureThemeObserver();
    _createSession(_lastTarget || await resolveDefaultTerminalTarget());
}

function _fontSize() {
    const n = parseInt(localStorage.getItem(FONT_KEY), 10);
    return Number.isFinite(n) && n >= 9 && n <= 28 ? n : 14;
}

function _termTheme() {

    const cs = getComputedStyle(document.documentElement);
    const v = name => cs.getPropertyValue(name).trim();
    return {
        background: v('--surface-0') || '#111827',
        foreground: v('--text-1') || '#e2e8f0',
        cursor: v('--accent') || '#3584e4',
        selectionBackground: 'rgba(60, 110, 245, 0.30)',
    };
}

function _ensureThemeObserver() {
    if (_themeObserver) return;
    _themeObserver = new MutationObserver(() => {
        for (const s of _sessions) {
            if (!s.disposed) s.term.options.theme = _termTheme();
        }
    });
    _themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
}

function _activeSession() {
    return _sessions.find(s => s.id === _activeId) || null;
}

// ---------------------------------------------------------------------------
// Session lifecycle
// ---------------------------------------------------------------------------

function _createSession(target) {
    if (!_termArea) return null;

    if (typeof window.Terminal === 'undefined' || typeof window.FitAddon === 'undefined') {
        _termArea.innerHTML = `<div style="padding:16px;color:var(--ui-bad);font-size:13px;">xterm.js failed to load from the CDN. Check the internet connection and reopen the Terminal.</div>`;
        return null;
    }

    const container = _el('div', { style: 'width:100%;height:100%;' });
    const term = new window.Terminal({
        cursorBlink: true,
        fontSize: _fontSize(),
        fontFamily: "'JetBrains Mono','Fira Code',Consolas,'Courier New',monospace",
        scrollback: SCROLLBACK,
        theme: _termTheme(),
    });
    const fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(container);

    // xterm's default keydown handling cancels Ctrl/Cmd+V (and sends \x16 to the PTY),
    // which suppresses the browser's native paste event — so clipboard text never reaches
    // xterm's own `paste` listener. Returning false makes xterm skip the event, letting the
    // browser run its default action; the resulting `paste` event is handled by xterm itself.
    // The same for Ctrl/Cmd+C with a selection: xterm's default handler sends ^C to the PTY
    // and preventDefaults the keydown, so the browser's native `copy` event never fires and
    // the clipboard is never updated. Returning false lets the browser dispatch the `copy`
    // event; xterm's built-in copy listener then fills the clipboard with the selected text.
    // Without a selection, ^C still reaches the shell as usual.
    term.attachCustomKeyEventHandler(ev => {
        if (ev.type === 'keydown' && ev.key.toLowerCase() === 'v' && (ev.ctrlKey || ev.metaKey) && !ev.altKey) {
            return false;
        }
        if (ev.type === 'keydown' && ev.key.toLowerCase() === 'c' && (ev.ctrlKey || ev.metaKey) && !ev.altKey && term.hasSelection()) {
            return false;
        }
        return true;
    });

    const s = {
        id: ++_sessionIdSeq, target, container, term, fit,
        ws: null, disposed: false, intentionalClose: false,
        encoder: new TextEncoder(),
        statusKind: 'connecting', statusText: `connecting → ${target}`,
    };
    _sessions.push(s);
    _termArea.append(container);

    try { fit.fit(); } catch {}
    term.write(`\x1b[90mConnecting to ${target}…\x1b[0m\r\n`);

    term.onData(data => {
        if (s.ws && s.ws.readyState === WebSocket.OPEN) {
            s.ws.send(s.encoder.encode(data));
        }
    });

    container.addEventListener('mousedown', () => term.focus());

    _lastTarget = target;
    _activate(s.id);
    _connectWs(s);
    return s;
}

function _reconnectSession(s, target) {
    // Swap the active tab's connection for a new target in place.
    if (s.ws) { try { s.ws.close(); } catch {} s.ws = null; }
    s.intentionalClose = false;
    s.target = target;
    _lastTarget = target;
    s.term.reset();
    s.term.write(`\x1b[90mConnecting to ${target}…\x1b[0m\r\n`);
    _setStatus(s, 'connecting', `connecting → ${target}`);
    _renderTabs(); // tab label changed
    _syncToolbar();
    _connectWs(s);
}

function _activate(id) {
    const s = _sessions.find(x => x.id === id);
    if (!s) return;
    _activeId = id;
    for (const t of _sessions) {
        t.container.style.display = t.id === id ? '' : 'none';
    }
    // Hidden containers have zero size — refit now that this one is visible.
    try { s.fit.fit(); } catch {}
    _sendResize(s);
    s.term.focus();
    _renderTabs();
    _syncToolbar();
}

function _closeSession(id) {
    const idx = _sessions.findIndex(s => s.id === id);
    if (idx < 0) return;
    const [s] = _sessions.splice(idx, 1);
    _disposeSessionObj(s);
    if (!_sessions.length) { _createSession(_lastTarget || 'host'); return; }
    _activate(_sessions[Math.min(idx, _sessions.length - 1)].id);
}

function _disposeSessionObj(s) {
    s.disposed = true;
    if (s.ws) { try { s.ws.close(); } catch {} s.ws = null; }
    try { s.term.dispose(); } catch {}
    s.container.remove();
}

function _disposeAll() {
    for (const s of [..._sessions]) _disposeSessionObj(s);
    _sessions = [];
    _activeId = null;
    _body = null;
    _tabBar = null;
    _termArea = null;
}

// Closing the window must kill every PTY session, otherwise each open tab
// leaks a WebSocket + shell process on the server until page reload.
registerCloseGuard('terminal', () => { _disposeAll(); return true; });

window._termNewTab = function() {
    _createSession(_lastTarget || 'host');
};

// ---------------------------------------------------------------------------
// Tab bar / toolbar rendering
// ---------------------------------------------------------------------------

function _renderTabs() {
    if (!_tabBar) return;
    _tabBar.innerHTML = '';
    for (const s of _sessions) {
        const active = s.id === _activeId;
        const dot = _el('span', {
            'data-tab-dot': String(s.id),
            style: `width:7px;height:7px;border-radius:50%;background:${STATUS_COLORS[s.statusKind] || 'var(--text-3)'};display:inline-block;flex-shrink:0;`,
        });
        const label = _el('span', {
            style: 'max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;',
        }, s.target);
        const closeBtn = _el('button', {
            title: 'Close tab',
            style: 'background:none;border:none;color:var(--text-3);cursor:pointer;font-size:12px;line-height:1;padding:0 2px;',
            onclick: (e) => { e.stopPropagation(); _closeSession(s.id); },
        }, '×');
        const tab = _el('div', {
            title: `Terminal — ${s.target}`,
            style: `display:flex;align-items:center;gap:6px;padding:4px 8px;border-radius:6px;font-size:12px;cursor:pointer;font-family:monospace;background:${active ? 'var(--accent-glow)' : 'transparent'};color:${active ? 'var(--text-1)' : 'var(--text-3)'};border:1px solid ${active ? 'var(--border)' : 'transparent'};`,
            onclick: () => _activate(s.id),
        }, dot, label, closeBtn);
        _tabBar.append(tab);
    }
    const add = _el('button', {
        class: 'btn-ghost', title: `New tab (${_lastTarget || 'host'})`,
        style: 'font-size:12px;padding:3px 8px;',
        onclick: () => window._termNewTab(),
    }, '+');
    _tabBar.append(add);
}

function _syncToolbar() {
    if (!_body) return;
    const s = _activeSession();
    const badge = _body.querySelector('#termTargetBadge');
    if (badge && s) badge.textContent = s.target;
    const dot = _body.querySelector('#termStatusDot');
    const label = _body.querySelector('#termStatusLabel');
    if (dot && s) dot.style.background = STATUS_COLORS[s.statusKind] || 'var(--text-3)';
    if (label && s) label.textContent = s.statusText || 'disconnected';
}

function _setStatus(s, kind, text) {
    if (s.disposed) return;
    s.statusKind = kind;
    s.statusText = text;
    const dot = _tabBar ? _tabBar.querySelector(`[data-tab-dot="${s.id}"]`) : null;
    if (dot) dot.style.background = STATUS_COLORS[kind] || 'var(--text-3)';
    if (s.id === _activeId) {
        const d = _body && _body.querySelector('#termStatusDot');
        const l = _body && _body.querySelector('#termStatusLabel');
        if (d) d.style.background = STATUS_COLORS[kind] || 'var(--text-3)';
        if (l) l.textContent = text;
    }
}

// ---------------------------------------------------------------------------
// WebSocket plumbing (per session)
// ---------------------------------------------------------------------------

function _sendResize(s) {
    if (!s.ws || s.ws.readyState !== WebSocket.OPEN) return;
    const cols = s.term.cols, rows = s.term.rows;
    if (!cols || !rows) return; // hidden container — nothing to report yet
    try {
        s.ws.send(JSON.stringify({ type: 'resize', cols, rows }));
    } catch {}
}

function _connectWs(s) {
    if (s.disposed) return;
    _setStatus(s, 'connecting', `connecting → ${s.target}`);

    // core/ws.js handles the URL/token, jittered backoff and the codes that must
    // not be retried; this function is UI + PTY wiring only (TODO.md P3-08).
    s.ws = connectWs(`/ws/terminal/${encodeURIComponent(s.target)}`, (data) => {
        if (s.disposed || !s.term) return;
        if (typeof data === 'string') s.term.write(data);
        else if (data instanceof ArrayBuffer) s.term.write(new Uint8Array(data));
    }, {
        appId: 'terminal',
        isJson: false,
        name: `terminal:${s.target}`,
        stopOnCodes: [4002, 4003],
        heartbeatMs: 30_000,
        onOpen: (ws) => {
            ws.binaryType = 'arraybuffer';
            if (s.disposed) return;
            _setStatus(s, 'connected', s.target);
            _sendResize(s);
            if (s.id === _activeId) s.term.focus();
        },
        onClose: (ev) => {
            if (s.disposed) return;
            if (s.intentionalClose) { _setStatus(s, 'disconnected', 'disconnected'); return; }
            if (ev.code === 4001) { _setStatus(s, 'error', 'authentication failed — reload the page'); return; }
            if (ev.code === 4002) { _setStatus(s, 'error', 'target rejected — pick another target'); return; }
            if (ev.code === 4003) { _setStatus(s, 'error', 'invalid working directory'); return; }
        },
        onReconnectScheduled: (delay) => {
            if (s.disposed) return;
            _setStatus(s, 'reconnecting', `disconnected — retrying in ${Math.ceil(delay / 1000)}s`);
        },
        onAuthFailure: () => _setStatus(s, 'error', 'authentication failed — reload the page'),
    });
}

// ---------------------------------------------------------------------------
// Toolbar actions (active tab unless noted)
// ---------------------------------------------------------------------------

window._termClear = function() {
    const s = _activeSession();
    if (!s || s.disposed) return;
    s.term.clear();
    if (s.ws && s.ws.readyState === WebSocket.OPEN) s.ws.send('\x0c');
};

window._termFont = function(delta) {
    // Font size is a global preference — apply to every open tab.
    const next = Math.min(28, Math.max(9, _fontSize() + delta));
    localStorage.setItem(FONT_KEY, String(next));
    for (const s of _sessions) {
        if (s.disposed) continue;
        s.term.options.fontSize = next;
        if (s.id === _activeId) {
            try { s.fit.fit(); } catch {}
            _sendResize(s);
        }
    }
};

window._termShowTargetPicker = async function() {
    const s = _activeSession() || { target: 'host' };
    const existing = document.querySelector('#term-target-dialog');
    if (existing) existing.remove();

    const dialog = document.createElement('div');
    dialog.id = 'term-target-dialog';
    dialog.style.cssText = 'position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;';

    dialog.innerHTML = `
        <div style="background:var(--surface-1);border:1.5px solid var(--border);border-radius:16px;padding:24px 28px;width:min(420px,90vw);box-shadow:0 32px 96px rgba(0,0,0,0.7);">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
                <h3 style="margin:0;font-size:16px;color:var(--text-1);font-weight:700;">🔌 Terminal Target</h3>
                <button onclick="document.querySelector('#term-target-dialog').remove()" style="background:none;border:none;color:var(--text-3);font-size:22px;cursor:pointer;line-height:1;">×</button>
            </div>
            <label style="font-size:12px;color:var(--text-2);display:block;margin-bottom:6px;">Connection target (reconnects the active tab)</label>
            <select aria-label="Termtargetselect" id="termTargetSelect" style="width:100%;padding:9px 12px;border-radius:10px;border:1.5px solid var(--border);background:var(--surface-0);color:var(--text-1);font-size:13px;outline:none;">
                <optgroup label="Host">
                    <option value="host">🐧 Host shell</option>
                    <option value="sudo">🔑 Sudo shell</option>
                </optgroup>
                <optgroup id="termUserGroup" label="Log in as user"></optgroup>
                <optgroup id="termContainerGroup" label="Containers (running)"></optgroup>
            </select>
            <div id="termSudoNote" style="font-size:11px;color:#f59e0b;margin-top:6px;display:none;"></div>
            <div id="termDockerNote" style="font-size:11px;color:var(--text-3);margin-top:6px;display:none;">Docker unavailable — container targets disabled.</div>
            <input id="termCustomContainer" type="text" placeholder="…or type a container name to connect directly" spellcheck="false"
                style="width:100%;margin-top:8px;padding:9px 12px;border-radius:10px;border:1.5px solid var(--border);background:var(--surface-0);color:var(--text-1);font-size:13px;font-family:monospace;outline:none;" />
            <div id="termPickerError" style="font-size:11px;color:#ef4444;margin-top:6px;"></div>
            <div style="display:flex;justify-content:flex-end;gap:10px;margin-top:16px;padding-top:12px;border-top:1px solid var(--border);">
                <button class="btn-ghost" onclick="document.querySelector('#term-target-dialog').remove()" style="padding:8px 16px;font-size:13px;">Cancel</button>
                <button class="btn-primary" onclick="_termPickerConnect()" style="padding:8px 18px;font-size:13px;">Connect</button>
            </div>
        </div>`;
    document.body.append(dialog);

    const group = dialog.querySelector('#termContainerGroup');
    const userGroup = dialog.querySelector('#termUserGroup');
    try {
        const opts = await apiJsonFetch('/api/terminal/options');
        const users = (opts && Array.isArray(opts.users)) ? opts.users : [];
        if (!users.length) {
            const opt = document.createElement('option');
            opt.disabled = true;
            opt.textContent = 'no other users available';
            userGroup.append(opt);
        }
        for (const u of users) {
            const opt = document.createElement('option');
            opt.value = `user:${u.username}`;
            const suffix = opts.server_user === u.username ? ' (server)' : ' (password)';
            opt.textContent = `👤 ${u.username}${suffix}`;
            if (s.target === opt.value) opt.selected = true;
            userGroup.append(opt);
        }
        if (!opts.sudo_available) {
            const sudoOpt = dialog.querySelector('#termTargetSelect option[value="sudo"]');
            if (sudoOpt) sudoOpt.disabled = true;
            const note = dialog.querySelector('#termSudoNote');
            if (note) {
                note.textContent = opts.sudo_reason || 'Sudo shell not available for the server account.';
                note.style.display = 'block';
            }
        }
    } catch {}

    try {
        const data = await apiJsonFetch('/api/docker/containers');
        const running = (data.containers || []).filter(c => c.status === 'running');
        if (!running.length) {
            const opt = document.createElement('option');
            opt.disabled = true;
            opt.textContent = 'no running containers';
            group.append(opt);
        }
        for (const c of running) {
            const opt = document.createElement('option');
            opt.value = `container:${c.name}`;
            opt.textContent = `📦 ${c.name}`;
            if (s.target === opt.value) opt.selected = true;
            group.append(opt);
        }
    } catch {
        const note = dialog.querySelector('#termDockerNote');
        if (note) note.style.display = 'block';
    }

    for (const opt of dialog.querySelectorAll('#termTargetSelect option')) {
        if (opt.value === s.target) { opt.selected = true; break; }
    }
};

window._termPickerConnect = function() {
    const s = _activeSession();
    if (!s) return;
    const custom = (document.querySelector('#termCustomContainer')?.value || '').trim();
    const errEl = document.querySelector('#termPickerError');

    let target;
    if (custom) {
        if (!_VALID_CONTAINER.test(custom)) {
            if (errEl) errEl.textContent = 'Invalid container name — use letters, digits, ".", "_" or "-" (max 63 chars).';
            return;
        }
        target = `container:${custom}`;
    } else {
        const sel = document.querySelector('#termTargetSelect');
        if (!sel) return;
        target = sel.value;
    }

    const dialog = document.querySelector('#term-target-dialog');
    if (dialog) dialog.remove();
    _reconnectSession(s, target);
};
