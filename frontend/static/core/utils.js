// EthOS NAS — Shared Utilities

// Escapes for HTML *text* content. NOTE: does not escape quotes — never use it
// inside an attribute value; use escapeAttr() there.
export function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
}

const _ATTR_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

// Safe inside single- or double-quoted attributes and inline handlers: the
// ampersand is escaped first so entity text like "&quot;" is not double-read,
// and both quote styles are neutralised (TODO.md P3-02).
export function escapeAttr(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => _ATTR_ESCAPES[c]);
}

export function formatBytes(bytes) {
    if (!bytes && bytes !== 0) return '—';
    const k = 1024, sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0;
    while (bytes >= k && i < sizes.length - 1) { bytes /= k; i++; }
    return parseFloat(bytes.toFixed(1)) + ' ' + sizes[i];
}

export function formatUptime(s) {
    if (!s || s < 0) return '—';
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function formatDate(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toLocaleDateString('pl-PL') + ' ' + d.toLocaleTimeString('pl-PL', { hour: '2-digit', minute: '2-digit' });
}

export function formatPorts(ports) {
    if (!ports) return '—';
    // Handle string format from Docker API (e.g. "8080→80, 443→443")
    if (typeof ports === 'string') return escapeHtml(ports);
    // Handle array format from Docker SDK (e.g. [{private_port: 80, public_port: '8080'}])
    if (Array.isArray(ports)) {
        return ports.map(p => {
            if (!p.private_port) return '';
            const ext = `${p.public_port || '—'}`;
            return `<span style="font-family:monospace;font-size:12px;">${ext}</span>→<span style="color: var(--text-2)">${p.private_port}</span>`;
        }).join('<br>') || '—';
    }
    return '—';
}

export function formatSpeed(bps) {
    if (!bps) return '0 B/s';
    if (bps < 1024) return bps + ' B/s';
    if (bps < 1048576) return (bps / 1024).toFixed(1) + ' KB/s';
    return (bps / 1048576).toFixed(1) + ' MB/s';
}

export function hexToRgb(hex) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `${r},${g},${b}`;
}

export function safeJsStr(s) {
    if (s == null) return '';
    // Everything that can either terminate the surrounding HTML attribute or
    // escape the JS string literal. `<`/`>` go out as \x3C/\x3E so a file named
    // `</script>` or `" onmouseover=…` cannot break out of either context.
    return String(s)
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/"/g, '\\"')
        .replace(/</g, '\\x3C')
        .replace(/>/g, '\\x3E')
        .replace(/\r/g, '\\r')
        .replace(/\n/g, '\\n')
        .replace(/\u2028/g, '\\u2028')
        .replace(/\u2029/g, '\\u2029');
}

/** Escape a value embedded inside inline JS *and* an HTML attribute:
 *  onclick="fn('${jsAttr(value)}')" — JS-escape first, then HTML-escape. */
export function jsAttr(s) { return escapeAttr(safeJsStr(s)); }

// ── SVG Icons ──

const _IC = (path, size = 20) => `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;

export const FILE_ICONS_SVG = {
    dir: `<span class="fm-icon fm-dir"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none"><defs><linearGradient id="fmFolderBack" x1="12" y1="5" x2="12" y2="19" gradientUnits="userSpaceOnUse"><stop stop-color="#3b82f6"/><stop offset="1" stop-color="#1e40af"/></linearGradient><linearGradient id="fmFolderFront" x1="12" y1="11" x2="12" y2="19" gradientUnits="userSpaceOnUse"><stop stop-color="#7cb3ff"/><stop offset="1" stop-color="#3b82f6"/></linearGradient></defs><path d="M3 7.4C3 6.07 4.07 5 5.4 5h3.9c.66 0 1.3.26 1.77.73l1.28 1.27H18.6C19.93 7 21 8.07 21 9.4v7.2c0 1.33-1.07 2.4-2.4 2.4H5.4C4.07 19 3 17.93 3 16.6z" fill="url(#fmFolderBack)"/><path d="M3 11h18v5.6c0 1.33-1.07 2.4-2.4 2.4H5.4C4.07 19 3 17.93 3 16.6z" fill="url(#fmFolderFront)"/></svg></span>`,
    file: `<span class="fm-icon fm-file">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/>') + `</span>`,
    link: `<span class="fm-icon fm-link">` + _IC('<path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/>') + `</span>`,
    '.py': `<span class="fm-icon fm-py">` + _IC('<path d="M8 3v4l4 5V3"/><path d="M8 21v-4l4-5v10"/><circle cx="16" cy="7" r="2"/><circle cx="16" cy="17" r="2"/>') + `</span>`,
    '.js': `<span class="fm-icon fm-js">` + _IC('<path d="M8 3v4l4 5V3"/><path d="M8 21v-4l4-5v10"/>') + `</span>`,
    '.ts': `<span class="fm-icon fm-ts">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 17v-4.5a1.5 1.5 0 013 0V17"/>') + `</span>`,
    '.jsx': `<span class="fm-icon fm-jsx">` + _IC('<polygon points="12,2 22,8.5 22,15.5 12,22 2,15.5 2,8.5"/>') + `</span>`,
    '.tsx': `<span class="fm-icon fm-tsx">` + _IC('<polygon points="12,2 22,8.5 22,15.5 12,22 2,15.5 2,8.5"/><path d="M9 17v-4.5a1.5 1.5 0 013 0V17"/>') + `</span>`,
    '.html': `<span class="fm-icon fm-html">` + _IC('<polyline points="4,7 1,7 12,21 23,7 20,7"/><path d="M6.5 12l-2.5 4"/><path d="M17.5 12l2.5 4"/>') + `</span>`,
    '.htm': `<span class="fm-icon fm-html">` + _IC('<polyline points="4,7 1,7 12,21 23,7 20,7"/><path d="M6.5 12l-2.5 4"/><path d="M17.5 12l2.5 4"/>') + `</span>`,
    '.css': `<span class="fm-icon fm-css">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6l-1 6h4"/>') + `</span>`,
    '.scss': `<span class="fm-icon fm-scss">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6l-1 6h4"/>') + `</span>`,
    '.json': `<span class="fm-icon fm-json">` + _IC('<path d="M8 3H5a2 2 0 00-2 2v3m18 0V5a2 2 0 00-2-2h-3"/><path d="M16 21h3a2 2 0 002-2v-3M4 21H1a2 2 0 01-2-2v-3"/>') + `</span>`,
    '.yaml': `<span class="fm-icon fm-yaml">` + _IC('<line x1="8" y1="6" x2="8" y2="6"/><line x1="8" y1="12" x2="8" y2="12"/><line x1="8" y1="18" x2="8" y2="18"/><line x1="4" y1="6" x2="4" y2="6"/><line x1="4" y1="12" x2="4" y2="12"/><line x1="4" y1="18" x2="4" y2="18"/><line x1="16" y1="6" x2="16" y2="6"/><line x1="16" y1="12" x2="16" y2="12"/><line x1="16" y1="18" x2="16" y2="18"/>') + `</span>`,
    '.yml': `<span class="fm-icon fm-yaml">` + _IC('<line x1="8" y1="6" x2="8" y2="6"/><line x1="8" y1="12" x2="8" y2="12"/><line x1="8" y1="18" x2="8" y2="18"/><line x1="4" y1="6" x2="4" y2="6"/><line x1="4" y1="12" x2="4" y2="12"/><line x1="4" y1="18" x2="4" y2="18"/>') + `</span>`,
    '.xml': `<span class="fm-icon fm-xml">` + _IC('<polyline points="4,7 1,7 12,21 23,7 20,7"/><path d="M6.5 12l-2.5 4"/><path d="M17.5 12l2.5 4"/>') + `</span>`,
    '.md': `<span class="fm-icon fm-md">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/><path d="M9 15l2 2 4-4"/>') + `</span>`,
    '.txt': `<span class="fm-icon fm-txt">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>') + `</span>`,
    '.log': `<span class="fm-icon fm-log">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/><line x1="9" y1="15" x2="15" y2="15"/>') + `</span>`,
    '.rst': `<span class="fm-icon fm-rst">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/>') + `</span>`,
    '.sh': `<span class="fm-icon fm-sh">` + _IC('<path d="M4 17l6-6-6-6"/><path d="M12 19h8"/>') + `</span>`,
    '.bash': `<span class="fm-icon fm-bash">` + _IC('<path d="M4 17l6-6-6-6"/><path d="M12 19h8"/>') + `</span>`,
    '.zsh': `<span class="fm-icon fm-zsh">` + _IC('<path d="M4 17l6-6-6-6"/><path d="M12 19h8"/>') + `</span>`,
    '.fish': `<span class="fm-icon fm-fish">` + _IC('<path d="M4 17l6-6-6-6"/><path d="M12 19h8"/>') + `</span>`,
    '.zip': `<span class="fm-icon fm-zip">` + _IC('<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/>') + `</span>`,
    '.tar': `<span class="fm-icon fm-tar">` + _IC('<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/>') + `</span>`,
    '.gz': `<span class="fm-icon fm-gz">` + _IC('<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/>') + `</span>`,
    '.bz2': `<span class="fm-icon fm-bz2">` + _IC('<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/>') + `</span>`,
    '.xz': `<span class="fm-icon fm-xz">` + _IC('<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/>') + `</span>`,
    '.7z': `<span class="fm-icon fm-7z">` + _IC('<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/>') + `</span>`,
    '.rar': `<span class="fm-icon fm-rar">` + _IC('<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/>') + `</span>`,
    '.tgz': `<span class="fm-icon fm-tgz">` + _IC('<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/>') + `</span>`,
    '.png': `<span class="fm-icon fm-png">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/>') + `</span>`,
    '.jpg': `<span class="fm-icon fm-jpg">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/>') + `</span>`,
    '.jpeg': `<span class="fm-icon fm-jpg">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/>') + `</span>`,
    '.gif': `<span class="fm-icon fm-gif">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/>') + `</span>`,
    '.svg': `<span class="fm-icon fm-svg">` + _IC('<path d="M4 4h16v16H4z"/><circle cx="8" cy="8" r="3"/><circle cx="16" cy="8" r="3"/>') + `</span>`,
    '.webp': `<span class="fm-icon fm-webp">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/>') + `</span>`,
    '.bmp': `<span class="fm-icon fm-bmp">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/>') + `</span>`,
    '.mp4': `<span class="fm-icon fm-mp4">` + _IC('<polygon points="23,7 16,12 23,17"/><rect x="1" y="5" width="15" height="14" rx="2"/>') + `</span>`,
    '.mkv': `<span class="fm-icon fm-mkv">` + _IC('<polygon points="23,7 16,12 23,17"/><rect x="1" y="5" width="15" height="14" rx="2"/>') + `</span>`,
    '.avi': `<span class="fm-icon fm-avi">` + _IC('<polygon points="23,7 16,12 23,17"/><rect x="1" y="5" width="15" height="14" rx="2"/>') + `</span>`,
    '.mov': `<span class="fm-icon fm-mov">` + _IC('<polygon points="23,7 16,12 23,17"/><rect x="1" y="5" width="15" height="14" rx="2"/>') + `</span>`,
    '.wmv': `<span class="fm-icon fm-wmv">` + _IC('<polygon points="23,7 16,12 23,17"/><rect x="1" y="5" width="15" height="14" rx="2"/>') + `</span>`,
    '.webm': `<span class="fm-icon fm-webm">` + _IC('<polygon points="23,7 16,12 23,17"/><rect x="1" y="5" width="15" height="14" rx="2"/>') + `</span>`,
    '.mp3': `<span class="fm-icon fm-mp3">` + _IC('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>') + `</span>`,
    '.ogg': `<span class="fm-icon fm-ogg">` + _IC('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>') + `</span>`,
    '.wav': `<span class="fm-icon fm-wav">` + _IC('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>') + `</span>`,
    '.flac': `<span class="fm-icon fm-flac">` + _IC('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>') + `</span>`,
    '.aac': `<span class="fm-icon fm-aac">` + _IC('<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>') + `</span>`,
    '.pdf': `<span class="fm-icon fm-pdf">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/><path d="M9 15l2 2 4-4"/>') + `</span>`,
    '.doc': `<span class="fm-icon fm-doc">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/><line x1="9" y1="15" x2="15" y2="15"/>') + `</span>`,
    '.docx': `<span class="fm-icon fm-doc">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/><line x1="9" y1="15" x2="15" y2="15"/>') + `</span>`,
    '.xls': `<span class="fm-icon fm-xls">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/><path d="M9 13l2 2 4-4"/>') + `</span>`,
    '.xlsx': `<span class="fm-icon fm-xls">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/><path d="M9 13l2 2 4-4"/>') + `</span>`,
    '.ppt': `<span class="fm-icon fm-ppt">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/>') + `</span>`,
    '.pptx': `<span class="fm-icon fm-ppt">` + _IC('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14,2 14,8 20,8"/>') + `</span>`,
    '.iso': `<span class="fm-icon fm-iso">` + _IC('<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/>') + `</span>`,
    '.img': `<span class="fm-icon fm-img">` + _IC('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21,15 16,10 5,21"/>') + `</span>`,
    '.vdi': `<span class="fm-icon fm-vdi">` + _IC('<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>') + `</span>`,
    '.vhdx': `<span class="fm-icon fm-vhdx">` + _IC('<rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>') + `</span>`,
};

export function getFileIcon(name, type) {
    if (type === 'dir') return FILE_ICONS_SVG.dir;
    if (!name || name === '..') return FILE_ICONS_SVG.file;
    const ext = '.' + name.toLowerCase().split('.').pop();
    return FILE_ICONS_SVG[ext] || FILE_ICONS_SVG.file;
}

export function getFileIconExt(name) {
    if (!name || name === '..') return 'file';
    const ext = '.' + name.toLowerCase().split('.').pop();
    return FILE_ICONS_SVG[ext] ? ext.substring(1) : 'generic';
}

// ── Toast Notifications ──
// Removed: this module used to own a second, incompatible `showToast(type, msg)`
// (emoji icons, its own #toastContainer). Every caller uses the unified
// notification centre instead — `window.showToast(message, type)` — so keeping a
// dead look-alike here only invited argument-order bugs (packages.js had one).
