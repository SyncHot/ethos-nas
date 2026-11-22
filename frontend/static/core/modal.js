// EthOS NAS — Unified Modal System (alert / confirm / prompt)
// Replaces all native browser dialogs with a slick modal overlay.
// Usage: showAlert(msg), showConfirm(msg, cb), showPrompt(msg, default, cb)

import { escapeAttr, escapeHtml } from './utils.js';

let _activeModal = null;
let _resolveCurrent = null;
let _lastFocused = null;   // element to hand focus back to on close (TODO.md P3-11)

const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),'
    + 'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function _focusables(box) {
    return [...box.querySelectorAll(FOCUSABLE)].filter((el) => el.offsetParent !== null || el === document.activeElement);
}

/* ── Core render engine ─────────────────────────────────────────────────── */

function _renderModal({ title, bodyHtml, footerHtml, width, onKeydown }) {
    if (_activeModal) _closeModal();
    // Remember where focus came from so Escape/close returns the user there.
    _lastFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const overlay = document.createElement('div');
    overlay.className = 'ethos-modal-overlay';
    overlay.setAttribute('data-testid', 'ethos-modal');

    const box = document.createElement('div');
    box.className = 'ethos-modal-box';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', 'ethosModalTitle');
    box.setAttribute('aria-describedby', 'ethosModalBody');
    if (width) box.style.width = width;

    box.innerHTML = `
        <div class="ethos-modal-header">
            <span class="ethos-modal-title" id="ethosModalTitle" data-testid="ethos-modal-title">${escapeHtml(title || 'EthOS NAS')}</span>
            <button class="ethos-modal-close" data-testid="ethos-modal-close" aria-label="Close dialog">&times;</button>
        </div>
        <div class="ethos-modal-body" id="ethosModalBody" data-testid="ethos-modal-body">${bodyHtml}</div>
        <div class="ethos-modal-footer" data-testid="ethos-modal-footer">${footerHtml}</div>`;

    overlay.appendChild(box);
    document.body.appendChild(overlay);
    _activeModal = overlay;

    /* Close handler */
    const closeBtn = box.querySelector('.ethos-modal-close');
    const _close = () => { _closeModal(); if (_resolveCurrent) { _resolveCurrent(null); _resolveCurrent = null; } };
    closeBtn.onclick = _close;

    /* Backdrop click closes */
    overlay.addEventListener('click', (e) => { if (e.target === overlay) _close(); });

    /* Keyboard: Escape always closes (this is the *only* Escape handler —
       unified-confirm used to bind its own with {once:true}) and Tab is trapped
       inside the dialog. */
    const _kd = (e) => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); _close(); return; }
        if (e.key === 'Tab') {
            const items = _focusables(box);
            if (!items.length) return;
            const first = items[0];
            const last = items[items.length - 1];
            if (e.shiftKey && (document.activeElement === first || !box.contains(document.activeElement))) {
                e.preventDefault(); last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault(); first.focus();
            }
            return;
        }
        if (onKeydown) onKeydown(e, _close);
    };
    document.addEventListener('keydown', _kd, true);
    overlay._keydownHandler = _kd;

    /* Focus the first useful control (an explicit [autofocus] wins). */
    const target = box.querySelector('[autofocus]') || box.querySelector('input,textarea,select')
        || box.querySelector('.ethos-modal-btn-primary') || _focusables(box)[0];
    if (target) setTimeout(() => { try { target.focus(); } catch (_) { /* detached */ } }, 40);

    return overlay;
}

function _closeModal() {
    if (_activeModal) {
        if (_activeModal._keydownHandler) document.removeEventListener('keydown', _activeModal._keydownHandler, true);
        _activeModal.remove();
        _activeModal = null;
    }
    // Hand focus back to whatever opened the dialog (keyboard users lose it otherwise).
    const back = _lastFocused;
    _lastFocused = null;
    if (back && back.isConnected) {
        setTimeout(() => { try { back.focus(); } catch (_) { /* removed */ } }, 0);
    }
}

/* ── showAlert(title, message) ────────────────────────────────────────────── */

export function showAlert(title, message) {
    if (message === undefined) { message = title; title = 'EthOS NAS'; }
    return new Promise((resolve) => {
        _resolveCurrent = resolve;
        _renderModal({
            title,
            bodyHtml: `<p class="ethos-modal-message">${escapeHtml(message)}</p>`,
            footerHtml: `<button class="ethos-modal-btn ethos-modal-btn-primary" data-testid="ethos-modal-ok" autofocus>OK</button>`,
            onKeydown(e, close) { if (e.key === 'Enter') { e.preventDefault(); close(); resolve(true); _resolveCurrent = null; } },
        });
        const okBtn = _activeModal.querySelector('[data-testid="ethos-modal-ok"]');
        if (okBtn) okBtn.onclick = () => { _closeModal(); resolve(true); _resolveCurrent = null; };
    });
}

/* ── showConfirm(title, message, onConfirm, onCancel) ─────────────────────── */

export function showConfirm(title, message, onConfirm, onCancel) {
    // Support both (title, message, cb) and (title, message, { onConfirm, onCancel, danger })
    let confirmCb = onConfirm;
    let cancelCb = onCancel;
    let danger = false;
    let confirmLabel = 'OK';
    let cancelLabel = 'Cancel';
    if (typeof onConfirm === 'object') {
        confirmCb = onConfirm.onConfirm || onConfirm.confirm;
        cancelCb = onConfirm.onCancel || onConfirm.cancel || onCancel;
        danger = !!onConfirm.danger;
        confirmLabel = onConfirm.confirmLabel || confirmLabel;
        cancelLabel = onConfirm.cancelLabel || cancelLabel;
    }
    if (message === undefined) { message = title; title = 'Confirm'; }

    return new Promise((resolve) => {
        _resolveCurrent = resolve;
        _renderModal({
            title,
            bodyHtml: `<p class="ethos-modal-message">${escapeHtml(message)}</p>`,
            footerHtml: `
                <button class="ethos-modal-btn ethos-modal-btn-ghost" data-testid="ethos-modal-cancel">${escapeHtml(cancelLabel)}</button>
                <button class="ethos-modal-btn ${danger ? 'ethos-modal-btn-danger' : 'ethos-modal-btn-primary'}" data-testid="ethos-modal-ok">${escapeHtml(confirmLabel)}</button>`,
            onKeydown(e, close) {
                if (e.key === 'Enter') { e.preventDefault(); close(); resolve(true); _resolveCurrent = null; if (confirmCb) confirmCb(); }
            },
        });
        const okBtn = _activeModal.querySelector('[data-testid="ethos-modal-ok"]');
        const cancelBtn = _activeModal.querySelector('[data-testid="ethos-modal-cancel"]');
        if (okBtn) okBtn.onclick = () => { _closeModal(); resolve(true); _resolveCurrent = null; if (confirmCb) confirmCb(); };
        if (cancelBtn) cancelBtn.onclick = () => { _closeModal(); resolve(false); _resolveCurrent = null; if (cancelCb) cancelCb(); };
    });
}

/* ── showPrompt(title, message, defaultValue, callback) ───────────────────── */

export function showPrompt(title, message, defaultValue, callback) {
    let cb = callback;
    let defVal = defaultValue || '';
    if (message === undefined) { message = title; title='Input'; }
    if (typeof defaultValue === 'function') { cb = defaultValue; defVal = ''; }

    return new Promise((resolve) => {
        _resolveCurrent = resolve;
        _renderModal({
            title,
            width: '420px',
            bodyHtml: `
                <p class="ethos-modal-message" style="margin-bottom:12px;">${escapeHtml(message)}</p>
                <input class="ethos-modal-input" data-testid="ethos-modal-input" value="${escapeAttr(defVal)}" placeholder="Type here..." autofocus>`,
            footerHtml: `
                <button class="ethos-modal-btn ethos-modal-btn-ghost" data-testid="ethos-modal-cancel">Cancel</button>
                <button class="ethos-modal-btn ethos-modal-btn-primary" data-testid="ethos-modal-ok">OK</button>`,
            onKeydown(e, close) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    const val = _activeModal.querySelector('[data-testid="ethos-modal-input"]')?.value || '';
                    close(); resolve(val); _resolveCurrent = null;
                    if (cb) cb(val);
                }
            },
        });
        const input = _activeModal.querySelector('[data-testid="ethos-modal-input"]');
        const okBtn = _activeModal.querySelector('[data-testid="ethos-modal-ok"]');
        const cancelBtn = _activeModal.querySelector('[data-testid="ethos-modal-cancel"]');
        if (okBtn) okBtn.onclick = () => { const v = input?.value || ''; _closeModal(); resolve(v); _resolveCurrent = null; if (cb) cb(v); };
        if (cancelBtn) cancelBtn.onclick = () => { _closeModal(); resolve(null); _resolveCurrent = null; if (cb) cb(null); };
    });
}

/**
 * Generic form modal — renders arbitrary body HTML (inputs/selects) and
 * resolves with an object of {name: value} for every named field, or null on
 * cancel.
 *
 * Options:
 *   validate(values, body) -> string|null   inline error; keeps the dialog open
 *   onSubmit(values, {setError, close}) -> false|void  async submit; return
 *       false (or throw) to keep the dialog open with an error
 *   onReady(body, overlay)                  wire dynamic fields after render
 *   width                                   dialog width (default 480px)
 *   danger                                  red primary button
 */
export function showFormModal({
    title, bodyHtml, confirmLabel = 'OK', danger = false,
    validate = null, onSubmit = null, onReady = null, width = null,
}) {
    return new Promise((resolve) => {
        _renderModal({
            title,
            width: width || '480px',
            bodyHtml,
            footerHtml: `
                <span data-modal-error role="alert"
                      style="display:none;flex:1;text-align:left;align-self:center;font-size:12px;line-height:1.5;color:#ef4444;"></span>
                <button class="ethos-modal-btn ethos-modal-btn-ghost" data-testid="modal-form-cancel">Cancel</button>
                <button class="ethos-modal-btn ${danger ? 'ethos-modal-btn-danger' : 'ethos-modal-btn-primary'}" data-testid="modal-form-ok">${escapeHtml(confirmLabel)}</button>`,
            onKeydown(e) {
                if (e.key === 'Enter' && e.target?.tagName !== 'TEXTAREA' && e.target?.tagName !== 'BUTTON') {
                    e.preventDefault();
                    submitForm();
                }
            },
        });
        const okBtn = _activeModal.querySelector('[data-testid="modal-form-ok"]');
        const cancelBtn = _activeModal.querySelector('[data-testid="modal-form-cancel"]');
        const body = _activeModal.querySelector('.ethos-modal-body');
        const errorEl = _activeModal.querySelector('[data-modal-error]');
        let busy = false;

        function showError(message) {
            if (!errorEl) return;
            errorEl.textContent = String(message || 'Something went wrong');
            errorEl.style.display = 'block';
            const invalid = _activeModal?.querySelector('[aria-invalid="true"]');
            if (invalid) setTimeout(() => { try { invalid.focus(); } catch (_) { /* detached */ } }, 0);
        }
        function clearError() {
            if (!errorEl) return;
            errorEl.textContent = '';
            errorEl.style.display = 'none';
        }
        function collect() {
            const out = {};
            for (const el of _activeModal.querySelectorAll('input,select,textarea')) {
                const key = el.name || el.dataset.testid;
                if (!key) continue;
                if (el.type === 'checkbox') out[key] = el.checked;
                else out[key] = el.value;
            }
            return out;
        }
        async function submitForm() {
            if (busy || !_activeModal) return;
            _activeModal.querySelectorAll('[aria-invalid="true"]').forEach((el) => el.removeAttribute('aria-invalid'));
            const values = collect();
            if (validate) {
                const message = validate(values, body);
                if (message) { showError(message); return; }
            }
            clearError();
            busy = true;
            if (okBtn) okBtn.disabled = true;
            if (cancelBtn) cancelBtn.disabled = true;
            try {
                if (onSubmit) {
                    const result = await onSubmit(values, {
                        setError: showError,
                        clearError,
                        close: () => { _closeModal(); resolve(values); _resolveCurrent = null; },
                    });
                    if (result === false) return;
                }
                _closeModal();
                resolve(values);
                _resolveCurrent = null;
            } catch (err) {
                showError(err?.message || String(err));
            } finally {
                busy = false;
                if (okBtn) okBtn.disabled = false;
                if (cancelBtn) cancelBtn.disabled = false;
            }
        }
        if (okBtn) okBtn.onclick = submitForm;
        if (cancelBtn) cancelBtn.onclick = () => { _closeModal(); resolve(null); _resolveCurrent = null; };
        if (onReady) {
            try { onReady(body, _activeModal); } catch (_) { /* caller wiring must not break the dialog */ }
        }
    });
}
