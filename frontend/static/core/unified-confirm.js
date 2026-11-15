// EthOS NAS — Unified Confirm Dialog (singleton)
// Global singleton modal triggered programmatically: showConfirm({ title, message, onConfirm, onCancel })
// Replaces inline window.confirm() calls throughout the codebase over time.

import { escapeHtml } from './utils.js';

let _confirmStack = [];  // stack of pending confirm dialogs for nested usage
let _activeDialog = null;

/**
 * Show a unified confirmation dialog with customizable title, message, and callbacks.
 * 
 * @param {Object} opts - Dialog options
 * @param {string} opts.title - Dialog title (default: "Confirm")
 * @param {string} opts.message - Confirmation message text
 * @param {Function} opts.onConfirm - Callback when user clicks OK (async supported)
 * @param {Function} opts.onCancel - Optional callback when dialog is dismissed via Cancel/Escape
 * @param {string} [opts.cancelText] - Custom cancel button text (default: "Cancel")
 * @param {string} [opts.okText] - Custom OK button text (default: "OK")
 * @returns {HTMLElement} The created container element for cleanup purposes
 */
export function showConfirm(opts) {
    const container = document.createElement('div');
    container.className = 'unified-confirm-overlay';
    container.setAttribute('data-testid', 'unified-confirm-dialog');
    
    const dialog = document.createElement('div');
    dialog.className = 'unified-confirm-box';
    dialog.setAttribute('role', 'alertdialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'unifiedConfirmTitle');
    dialog.setAttribute('aria-describedby', 'unifiedConfirmBody');
    
    // Build dialog HTML structure with semantic class names for CSS targeting
    dialog.innerHTML = `
        <div class="unified-confirm-header">
            <span class="unified-confirm-title" id="unifiedConfirmTitle">${escapeHtml(opts.title || 'Confirm')}</span>
            <button class="unified-confirm-close" data-testid="unified-confirm-close" aria-label="Close dialog">&times;</button>
        </div>
        <div class="unified-confirm-body" id="unifiedConfirmBody">${escapeHtml(opts.message)}</div>
        <div class="unified-confirm-actions">
            <button class="btn-ghost unified-confirm-cancel" 
                    data-testid="unified-confirm-cancel">${opts.cancelText || 'Cancel'}</button>
            <button class="btn-primary unified-confirm-ok" 
                    data-testid="unified-confirm-ok">${opts.okText || 'OK'}</button>
        </div>`;

    container.appendChild(dialog);
    
    // Track all close handlers for cleanup. Focus returns to the element that
    // opened the dialog (keyboard users would otherwise be dropped at <body>).
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    let _keyHandler = null;   // Escape listener, removed by close()
    const close = () => {
        if (_keyHandler) { document.removeEventListener('keydown', _keyHandler, true); _keyHandler = null; }
        if (container.parentNode) container.remove();
        _activeDialog = null;
        if (opener && opener.isConnected) {
            setTimeout(() => { try { opener.focus(); } catch (_) { /* removed */ } }, 0);
        }
    };
    
    // Wire up click handlers: close button, cancel button, and OK button
    dialog.querySelector('.unified-confirm-close').onclick = close;
    dialog.querySelector('.unified-confirm-cancel').onclick = () => { 
        opts.onCancel?.();  // Call onCancel callback before closing
        close(); 
    };
    
    const okBtn = dialog.querySelector('.unified-confirm-ok');
    okBtn.onclick = async () => {
        try { await opts.onConfirm(); } catch(e) {}
        close();
    };

    document.body.appendChild(container);
    _activeDialog = container;
    
    // Focus the OK button for accessibility and keyboard navigation
    okBtn.focus();
    
    // Escape dismisses the dialog. This used to be `{ once: true }` on the
    // container itself, so it silently stopped working after the first key event
    // (and never fired when focus was elsewhere). Bound on the document, removed
    // by close() (TODO.md P3-11).
    _keyHandler = (e) => {
        if (e.key !== 'Escape') return;
        e.preventDefault();
        e.stopPropagation();
        if (_activeDialog !== container) return;   // a newer dialog owns Escape
        close();
        opts.onCancel?.();
    };
    document.addEventListener('keydown', _keyHandler, true);

    return container;
}
