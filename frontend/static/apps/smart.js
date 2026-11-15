// EthOS NAS — SMART Disk Health Dashboard App
// Shows physical disk health, temperature, and SMART attributes

import { escapeHtml, formatBytes } from '../core/utils.js';
import { apiFetch } from '../core/api.js';
import { skeletonRows, emptyState, errorState } from '../core/states.js';

export async function renderSmartPage(body) {
    if (!body) return;

    body.innerHTML = `
        <div style="padding:16px;height:100%;display:flex;flex-direction:column;background:var(--bg, #0f172a);font-family:system-ui,sans-serif;color: var(--text-1);">
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
                <h2 style="margin:0;font-size:18px;font-weight:700;color: var(--text-1);display:flex;align-items:center;gap:8px;">
                    <span>💿</span> SMART Disk Health
                </h2>
                <button class="btn-ghost" data-testid="smart-refresh-btn" onclick="window._smartLoad()" style="padding:6px 14px;font-size:12px;border-radius:8px;border:1px solid var(--border);background:var(--surface-2);color: var(--text-2);cursor:pointer;display:flex;align-items:center;gap:6px;">
                    🔄 Refresh
                </button>
            </div>
            <div id="smartContent" data-testid="smart-content" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:8px;">
                ${skeletonRows(4)}
            </div>
        </div>`;

    // Expose load function globally and call it
    window._smartLoad = async function () {
        const content = document.getElementById('smartContent');
        if (!content) return;
        content.innerHTML = skeletonRows(4);

        try {
            const resp = await apiFetch('/api/smart');
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();

            if (!data.disks || data.disks.length === 0) {
                content.innerHTML = '<div style="text-align:center;padding:40px;color: var(--text-3);font-size:14px;">No disks detected. SMART data may require root privileges.</div>';
                return;
            }

            let html = '';
            for (const disk of data.disks) {
                const isHealthy = disk.healthy === true;
                const isUnhealthy = disk.healthy === false;
                const healthyClass = isHealthy ? 'badge-green' : isUnhealthy ? 'badge-red' : 'badge-yellow';
                const statusText = isHealthy ? 'Healthy' : isUnhealthy ? 'FAILING' : 'Unknown';
                const statusColor = isHealthy ? '#22c55e' : isUnhealthy ? '#ef4444' : '#eab308';

                html += `
                <div class="smart-disk-card" data-testid="smart-disk-${disk.name}" style="background:rgba(30,41,59,0.8);border:1px solid var(--border);border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:10px;">
                    <div style="display:flex;align-items:center;justify-content:space-between;">
                        <div style="display:flex;align-items:center;gap:10px;">
                            <span style="font-size:20px;">${isHealthy ? '✅' : isUnhealthy ? '⚠️' : '❓'}</span>
                            <div>
                                <div style="font-weight:600;font-size:14px;color: var(--text-1);">${escapeHtml(disk.name)}</div>
                                ${disk.model ? `<div style="font-size:11px;color: var(--text-3);">${escapeHtml(disk.model)}</div>` : ''}
                            </div>
                        </div>
                        <div style="display:flex;align-items:center;gap:12px;">
                            ${disk.temperature != null ? `
                                <span style="font-size:12px;color: var(--text-2);" title="Temperature">
                                    🌡️ ${disk.temperature}°C
                                </span>
                            ` : ''}
                            <span class="${healthyClass}" data-testid="smart-status-${disk.name}" style="padding:3px 10px;border-radius:6px;font-size:11px;font-weight:700;color:${statusColor};background:rgba(${isHealthy ? '34,197,94' : isUnhealthy ? '239,68,68' : '234,179,8'},0.15);">
                                ${statusText}
                            </span>
                        </div>
                    </div>
                    <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color: var(--text-2);">
                        <span>💾 ${formatBytes(disk.size_bytes || 0)}</span>
                        ${disk.power_on_hours != null ? `<span>⏱ ${disk.power_on_hours}h power-on</span>` : ''}
                        ${disk.serial ? `<span>🔖 ${escapeHtml(disk.serial)}</span>` : ''}
                    </div>
                    ${disk.note ? `<div style="font-size:11px;color:#eab308;font-style:italic;">ℹ️ ${escapeHtml(disk.note)}</div>` : ''}
                </div>`;
            }

            content.innerHTML = html || '<div style="text-align:center;padding:40px;color: var(--text-3);">No disk data available.</div>';

            // Update total count
            const header = document.querySelector('#smartContent');
            if (header) {
                const countEl = document.createElement('div');
                countEl.style.cssText = 'font-size:11px;color: var(--text-3);margin-bottom:4px;';
                countEl.textContent = `${data.total} disk(s) detected`;
                header.parentElement?.insertBefore(countEl, header);
            }
        } catch (e) {
            content.innerHTML = `<div style="text-align:center;padding:40px;color:#ef4444;">❌ ${escapeHtml(e.message)}</div>`;
        }
    };

    await window._smartLoad();
}
