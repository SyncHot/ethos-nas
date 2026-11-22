// EthOS NAS — App Icon System
// Single source of truth for app iconography: a consistent set of 24x24
// stroke-based line glyphs (1.8px, rounded caps) + a cohesive muted gradient
// palette. Replaces raw emoji so icons render identically on every platform.

const GLYPHS = {
    // Docker — stacked container layers
    containers: '<path d="M12 2.5L20.5 7.25L12 12L3.5 7.25L12 2.5Z"/><path d="M3.5 11.75L12 16.5L20.5 11.75"/><path d="M3.5 16.25L12 21L20.5 16.25"/>',
    // Docker images — image frame with mountain
    images: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="9.5" r="1.8"/><path d="M20.5 15.5L16.5 11.5L7 20"/>',
    // Docker volumes — stacked drives
    volumes: '<rect x="3" y="4.5" width="18" height="6.5" rx="2"/><rect x="3" y="13" width="18" height="6.5" rx="2"/><path d="M6.5 7.75h.01"/><path d="M6.5 16.25h.01"/>',
    // Storage — database cylinder
    storage: '<ellipse cx="12" cy="5.5" rx="8" ry="3"/><path d="M4 5.5V18.5C4 20.16 7.58 21.5 12 21.5S20 20.16 20 18.5V5.5"/><path d="M4 12C4 13.66 7.58 15 12 15S20 13.66 20 12"/>',
    // Files — folder
    files: '<path d="M3 7a2 2 0 0 1 2-2h4.2l2 2.5H19a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
    // Trash — bin with lid and vertical ribs
    trash: '<path d="M4 7h16"/><path d="M9.5 7V5h5v2"/><path d="M6.5 7 7.5 20h9L17.5 7"/><path d="M10 11v5"/><path d="M14 11v5"/>',
    // Downloads — arrow into tray
    // Terminal — prompt in frame
    terminal: '<rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="M7 9.5l3.5 3L7 16"/><path d="M12.5 16H17"/>',
    // System Overview — bar chart / metrics
    overview: '<path d="M4 20V10"/><path d="M9 20V4"/><path d="M14 20v-6"/><path d="M19 20V8"/>',
    // Network — globe
    network: '<circle cx="12" cy="12" r="9"/><path d="M12 3a13.5 13.5 0 0 0 0 18 13.5 13.5 0 0 0 0-18"/><path d="M3 12h18"/>',
    // Firewall & Access Control — shield
    security: '<path d="M12 3l7.5 3v5.6c0 4.6-3.2 8.1-7.5 9.9-4.3-1.8-7.5-5.3-7.5-9.9V6L12 3Z"/>',
    // Services — app grid
    services: '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
    // Settings — sliders
    settings: '<path d="M4 7h16"/><circle cx="15" cy="7" r="2.3"/><path d="M4 12h16"/><circle cx="8" cy="12" r="2.3"/><path d="M4 17h16"/><circle cx="14" cy="17" r="2.3"/>',
    // Process Manager — activity pulse
    processes: '<path d="M3 12h4l2.5-6.5L14 18.5l2.5-6.5H21"/>',
    // Log Viewer — document with lines
    logs: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"/><path d="M14 3v5h5"/><path d="M9.5 13H14.5"/><path d="M9.5 17H13"/>',
    // Packages — cube
    packages: '<path d="M12 3l8 4.6v8.8L12 21l-8-4.6V7.6L12 3Z"/><path d="M4 7.6l8 4.6 8-4.6"/><path d="M12 12.2V21"/>',
    // Photos — image frame
    // Samba Shares — share nodes
    // SMART Health — gauge with needle
    smart: '<path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    // System Services — server rack
    systemd: '<rect x="3" y="4" width="18" height="6.5" rx="2"/><rect x="3" y="13.5" width="18" height="6.5" rx="2"/><path d="M6.5 7.25h.01"/><path d="M6.5 16.75h.01"/>',
    // Cron Jobs — clock
    cron: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
    // Music — beamed notes
    // Videos — play in screen
    // Code Editor — code brackets
    // Disk Cleaner — sparkles
    // Universal Search — magnifier
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M20.5 20.5L15.6 15.6"/>',
    sharing: '<circle cx="9" cy="8" r="3"/><path d="M3.5 20c0-3 2.5-5 5.5-5s5.5 2 5.5 5"/><circle cx="17.5" cy="9" r="2.5"/><path d="M14.8 14.6c2.9-.4 5.2 1.6 5.2 4.4"/>',
    // Shared Links — chain link
    'shared-links': '<path d="M10.5 13.5a4.5 4.5 0 0 0 6.8.5l2.2-2.2a4.5 4.5 0 0 0-6.4-6.4l-1.3 1.3"/><path d="M13.5 10.5a4.5 4.5 0 0 0-6.8-.5l-2.2 2.2a4.5 4.5 0 0 0 6.4 6.4l1.3-1.3"/>',
};

// Cohesive muted palette — every tile uses the same 135deg angle and a
// similar saturation/lightness range so the set reads as one design system.
export const APP_ICON_GRADIENTS = {
    containers: 'linear-gradient(135deg,#2f80ed,#1b4fae)',
    images:     'linear-gradient(135deg,#38bdf8,#0284c7)',
    volumes:    'linear-gradient(135deg,#64748b,#334155)',
    storage:    'linear-gradient(135deg,#f97316,#c2410c)',
    files:      'linear-gradient(135deg,#3ecf8e,#178f5e)',
    trash:      'linear-gradient(135deg,#94a3b8,#475569)',
    terminal:   'linear-gradient(135deg,#33415c,#101827)',
    overview:   'linear-gradient(135deg,#818cf8,#4338ca)',
    network:    'linear-gradient(135deg,#22b8e6,#0e7490)',
    security:   'linear-gradient(135deg,#f43f5e,#b91c3c)',
    services:   'linear-gradient(135deg,#ec6bb0,#be2a78)',
    settings:   'linear-gradient(135deg,#9aa7bd,#525f78)',
    processes:  'linear-gradient(135deg,#c084fc,#86198f)',
    logs:       'linear-gradient(135deg,#cf9e5f,#96682f)',
    packages:   'linear-gradient(135deg,#6366f1,#4338ca)',
    smart:      'linear-gradient(135deg,#ef4444,#991b1b)',
    systemd:    'linear-gradient(135deg,#7c8ba1,#3e4c63)',
    cron:       'linear-gradient(135deg,#fbbf24,#b45309)',
    search:     'linear-gradient(135deg,#fbbf24,#d97706)',
    sharing:    'linear-gradient(135deg,#2dd4bf,#0f766e)',
    'shared-links': 'linear-gradient(135deg,#60a5fa,#4338ca)',
};

export const FALLBACK_GRADIENT = 'linear-gradient(135deg,#64748b,#475569)';

/**
 * Inline SVG glyph for an app id. Uses stroke="currentColor" so the host
 * element's `color` controls the glyph color (white on tiles).
 */
export function appIconSvg(appId, size = 24, strokeWidth = 1.8) {
    const g = GLYPHS[appId];
    if (!g) return '';
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${g}</svg>`;
}

/**
 * { svg, gradient } for a known app id (glyph sized `size`), or null.
 */
export function getAppIcon(appId, size = 24) {
    if (!GLYPHS[appId]) return null;
    return { svg: appIconSvg(appId, size), gradient: APP_ICON_GRADIENTS[appId] || FALLBACK_GRADIENT };
}

/**
 * Self-contained mini tile (gradient + white glyph) for compact surfaces
 * like the window title bar and taskbar. Returns '' when unknown so callers
 * can fall back to an emoji character.
 */
export function appMiniTileHtml(appId, size = 18) {
    const ic = getAppIcon(appId);
    if (!ic) return '';
    const glyphSize = Math.max(10, Math.round(size * 0.62));
    const radius = Math.max(4, Math.round(size * 0.3));
    return `<span class="app-icon-mini" style="display:inline-flex;align-items:center;justify-content:center;width:${size}px;height:${size}px;border-radius:${radius}px;background:${ic.gradient};color:#fff;flex-shrink:0;box-shadow:inset 0 1px 0 rgba(255,255,255,.2), inset 0 0 0 .5px rgba(255,255,255,.12), 0 1px 3px rgba(0,0,0,.35);">${appIconSvg(appId, glyphSize, 2)}</span>`;
}
