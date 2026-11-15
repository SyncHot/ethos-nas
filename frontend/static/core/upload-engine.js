// EthOS NAS — Unified Upload Engine
// Supports multi-file drag-and-drop and programmatic uploads via chunked transfer.
// Hooks into backend endpoints: /api/files/chunked/init, upload, complete
//
// Each file is tracked as a "job" keyed by a client-side id (jobId). The server
// session id (uploadId) is re-created when a failed transfer is retried, so the
// progress row — and its DOM identity — survives the retry.

import { apiFetch, readErrorDetail } from './api.js';
import { formatBytes } from './utils.js';
import { showUnifiedToast } from './notification-center.js';
import { clearFinishedTasks, showTaskPanel, startTask } from './progress.js';

const CHUNK_SIZE = 5 * 1024 * 1024; // 5 MB chunks (configurable based on network conditions)
const EVENT_THROTTLE_MS = 250;      // cap for ethos:uploads-changed (status bar listeners)

/** @type {Map<string, object>} jobId -> upload job */
const _jobs = new Map();
let _jobSeq = 0;
let _lastEventAt = 0;
let _eventTimer = null;

/**
 * Start an upload session for one or more files.
 *
 * @param {File[]} files - Array of File objects to upload
 * @param {string} destDir - Destination directory path on the server (absolute filesystem path)
 * @returns {Promise<string[]>} Job ids for the queued files (one row per file)
 */
export async function startUpload(files, destDir) {
    const jobIds = [];
    const list = Array.from(files || []);
    if (list.length === 0) return jobIds;

    const started = [];
    for (const file of list) {
        const job = _createJob(file, destDir);
        _jobs.set(job.jobId, job);
        _beginTask(job);
        jobIds.push(job.jobId);
        started.push(job);
    }
    _emitUploadsChanged(true);

    // Transfers run in the background; every failure surfaces on its own row.
    for (const job of started) _runJob(job);

    return jobIds;
}

/** Create the in-memory record for one queued file. */
function _createJob(file, destDir) {
    _jobSeq += 1;
    return {
        jobId: `up-${Date.now().toString(36)}-${_jobSeq.toString(36)}`,
        uploadId: null,
        file,
        destDir: destDir || '/',
        filename: file.name,
        size: file.size,
        bytesSent: 0,
        progress: 0,
        speed: 0,
        status: 'queued',   // queued | uploading | finalizing | complete | failed
        error: '',
        startTime: Date.now(),
        task: null,         // handle from core/progress.js
        running: false,
    };
}

/** Run one job to completion (init → chunks → complete), recording any failure. */
async function _runJob(job) {
    if (job.running) return;
    job.running = true;
    try {
        job.uploadId = null;
        job.bytesSent = 0;
        job.progress = 0;
        job.speed = 0;
        job.error = '';
        job.startTime = Date.now();
        job.status = 'queued';
        _syncTask(job);
        _emitUploadsChanged(true);

        const initData = await _initSession(job);
        job.uploadId = initData.upload_id || initData.uploadId;

        job.status = 'uploading';
        job.startTime = Date.now();
        _syncTask(job);
        _emitUploadsChanged(true);

        if (job.size > 0) await _transferChunks(job);

        job.status = 'finalizing';
        job.progress = 100;
        _syncTask(job);
        _emitUploadsChanged(true);

        await _completeUpload(job);

        job.status = 'complete';
        job.progress = 100;
        job.speed = 0;
        _finishTask(job);
    } catch (err) {
        job.status = 'failed';
        job.error = _errorText(err);
        _finishTask(job);
        console.error('[EthOS] Upload failed', job.filename, err);
        showUnifiedToast(`Upload failed for ${job.filename}: ${job.error}`, 'error');
    } finally {
        job.running = false;
        _emitUploadsChanged(true);
        _settleIfIdle();
    }
}

/** Human-readable reason for a failed transfer (server `detail` first). */
function _errorText(err) {
    return (err && (err.detail || err.message)) || 'Upload failed';
}

/** POST /chunked/init — create the server-side session. */
async function _initSession(job) {
    const resp = await apiFetch('/api/files/chunked/init', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            file_id: _generateFileId(job.file),
            filename: job.filename,
            dest_dir: job.destDir,
            total_size: job.size,
        }),
    });
    if (!resp.ok) {
        throw new Error(await readErrorDetail(resp, `Could not start upload (HTTP ${resp.status})`));
    }
    return resp.json();
}

/** Send every chunk of a file sequentially, updating the row after each one. */
async function _transferChunks(job) {
    const totalChunks = Math.ceil(job.size / CHUNK_SIZE);

    for (let i = 0; i < totalChunks; i++) {
        if (job.status !== 'uploading') return;  // cancelled/restarted mid-flight

        const start = i * CHUNK_SIZE;
        const end = Math.min(start + CHUNK_SIZE, job.size);
        const chunkData = await job.file.slice(start, end).arrayBuffer();

        const formData = new FormData();
        formData.append('upload_id', job.uploadId);
        formData.append('chunk_index', String(i));
        formData.append('data', new Blob([chunkData]));

        const resp = await apiFetch(
            `/api/files/chunked/upload?upload_id=${encodeURIComponent(job.uploadId)}&chunk_index=${i}`,
            { method: 'POST', body: formData },
        );
        if (!resp.ok) {
            const detail = await readErrorDetail(resp, `HTTP ${resp.status}`);
            throw new Error(`Chunk ${i + 1} of ${totalChunks} failed — ${detail}`);
        }

        job.bytesSent += chunkData.byteLength;
        // Progress is computed locally: the backend's total_chunks is unknown
        // until every chunk has arrived.
        job.progress = Math.min(100, (job.bytesSent / job.size) * 100);
        const elapsed = (Date.now() - job.startTime) / 1000;
        job.speed = elapsed > 0 ? job.bytesSent / elapsed : 0;

        _syncTask(job);
        _emitUploadsChanged(false);
    }
}

/** POST /chunked/complete — move the temp file into the destination folder. */
async function _completeUpload(job) {
    const formData = new FormData();
    formData.append('upload_id', job.uploadId);

    const resp = await apiFetch('/api/files/chunked/complete', { method: 'POST', body: formData });
    if (!resp.ok) {
        throw new Error(await readErrorDetail(resp, `Could not finalize upload (HTTP ${resp.status})`));
    }
    return resp.json();
}

// ── Public actions ───────────────────────────────────────────────────────────

/**
 * Re-queue a failed upload. The failed server session is abandoned and a fresh
 * one is created (the backend requires strictly sequential chunk indexes, so a
 * partial session cannot be resumed safely).
 *
 * @returns {boolean} true when a retry was started
 */
export function retryUpload(jobId) {
    const job = _jobs.get(jobId);
    if (!job || job.running || job.status !== 'failed') return false;

    job.uploadId = null;
    job.bytesSent = 0;
    job.progress = 0;
    job.speed = 0;
    job.error = '';
    job.status = 'queued';
    // Revive the existing row instead of creating a second one.
    if (job.task) job.task.update({ status: 'running', percent: 0, detail: `${formatBytes(job.size)} — retrying…` });
    _emitUploadsChanged(true);
    _runJob(job);
    return true;
}

/** Remove every finished (complete/failed) upload row. Active transfers are untouched. */
export function clearFinishedUploads() {
    clearFinishedTasks('uploads');
    _emitUploadsChanged(true);
}

/** Re-open the shared progress panel (used by the File Manager status bar). */
export function showUploadPanel() {
    showTaskPanel();
}

/** Current aggregate upload state — snapshot for the status-bar chip. */
export function getUploadStatus() {
    return _snapshot();
}

// ── Progress rendering (shared core/progress.js) ────────────────────────────
//
// Uploads do not own a panel any more: every row is a task in the unified
// progress center, so uploads, copy/move, delete/trash and package jobs all
// look and behave the same.

/** Create the shared progress row for a job (one row per file). */
function _beginTask(job) {
    job.task = startTask({
        appId: 'uploads',
        title: job.filename,
        detail: `${formatBytes(job.size)} — waiting…`,
        percent: 0,
        testIdPrefix: 'upload',
        retry: () => retryUpload(job.jobId),
        // The row is shared with every other app — clearing it must also drop
        // the job record so the File Manager chip stops counting it.
        onDismiss: () => { _jobs.delete(job.jobId); },
    });
}

/** Detail line for the current job state (bytes, speed, step). */
function _jobDetail(job) {
    if (job.status === 'finalizing') return `${formatBytes(job.size)} — finalizing…`;
    if (job.status === 'queued') return `${formatBytes(job.size)} — waiting…`;
    return `${formatBytes(job.bytesSent)} / ${formatBytes(job.size)} · ` +
        `${formatBytes(Math.round(job.speed))}/s`;
}

/** Push the running job state into its shared progress row. */
function _syncTask(job) {
    if (!job.task) return;
    job.task.update({
        percent: job.status === 'queued' ? 0 : Math.max(0, Math.min(100, job.progress || 0)),
        detail: _jobDetail(job),
    });
}

/** Terminal state for a job row (complete/failed); retry revives it. */
function _finishTask(job) {
    if (!job.task) return;
    if (job.status === 'complete') {
        job.task.finish('done', { percent: 100, detail: `${formatBytes(job.size)} — uploaded` });
    } else if (job.status === 'failed') {
        job.task.finish('failed', { detail: job.error || 'Upload failed' });
    }
}

// Row rendering lives in core/progress.js — uploads only own job/transfer state.

/** Aggregate counters + byte totals for the panel summary and status bars. */
function _snapshot() {
    const detail = {
        active: 0, queued: 0, completed: 0, failed: 0,
        total: 0, bytesSent: 0, bytesTotal: 0, percent: 0,
    };
    for (const job of _jobs.values()) {
        detail.total += 1;
        if (job.status === 'complete') detail.completed += 1;
        else if (job.status === 'failed') detail.failed += 1;
        else if (job.status === 'queued') detail.queued += 1;
        else detail.active += 1;

        if (job.status !== 'complete' && job.status !== 'failed') {
            detail.bytesSent += job.bytesSent;
            detail.bytesTotal += job.size;
        }
    }
    detail.percent = detail.bytesTotal > 0
        ? Math.min(100, (detail.bytesSent / detail.bytesTotal) * 100)
        : (detail.active + detail.queued > 0 ? 0 : 100);
    detail.percent = Math.round(detail.percent);
    return detail;
}

/**
 * Broadcast the aggregate state. Progress events are throttled so a 5 MB chunk
 * does not re-render every File Manager status bar in a burst; status changes
 * pass `immediate` and are never dropped.
 */
function _emitUploadsChanged(immediate = false) {
    const fire = () => {
        _lastEventAt = Date.now();
        _eventTimer = null;
        const detail = _snapshot();
        try {
            window.dispatchEvent(new CustomEvent('ethos:uploads-changed', { detail }));
        } catch { /* non-fatal */ }
    };

    if (immediate) {
        if (_eventTimer) { clearTimeout(_eventTimer); _eventTimer = null; }
        fire();
        return;
    }
    if (_eventTimer) return;
    const wait = EVENT_THROTTLE_MS - (Date.now() - _lastEventAt);
    if (wait <= 0) fire();
    else _eventTimer = setTimeout(fire, wait);
}

/** Fire ethos:uploads-settled once nothing is in flight any more. */
function _settleIfIdle() {
    for (const job of _jobs.values()) {
        if (job.status === 'queued' || job.status === 'uploading' || job.status === 'finalizing') return;
    }
    // Files refetches listings on this event: the file-events WebSocket
    // is not always connected/available.
    try { window.dispatchEvent(new CustomEvent('ethos:uploads-settled')); } catch { /* non-fatal */ }
}

/**
 * Initialize drag-and-drop on an element for file uploads.
 *
 * @param {HTMLElement} dropZone - The DOM element to attach DnD handlers to
 * @param {string} destDir - Destination directory path for uploaded files
 */
export function initDragDrop(dropZone, destDir) {
    let dragCounter = 0;  // Track nested dragenter/dragleave events

    dropZone.addEventListener('dragenter', (e) => {
        e.preventDefault();
        dragCounter++;
        dropZone.classList.add('upload-drag-over');  // Visual feedback that drops are accepted
    });

    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dragCounter--;
        if (dragCounter === 0) {  // Only remove visual feedback when all nested drags leave
            dropZone.classList.remove('upload-drag-over');
        }
    });

    dropZone.addEventListener('dragover', (e) => { e.preventDefault(); });  // Prevent default to allow drops

    // Handle actual file drops — start upload with dropped files
    dropZone.addEventListener('drop', async (e) => {
        e.preventDefault();
        dragCounter = 0;
        dropZone.classList.remove('upload-drag-over');

        const files = Array.from(e.dataTransfer.files);
        if (files.length > 0) await startUpload(files, destDir);  // Queue all dropped files for upload
    });
}

/**
 * Generate a unique file ID for upload tracking. Combines filename + timestamp + random suffix
 * to ensure uniqueness across concurrent uploads of the same filename.
 *
 * @param {File} file - The File object to generate an ID for
 * @returns {string} Unique identifier string (e.g., "photo.jpg_1723456789012_x7k2m")
 */
function _generateFileId(file) {
    return `${file.name}_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}
