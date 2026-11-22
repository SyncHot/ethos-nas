"""Downloads — watch-folder scanner pass (TODO.md P2-01).

One pass over a folder: read each new ``.torrent``, ask the router to build and
persist a task, then move the file into ``.processed/`` so a restart does not
queue it again. Torrent parsing, the task shape and persistence stay in
``routers/downloads.py`` (injected as callbacks); this module owns the file
iteration, dedupe sets and the move, so all of it is testable without a server.

The background thread, its stop event and the per-user path scope stay in the
router — they are application lifecycle, not scanning logic.
"""

import os
import shutil

DEFAULT_SEEN_CAP = 5000


def remember(bucket: set, value: str, cap: int = DEFAULT_SEEN_CAP) -> None:
    """Add to a dedupe set while keeping it bounded (the scanner is long-lived)."""
    bucket.add(value)
    if len(bucket) > cap:
        for extra in list(bucket)[: len(bucket) - cap]:
            bucket.discard(extra)


def move_processed(folder: str, filename: str, logger) -> str | None:
    """Move a queued .torrent into ``<folder>/.processed`` (unique name).

    Returns the destination path, or ``None`` when the move failed (the task is
    already persisted, so a failure here only means a re-scan will see the file
    again and dedupe it by infohash).
    """
    try:
        proc = os.path.join(folder, ".processed")
        os.makedirs(proc, exist_ok=True)
        dest = os.path.join(proc, filename)
        if os.path.exists(dest):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(os.path.join(proc, f"{base}.{counter}{ext}")):
                counter += 1
            dest = os.path.join(proc, f"{base}.{counter}{ext}")
        shutil.move(os.path.join(folder, filename), dest)
        return dest
    except OSError as exc:
        logger.warning("Watch folder: could not move %s to .processed (%s)", filename, exc)
        return None


def scan_folder(folder: str, *, parse_torrent, build_task, enqueue,
                seen: set, failed: set, logger, on_queued=None) -> int:
    """Queue every new torrent in ``folder``; returns how many were queued.

    - ``parse_torrent(bytes) -> dict`` must return at least ``hash``, ``name``,
      ``magnet`` (and optionally ``size``); a ``ValueError``/``OSError`` means
      "not a usable torrent" and is logged once per filename.
    - ``build_task(info) -> dict`` creates the router's task document.
    - ``enqueue(task, info_hash)`` appends + persists it (the router holds its
      save lock inside this callback).
    - ``on_queued()`` is called after each queued task (the router schedules the
      pump; it may run from a worker thread).
    """
    wf = (folder or "").strip()
    if not wf or not os.path.isdir(wf):
        return 0
    try:
        entries = sorted(os.listdir(wf))
    except OSError as exc:
        logger.warning("Watch folder unreadable (%s): %s", wf, exc)
        return 0

    queued = 0
    for filename in entries:
        if not filename.lower().endswith(".torrent"):
            continue
        path = os.path.join(wf, filename)
        try:
            with open(path, "rb") as handle:
                data = handle.read()
            info = parse_torrent(data)
        except (ValueError, OSError) as exc:
            if filename not in failed:
                logger.warning("Watch folder: skipping %s (%s)", filename, exc)
                remember(failed, filename)
            continue
        info_hash = str(info.get("hash") or "")
        if not info_hash or info_hash in seen:
            continue
        task = build_task(info)
        enqueue(task, info_hash)
        remember(seen, info_hash)
        move_processed(wf, filename, logger)
        logger.info("Watch folder: queued %s (%s)", filename, info_hash[:12])
        queued += 1
        if on_queued:
            on_queued()
    return queued
