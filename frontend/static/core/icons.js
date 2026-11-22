// EthOS NAS — shared UI icon set
//
// One stroke-based SVG set for *structural* UI (section headings, table rows,
// toolbar buttons) so apps stop using decorative emoji. Emoji render
// differently on every OS/browser, can't inherit colour, and made the same
// concept look different in Storage, Backups, and Docker.
//
// App tiles keep their own branded artwork (core/app-icons.js) — this module is
// only for the generic chrome.

const PATHS = {
    // A stack of platters — physical disk / HDD.
    disk: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10.5h.01M9 10.5h.01"/><path d="M2.5 12.5h19"/>',
    // Flat rectangle with a chip — SSD.
    ssd: '<rect x="2.5" y="7" width="19" height="10" rx="2"/><path d="M6 10.5h4M6 13.5h7"/><circle cx="17.5" cy="12" r="1.6"/>',
    // USB plug.
    usb: '<path d="M12 21V7"/><path d="M9.2 9.8 12 4l2.8 5.8"/><path d="M6.5 12.5h11v2a5.5 5.5 0 0 1-11 0v-2Z"/>',
    // Split rectangle — partitions / volumes.
    partition: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/><path d="M3 11h9"/>',
    // Folder.
    folder: '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2.5h8.5A1.5 1.5 0 0 1 20.5 10v7.5A1.5 1.5 0 0 1 19 19H4.5A1.5 1.5 0 0 1 3 17.5Z"/>',
    check: '<path d="m4.5 12.5 5 5 10-11"/>',
    bell: '<path d="M18 8.5a6 6 0 1 0-12 0c0 6-2 7.5-2 7.5h16s-2-1.5-2-7.5"/><path d="M10.3 20a2 2 0 0 0 3.4 0"/>',
    alert: '<path d="M12 4.5 2.8 20h18.4L12 4.5Z"/><path d="M12 10v4.5"/><path d="M12 17.2h.01"/>',
    info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><path d="M12 8.2h.01"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    search: '<circle cx="11" cy="11" r="6.8"/><path d="m16 16 4.2 4.2"/>',
    refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v4h-4"/>',
    arrowUp: '<path d="M12 19V5"/><path d="m6 11 6-6 6 6"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    trash: '<path d="M4 7h16"/><path d="M9.5 7V5h5v2"/><path d="M6.5 7 7.5 20h9L17.5 7"/>',
    edit: '<path d="M4 20h4L20 8l-4-4L4 16v4Z"/>',
    download: '<path d="M12 4v10"/><path d="m7.5 10.5 4.5 4 4.5-4"/><path d="M4.5 19.5h15"/>',
    subtitles: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M6.5 14.5h5M13.5 14.5h4"/>',
    volume: '<path d="M4 9.5h3.2L12 6v12l-4.8-3.5H4Z"/><path d="M15.5 9.2a4 4 0 0 1 0 5.6"/><path d="M18 6.8a7.5 7.5 0 0 1 0 10.4"/>',
    film: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M7.5 4.5v15M16.5 4.5v15"/><path d="M3 12h18"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M18.4 5.6l-1.6 1.6M7.2 16.8l-1.6 1.6"/>',
    shield: '<path d="M12 3.2 19.5 6v6c0 4.4-3 7.6-7.5 8.8C7.5 19.6 4.5 16.4 4.5 12V6L12 3.2Z"/>',
    lock: '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7"/>',
    unlock: '<rect x="4.5" y="10.5" width="15" height="9.5" rx="2"/><path d="M8 10.5V7.8a4 4 0 0 1 7.4-2"/>',
    ban: '<circle cx="12" cy="12" r="8.5"/><path d="M6.2 6.2l11.6 11.6"/>',
    chart: '<path d="M4 19.5h16"/><rect x="5.5" y="11" width="3.4" height="6"/><rect x="10.3" y="7" width="3.4" height="10"/><rect x="15.1" y="13.5" width="3.4" height="3.5"/>',
    users: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.2 19.5a5.9 5.9 0 0 1 11.6 0"/><path d="M16.2 6.2a3.2 3.2 0 0 1 0 6.3"/><path d="M17.5 13.6a5.4 5.4 0 0 1 3.3 5"/>',
    activity: '<path d="M3 12.5h3.6l2.2-6 3.4 12 2.4-8 1.6 2h4.8"/>',
    play: '<path d="M8.5 5.5 18 12l-9.5 6.5v-13Z"/>',
    pause: '<path d="M9 5.5v13M15 5.5v13"/>',
    memory: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>',
    thermometer: '<path d="M12 4.5a2 2 0 0 1 2 2v7a4 4 0 1 1-4 0v-7a2 2 0 0 1 2-2Z"/><path d="M12 8.5v6"/>',
    network: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5C14.5 5.9 14.5 18.1 12 20.5 9.5 18.1 9.5 5.9 12 3.5Z"/>',
    phone: '<rect x="7" y="3" width="10" height="18" rx="2.5"/><path d="M11 18h2"/>',
    music: '<path d="M9 17.5V6l9-2v11"/><circle cx="6.8" cy="17.6" r="2.2"/><circle cx="15.8" cy="15" r="2.2"/>',
    book: '<path d="M12 6.5C10.5 5.2 8 4.5 5 4.5v14c3 0 5.5.7 7 2 1.5-1.3 4-2 7-2v-14c-3 0-5.5.7-7 2Z"/><path d="M12 6.5v14"/>',
    video: '<rect x="3" y="6" width="12" height="12" rx="2"/><path d="m15 10.5 6-3.5v10l-6-3.5"/>',
    palette: '<path d="M12 3.5a8.5 8.5 0 0 0 0 17c1.6 0 2-1 1.4-2.2-.7-1.3 0-2.3 1.4-2.3h1.5a3.7 3.7 0 0 0 3.7-3.7A8.5 8.5 0 0 0 12 3.5Z"/><circle cx="8.6" cy="9.6" r=".9"/><circle cx="12" cy="7.7" r=".9"/><circle cx="15.4" cy="9.6" r=".9"/>',
};

const DEFAULT_SIZE = 18;

/**
 * Inline SVG for a UI glyph. Inherits `currentColor`, so it picks up the
 * surrounding text colour in both light and dark themes.
 *
 * @param {string} name  key from PATHS
 * @param {number} [size] pixel size (default 18)
 * @param {object} [opts] { strokeWidth, className, title }
 * @returns {string} HTML string, or '' for an unknown name
 */
export function uiIcon(name, size = DEFAULT_SIZE, opts = {}) {
    const d = PATHS[name];
    if (!d) return '';
    const stroke = opts.strokeWidth ?? 1.7;
    const cls = opts.className ? ` class="${opts.className}"` : '';
    const title = opts.title ? `<title>${String(opts.title).replace(/[<>&]/g, '')}</title>` : '';
    return `<svg${cls} xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" `
        + `viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${stroke}" `
        + `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" `
        + `style="flex-shrink:0;vertical-align:-3px">${title}${d}</svg>`;
}

/** True when a glyph exists (lets callers fall back to text). */
export function hasUiIcon(name) {
    return Object.prototype.hasOwnProperty.call(PATHS, name);
}
