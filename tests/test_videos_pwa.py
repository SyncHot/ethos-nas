#!/usr/bin/env python3
"""Static guards for the Videos PWA download queue (no server needed).

The 2.x download rework replaced the fire-and-forget transfer with a
server-backed queue. These checks keep the contract from silently regressing:
a reload must reconcile with `/api/videos/exports` instead of declaring the
download lost, and the transfer must stream into Cache Storage rather than
buffering a whole film in RAM.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
VIDEOS = ROOT / "frontend" / "static" / "videos"
CSS = ROOT / "frontend" / "static" / "css" / "videos.css"
PAGE = ROOT / "frontend" / "videos.html"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main() -> int:
    offline = (VIDEOS / "offline.js").read_text(encoding="utf-8")
    videos = (VIDEOS / "videos.js").read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    page = PAGE.read_text(encoding="utf-8")

    for name in ("syncDownloads", "queueDownload", "pumpDownloads",
                 "cancelDownload", "retryDownload", "initOffline", "deleteDownload"):
        check(f"offline.js exports {name}", f"export async function {name}(" in offline
              or f"export function {name}(" in offline)

    check("a reload reconciles with the NAS queue",
          "apiFetch('/api/videos/exports')" in offline and "syncDownloads" in offline)
    check("active rows survive a reload",
          "Interrupted — download it again" not in offline)
    check("stored files free the NAS copy",
          "/consumed" in offline and "acknowledgeDownload" in offline)
    check("transfer streams into Cache Storage",
          "pipeThrough" in offline and "cache.put" in offline)
    check("no whole-file Blob buffering",
          "new Blob(chunks" not in offline and "const chunks = []" not in offline)
    check("queueing no longer blocks on a render poll",
          "waitForRender" not in offline and "POLL_TIMEOUT_MS" not in offline)

    check("the page imports the queue helpers",
          "queueDownload" in videos and "syncDownloads" in videos and "pumpDownloads" in videos)
    check("downloads are rendered in stages",
          "Preparing" in videos and "Ready to save" in videos and "On this device" in videos)
    check("default version comes from the server",
          "/api/videos/download-settings" in videos)
    check("the old source-quality chip is gone",
          'data-q="high"' not in videos and "Source</button>" not in videos)
    check("polling is throttled and visibility-aware",
          "startDownloadPolling" in videos and "visibilityState" in videos)
    check("multi-select queues many films",
          "downloadSelected" in videos and "selectAllVisible" in videos
          and "state.selected" in videos and "is-selecting" in videos)

    check("markup has the settings entry", 'id="vd-settings-btn"' in page)
    check("markup has the download version group", 'id="vd-quality-options"' in page)
    check("markup has the queue summary", 'id="vd-downloads-summary"' in page)
    check("markup has the multi-select bar",
          'id="vd-select-bar"' in page and 'id="vd-select-btn"' in page
          and 'id="vd-select-download"' in page)
    check("styles define the version choices", ".vd-choice" in css)
    check("styles define the stage labels", '.vd-dl-stage[data-tone="ok"]' in css)
    check("styles define the selection state", ".is-selecting" in css and ".vd-select-bar" in css)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
