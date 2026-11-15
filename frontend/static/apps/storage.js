// EthOS NAS — Storage Page (tabbed: Disks, Partitions, USB)
import { escapeHtml, escapeAttr, formatBytes } from '../core/utils.js';
import { uiIcon } from '../core/icons.js';
import { apiFetch } from '../core/api.js';
import { showConfirm as showModalConfirm, showFormModal } from '../core/modal.js';
import { skeletonRows } from '../core/states.js';

// ════════════════════════════════════════════
// APP: Storage Page (tabbed: Disks, Partitions, USB)
// ════════════════════════════════════════════

let _currentStorageTab = 'disks';
let _usbFilesystems = [];  // [{name, value}] — filesystems this host can create

export async function renderStoragePage(body) {
    body.innerHTML = `
        <div style="display:flex;flex-direction:column;height:100%;background:var(--surface-0);color: var(--text-1);font-family:system-ui,sans-serif;">
            <div style="display:flex;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--surface-0);">
                <button class="app-tab active" data-tab="disks" data-testid="storage-tab-disks">${uiIcon('disk', 16)}<span>Disks</span></button>
                <button class="app-tab" data-tab="partitions" data-testid="storage-tab-partitions">${uiIcon('partition', 16)}<span>Partitions</span></button>
                <button class="app-tab" data-tab="usb" data-testid="storage-tab-usb">${uiIcon('usb', 16)}<span>USB</span></button>
            </div>
            <div id="storageContent" style="flex:1;overflow-y:auto;padding:16px 20px;" tabindex="0" role="region" aria-label="Storage details">
                ${skeletonRows(5)}
            </div>
        </div>`;

    // Tab switching
    body.querySelectorAll('.app-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            body.querySelectorAll('.app-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            _currentStorageTab = tab.dataset.tab;
            switch (_currentStorageTab) {
                case 'disks': loadDisksTab(); break;
                case 'partitions': loadPartitionsTab(); break;
                case 'usb': loadUsbTab(); break;
            }
        });
    });

    await loadDisksTab();
}

// ════════════════════════════════════════════
// TAB 1: Disks
// ════════════════════════════════════════════

async function loadDisksTab() {
    const content = document.getElementById('storageContent');
    if (!content) return;
    content.innerHTML = skeletonRows(5);

    try {
        const resp = await apiFetch('/api/storage/disks');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        const disks = data.disks || [];

        let html = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
                <h3 class="section-title">${uiIcon('disk', 16)} Physical Disks</h3>
                <span style="font-size:12px;color: var(--text-3);">${disks.length} disk${disks.length !== 1 ? 's' : ''}</span>
            </div>`;

        if (disks.length === 0) {
            html += '<div class="glass-card" style="text-align:center;padding:24px;color: var(--text-3);">No disks detected.</div>';
        } else {
            html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px;">';
            for (const d of disks) {
                const smart = (d.smart && typeof d.smart.healthy === 'boolean') ? d.smart.healthy : null;
                // rotational: true = spinning, false = flash, null = unknown
                // (USB-SATA bridges lie about it, so the backend sends null).
                const rot = typeof d.rotational === 'boolean' ? d.rotational : null;
                const isHDD = rot === true;
                const kind = rot === null ? 'USB drive' : (isHDD ? 'HDD' : 'SSD');
                const kindIcon = rot === null ? 'usb' : (isHDD ? 'disk' : 'ssd');
                const sizeGb = d.size_bytes ? (d.size_bytes / 1073741824).toFixed(1) : '?';
                html += `<div class="glass-card" style="padding:16px;display:flex;flex-direction:column;gap:8px;">
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <div style="display:flex;align-items:center;gap:8px;">
                            <span style="display:flex;color:var(--text-3);" title="${kind}">${uiIcon(kindIcon, 22)}</span>
                            <div>
                                <div style="font-weight:700;color:white;font-size:14px;">${escapeHtml(d.label || d.model || d.name)}</div>
                                <div style="color: var(--text-3);font-size:11px;font-family:monospace;">${escapeHtml(d.name)}</div>
                            </div>
                        </div>
                        ${smart === null
                            ? `<span style="padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;color:var(--text-3);border:1px solid var(--border);" title="Health is measured by the SMART Health app (smartctl)">SMART —</span>`
                            : `<span class="${smart ? 'badge-green' : 'badge-red'}" style="padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase;">${smart ? 'Healthy' : 'Warning'}</span>`}
                    </div>
                    <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:12px;">
                        <div><span style="color: var(--text-3);">Size</span><br><span style="color:white;font-weight:600;">${sizeGb} GB</span></div>
                        <div><span style="color: var(--text-3);">Type</span><br><span style="color: var(--text-2);">${kind}</span></div>
                        <div><span style="color: var(--text-3);">Interface</span><br><span style="color: var(--text-2);">${escapeHtml(d.transport || '—')}</span></div>
                        <div><span style="color: var(--text-3);">Partitions</span><br><span style="color: var(--text-2);">${Number(d.partitions ?? 0)}</span></div>
                        <div style="grid-column:1/-1;"><span style="color: var(--text-3);">Model</span><br><span style="color: var(--text-2);font-size:11px;">${escapeHtml(d.model || '—')}</span></div>
                    </div>
                    ${d.filesystem ? `<div style="font-size:12px;color: var(--text-2);border-top:1px solid var(--border);padding-top:8px;">
                        <span>Filesystem: <strong style="color:white;">${escapeHtml(d.filesystem)}</strong></span>
                        ${d.mountpoint ? `<span style="margin-left:12px;">Mount: <strong style="color:white;font-family:monospace;">${escapeHtml(d.mountpoint)}</strong></span>` : ''}
                    </div>` : ''}
                </div>`;
            }
            html += '</div>';
        }

        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = `<div class="alert alert-error" style="justify-content:center;margin:20px 0;">${uiIcon('alert', 16)}<span>${escapeHtml(e.message)}</span></div>`;
    }
}

// ════════════════════════════════════════════
// TAB 2: Partitions
// ════════════════════════════════════════════

async function loadPartitionsTab() {
    const content = document.getElementById('storageContent');
    if (!content) return;
    content.innerHTML = skeletonRows(5);

    try {
        const resp = await apiFetch('/api/storage/partitions');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        const parts = data.partitions || [];

        let html = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
                <h3 class="section-title">${uiIcon('partition', 16)} Mounted Partitions</h3>
                <span style="font-size:12px;color: var(--text-3);">${parts.length} partition${parts.length !== 1 ? 's' : ''}</span>
            </div>`;

        if (parts.length === 0) {
            html += '<div class="glass-card" style="text-align:center;padding:24px;color: var(--text-3);">No partitions detected.</div>';
        } else {
            html += '<table class="app-table" style="width:100%;">';
            html += `<thead><tr>
                <th style="padding:8px 10px;text-align:left;">Device</th>
                <th style="padding:8px 10px;text-align:left;">Mountpoint</th>
                <th style="padding:8px 10px;text-align:left;">Filesystem</th>
                <th style="padding:8px 10px;text-align:right;">Size</th>
                <th style="padding:8px 10px;text-align:right;">Used</th>
                <th style="padding:8px 10px;text-align:right;">Free</th>
                <th style="padding:8px 10px;text-align:right;">Use%</th>
            </tr></thead><tbody>`;
            for (const p of parts) {
                const usePct = p.percent || 0;
                const barColor = usePct > 85 ? '#ef4444' : usePct > 60 ? '#eab308' : '#22c55e';
                html += `<tr>
                    <td style="padding:8px 10px;font-family:monospace;color:white;font-weight:600;">${escapeHtml(p.device)}</td>
                    <td style="padding:8px 10px;font-family:monospace;color: var(--text-2);">${escapeHtml(p.mountpoint)}</td>
                    <td style="padding:8px 10px;"><span class="badge-blue" style="padding:2px 8px;border-radius:4px;font-size:11px;">${escapeHtml(p.fstype)}</span></td>
                    <td style="padding:8px 10px;text-align:right;color:white;font-family:monospace;font-size:12px;">${p.total_human || formatBytes(p.total || 0)}</td>
                    <td style="padding:8px 10px;text-align:right;color:white;font-family:monospace;font-size:12px;">${p.used_human || formatBytes(p.used || 0)}</td>
                    <td style="padding:8px 10px;text-align:right;color:white;font-family:monospace;font-size:12px;">${p.free_human || formatBytes(p.free || 0)}</td>
                    <td style="padding:8px 10px;text-align:right;">
                        <div style="display:flex;align-items:center;justify-content:flex-end;gap:6px;">
                            <div class="progress-track" style="width:50px;height:6px;border-radius:3px;">
                                <div class="progress-fill" style="width:${usePct}%;height:100%;border-radius:3px;background:${barColor};"></div>
                            </div>
                            <span style="font-size:12px;font-family:monospace;color:${barColor};">${usePct}%</span>
                        </div>
                    </td>
                </tr>`;
            }
            html += '</tbody></table>';
        }

        content.innerHTML = html;
    } catch (e) {
        content.innerHTML = `<div class="alert alert-error" style="justify-content:center;margin:20px 0;">${uiIcon('alert', 16)}<span>${escapeHtml(e.message)}</span></div>`;
    }
}

// ════════════════════════════════════════════
// TAB 3: USB
// ════════════════════════════════════════════

async function loadUsbTab() {
    const content = document.getElementById('storageContent');
    if (!content) return;
    content.innerHTML = skeletonRows(4);

    try {
        const resp = await apiFetch('/api/storage/usb');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const data = await resp.json();
        const devices = data.usb_devices || [];
        _usbFilesystems = data.supported_filesystems || [];

        let html = `
            <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
                <h3 class="section-title">${uiIcon('usb', 16)} USB Storage Devices</h3>
                <span style="font-size:12px;color: var(--text-3);">${devices.length} device${devices.length !== 1 ? 's' : ''}</span>
            </div>`;

        if (devices.length === 0) {
            html += '<div class="glass-card" style="text-align:center;padding:24px;color: var(--text-3);">No USB storage devices connected.</div>';
        } else {
            html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:12px;">';
            for (const u of devices) {
                const sizeGb = u.size_bytes ? (u.size_bytes / 1073741824).toFixed(1) : '?';
                html += `<div class="glass-card" style="padding:14px 16px;display:flex;flex-direction:column;gap:10px;">
                    <div style="display:flex;align-items:center;gap:12px;">
                        <span style="display:flex;color:var(--text-3);">${uiIcon('usb', 26)}</span>
                        <div style="flex:1;min-width:0;">
                            <div style="font-weight:700;color:white;font-size:14px;">${escapeHtml(u.label || u.model || u.name)}</div>
                            <div style="display:flex;gap:16px;margin-top:2px;font-size:12px;color: var(--text-2);">
                                <span>${escapeHtml(u.device)}</span>
                                <span>${sizeGb} GB</span>
                            </div>
                        </div>
                        <div style="display:flex;gap:6px;flex-shrink:0;">
                            <button class="btn-ghost btn-sm usb-addpart-btn" data-device="${escapeAttr(u.device)}" data-name="${escapeAttr(u.name)}" data-testid="usb-addpart-${escapeAttr(u.name)}" title="Create a new partition in the free space of this disk">${uiIcon('plus', 13)} Partition</button>
                            <button class="btn-danger btn-sm usb-init-btn" data-device="${escapeAttr(u.device)}" data-name="${escapeAttr(u.name)}" data-testid="usb-init-${escapeAttr(u.name)}" title="Wipe all partitions and recreate one full-size volume">${uiIcon('refresh', 13)} Initialize</button>
                        </div>
                    </div>`;

                const partitions = u.partitions || [];
                if (partitions.length === 0) {
                    html += '<div style="font-size:12px;color:#eab308;">No partition table — use Partition or Initialize to set this drive up.</div>';
                } else {
                    for (const p of partitions) {
                        const sizeGbP = p.size_bytes ? (p.size_bytes / 1073741824).toFixed(1) : '?';
                        let status, mountBtn;
                        if (!p.mountable) {
                            status = `<span style="color:#eab308;">${escapeHtml(p.fstype || 'unformatted')}</span>`;
                            mountBtn = '';
                        } else if (p.mounted) {
                            status = `<span style="font-family:monospace;color:#22c55e;">${escapeHtml(p.mountpoint)}</span>`;
                            mountBtn = `<button class="btn-warning btn-sm usb-unmount-btn" data-device="${escapeAttr(p.device)}" data-testid="usb-unmount-${escapeAttr(p.name)}">Unmount</button>`;
                        } else {
                            status = p.last_mountpoint
                                ? `<span style="color:#eab308;">Not mounted <span style="font-family:monospace;color:var(--text-3);">(was ${escapeHtml(p.last_mountpoint)})</span></span>`
                                : '<span style="color:#eab308;">Not mounted</span>';
                            mountBtn = `<button class="btn-success btn-sm usb-mount-btn" data-device="${escapeAttr(p.device)}" data-folder="${escapeAttr(p.last_mountpoint || p.label || ('usb_' + p.name))}" data-testid="usb-mount-${escapeAttr(p.name)}">Mount</button>`;
                        }
                        html += `<div style="display:flex;align-items:center;gap:8px;padding:8px 10px;border-top:1px solid var(--border);font-size:12px;">
                            <span style="font-family:monospace;color:white;font-weight:600;">${escapeHtml(p.name)}</span>
                            ${p.label ? `<span style="color:white;">${escapeHtml(p.label)}</span>` : ''}
                            ${p.mountable ? `<span class="badge-blue" style="padding:2px 8px;border-radius:4px;font-size:11px;">${escapeHtml(p.fstype)}</span>` : ''}
                            <span style="color: var(--text-3);">${sizeGbP} GB</span>
                            ${p.managed ? '<span class="badge-green" style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;text-transform:uppercase;">Persistent</span>' : ''}
                            <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${status}</span>
                            ${mountBtn}
                            ${p.mountable ? `<button class="btn-ghost btn-sm usb-relabel-btn" data-device="${escapeAttr(p.device)}" data-label="${escapeAttr(p.label || '')}" data-fstype="${escapeAttr(p.fstype || '')}" data-testid="usb-relabel-${escapeAttr(p.name)}" title="Change the volume label (no reformat, no data loss)">${uiIcon('edit', 13)} Rename</button>` : ''}
                            <button class="btn-ghost btn-sm usb-format-btn" data-device="${escapeAttr(p.device)}" data-name="${escapeAttr(p.name)}" data-fstype="${escapeAttr(p.fstype || '')}" data-testid="usb-format-${escapeAttr(p.name)}" title="Format this partition (erases all data)">${uiIcon('settings', 13)} Format</button>
                            <button class="btn-danger btn-sm usb-delete-btn" data-device="${escapeAttr(p.device)}" data-testid="usb-delete-${escapeAttr(p.name)}" title="Delete this partition (erases all data)">${uiIcon('trash', 13)} Delete</button>
                        </div>`;
                    }
                }

                html += '</div>';
            }
            html += '</div>';
        }

        content.innerHTML = html;

        // Wire up mount/unmount buttons.
        content.querySelectorAll('.usb-mount-btn').forEach(btn => {
            btn.addEventListener('click', () => usbMountAction(btn));
        });
        content.querySelectorAll('.usb-unmount-btn').forEach(btn => {
            btn.addEventListener('click', () => usbUnmountAction(btn));
        });
        // Wire up partition management buttons.
        content.querySelectorAll('.usb-relabel-btn').forEach(btn => {
            btn.addEventListener('click', () => openRelabelDialog(btn.dataset.device, btn.dataset.label || '', btn.dataset.fstype || ''));
        });
        content.querySelectorAll('.usb-format-btn').forEach(btn => {
            btn.addEventListener('click', () => openFormatDialog(btn.dataset.device, btn.dataset.name || btn.dataset.device, btn.dataset.fstype || ''));
        });
        content.querySelectorAll('.usb-delete-btn').forEach(btn => {
            btn.addEventListener('click', () => usbDeletePartitionAction(btn));
        });
        content.querySelectorAll('.usb-addpart-btn').forEach(btn => {
            btn.addEventListener('click', () => openPartitionDialog(btn.dataset.device, btn.dataset.name));
        });
        content.querySelectorAll('.usb-init-btn').forEach(btn => {
            btn.addEventListener('click', () => openInitDialog(btn.dataset.device, btn.dataset.name));
        });
    } catch (e) {
        content.innerHTML = `<div class="alert alert-error" style="justify-content:center;margin:20px 0;">${uiIcon('alert', 16)}<span>${escapeHtml(e.message)}</span></div>`;
    }
}

function fsSelectHtml(name, current) {
    const opts = _usbFilesystems.length ? _usbFilesystems : [{ name: 'ext4', value: 'ext4' }];
    return `<select class="ethos-modal-input" name="${name}" aria-label="${name}" style="margin-top:4px;">` +
        opts.map(f => `<option value="${escapeAttr(f.value)}"${f.value === current ? ' selected' : ''}>${escapeHtml(f.name)}</option>`).join('') +
        `</select>`;
}

function labelInputHtml(name) {
    return `<input class="ethos-modal-input" name="${name}" maxlength="16" placeholder="Volume label (optional)" style="margin-top:4px;">`;
}

const FIELD_LABEL = 'font-size:12px;color:var(--modal-text,#cbd5e1);opacity:.75;margin-bottom:0;';

async function usbPostAction(url, payload, successMsg) {
    try {
        const resp = await apiFetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
        window.addNotification(successMsg, 'success');
    } catch (e) {
        window.addNotification(e.message, 'error');
        return false;
    }
    await loadUsbTab();
    return true;
}

async function openFormatDialog(device, devName, currentFstype = '') {
    const values = await showFormModal({
        title: `Format ${devName}`,
        danger: true,
        confirmLabel: 'Format',
        bodyHtml: `
            <p style="margin:0 0 14px;font-size:13px;color:#ef4444;font-weight:600;display:flex;align-items:center;gap:6px;">${uiIcon('alert', 14)} All data on ${escapeHtml(device)} will be permanently erased.</p>
            <div style="margin-bottom:0;${FIELD_LABEL}">Filesystem</div>
            ${fsSelectHtml('fstype', currentFstype)}
            <div style="margin-top:12px;${FIELD_LABEL}">Label</div>
            ${labelInputHtml('label')}
            <p style="margin:14px 0 0;font-size:11px;color:var(--text-3);line-height:1.5;">If the volume is mounted it will be unmounted, reformatted and remounted automatically.</p>`,
    });
    if (!values) return;
    await usbPostAction('/api/storage/usb/format', {
        device, fstype: values.fstype, label: (values.label || '').trim(),
    }, `Formatted ${device} as ${values.fstype}`);
}

async function openRelabelDialog(device, currentLabel, fstype) {
    const fs = _usbFilesystems.find(f => f.value === fstype);
    const maxLen = fs ? fs.max_label : 16;
    const values = await showFormModal({
        title: `Rename ${device}`,
        confirmLabel: 'Rename',
        bodyHtml: `
            <div style="${FIELD_LABEL}">New volume label</div>
            <input class="ethos-modal-input" name="label" maxlength="${maxLen}" value="${escapeAttr(currentLabel)}" placeholder="Volume label" style="margin-top:4px;">
            <p style="margin:12px 0 0;font-size:11px;color:var(--text-3);line-height:1.5;">Letters, digits, dot and dash only (max ${maxLen} chars). No data is touched — if the volume is mounted it is briefly unmounted and remounted at the same path.</p>`,
    });
    if (!values) return;
    const label = (values.label || '').trim();
    if (!label) {
        window.addNotification('Label must not be empty', 'error');
        return;
    }
    await usbPostAction('/api/storage/usb/relabel', { device, label }, `Renamed ${device} to “${label}”`);
}

async function openPartitionDialog(diskDevice, diskName) {
    const values = await showFormModal({
        title: `New partition — ${diskName}`,
        confirmLabel: 'Create',
        bodyHtml: `
            <p style="margin:0 0 14px;font-size:13px;color:var(--modal-text,#cbd5e1);line-height:1.6;">A new partition will be created in the free space of <strong>${escapeHtml(diskDevice)}</strong>, formatted and mounted persistently (auto-mount on boot).</p>
            <div style="${FIELD_LABEL}">Size (GB) — leave empty to use all remaining free space</div>
            <input class="ethos-modal-input" name="size_gb" type="number" min="1" step="0.5" placeholder="all remaining free space" style="margin-top:4px;">
            <div style="margin-top:12px;${FIELD_LABEL}">Filesystem</div>
            ${fsSelectHtml('fstype', 'ext4')}
            <div style="margin-top:12px;${FIELD_LABEL}">Label</div>
            ${labelInputHtml('label')}`,
    });
    if (!values) return;
    const sizeGb = values.size_gb ? parseFloat(values.size_gb) : null;
    await usbPostAction('/api/storage/usb/partition', {
        device: diskDevice, size_gb: sizeGb, fstype: values.fstype, label: (values.label || '').trim(), auto_mount: true,
    }, `Created ${values.fstype} partition on ${diskDevice}`);
}

async function openInitDialog(diskDevice, diskName) {
    const ok = await showModalConfirm(
        'Initialize USB disk',
        `This wipes EVERYTHING on ${diskDevice}: all partitions and all data are permanently destroyed, then a fresh GPT table with one full-size volume is created. Continue?`
    );
    if (!ok) return;

    const values = await showFormModal({
        title: `Initialize ${diskName}`,
        danger: true,
        confirmLabel: 'Wipe & Initialize',
        bodyHtml: `
            <p style="margin:0 0 14px;font-size:13px;color:#ef4444;font-weight:600;display:flex;align-items:center;gap:6px;">${uiIcon('alert', 14)} All partitions and data on ${escapeHtml(diskDevice)} will be destroyed.</p>
            <div style="${FIELD_LABEL}">Filesystem for the new volume</div>
            ${fsSelectHtml('fstype', 'ext4')}
            <div style="margin-top:12px;${FIELD_LABEL}">Label</div>
            ${labelInputHtml('label')}`,
    });
    if (!values) return;
    await usbPostAction('/api/storage/usb/init', {
        device: diskDevice, fstype: values.fstype, label: (values.label || '').trim(), auto_mount: true,
    }, `Initialized ${diskDevice} as ${values.fstype}`);
}

async function usbDeletePartitionAction(btn) {
    const device = btn.dataset.device;
    const ok = await showModalConfirm(
        'Delete partition',
        `Delete ${device}? The partition is removed from the disk and ALL data on it is permanently erased.`
    );
    if (!ok) return;

    const prev = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Deleting...';
    const done = await usbPostAction('/api/storage/usb/delete-partition', { device }, `Deleted partition ${device}`);
    if (!done) {
        btn.disabled = false;
        btn.innerHTML = prev;   // keep the trash icon instead of an emoji
    }
}

async function usbMountAction(btn) {
    const device = btn.dataset.device;
    const values = await showFormModal({
        title: 'Mount USB drive',
        confirmLabel: 'Mount',
        bodyHtml: `
            <p style="margin:0 0 14px;font-size:13px;color:var(--text-2);line-height:1.6;">Mount ${escapeHtml(device)} permanently? An entry with the nofail option is added to /etc/fstab: the drive mounts automatically on every boot, and if it is disconnected at boot time the system still starts normally.</p>
            <div style="${FIELD_LABEL}">Folder name under /mnt</div>
            <input class="ethos-modal-input" name="folder" maxlength="40" value="${escapeAttr(btn.dataset.folder || '')}" placeholder="e.g. Backups — stable across re-mounts" style="margin-top:4px;">
            <p style="margin:12px 0 0;font-size:11px;color:var(--text-3);line-height:1.5;">The same folder is reused on every mount of this drive, so backup jobs can target a fixed path.</p>`,
    });
    if (!values) return;

    const prev = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Mounting...';
    try {
        const resp = await apiFetch('/api/storage/usb/mount', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device, folder: (values.folder || '').trim() || null }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
        window.addNotification(`Mounted ${device} at ${data.mountpoint}`, 'success');
    } catch (e) {
        window.addNotification(e.message, 'error');
        btn.disabled = false;      // a failed mount must not leave a dead button
        btn.innerHTML = prev;
        return;
    }
    await loadUsbTab();
}

async function usbUnmountAction(btn) {
    const device = btn.dataset.device;
    const ok = await showModalConfirm(
        'Unmount USB drive',
        `Unmount ${device} permanently? The drive is unmounted now and its /etc/fstab entries are removed, so it will not be mounted automatically on the next boot.`
    );
    if (!ok) return;

    const prev = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Unmounting...';
    try {
        const resp = await apiFetch('/api/storage/usb/unmount', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
        window.addNotification(`Unmounted ${device}`, 'success');
    } catch (e) {
        window.addNotification(e.message, 'error');
        btn.disabled = false;
        btn.innerHTML = prev;
        return;
    }
    await loadUsbTab();
}

window.renderStoragePage = renderStoragePage;
