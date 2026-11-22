// EthOS NAS — Login Form (PAM authentication)

import { loadRemoteLayout } from '../core/desktop-layout.js';
import { renderDesktopIcons } from '../core/registry.js';
import { loadRemoteWallpaper } from '../core/wallpaper.js';

/**
 * Re-apply everything that is stored per account on the server: desktop icons
 * and wallpaper.
 *
 * The boot-time hydration in app.js runs before the overlay login completes, so
 * it is a no-op when the token was expired: without this, a fresh sign-in kept
 * showing whatever the previous account left in localStorage.
 */
function _hydratePerUserDesktop() {
    loadRemoteLayout()
        .then((layout) => { if (layout) renderDesktopIcons(); })
        .catch(() => {});
    loadRemoteWallpaper();
}

export function initLoginForm() {
    const overlay = document.getElementById('loginOverlay');
    if (!overlay) return;

    // If a valid token already exists, skip login screen entirely
    try {
        const stored = localStorage.getItem('token');
        if (stored && _isLikelyValidToken(stored)) {
            hideLogin();
            showDesktop();
            return;
        }
    } catch (_) {}

    overlay.style.display = 'flex';
}

function _isLikelyValidToken(token) {
    try {
        const parts = token.split('.');
        if (parts.length !== 3) return false;
        // Decode payload and check expiry
        let payload = parts[1];
        while (payload.length % 4) payload += '=';
        const decoded = JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/')));
        if (!decoded.exp) return false;
        return decoded.exp > Date.now() / 1000;
    } catch (_) {
        return false;
    }
}

export function hideLogin() {
    const overlay = document.getElementById('loginOverlay');
    if (overlay) overlay.style.display = 'none';
}

export function showDesktop() {
    const desktop = document.getElementById('desktopIcons');
    if (desktop) desktop.style.display = '';
    // `renderDesktopIcons` was called here without being imported, so the throw
    // was swallowed by the try/catch and the grid kept the previous account's
    // icons. Now the import is real and the re-render always happens.
    try { renderDesktopIcons(); } catch (_) {}
}

export async function submitLogin(username, password) {
    const errorEl = document.getElementById('loginError');
    const submitBtn = document.getElementById('loginSubmitBtn');

    // Clear previous errors
    if (errorEl) errorEl.textContent = '';

    // Show loading state
    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = 'Signing in...';
    }

    try {
        const res = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });

        if (res.ok) {
            const data = await res.json();
            localStorage.setItem('token', data.token);
            // Overlay login does not reload the page — start the notification
            // badge poll now that a token exists (no-op if already running).
            if (window.startNotificationPolling) window.startNotificationPolling();
            // Mount a pinned package's right-edge ribbon, if one is installed.
            if (window.initPackageRibbons) window.initPackageRibbons();
            hideLogin();
            showDesktop();
            _hydratePerUserDesktop();
            return true;
        }

        // Show error message
        let msg = 'Invalid credentials';
        try {
            const errData = await res.json();
            msg = errData.detail || msg;
        } catch (_) {}

        if (errorEl) errorEl.textContent = msg;
        return false;
    } catch (err) {
        if (errorEl) errorEl.textContent = 'Connection failed. Is the server running?';
        return false;
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = 'Sign in';
        }
    }
}

// ── Login overlay styles live in frontend/static/css/ethos-ui.css ──────────

// ── Export for dynamic creation from index.html ───────────────────────────────

export function renderLoginOverlay() {
    if (document.getElementById('loginOverlay')) return;

    const overlay = document.createElement('div');
    overlay.id = 'loginOverlay';
    overlay.innerHTML = `
        <div class="login-card">
            <div class="login-brand">
                <span class="login-brand-icon">&#x1F5A5;</span>
                <div class="login-brand-title">EthOS NAS</div>
                <div class="login-brand-subtitle">Sign in with your Linux credentials</div>
            </div>
            <form id="loginForm" onsubmit="return false;" data-testid="login-form">
                <div class="login-field">
                    <label class="login-label" for="loginUsername">Username</label>
                    <input class="login-input" id="loginUsername" type="text" autocomplete="username" placeholder="Enter your username" data-testid="login-username" autofocus />
                </div>
                <div class="login-field">
                    <label class="login-label" for="loginPassword">Password</label>
                    <input class="login-input" id="loginPassword" type="password" autocomplete="current-password" placeholder="Enter your password" data-testid="login-password" />
                </div>
                <button class="login-submit" id="loginSubmitBtn" type="submit" data-testid="login-submit">Sign in</button>
            </form>
            <div id="loginError"></div>
        </div>
    `;

    // Wire up form submission — use window.submitLogin (imported via app.js) or local fallback
    const loginFn = typeof window.submitLogin === 'function' ? window.submitLogin : submitLogin;

    overlay.addEventListener('submit', (e) => { e.preventDefault(); });
    document.getElementById('loginSubmitBtn')?.addEventListener('click', () => {
        loginFn(
            document.getElementById('loginUsername').value.trim(),
            document.getElementById('loginPassword').value,
        );
    });
    overlay.querySelector('#loginForm').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            loginFn(
                document.getElementById('loginUsername').value.trim(),
                document.getElementById('loginPassword').value,
            );
        }
    });

    // Store refs for direct access
    overlay._usernameInput = () => overlay.querySelector('#loginUsername');
    overlay._passwordInput = () => overlay.querySelector('#loginPassword');

    document.body.appendChild(overlay);
}
