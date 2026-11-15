// EthOS NAS — Storage (rail: Disks · Partitions · USB)
import { escapeAttr, escapeHtml, formatBytes } from '../core/utils.js';
import { uiIcon } from '../core/icons.js';
import { apiRequest } from '../core/api.js';
import { showConfirm, showFormModal } from '../core/modal.js';
import { skeletonRows, emptyState, errorState } from '../core/states.js';
import { logError } from '../core/logger.js';
import { notify } from '../core/notification-center.js';
import { mountShell, applyBarWidths } from '../core/ui-shell.js';

const SECTIONS = [
    { id: 'disks', label: 'Disks', title: 'Physical Disks', subtitle: 'Drives attached to this host.', testId: 'storage-tab-disks' },
    { id: 'partitions', label: 'Partitions', title: 'Mounted Partitions', subtitle: 'Mounted filesystems and their usage.', testId: 'storage-tab-partitions' },
    { id: 'usb', label: 'USB', title: 'USB Storage', subtitle: 'Removable drives, partitions and mount state.', testId: 'storage-tab-usb' },
];

let shell = null;
let navToken = 0;
let _usbFilesystems = []; // [{name, value}] — filesystems this host can create

export async function renderStoragePage(body) {
    shell?.destroy();
    shell = mountShell(body, {
        idPrefix: 'storage',
        title: 'Storage',
        subtitle: 'Disks, partitions and removable media.',
        icon: '💾',
        sections: SECTIONS,
        note: 'Formatting and partition changes are destructive.',
    });
    shell.onSection(_runSection);
    shell.setActive('disks');
}

function _runSection(id) {
    const token = ++navToken;
    const isCurrent = () => token === navToken && shell && shell.root.isConnected;
    const loader = LOADERS[id];
    if (loader) loader(isCurrent);
}

// ════════════════════════════════════════════════════════════════════════════
// Disks
// ════════════════════════════════════════════════════════════════════════════

async function loadDisksTab(isCurrent) {
    const content = shell.view;
    content.innerHTML = skeletonRows(5);
    shell.setStatus('Loading disks…');

    try {
        const data = await apiRequest('/api/storage/disks');
        if (!isCurrent()) return;
        const disks = data.disks || [];

        const cards = disks.map((disk) => {
            const smart = (disk.smart && typeof disk.smart.healthy === 'boolean') ? disk.smart.healthy : null;
            const rotational = typeof disk.rotational === 'boolean' ? disk.rotational : null;
            const kind = rotational === null ? 'USB drive' : (rotational ? 'HDD' : 'SSD');
            const kindIcon = rotational === null ? 'usb' : (rotational ? 'disk' : 'ssd');
            const sizeGb = disk.size_bytes ? (disk.size_bytes / 1073741824).toFixed(1) : '?';
            const smartTag = smart === null
                ? '<span class="ui-tag" title="Health is measured by the SMART Health app (smartctl)">SMART —</span>'
                : `<span class="ui-tag ${smart ? 'is-ok' : 'is-bad'}">${smart ? 'Healthy' : 'Warning'}</span>`;
            return `<section class="ui-panel">
                <div class="ui-toolbar">
                    <div class="ui-row">
                        <span class="ui-muted">${uiIcon(kindIcon, 22)}</span>
                        <div>
                            <h2>${escapeHtml(disk.label || disk.model || disk.name)}</h2>
                            <p class="ui-muted path">${escapeHtml(disk.name)}</p>
                        </div>
                    </div>
                    ${smartTag}
                </div>
                <div class="ui-panel-body">
                    <dl class="ui-kv">
                        <div><dt class="ui-kv-key">Size</dt><dd class="ui-kv-value mono">${sizeGb} GB</dd></div>
                        <div><dt class="ui-kv-key">Type</dt><dd class="ui-kv-value">${kind}</dd></div>
                        <div><dt class="ui-kv-key">Interface</dt><dd class="ui-kv-value">${escapeHtml(disk.transport || '—')}</dd></div>
                        <div><dt class="ui-kv-key">Partitions</dt><dd class="ui-kv-value">${Number(disk.partitions ?? 0)}</dd></div>
                        <div><dt class="ui-kv-key">Model</dt><dd class="ui-kv-value">${escapeHtml(disk.model || '—')}</dd></div>
                    </dl>
                    ${disk.filesystem ? `<p class="ui-muted ui-panel-body">Filesystem <code class="ui-code">${escapeHtml(disk.filesystem)}</code>${disk.mountpoint ? ` · mounted at <code class="ui-code">${escapeHtml(disk.mountpoint)}</code>` : ''}</p>` : ''}
                </div>
            </section>`;
        }).join('');

        content.innerHTML = `
            <div class="ui-toolbar">
                <h2>${uiIcon('disk', 13)} Physical Disks</h2>
                <span class="ui-tag">${disks.length} disk${disks.length === 1 ? '' : 's'}</span>
            </div>
            ${disks.length
                ? `<div class="ui-grid">${cards}</div>`
                : emptyState({ icon: 'disk', title: 'No disks detected', hint: 'Attached drives appear here.' })}`;

        shell.setStatus(`${disks.length} disk(s)`, 'ok');
    } catch (err) {
        if (!isCurrent()) return;
        content.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read disks', 'error');
        logError('Storage: disks unavailable', err?.detail || err?.message);
    }
}

// ════════════════════════════════════════════════════════════════════════════
// Partitions
// ════════════════════════════════════════════════════════════════════════════

async function loadPartitionsTab(isCurrent) {
    const content = shell.view;
    content.innerHTML = skeletonRows(5);
    shell.setStatus('Loading partitions…');

    try {
        const data = await apiRequest('/api/storage/partitions');
        if (!isCurrent()) return;
        const parts = data.partitions || [];

        const rows = parts.map((part) => {
            const percent = Math.max(0, Math.min(100, Number(part.percent) || 0));
            const tone = percent > 85 ? 'bad' : percent > 60 ? 'warn' : 'ok';
            return `<tr>
                <td class="path">${escapeHtml(part.device)}</td>
                <td class="path">${escapeHtml(part.mountpoint)}</td>
                <td><span class="ui-tag is-info">${escapeHtml(part.fstype || '—')}</span></td>
                <td class="num">${escapeHtml(part.total_human || formatBytes(part.total || 0))}</td>
                <td class="num">${escapeHtml(part.used_human || formatBytes(part.used || 0))}</td>
                <td class="num">${escapeHtml(part.free_human || formatBytes(part.free || 0))}</td>
                <td class="num">
                    <div class="ui-row">
                        <div class="ui-bar" title="${escapeAttr(part.mountpoint)} usage">
                            <div class="ui-bar-fill ${tone}" data-width="${percent}"></div>
                        </div>
                        <span class="ui-tag is-${tone}">${percent}%</span>
                    </div>
                </td>
            </tr>`;
        }).join('');

        content.innerHTML = `
            <div class="ui-toolbar">
                <h2>${uiIcon('partition', 13)} Mounted Partitions</h2>
                <span class="ui-tag">${parts.length} partition${parts.length === 1 ? '' : 's'}</span>
            </div>
            ${parts.length ? `<div class="ui-panel"><div class="ui-table-wrap"><table class="ui-table">
                    <thead><tr><th>Device</th><th>Mountpoint</th><th>Filesystem</th>
                        <th class="num">Size</th><th class="num">Used</th><th class="num">Free</th><th class="num">Use%</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div></div>`
                : emptyState({ icon: 'partition', title: 'No partitions detected', hint: 'Attached disks have no mounted filesystems.' })}`;

        applyBarWidths(content);
        shell.setStatus(`${parts.length} mounted partition(s)`, 'ok');
    } catch (err) {
        if (!isCurrent()) return;
        content.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read partitions', 'error');
        logError('Storage: partitions unavailable', err?.detail || err?.message);
    }
}

// ════════════════════════════════════════════════════════════════════════════
// USB
// ════════════════════════════════════════════════════════════════════════════

async function loadUsbTab(isCurrent) {
    const content = shell.view;
    content.innerHTML = skeletonRows(4);
    shell.setStatus('Loading USB devices…');

    try {
        const data = await apiRequest('/api/storage/usb');
        if (!isCurrent()) return;
        const devices = data.usb_devices || [];
        _usbFilesystems = data.supported_filesystems || [];

        const cards = devices.map((device) => {
            const sizeGb = device.size_bytes ? (device.size_bytes / 1073741824).toFixed(1) : '?';
            const partitions = device.partitions || [];
            const partitionRows = partitions.length
                ? partitions.map((part) => {
                    const size = part.size_bytes ? (part.size_bytes / 1073741824).toFixed(1) : '?';
                    let status;
                    let mountButton = '';
                    if (!part.mountable) {
                        status = `<span class="ui-tag is-warn">${escapeHtml(part.fstype || 'unformatted')}</span>`;
                    } else if (part.mounted) {
                        status = `<span class="ui-tag is-ok">${escapeHtml(part.mountpoint)}</span>`;
                        mountButton = `<button class="ui-btn small" data-class="usb-unmount" data-device="${escapeAttr(part.device)}" data-testid="usb-unmount-${escapeAttr(part.name)}">Unmount</button>`;
                    } else {
                        status = `<span class="ui-tag is-warn">Not mounted${part.last_mountpoint ? ` (was ${escapeHtml(part.last_mountpoint)})` : ''}</span>`;
                        mountButton = `<button class="ui-btn small primary" data-class="usb-mount" data-device="${escapeAttr(part.device)}" data-folder="${escapeAttr(part.last_mountpoint || part.label || (`usb_${part.name}`))}" data-testid="usb-mount-${escapeAttr(part.name)}">Mount</button>`;
                    }
                    return `<div class="ui-list-row">
                        <span class="ui-tag">${escapeHtml(part.name)}</span>
                        ${part.label ? `<span>${escapeHtml(part.label)}</span>` : ''}
                        ${part.mountable ? `<span class="ui-tag is-info">${escapeHtml(part.fstype || '—')}</span>` : ''}
                        <span class="ui-tag">${size} GB</span>
                        ${part.managed ? '<span class="ui-tag is-ok">Persistent</span>' : ''}
                        <span class="ui-list-row-main">${status}</span>
                        ${mountButton}
                        ${part.mountable ? `<button class="ui-btn small" data-class="usb-relabel" data-device="${escapeAttr(part.device)}" data-label="${escapeAttr(part.label || '')}" data-fstype="${escapeAttr(part.fstype || '')}" data-testid="usb-relabel-${escapeAttr(part.name)}">Rename</button>` : ''}
                        <button class="ui-btn small" data-class="usb-format" data-device="${escapeAttr(part.device)}" data-name="${escapeAttr(part.name)}" data-fstype="${escapeAttr(part.fstype || '')}" data-testid="usb-format-${escapeAttr(part.name)}">Format</button>
                        <button class="ui-btn small danger" data-class="usb-delete" data-device="${escapeAttr(part.device)}" data-testid="usb-delete-${escapeAttr(part.name)}">Delete</button>
                    </div>`;
                }).join('')
                : '<p class="ui-callout">No partition table — use Partition or Initialize to set this drive up.</p>';

            return `<section class="ui-panel">
                <div class="ui-toolbar">
                    <div class="ui-row">
                        <span class="ui-muted">${uiIcon('usb', 24)}</span>
                        <div>
                            <h2>${escapeHtml(device.label || device.model || device.name)}</h2>
                            <p class="ui-muted path">${escapeHtml(device.device)} · ${sizeGb} GB</p>
                        </div>
                    </div>
                    <div class="ui-row">
                        <button class="ui-btn small" data-class="usb-addpart" data-device="${escapeAttr(device.device)}" data-name="${escapeAttr(device.name)}" data-testid="usb-addpart-${escapeAttr(device.name)}">${uiIcon('plus', 12)} Partition</button>
                        <button class="ui-btn small danger" data-class="usb-init" data-device="${escapeAttr(device.device)}" data-name="${escapeAttr(device.name)}" data-testid="usb-init-${escapeAttr(device.name)}">${uiIcon('refresh', 12)} Initialize</button>
                    </div>
                </div>
                <div class="ui-panel-body ui-list">${partitionRows}</div>
            </section>`;
        }).join('');

        content.innerHTML = `
            <div class="ui-toolbar">
                <h2>${uiIcon('usb', 13)} USB Storage Devices</h2>
                <span class="ui-tag">${devices.length} device${devices.length === 1 ? '' : 's'}</span>
            </div>
            ${devices.length
                ? `<div class="ui-grid">${cards}</div>`
                : emptyState({ icon: 'usb', title: 'No USB storage connected', hint: 'Plug a drive in and refresh this view.' })}`;

        _wireUsbActions(content);
        shell.setStatus(`${devices.length} USB device(s)`, 'ok');
    } catch (err) {
        if (!isCurrent()) return;
        content.innerHTML = errorState({ detail: err?.detail || err?.message || 'Unknown error' });
        shell.setStatus('Could not read USB devices', 'error');
        logError('Storage: USB devices unavailable', err?.detail || err?.message);
    }
}

function _wireUsbActions(content) {
    content.querySelectorAll('[data-class="usb-mount"]').forEach((btn) => {
        btn.addEventListener('click', () => usbMountAction(btn));
    });
    content.querySelectorAll('[data-class="usb-unmount"]').forEach((btn) => {
        btn.addEventListener('click', () => usbUnmountAction(btn));
    });
    content.querySelectorAll('[data-class="usb-relabel"]').forEach((btn) => {
        btn.addEventListener('click', () => openRelabelDialog(btn.dataset.device, btn.dataset.label || '', btn.dataset.fstype || ''));
    });
    content.querySelectorAll('[data-class="usb-format"]').forEach((btn) => {
        btn.addEventListener('click', () => openFormatDialog(btn.dataset.device, btn.dataset.name || btn.dataset.device, btn.dataset.fstype || ''));
    });
    content.querySelectorAll('[data-class="usb-delete"]').forEach((btn) => {
        btn.addEventListener('click', () => usbDeletePartitionAction(btn));
    });
    content.querySelectorAll('[data-class="usb-addpart"]').forEach((btn) => {
        btn.addEventListener('click', () => openPartitionDialog(btn.dataset.device, btn.dataset.name));
    });
    content.querySelectorAll('[data-class="usb-init"]').forEach((btn) => {
        btn.addEventListener('click', () => openInitDialog(btn.dataset.device, btn.dataset.name));
    });
}

const LOADERS = {
    disks: loadDisksTab,
    partitions: loadPartitionsTab,
    usb: loadUsbTab,
};

// ════════════════════════════════════════════════════════════════════════════
// USB dialogs
// ════════════════════════════════════════════════════════════════════════════

function fsSelectHtml(name, current) {
    const options = _usbFilesystems.length ? _usbFilesystems : [{ name: 'ext4', value: 'ext4' }];
    return `<select class="ethos-modal-input" name="${name}" aria-label="${name}" style="margin-top:4px;">`
        + options.map((fs) => `<option value="${escapeAttr(fs.value)}"${fs.value === current ? ' selected' : ''}>${escapeHtml(fs.name)}</option>`).join('')
        + '</select>';
}

function labelInputHtml(name) {
    return `<input class="ethos-modal-input" name="${name}" maxlength="16" placeholder="Volume label (optional)" style="margin-top:4px;">`;
}

const FIELD_LABEL = 'font-size:12px;color:var(--text-2);opacity:.75;margin-bottom:0;';
const FIELD_DANGER = 'margin:0 0 14px;font-size:13px;color:var(--ui-bad);font-weight:600;';
const FIELD_NOTE = 'margin:12px 0 0;font-size:11px;color:var(--text-3);line-height:1.5;';

async function usbPostAction(url, payload, successMessage) {
    try {
        await apiRequest(url, { method: 'POST', body: payload });
        notify({ kind: 'success', message: successMessage });
    } catch (err) {
        logError(`Storage: ${url} failed`, err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Unknown error' });
        return false;
    }
    _runSection('usb');
    return true;
}

async function openFormatDialog(device, devName, currentFstype = '') {
    const values = await showFormModal({
        title: `Format ${devName}`,
        danger: true,
        confirmLabel: 'Format',
        bodyHtml: `
            <p style="${FIELD_DANGER}">${uiIcon('alert', 14)} All data on ${escapeHtml(device)} will be permanently erased.</p>
            <div style="${FIELD_LABEL}">Filesystem</div>
            ${fsSelectHtml('fstype', currentFstype)}
            <div style="margin-top:12px;${FIELD_LABEL}">Label</div>
            ${labelInputHtml('label')}
            <p style="${FIELD_NOTE}">If the volume is mounted it will be unmounted, reformatted and remounted automatically.</p>`,
    });
    if (!values) return;
    await usbPostAction('/api/storage/usb/format', {
        device, fstype: values.fstype, label: (values.label || '').trim(),
    }, `Formatted ${device} as ${values.fstype}`);
}

async function openRelabelDialog(device, currentLabel, fstype) {
    const fs = _usbFilesystems.find((item) => item.value === fstype);
    const maxLength = fs ? fs.max_label : 16;
    const values = await showFormModal({
        title: `Rename ${device}`,
        confirmLabel: 'Rename',
        bodyHtml: `
            <div style="${FIELD_LABEL}">New volume label</div>
            <input class="ethos-modal-input" name="label" maxlength="${maxLength}" value="${escapeAttr(currentLabel)}" placeholder="Volume label" style="margin-top:4px;">
            <p style="${FIELD_NOTE}">Letters, digits, dot and dash only (max ${maxLength} chars). No data is touched — if the volume is mounted it is briefly unmounted and remounted at the same path.</p>`,
    });
    if (!values) return;
    const label = (values.label || '').trim();
    if (!label) {
        notify({ kind: 'error', message: 'Label must not be empty' });
        return;
    }
    await usbPostAction('/api/storage/usb/relabel', { device, label }, `Renamed ${device} to “${label}”`);
}

async function openPartitionDialog(diskDevice, diskName) {
    const values = await showFormModal({
        title: `New partition — ${diskName}`,
        confirmLabel: 'Create',
        bodyHtml: `
            <p style="margin:0 0 14px;font-size:13px;color:var(--text-2);line-height:1.6;">A new partition will be created in the free space of <strong>${escapeHtml(diskDevice)}</strong>, formatted and mounted persistently (auto-mount on boot).</p>
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
    const ok = await showConfirm(
        'Initialize USB disk',
        `This wipes EVERYTHING on ${diskDevice}: all partitions and all data are permanently destroyed, then a fresh GPT table with one full-size volume is created. Continue?`,
    );
    if (!ok) return;

    const values = await showFormModal({
        title: `Initialize ${diskName}`,
        danger: true,
        confirmLabel: 'Wipe & Initialize',
        bodyHtml: `
            <p style="${FIELD_DANGER}">${uiIcon('alert', 14)} All partitions and data on ${escapeHtml(diskDevice)} will be destroyed.</p>
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
    const ok = await showConfirm(
        'Delete partition',
        `Delete ${device}? The partition is removed from the disk and ALL data on it is permanently erased.`,
    );
    if (!ok) return;

    const previous = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Deleting...';
    const done = await usbPostAction('/api/storage/usb/delete-partition', { device }, `Deleted partition ${device}`);
    if (!done) {
        btn.disabled = false;
        btn.innerHTML = previous;
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
            <p style="${FIELD_NOTE}">The same folder is reused on every mount of this drive, so backup jobs can target a fixed path.</p>`,
    });
    if (!values) return;

    const previous = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Mounting...';
    try {
        const result = await apiRequest('/api/storage/usb/mount', {
            method: 'POST',
            body: { device, folder: (values.folder || '').trim() || null },
        });
        notify({ kind: 'success', message: `Mounted ${device} at ${result.mountpoint}` });
    } catch (err) {
        logError('Storage: USB mount failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Mount failed' });
        btn.disabled = false;
        btn.innerHTML = previous;
        return;
    }
    _runSection('usb');
}

async function usbUnmountAction(btn) {
    const device = btn.dataset.device;
    const ok = await showConfirm(
        'Unmount USB drive',
        `Unmount ${device} permanently? The drive is unmounted now and its /etc/fstab entries are removed, so it will not be mounted automatically on the next boot.`,
    );
    if (!ok) return;

    const previous = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Unmounting...';
    try {
        await apiRequest('/api/storage/usb/unmount', { method: 'POST', body: { device } });
        notify({ kind: 'success', message: `Unmounted ${device}` });
    } catch (err) {
        logError('Storage: USB unmount failed', err?.detail || err?.message);
        notify({ kind: 'error', message: err?.detail || err?.message || 'Unmount failed' });
        btn.disabled = false;
        btn.innerHTML = previous;
        return;
    }
    _runSection('usb');
}
