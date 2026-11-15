// EthOS NAS — API helper with auth header injection

let _token = null;

export function getToken() {
    if (_token) return _token;
    const stored = localStorage.getItem('token');
    if (stored) _token = stored;
    return _token;
}

export function setToken(t) { _token = t; }

/**
 * Error thrown by every JSON helper. Carries the backend's `detail` so callers
 * can show the real reason (e.g. the 403 permission explanation from the file
 * API) instead of a bare "HTTP 403".
 *
 * kind: 'http' | 'forbidden' | 'server' | 'network' | 'timeout'
 */
export class ApiError extends Error {
    constructor(message, { status = 0, detail = '', kind = 'http', payload = null } = {}) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
        this.detail = detail;
        this.kind = kind;
        this.payload = payload;
    }
}

function _addAuthHeaders(options = {}) {
    const headers = { ...options.headers };
    const token = getToken();
    if (token && !headers['Authorization']) {
        headers['Authorization'] = `Bearer ${token}`;
    }
    return { ...options, headers };
}

/**
 * Fetch with Bearer token injected from localStorage / in-memory store.
 *
 * Session-expiry handling: if a protected endpoint answers 401 while we hold
 * a token (i.e. the JWT expired), clear it and reload — bootstrap re-runs and
 * shows the login overlay instead of letting every app render scattered
 * "HTTP 401" errors. The login endpoint itself is excluded: a failed login
 * legitimately returns 401 and must not trigger a reload loop.
 */
export async function apiFetch(path, options = {}) {
    const opts = _addAuthHeaders(options);
    const res = await fetch(path, opts);
    if (res.status === 401 && !path.startsWith('/api/auth/login') && getToken()) {
        _token = null;
        localStorage.removeItem('token');
        window.location.reload();
    }
    return res;
}

/**
 * Convenience wrapper: auto-throws on non-OK responses.
 *
 * Bodies are passed through untouched (callers stringify themselves), which is
 * why this predates `apiRequest`. Errors now carry `status`, `detail` and `kind`
 * so `err.detail || err.message` is always the useful text.
 */
export async function apiJsonFetch(path, options = {}) {
    let res;
    try {
        res = await apiFetch(path, options);
    } catch (err) {
        throw new ApiError('Network error — the NAS is unreachable', { kind: 'network' });
    }
    if (!res.ok) {
        let payload = null;
        try { payload = await res.clone().json(); } catch { /* non-JSON error body */ }
        const detail = payload?.detail || payload?.error || '';
        throw new ApiError(detail || `HTTP ${res.status}`, {
            status: res.status,
            detail,
            payload,
            kind: res.status === 403 ? 'forbidden' : res.status >= 500 ? 'server' : 'http',
        });
    }
    if (res.status === 204) return null;
    return res.json();
}

/**
 * Extract the human-readable reason from an error Response without throwing the
 * Response away — for call sites that still branch on `res.ok`. Mirrors the
 * `detail` handling inside `apiRequest`/`apiJsonFetch`.
 */
export async function readErrorDetail(res, fallback = '') {
    let payload = null;
    try { payload = await res.clone().json(); } catch { /* non-JSON body */ }
    const detail = payload?.detail ?? payload?.error ?? '';
    const text = typeof detail === 'string' ? detail : JSON.stringify(detail);
    return text || fallback || `HTTP ${res.status}`;
}

/**
 * Preferred helper for new code: pass a plain object as `body`, get parsed JSON
 * back, and receive an `ApiError` with `detail` on failure.
 *
 *   const cfg = await apiRequest('/api/downloads/config');
 *   await apiRequest('/api/downloads/config', { method: 'POST', body: cfg });
 */
export async function apiRequest(path, { method = 'GET', body, headers = {}, timeoutMs = 0, ...rest } = {}) {
    const opts = { ...rest, method, headers: { ...headers } };
    if (body !== undefined) {
        if (typeof body === 'string' || body instanceof FormData || body instanceof Blob) {
            opts.body = body;
        } else {
            opts.body = JSON.stringify(body);
            if (!opts.headers['Content-Type']) opts.headers['Content-Type'] = 'application/json';
        }
    }

    let timer = null;
    if (timeoutMs > 0) {
        const controller = new AbortController();
        opts.signal = controller.signal;
        timer = setTimeout(() => controller.abort(), timeoutMs);
    }

    try {
        const res = await apiFetch(path, opts);
        if (!res.ok) {
            let payload = null;
            try { payload = await res.clone().json(); } catch { /* non-JSON */ }
            const detail = payload?.detail || payload?.error || '';
            throw new ApiError(detail || `HTTP ${res.status}`, {
                status: res.status,
                detail,
                payload,
                kind: res.status === 403 ? 'forbidden' : res.status >= 500 ? 'server' : 'http',
            });
        }
        if (res.status === 204) return null;
        return await res.json().catch(() => null);
    } catch (err) {
        if (err instanceof ApiError) throw err;
        if (err?.name === 'AbortError') throw new ApiError('Request timed out', { kind: 'timeout' });
        throw new ApiError('Network error — the NAS is unreachable', { kind: 'network' });
    } finally {
        if (timer) clearTimeout(timer);
    }
}

export const getJson = (path, options = {}) => apiRequest(path, { ...options, method: 'GET' });
export const postJson = (path, body, options = {}) => apiRequest(path, { ...options, method: 'POST', body });
export const putJson = (path, body, options = {}) => apiRequest(path, { ...options, method: 'PUT', body });
export const deleteJson = (path, options = {}) => apiRequest(path, { ...options, method: 'DELETE' });

/** Internal helper — identical to apiFetch, exported for direct use. */
export async function _fetchAuth(path, options = {}) {
    return apiFetch(path, options);
}
