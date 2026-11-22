// EthOS NAS — global command palette (Ctrl+K / Cmd+K).
//
// 18 apps plus nine Settings sections are too many for a desktop grid: one
// search over apps and settings beats hunting through icons. The palette is
// built from the same registry the desktop uses, so a new app appears here
// automatically. Styling lives in `static/css/ethos-ui.css` (`.cmdk-*`) — no
// injected <style>, no new window globals.

import { getRegistry } from './registry.js';
import { getAppIcon } from './app-icons.js';
import { escapeHtml } from './utils.js';
import { openAppAt } from './app-links.js';

// Mirrors Settings' rail (apps/settings.js SECTIONS). Kept as plain data so the
// palette never imports the whole Settings module; the guard test asserts the
// ids still exist.
const SETTINGS_SECTIONS = [
    ['overview', 'Health', 'Health checks and system alerts'],
    ['updates', 'Updates', 'Core releases and the phone apps'],
    ['system', 'System', 'Ubuntu release, package updates, reboot'],
    ['maintenance', 'Maintenance', 'Caches, health thresholds, danger zone'],
    ['users', 'Users', 'Accounts, roles and passwords'],
    ['sharing', 'Sharing', 'Resources you share'],
    ['security', 'Security', 'Session, API key and hardening'],
    ['appearance', 'Appearance', 'Desktop wallpaper'],
    ['about', 'About', 'Build identity and self-check'],
];

const MAX_RENDERED = 40;

let _root = null;
let _input = null;
let _list = null;
let _commands = [];
let _filtered = [];
let _active = 0;
let _isOpen = false;

function _iconTile(appId, fallbackIcon) {
    const icon = getAppIcon(appId, 16);
    if (!icon) return `<span class="cmdk-icon cmdk-icon-emoji" aria-hidden="true">${escapeHtml(fallbackIcon || '▸')}</span>`;
    return `<span class="cmdk-icon" style="background:${icon.gradient}" aria-hidden="true">${icon.svg}</span>`;
}

function _buildCommands() {
    const commands = [];
    const registry = getRegistry();
    for (const [id, app] of Object.entries(registry)) {
        commands.push({
            id: `app:${id}`,
            keywords: `app ${id}`,
            title: app.title,
            subtitle: 'Application',
            icon: _iconTile(id, app.icon),
            run: () => window.openApp?.(id),
        });
    }
    commands.push({
        id: 'action:search',
        keywords: 'search files find',
        title: 'Search files',
        subtitle: 'Universal Search',
        icon: _iconTile('search', '🔍'),
        run: () => window.openApp?.('search'),
    });
    commands.push({
        id: 'action:logs',
        keywords: 'logs journal diagnostics error',
        title: 'Open Logs',
        subtitle: 'All diagnostic sources',
        icon: _iconTile('logs', '📜'),
        run: () => window.openApp?.('logs'),
    });
    for (const [section, label, hint] of SETTINGS_SECTIONS) {
        commands.push({
            id: `settings:${section}`,
            keywords: `settings ${section} ${label}`,
            title: `Settings → ${label}`,
            subtitle: hint,
            icon: _iconTile('settings', '🔧'),
            run: () => openAppAt('settings', section),
        });
    }
    return commands;
}

function _filter(query) {
    const term = String(query || '').trim().toLowerCase();
    if (!term) return _commands;
    return _commands.filter((command) =>
        `${command.title} ${command.subtitle} ${command.keywords}`.toLowerCase().includes(term));
}

function _paint() {
    if (!_list) return;
    _active = Math.min(_active, Math.max(0, _filtered.length - 1));
    if (!_filtered.length) {
        _list.innerHTML = '<div class="cmdk-empty">No matching commands</div>';
        return;
    }
    _list.innerHTML = _filtered.slice(0, MAX_RENDERED).map((command, index) => `
        <button type="button" class="cmdk-item${index === _active ? ' active' : ''}"
                data-command-index="${index}" role="option" aria-selected="${index === _active}">
            ${command.icon}
            <span class="cmdk-text"><span>${escapeHtml(command.title)}</span>
            <small>${escapeHtml(command.subtitle || '')}</small></span>
        </button>`).join('');
    _list.querySelectorAll('[data-command-index]').forEach((button) => {
        button.addEventListener('click', () => _run(Number(button.dataset.commandIndex)));
        button.addEventListener('mousemove', () => {
            const index = Number(button.dataset.commandIndex);
            if (index === _active) return;
            _active = index;
            _list.querySelectorAll('[data-command-index]').forEach((el) => {
                const isActive = Number(el.dataset.commandIndex) === _active;
                el.classList.toggle('active', isActive);
                el.setAttribute('aria-selected', String(isActive));
            });
        });
    });
}

function _run(index) {
    const command = _filtered[index];
    if (!command) return;
    closeCommandPalette();
    try {
        command.run();
    } catch (err) {
        console.error('[ethos] command failed:', err);
    }
}

function _onInput() {
    _filtered = _filter(_input.value);
    _active = 0;
    _paint();
}

function _onKey(event) {
    if (!_isOpen) return;
    if (event.key === 'Escape') { event.preventDefault(); closeCommandPalette(); return; }
    if (event.key === 'ArrowDown') { event.preventDefault(); _active = Math.min(_active + 1, Math.min(_filtered.length, MAX_RENDERED) - 1); _paint(); return; }
    if (event.key === 'ArrowUp') { event.preventDefault(); _active = Math.max(_active - 1, 0); _paint(); return; }
    if (event.key === 'Enter') { event.preventDefault(); _run(_active); }
}

function _ensureDom() {
    if (_root) return;
    _root = document.createElement('div');
    _root.className = 'cmdk-backdrop';
    _root.setAttribute('data-testid', 'command-palette');
    _root.setAttribute('role', 'dialog');
    _root.setAttribute('aria-modal', 'true');
    _root.setAttribute('aria-label', 'Command palette');
    _root.style.display = 'none';
    _root.innerHTML = `<div class="cmdk-panel">
        <input class="cmdk-input" data-testid="command-input" type="search" autocomplete="off"
               spellcheck="false" placeholder="Search apps and settings…" aria-label="Search apps and settings">
        <div class="cmdk-list" data-testid="command-list" role="listbox" aria-label="Commands"></div>
    </div>`;
    document.body.appendChild(_root);

    _input = _root.querySelector('.cmdk-input');
    _list = _root.querySelector('.cmdk-list');
    _input.addEventListener('input', _onInput);
    _input.addEventListener('keydown', _onKey);
    _root.addEventListener('mousedown', (event) => { if (event.target === _root) closeCommandPalette(); });
}

export function openCommandPalette() {
    _ensureDom();
    _commands = _buildCommands();
    _filtered = _commands;
    _active = 0;
    _isOpen = true;
    _root.style.display = 'flex';
    _input.value = '';
    _paint();
    setTimeout(() => _input?.focus(), 0);
}

export function closeCommandPalette() {
    if (!_root) return;
    _isOpen = false;
    _root.style.display = 'none';
}

export function isCommandPaletteOpen() { return _isOpen; }

/** Bind the global shortcut once (called from app.js bootstrap). */
export function initCommandPalette() {
    if (document.body.dataset.cmdkBound === '1') return;
    document.body.dataset.cmdkBound = '1';
    document.addEventListener('keydown', (event) => {
        const key = String(event.key || '').toLowerCase();
        if (!(key === 'k' && (event.ctrlKey || event.metaKey) && !event.altKey)) return;
        // Ctrl+K belongs to readline inside the terminal.
        if (event.target instanceof Element && event.target.closest('.xterm')) return;
        if (event.target instanceof Element && event.target.closest('input, textarea, [contenteditable="true"]') && !_isOpen) return;
        event.preventDefault();
        if (_isOpen) closeCommandPalette(); else openCommandPalette();
    });
}
