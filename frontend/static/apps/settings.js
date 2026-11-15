// EthOS NAS — Settings Page (System, Users, Security tabs)
// Download settings live in the Download Manager package window (Packages).
import { escapeHtml, formatBytes, formatUptime, formatDate } from '../core/utils.js';
import { uiIcon } from '../core/icons.js';
import { apiFetch, apiRequest, readErrorDetail } from '../core/api.js';
import { addNotification } from '../core/notification-center.js';
import { showRestartOverlay } from '../core/window-manager.js';
import { skeletonRows } from '../core/states.js';
import {
    WALLPAPER_IDS, CUSTOM_ID, DEFAULT_WALLPAPER,
    saveWallpaper, uploadCustomWallpaper, resetWallpaper, customImageUrl,
} from '../core/wallpaper.js';

// Android APK downloads: the server resolves the file and exposes an
// app-scoped URL (`/api/app/download?app=…`), so no host path is baked into
// this bundle. The href is filled in by loadAppVersions().
const APK_APPS = [
    ['moments', 'appVerMoments', 'apkLinkMoments'],
    ['music', 'appVerMusic', 'apkLinkMusic'],
    ['video', 'appVerVideo', 'apkLinkVideo'],
];

function _withToken(url) {
    const token = encodeURIComponent(localStorage.getItem('token') || '');
    return `${url}${url.includes('?') ? '&' : '?'}token=${token}`;
}

// Exposed for the inline onclick handler in the System tab (module scope is not visible to HTML handlers)
window._ethosRestartApp = async function () {
    const ok = await window.showModalConfirm('Restart', 'Restart EthOS NAS?\n\nThe web server will restart and this page will refresh automatically.');
    if (!ok) return;
    try {
        const res = await fetch('/api/system/restart', { method: 'POST' });
        if (res.ok) showRestartOverlay();
        else window.showAlert('Restart failed');
    } catch (_) {
        window.showAlert('Could not reach the server');
    }
};

export async function renderSettingsPage(body) {
    body.innerHTML = `
        <div style="display:flex;flex-direction:column;height:100%;background:var(--surface-0);color: var(--text-1);font-family:system-ui,sans-serif;">
            <div style="display:flex;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--surface-0);">
                <button class="app-tab settings-tab active" data-tab="system">${uiIcon('settings', 15)}<span>System</span></button>
                <button class="app-tab settings-tab" data-tab="users">${uiIcon('users', 15)}<span>Users</span></button>
                <button class="app-tab settings-tab" data-tab="security">${uiIcon('lock', 15)}<span>Security</span></button>
                <button class="app-tab settings-tab" data-tab="appearance">${uiIcon('palette', 15)}<span>Appearance</span></button>
            </div>
            <div id="settingsContent" style="flex:1;overflow-y:auto;padding:16px 20px;">
                ${skeletonRows(6)}
            </div>
        </div>`;

    body.querySelectorAll('.settings-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            body.querySelectorAll('.settings-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            const tabName = tab.dataset.tab;
            if (tabName === 'system') loadSystemTab();
            else if (tabName === 'users') loadUsersTab();
            else if (tabName === 'security') loadSecurityTab();
            else if (tabName === 'appearance') loadAppearanceTab();
        });
    });

    await loadSystemTab();
}

// ════════════════════════════════════════════
// TAB 1: System Information
// ════════════════════════════════════════════

async function loadSystemTab() {
    const content = document.getElementById('settingsContent');
    if (!content) return;
    content.innerHTML = skeletonRows(6);

    try {
        const resp = await apiFetch('/api/system/status');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const sys = await resp.json();

        content.innerHTML = `
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;">
                <div class="glass-card" style="padding:20px;">
                    <div class="section-label">${uiIcon('settings', 12)} System</div>
                    <div style="display:flex;flex-direction:column;gap:10px;">
                        <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">Hostname</span><span style="color:white;font-weight:600;">${escapeHtml(sys.hostname || '—')}</span></div>
                        <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">Uptime</span><span style="color:#22c55e;font-family:monospace;">${formatUptime(sys.uptime_seconds)}</span></div>
                        <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">CPU Cores</span><span style="color:white;">${(sys.cpu_per_core || []).length} cores</span></div>
                        <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">CPU Load</span><span style="color:${sys.cpu_percent > 80 ? '#ef4444' : sys.cpu_percent > 50 ? '#eab308' : '#22c55e'};font-weight:700;">${Math.round(sys.cpu_percent)}%</span></div>
                        <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">Temperature</span><span style="color:white;">${sys.temperature != null ? sys.temperature + '°C' : '—'}</span></div>
                        <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">Platform</span><span style="color: var(--text-2);font-size:12px;">Linux</span></div>
                    </div>
                </div>

                <div class="glass-card" style="padding:20px;">
                    <div class="section-label">${uiIcon('memory', 12)} Memory</div>
                    <div class="progress-track" style="height:8px;margin-bottom:8px;"><div class="progress-fill" style="width:${sys.memory?.percent || 0}%;background:${sys.memory?.percent > 80 ? '#ef4444' : sys.memory?.percent > 50 ? '#eab308' : '#3b82f6'};"></div></div>
                    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px;"><span style="color: var(--text-2);">Used</span><span style="color:white;font-family:monospace;">${formatBytes(sys.memory?.used || 0)}</span></div>
                    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px;"><span style="color: var(--text-2);">Free</span><span style="color:white;font-family:monospace;">${formatBytes(sys.memory?.free || 0)}</span></div>
                    <div style="display:flex;justify-content:space-between;font-size:13px;"><span style="color: var(--text-2);">Total</span><span style="color:white;font-family:monospace;">${formatBytes(sys.memory?.total || 0)}</span></div>
                </div>

                <div class="glass-card" style="padding:20px;">
                    <div class="section-label">${uiIcon('disk', 12)} Disk (/)</div>
                    <div class="progress-track" style="height:8px;margin-bottom:8px;"><div class="progress-fill" style="width:${sys.disk?.percent || 0}%;background:${sys.disk?.percent > 80 ? '#ef4444' : sys.disk?.percent > 50 ? '#eab308' : '#3b82f6'};"></div></div>
                    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px;"><span style="color: var(--text-2);">Used</span><span style="color:white;font-family:monospace;">${formatBytes(sys.disk?.used || 0)}</span></div>
                    <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px;"><span style="color: var(--text-2);">Free</span><span style="color:white;font-family:monospace;">${formatBytes(sys.disk?.free || 0)}</span></div>
                    <div style="display:flex;justify-content:space-between;font-size:13px;"><span style="color: var(--text-2);">Total</span><span style="color:white;font-family:monospace;">${formatBytes(sys.disk?.total || 0)}</span></div>
                </div>

                <div class="glass-card" style="padding:20px;">
                    <div class="section-label">${uiIcon('network', 12)} Network</div>
                    <div style="display:flex;flex-direction:column;gap:10px;">
                        <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">Download</span><span style="color:white;font-family:monospace;">${formatBytes(sys.network?.bytes_recv || 0)}</span></div>
                        <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">Upload</span><span style="color:white;font-family:monospace;">${formatBytes(sys.network?.bytes_sent || 0)}</span></div>
                    </div>
                </div>
            </div>

            <div id="healthMonitorCard" class="glass-card" style="margin-top:16px;padding:16px 20px;">
                <div style="font-size:12px;color: var(--text-3);">Loading health monitor…</div>
            </div>

            <!-- Caches / retention (TODO.md P5-07): bytes per managed area + Clean now -->
            <div id="cachesCard" class="glass-card" style="margin-top:16px;padding:16px 20px;"
                 data-testid="settings-caches-card">
                <div style="font-size:12px;color: var(--text-3);">Loading caches…</div>
            </div>

            <div class="glass-card" style="margin-top:16px;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;">
                <div>
                    <div style="font-size:13px;color:white;font-weight:600;">${uiIcon('phone', 14)} EthOS Photos — Android app <span id="appVerMoments" style="font-size:11px;color: var(--text-3);font-weight:400;">(version…)</span></div>
                    <div style="font-size:11px;color: var(--text-3);">Download the APK and install it on your phone (allow “install unknown apps”)</div>
                </div>
                <a id="apkLinkMoments" href="#" aria-disabled="true" data-testid="apk-link-moments" class="btn-ghost btn-sm" style="text-decoration:none;">${uiIcon('download', 13)} Download APK</a>
            </div>

            <div class="glass-card" style="margin-top:12px;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;">
                <div>
                    <div style="font-size:13px;color:white;font-weight:600;">${uiIcon('music', 14)} EthOS Music — Android app <span id="appVerMusic" style="font-size:11px;color: var(--text-3);font-weight:400;">(version…)</span></div>
                    <div style="font-size:11px;color: var(--text-3);">Download the APK and install it on your phone (allow “install unknown apps”)</div>
                </div>
                <a id="apkLinkMusic" href="#" aria-disabled="true" data-testid="apk-link-music" class="btn-ghost btn-sm" style="text-decoration:none;">${uiIcon('download', 13)} Download APK</a>
            </div>

            <div class="glass-card" style="margin-top:12px;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;">
                <div>
                    <div style="font-size:13px;color:white;font-weight:600;">${uiIcon('video', 14)} EthOS Video — Android app <span id="appVerVideo" style="font-size:11px;color: var(--text-3);font-weight:400;">(version…)</span></div>
                    <div style="font-size:11px;color: var(--text-3);">Download the APK and install it on your phone (allow “install unknown apps”). Updates are checked in the app’s Settings.</div>
                </div>
                <a id="apkLinkVideo" href="#" aria-disabled="true" data-testid="apk-link-video" class="btn-ghost btn-sm" style="text-decoration:none;">${uiIcon('download', 13)} Download APK</a>
            </div>

            <div class="glass-card" id="coreUpdateCard" data-testid="core-update-card" style="margin-top:12px;padding:16px 20px;">
                <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
                    <div>
                        <div style="font-size:13px;color:white;font-weight:600;">${uiIcon('settings', 14)} EthOS NAS <span id="coreVersion" style="font-size:11px;color: var(--text-3);font-weight:400;">(version…)</span></div>
                        <div id="coreUpdateStatus" data-testid="core-update-status" style="font-size:11px;color: var(--text-3);">Loading update status…</div>
                    </div>
                    <button id="coreUpdateCheck" data-testid="core-update-check" class="btn-ghost btn-sm" hidden>${uiIcon('refresh', 13)} Check for updates</button>
                </div>
                <div id="coreUpdateNotes" style="display:none;margin-top:10px;font-size:11px;color:var(--text-2);white-space:pre-wrap;"></div>
            </div>

            <div class="glass-card" style="margin-top:12px;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;">
                <div>
                    <div style="font-size:13px;color:white;font-weight:600;">Restart Application</div>
                    <div style="font-size:11px;color: var(--text-3);">Gracefully restart the web server</div>
                </div>
                <button class="btn-ghost btn-sm" onclick="_ethosRestartApp()">${uiIcon('refresh', 13)} Restart</button>
            </div>
            <div class="glass-card" style="margin-top:12px;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;">
                <div>
                    <div style="font-size:13px;color:#ef4444;font-weight:600;">${uiIcon('alert', 14)} Reboot Server</div>
                    <div style="font-size:11px;color: var(--text-3);">Shutdown and restart the entire server</div>
                </div>
                <button class="btn-danger btn-sm" onclick="window.showModalConfirm('Reboot Server','Reboot the entire server? This will disconnect all users.').then(ok=>{if(ok)fetch('/api/system/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'reboot',confirm:true})}).then(()=>window.showAlert('Server rebooting...'))})">${uiIcon('refresh', 13)} Reboot</button>
            </div>`;

    loadHealthCard();
    loadAppVersions();
    loadCachesCard();
    loadUpdateCard();
    } catch (e) {
        content.innerHTML = `<div class="alert alert-error" style="justify-content:center;margin:20px 0;">${uiIcon('alert', 16)}<span>${escapeHtml(e.message)}</span></div>`;
    }
}

/** Role from the JWT payload (the API enforces it; this only hides buttons). */
function _jwtRole() {
    try {
        const parts = (localStorage.getItem('token') || '').split('.');
        if (parts.length < 2) return 'user';
        return JSON.parse(atob(parts[1])).role || 'user';
    } catch (_) {
        return 'user';
    }
}

/**
 * Core update card: version of the running checkout, the last cached check and
 * (admins only) a button that refreshes the signed release index.
 * Check-only — nothing is installed from the browser (docs/updates.md).
 */
async function loadUpdateCard() {
    const versionEl = document.getElementById('coreVersion');
    const statusEl = document.getElementById('coreUpdateStatus');
    const notesEl = document.getElementById('coreUpdateNotes');
    const btn = document.getElementById('coreUpdateCheck');
    if (!versionEl || !statusEl) return;
    const isAdmin = _jwtRole() === 'admin';
    if (btn) btn.hidden = !isAdmin;

    const render = (data) => {
        const cur = data.current || {};
        versionEl.textContent = `v${cur.version || '?'}${cur.git_sha ? ' · ' + cur.git_sha : ''}`;
        const latest = data.latest || null;
        const checked = data.checked_at_iso ? `last checked ${formatDate(data.checked_at_iso)}` : 'never checked';
        if (notesEl) {
            notesEl.textContent = (data.update_available === true && latest && latest.notes) ? latest.notes : '';
            notesEl.style.display = notesEl.textContent ? 'block' : 'none';
        }
        if (data.enabled === false) {
            statusEl.textContent = 'Update checks are disabled on this server';
            statusEl.style.color = 'var(--text-3)';
        } else if (data.error) {
            statusEl.textContent = `Check failed — ${data.error} (${checked})`;
            statusEl.style.color = '#eab308';
        } else if (data.update_available === true && latest) {
            statusEl.textContent = `Update available: v${latest.version || '?'}${latest.git_sha ? ' · ' + latest.git_sha : ''} — ${checked}`;
            statusEl.style.color = '#eab308';
        } else if (data.update_available === false) {
            statusEl.textContent = `Up to date — ${checked}`;
            statusEl.style.color = 'var(--text-2)';
        } else {
            statusEl.textContent = `Not checked yet — ${checked}`;
            statusEl.style.color = 'var(--text-3)';
        }
    };

    try {
        render(await apiRequest('/api/system/update'));
    } catch (err) {
        statusEl.textContent = `Could not read update status — ${err.detail || err.message}`;
        statusEl.style.color = '#eab308';
    }

    if (btn && isAdmin && !btn.dataset.wired) {
        btn.dataset.wired = '1';
        btn.addEventListener('click', async () => {
            const label = btn.innerHTML;
            btn.disabled = true;
            btn.textContent = 'Checking…';
            try {
                render(await apiRequest('/api/system/update/check', { method: 'POST' }));
            } catch (err) {
                statusEl.textContent = `Check failed — ${err.detail || err.message}`;
                statusEl.style.color = '#eab308';
            } finally {
                btn.disabled = false;
                btn.innerHTML = label;
            }
        });
    }
}

/** Live APK versions from the update endpoint (same data the apps poll for self-updates). */
async function loadAppVersions() {
    await Promise.all(APK_APPS.map(async ([app, elId, linkId]) => {
        const el = document.getElementById(elId);
        const link = document.getElementById(linkId);
        if (!el) return;
        try {
            const resp = await apiFetch(`/api/app/version?app=${app}`);
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            const v = await resp.json();
            el.textContent = `(v${v.version_name} · code ${v.version_code})`;
            el.style.color = 'var(--text-2)';
            if (link) {
                link.href = _withToken(v.url || `/api/app/download?app=${app}`);
                link.setAttribute('aria-disabled', 'false');
            }
        } catch (_) {
            el.textContent = '(no update info — rebuild the APK)';
            el.style.color = '#eab308';
            if (link) link.setAttribute('aria-disabled', 'true');
        }
    }));
}

// ── Caches / retention card (System tab) ────────────────────────────────────

// Friendly names for the areas `services/retention_service.py` manages.
const CACHE_LABELS = {
    temp_chunks: 'Abandoned uploads',
    transcodes: 'Playback transcodes',
    trash: 'Recycle bin',
    thumbnails: 'Image thumbnails',
    video_thumbs: 'Video thumbnails',
};

async function loadCachesCard() {
    const card = document.getElementById('cachesCard');
    if (!card) return;
    try {
        const resp = await apiFetch('/api/system/storage-usage');
        if (!resp.ok) throw new Error(await readErrorDetail(resp));
        const usage = await resp.json();
        const rows = Object.entries(usage.areas || {})
            .sort((a, b) => (b[1].bytes || 0) - (a[1].bytes || 0))
            .map(([name, area]) => `
                <div style="display:flex;justify-content:space-between;font-size:12px;padding:3px 0;">
                    <span style="color: var(--text-2);">${escapeHtml(CACHE_LABELS[name] || name)}</span>
                    <span style="color:white;font-family:monospace;">${formatBytes(area.bytes || 0)}
                        <span style="color: var(--text-3);">· ${area.files || 0} file(s)</span></span>
                </div>`).join('');

        card.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
                <div>
                    <div style="font-size:13px;color:white;font-weight:600;">${uiIcon('disk', 14)} Caches &amp; recycle bin</div>
                    <div style="font-size:11px;color: var(--text-3);">
                        ${formatBytes(usage.total_bytes || 0)} in ${usage.total_files || 0} file(s) — swept automatically
                    </div>
                </div>
                <button class="btn-ghost btn-sm" id="cachesCleanNow" style="white-space:nowrap;">
                    ${uiIcon('trash', 13)} Clean now
                </button>
            </div>
            ${rows || '<div style="font-size:12px;color: var(--text-3);">Nothing cached.</div>'}`;

        document.getElementById('cachesCleanNow')?.addEventListener('click', async (ev) => {
            const btn = ev.currentTarget;
            btn.disabled = true;
            try {
                const res = await apiFetch('/api/system/retention/sweep', { method: 'POST' });
                if (!res.ok) throw new Error(await readErrorDetail(res));
                const summary = await res.json();
                addNotification(`Freed ${formatBytes(summary.freed_bytes || 0)} ` +
                                `(${summary.removed_files || 0} item(s))`, 'success');
                loadCachesCard();
            } catch (err) {
                addNotification(err.message || 'Cleanup failed', 'error');
                btn.disabled = false;
            }
        });
    } catch (err) {
        card.innerHTML = `<div style="font-size:12px;color:#ef4444;">
            ${uiIcon('alert', 13)} Cannot read cache usage: ${escapeHtml(err.message || 'error')}</div>`;
    }
}

// ── Health Monitor card (System tab) ────────────────────────────────────────

function _relTime(iso) {
    if (!iso) return 'never';
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return iso;
    const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (s < 5) return 'just now';
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m} min ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h} h ${m % 60} min ago`;
    return `${Math.floor(h / 24)} d ago`;
}

async function loadHealthCard() {
    const card = document.getElementById('healthMonitorCard');
    if (!card) return;
    try {
        const resp = await apiFetch('/api/health/status');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const h = await resp.json();
        const mon = h.monitor || {};
        const failed = h.failed_units || [];
        const hotDisks = (h.disks || []).filter(d => d.percent >= (mon.disk_recover_percent ?? 85));

        let rows = '';
        for (const u of failed) {
            rows += `<div style="font-size:12px;color:#ef4444;font-family:monospace;display:flex;align-items:center;gap:5px;">${uiIcon('close', 12)} ${escapeHtml(u.name)} — ${escapeHtml(u.description || 'failed')}</div>`;
        }
        for (const d of hotDisks) {
            const color = d.percent >= (mon.disk_warn_percent ?? 90) ? '#ef4444' : '#eab308';
            rows += `<div style="font-size:12px;color:${color};font-family:monospace;display:flex;align-items:center;gap:5px;">${uiIcon('disk', 12)} ${escapeHtml(d.mount)} — ${Math.round(d.percent)}% used (${formatBytes(d.free)} free)</div>`;
        }
        if (!rows) {
            rows += `<div style="font-size:12px;color:#22c55e;display:flex;align-items:center;gap:5px;">${uiIcon('check', 12)} All disks below the warning threshold · no failed services</div>`;
        }
        const mem = h.memory;
        if (mem && mem.total > 0) {
            const mWarn = mon.memory_warn_percent ?? 90, mRec = mon.memory_recover_percent ?? 85;
            const color = mem.percent >= mWarn ? '#ef4444' : mem.percent >= mRec ? '#eab308' : 'var(--text-2)';
            rows += `<div style="font-size:12px;color:${color};font-family:monospace;display:flex;align-items:center;gap:5px;">${uiIcon('memory', 12)} Memory — ${Math.round(mem.percent)}% used (${formatBytes(mem.used)} of ${formatBytes(mem.total)})</div>`;
        }
        const temps = h.temps || [];
        if (temps.length) {
            const tWarn = mon.temp_warn_celsius ?? 85, tRec = tWarn - 5;
            const hottest = temps.reduce((a, b) => (b.current > a.current ? b : a));
            const color = hottest.current >= tWarn ? '#ef4444' : hottest.current >= tRec ? '#eab308' : 'var(--text-2)';
            rows += `<div style="font-size:12px;color:${color};font-family:monospace;display:flex;align-items:center;gap:5px;">${uiIcon('thermometer', 12)} ${escapeHtml(hottest.label)} — ${hottest.current}°C (hottest of ${temps.length} sensors)</div>`;
        }
        const smart = h.smart;
        if (smart && Array.isArray(smart.results) && smart.results.length) {
            for (const s of smart.results) {
                let color, text;
                if (!s.ok) {
                    color = 'var(--text-3)';
                    text = `${uiIcon('disk', 12)} ${escapeHtml(s.device)} — SMART unavailable (${escapeHtml(String(s.error || 'error').slice(0, 60))})`;
                } else if (s.passed) {
                    color = '#22c55e';
                    text = `${uiIcon('disk', 12)} ${escapeHtml(s.device)} — SMART OK${s.model ? ' (' + escapeHtml(String(s.model).slice(0, 40)) + ')' : ''}`;
                } else {
                    color = '#ef4444';
                    text = `${uiIcon('disk', 12)} ${escapeHtml(s.device)} — ${uiIcon('alert', 12)} SMART FAILING`;
                }
                rows += `<div style="font-size:12px;color:${color};font-family:monospace;">${text}</div>`;
            }
        }
        const trends = h.smart_trends;
        if (trends && Array.isArray(trends.devices)) {
            for (const t of trends.devices) {
                const attrs = t.attrs || {};
                const names = Object.keys(attrs);
                if (!names.length) continue;
                const shortName = (n) => n === 'Reallocated_Sector_Ct' ? 'Realloc'
                    : n === 'Current_Pending_Sector' ? 'Pending'
                    : n === 'Offline_Uncorrectable' ? 'Uncorr'
                    : n === 'Wear_Leveling_Count' ? 'WearLvl'
                    : n === 'Percentage_Used' ? '%Used' : n;
                const bits = names.map((n) => {
                    const grew = (t.changes || []).includes(n);
                    return grew
                        ? `<span style="color:#ef4444;">${shortName(n)} ${attrs[n]} ↑</span>`
                        : `${shortName(n)} ${attrs[n]}`;
                });
                rows += `<div style="font-size:12px;color:var(--text-3);font-family:monospace;display:flex;align-items:center;gap:5px;">${uiIcon('activity', 12)} ${escapeHtml(t.device)} — ${bits.join(' · ')}</div>`;
            }
        }

        const num = (v) => (v == null ? '' : String(v));
        card.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:${rows ? '10px' : '0'};">
                <div>
                    <div style="font-size:13px;color:white;font-weight:600;display:flex;align-items:center;gap:6px;">${uiIcon('activity', 14)} Health Monitor</div>
                    <div style="font-size:11px;color: var(--text-3);margin-top:2px;">Checks every ${mon.interval_seconds ?? '—'} s · disk warn ≥${mon.disk_warn_percent ?? '—'}% (recover &lt;${mon.disk_recover_percent ?? '—'}%) · last check ${_relTime(mon.last_check_at)} · ${mon.checks_run ?? 0} run(s)</div>
                </div>
                <button class="btn-ghost" onclick="window._ethosRunHealthCheck()" style="padding:6px 16px;font-size:12px;">▶️ Run check now</button>
            </div>
            <div style="display:flex;flex-direction:column;gap:4px;margin-bottom:10px;">${rows}</div>
            <div data-testid="health-config-row" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;border-top:1px solid var(--border);padding-top:10px;font-size:12px;">
                <span style="color:var(--text-3);">Settings:</span>
                <label style="color:var(--text-2);display:flex;align-items:center;gap:4px;">Warn %
                    <input id="healthCfgWarn" type="number" min="1" max="100" value="${num(mon.disk_warn_percent)}" style="width:64px;padding:3px 6px;font-size:12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text-1);"></label>
                <label style="color:var(--text-2);display:flex;align-items:center;gap:4px;">Recover %
                    <input id="healthCfgRecover" type="number" min="0" max="100" value="${num(mon.disk_recover_percent)}" style="width:64px;padding:3px 6px;font-size:12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text-1);"></label>
                <label style="color:var(--text-2);display:flex;align-items:center;gap:4px;">Mem warn %
                    <input id="healthCfgMemWarn" type="number" min="1" max="100" value="${num(mon.memory_warn_percent)}" style="width:64px;padding:3px 6px;font-size:12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text-1);"></label>
                <label style="color:var(--text-2);display:flex;align-items:center;gap:4px;">Mem recover %
                    <input id="healthCfgMemRecover" type="number" min="0" max="100" value="${num(mon.memory_recover_percent)}" style="width:64px;padding:3px 6px;font-size:12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text-1);"></label>
                <label style="color:var(--text-2);display:flex;align-items:center;gap:4px;">Temp warn °C
                    <input id="healthCfgTempWarn" type="number" min="40" max="125" value="${num(mon.temp_warn_celsius)}" style="width:64px;padding:3px 6px;font-size:12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text-1);"></label>
                <label style="color:var(--text-2);display:flex;align-items:center;gap:4px;">Cooldown (s)
                    <input id="healthCfgCooldown" type="number" min="60" max="604800" step="60" value="${num(mon.alert_cooldown_seconds)}" style="width:80px;padding:3px 6px;font-size:12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text-1);"></label>
                <label style="color:var(--text-2);display:flex;align-items:center;gap:4px;">Interval (s)
                    <input id="healthCfgInterval" type="number" min="30" max="86400" step="30" value="${num(mon.interval_seconds)}" style="width:80px;padding:3px 6px;font-size:12px;background:var(--surface-1);border:1px solid var(--border);border-radius:4px;color:var(--text-1);"></label>
                <button class="btn-primary btn-sm" data-testid="health-cfg-save" onclick="window._ethosSaveHealthConfig()">${uiIcon('check', 13)} Save</button>
                <button class="btn-ghost btn-sm" data-testid="health-cfg-reset" onclick="window._ethosResetHealthConfig()">${uiIcon('refresh', 13)} Reset to defaults</button>
            </div>`;
    } catch (e) {
        card.innerHTML = `<div style="font-size:12px;color:#ef4444;">Health monitor unavailable — ${escapeHtml(e.message)}</div>`;
    }
}

window._ethosRunHealthCheck = async function () {
    try {
        const resp = await apiFetch('/api/health/check-now', { method: 'POST' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const r = await resp.json();
        const n = (r.emitted || []).length;
        if (window.showToast) window.showToast(n ? `Health check: ${n} new alert(s)` : 'Health check: all clear', n ? 'warning' : 'success');
    } catch (_) {
        if (window.showToast) window.showToast('Health check failed', 'error');
    }
    loadHealthCard(); // refresh last-check / checks-run
};

function _healthCfgBody() {
    const read = (id, field) => {
        const el = document.getElementById(id);
        if (!el || el.value === '') return null;
        const v = Number(el.value);
        return Number.isFinite(v) ? { [field]: v } : null;
    };
    const body = {};
    for (const part of [
        read('healthCfgWarn', 'disk_warn_percent'),
        read('healthCfgRecover', 'disk_recover_percent'),
        read('healthCfgMemWarn', 'memory_warn_percent'),
        read('healthCfgMemRecover', 'memory_recover_percent'),
        read('healthCfgTempWarn', 'temp_warn_celsius'),
        read('healthCfgCooldown', 'alert_cooldown_seconds'),
        read('healthCfgInterval', 'check_interval_seconds'),
    ]) {
        if (part) Object.assign(body, part);
    }
    return body;
}

window._ethosSaveHealthConfig = async function () {
    const body = _healthCfgBody();
    if (!Object.keys(body).length) {
        if (window.showToast) window.showToast('Nothing to save', 'info');
        return;
    }
    try {
        const resp = await apiFetch('/api/health/config', { method: 'PUT', body: JSON.stringify(body) });
        const data = resp.ok ? null : await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error((data && data.detail) || ('HTTP ' + resp.status));
        if (window.showToast) window.showToast('Health monitor settings saved', 'success');
    } catch (e) {
        if (window.showToast) window.showToast('Save failed: ' + e.message, 'error');
    }
    loadHealthCard(); // refresh effective values
};

window._ethosResetHealthConfig = async function () {
    try {
        const resp = await apiFetch('/api/health/config', { method: 'DELETE' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        if (window.showToast) window.showToast('Health monitor reset to defaults', 'success');
    } catch (_) {
        if (window.showToast) window.showToast('Reset failed', 'error');
    }
    loadHealthCard();
};

// ════════════════════════════════════════════
// TAB 2: Users
// ════════════════════════════════════════════

async function loadUsersTab() {
    const content = document.getElementById('settingsContent');
    if (!content) return;
    content.innerHTML = skeletonRows(5);

    try {
        const resp = await apiFetch('/api/users');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        const users = data.users || [];

        let html = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
                <h3 class="section-title" style="font-size:16px;">${uiIcon('users', 16)} User Accounts</h3>
                <span style="font-size:12px;color: var(--text-3);">${users.length} user${users.length !== 1 ? 's' : ''}</span>
            </div>`;

        if (users.length === 0) {
            html += '<div class="glass-card" style="text-align:center;padding:24px;color: var(--text-3);">No users configured.</div>';
        } else {
            html += `<table class="app-table" style="width:100%;">
                <thead><tr>
                    <th style="padding:8px 12px;">Username</th>
                    <th style="padding:8px 12px;">Role</th>
                    <th style="padding:8px 12px;">Created</th>
                    <th style="padding:8px 12px;">Last Login</th>
                </tr></thead><tbody>`;
            for (const u of users) {
                const roleClass = u.role === 'admin' ? 'badge-yellow' : 'badge-blue';
                html += `<tr>
                    <td style="font-weight:600;color:white;padding:8px 12px;">
                        <span style="display:flex;align-items:center;gap:8px;"><span style="display:flex;color:var(--text-3);">${uiIcon('users', 14)}</span> ${escapeHtml(u.username)}</span>
                    </td>
                    <td style="padding:8px 12px;"><span class="${roleClass}" style="padding:2px 8px;border-radius:4px;font-size:11px;">${escapeHtml(u.role)}</span></td>
                    <td style="color: var(--text-2);padding:8px 12px;font-size:12px;">${u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}</td>
                    <td style="color: var(--text-2);padding:8px 12px;font-size:12px;">${u.last_login ? new Date(u.last_login).toLocaleString() : '—'}</td>
                </tr>`;
            }
            html += '</tbody></table>';
        }

        html += `
            <div class="glass-card" style="margin-top:16px;padding:16px 20px;">
                <div style="font-size:13px;color: var(--text-2);margin-bottom:4px;display:flex;align-items:center;gap:6px;">${uiIcon('info', 14)} Users are automatically imported from Linux system users (PAM).</div>
                <div style="font-size:12px;color: var(--text-3);">New users logging in via PAM are registered automatically with "user" role.</div>
            </div>`;

        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = `<div class="alert alert-error" style="justify-content:center;margin:20px 0;">${uiIcon('alert', 16)}<span>${escapeHtml(e.message)}</span></div>`;
    }
}

// ════════════════════════════════════════════
// TAB 3: Security
// ════════════════════════════════════════════

async function loadSecurityTab() {
    const content = document.getElementById('settingsContent');
    if (!content) return;
    content.innerHTML = skeletonRows(5);

    const token = localStorage.getItem('token') || '';
    let tokenInfo = { valid: false, username: '—', role: '—', expires: '—' };
    if (token) {
        try {
            const parts = token.split('.');
            const payload = JSON.parse(atob(parts[1]));
            tokenInfo = {
                valid: true,
                username: payload.username || '—',
                role: payload.role || '—',
                expires: new Date(payload.exp * 1000).toLocaleString(),
            };
        } catch (e) {}
    }

    content.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;">
            <div class="glass-card" style="padding:20px;">
                <div class="section-label">${uiIcon('lock', 12)} Session</div>
                <div style="display:flex;flex-direction:column;gap:10px;">
                    <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">Authenticated</span>
                        <span class="${tokenInfo.valid ? 'badge-green' : 'badge-red'}" style="padding:2px 8px;border-radius:4px;font-size:11px;">${tokenInfo.valid ? 'Yes' : 'No'}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">User</span><span style="color:white;font-weight:600;">${escapeHtml(tokenInfo.username)}</span></div>
                    <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">Role</span><span style="color:white;">${escapeHtml(tokenInfo.role)}</span></div>
                    <div style="display:flex;justify-content:space-between;"><span style="color: var(--text-2);">Token Expires</span><span style="color: var(--text-2);font-size:12px;">${escapeHtml(tokenInfo.expires)}</span></div>
                </div>
            </div>

            <div class="glass-card" style="padding:20px;">
                <div class="section-label">${uiIcon('phone', 12)} API Key (EthOS Photos app)</div>
                <div id="apiKeyCard" style="color: var(--text-3);font-size:12px;">Loading…</div>
            </div>
        </div>

        <div class="glass-card" style="margin-top:16px;padding:16px 20px;">
            <div style="font-size:13px;color:#eab308;font-weight:600;margin-bottom:8px;display:flex;align-items:center;gap:6px;">${uiIcon('alert', 14)} Security Notes</div>
            <ul style="margin:0;padding-left:20px;color: var(--text-2);font-size:12px;line-height:1.8;">
                <li>Set <code style="background:var(--surface-2);padding:1px 6px;border-radius:3px;">ETHOS_JWT_SECRET</code> to a strong random value in production</li>
                <li>Restrict CORS origins via <code style="background:var(--surface-2);padding:1px 6px;border-radius:3px;">ETHOS_CORS_ORIGINS</code> environment variable</li>
                <li>This app should never be exposed to the public internet without TLS</li>
                <li>All file operations are validated through the <code style="background:var(--surface-2);padding:1px 6px;border-radius:3px;">sanitize_path()</code> function</li>
            </ul>
        </div>`;

    loadApiKeyCard();
}

// ── API key card (per-user key for the EthOS Photos Android app) ────────────

async function loadApiKeyCard() {
    const el = document.getElementById('apiKeyCard');
    if (!el) return;
    try {
        const resp = await apiFetch('/api/auth/api-key');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        el.innerHTML = `
            <div style="font-size:12px;color: var(--text-2);margin-bottom:8px;">Use this key to log in from the EthOS Photos or EthOS Music Android app (Settings → Server → API key). It is generated automatically for your account.</div>
            <div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;">
                <code id="apiKeyValue" style="flex:1;background:var(--surface-2);padding:8px 10px;border-radius:6px;font-size:12px;color:var(--text-1);word-break:break-all;">${escapeHtml(data.api_key || '')}</code>
            </div>
            <div style="display:flex;gap:8px;">
                <button class="btn-ghost btn-sm" onclick="_ethosCopyApiKey()">${uiIcon('edit', 13)} Copy</button>
                <button class="btn-ghost btn-sm" onclick="_ethosRegenApiKey()" style="color:#eab308;">${uiIcon('refresh', 13)} Regenerate</button>
            </div>`;
    } catch (e) {
        el.innerHTML = `<span class="alert alert-error" style="display:inline-flex;">${uiIcon('alert', 14)}<span>${escapeHtml(e.message)}</span></span>`;
    }
}

window._ethosCopyApiKey = async function () {
    const el = document.getElementById('apiKeyValue');
    if (!el) return;
    try {
        await navigator.clipboard.writeText(el.textContent);
        window.showAlert && window.showAlert('API key copied to clipboard');
    } catch (_) {
        window.showAlert && window.showAlert('Could not copy — select the text manually');
    }
};

window._ethosRegenApiKey = async function () {
    const ok = await window.showModalConfirm('Regenerate API key', 'The current key will stop working immediately.\n\nContinue?');
    if (!ok) return;
    try {
        const resp = await apiFetch('/api/auth/api-key/regenerate', { method: 'POST' });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        await loadApiKeyCard();
        window.showAlert && window.showAlert('New API key generated — update it in the app');
    } catch (e) {
        window.showAlert && window.showAlert('Regenerate failed: ' + e.message);
    }
};

// ── Appearance tab (per-user desktop wallpaper) ─────────────────────────────

async function loadAppearanceTab() {
    const content = document.getElementById('settingsContent');
    if (!content) return;
    content.innerHTML = skeletonRows(4);

    let current = DEFAULT_WALLPAPER, hasCustom = false;
    try {
        const res = await apiFetch('/api/appearance/wallpaper');
        if (res.ok) {
            const data = await res.json();
            current = data.wallpaper || DEFAULT_WALLPAPER;
            hasCustom = !!data.has_custom;
        } else if (res.status !== 404) throw new Error('HTTP ' + res.status);
    } catch (_) { /* keep local defaults below */ }
    _renderAppearanceTab(current, hasCustom);
}

function _renderAppearanceTab(current, hasCustom) {
    const content = document.getElementById('settingsContent');
    if (!content) return;

    const tiles = WALLPAPER_IDS.map(id => `
        <div class="wp-tile" data-wp="${id}" role="button" tabindex="0"
             style="position:relative;height:84px;border-radius:10px;overflow:hidden;cursor:pointer;border:2px solid ${current === id ? 'var(--accent)' : 'transparent'};transition:border-color .15s;">
            <div class="wallpaper-${id}" style="position:absolute;inset:0;"></div>
            <div style="position:absolute;left:8px;bottom:6px;font-size:11px;font-weight:600;color:white;text-shadow:0 1px 3px rgba(0,0,0,.55);">${id[0].toUpperCase() + id.slice(1)}${current === id ? ' ✓' : ''}</div>
        </div>`).join('');

    const customPreviewStyle = hasCustom
        ? `background-image:url('${customImageUrl()}');background-size:cover;background-position:center;`
        : 'background:var(--surface-2);';

    content.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
            <h3 class="section-title" style="font-size:16px;">${uiIcon('palette', 16)} Desktop Wallpaper</h3>
            <span style="font-size:12px;color: var(--text-3);">Saved per user — each account keeps its own background</span>
        </div>

        <div class="glass-card" style="padding:16px 20px;">
            <div style="font-size:11px;color: var(--text-3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px;">Built-in wallpapers</div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;">${tiles}</div>
        </div>

        <div class="glass-card" style="margin-top:16px;padding:16px 20px;">
            <div style="font-size:11px;color: var(--text-3);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px;">Custom image</div>
            <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;">
                <div data-testid="wp-custom-preview" style="position:relative;width:180px;height:96px;border-radius:10px;overflow:hidden;border:2px solid ${current === CUSTOM_ID ? 'var(--accent)' : 'transparent'};${customPreviewStyle}">
                    ${!hasCustom ? '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:11px;color: var(--text-3);">No image uploaded</div>' : ''}
                </div>
                <div style="display:flex;flex-direction:column;gap:8px;">
                    <input type="file" id="wpUploadInput" accept="image/jpeg,image/png,image/webp" style="display:none;">
                    <button class="btn-ghost btn-sm" onclick="document.getElementById('wpUploadInput').click()">${uiIcon('plus', 13)} Upload image…</button>
                    ${hasCustom ? '<button class="btn-danger btn-sm" data-testid="wp-remove-custom">' + uiIcon('trash', 13) + ' Remove custom image</button>' : ''}
                    <div style="font-size:11px;color: var(--text-3);">JPEG, PNG or WebP · up to 8 MB</div>
                </div>
            </div>
        </div>`;

    content.querySelectorAll('.wp-tile').forEach(tile => {
        const pick = () => {
            saveWallpaper(tile.dataset.wp);
            _renderAppearanceTab(tile.dataset.wp, hasCustom);
            if (window.showToast) window.showToast('Wallpaper updated', 'success');
        };
        tile.addEventListener('click', pick);
        tile.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
    });

    const fileInput = document.getElementById('wpUploadInput');
    if (fileInput) {
        fileInput.addEventListener('change', async () => {
            const file = fileInput.files && fileInput.files[0];
            if (!file) return;
            try {
                await uploadCustomWallpaper(file);
                _renderAppearanceTab(CUSTOM_ID, true);
                if (window.showToast) window.showToast('Custom wallpaper uploaded', 'success');
            } catch (e) {
                if (window.showToast) window.showToast('Upload failed: ' + e.message, 'error');
            }
        });
    }

    const removeBtn = document.querySelector('[data-testid="wp-remove-custom"]');
    if (removeBtn) {
        removeBtn.addEventListener('click', async () => {
            const ok = await window.showModalConfirm('Remove custom image', 'Your uploaded wallpaper will be deleted and the default background restored.\n\nContinue?');
            if (!ok) return;
            try {
                await resetWallpaper();
                _renderAppearanceTab(DEFAULT_WALLPAPER, false);
                if (window.showToast) window.showToast('Custom image removed', 'success');
            } catch (e) {
                if (window.showToast) window.showToast('Remove failed: ' + e.message, 'error');
            }
        });
    }
}

