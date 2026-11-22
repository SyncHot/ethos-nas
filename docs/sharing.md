# Feature: Share with other users (view-only grants)

Status: **in progress** — core layer + Files/Photos/Video/Music/Books integrations.

## Problem

EthOS is multi-user, but every app's data is private by construction:

- per-user stores live in `<DATA_DIR>/users/<name>/…`, so ownership is implicit;
- `path_scope` limits non-admins to their own home plus `/mnt` and `/media`;
- `GET /api/users` is admin-only, so no UI can offer a user picker;
- notifications are global — every account sees every event.

Users want to share a photo album, a video folder, a playlist or a book without copying
files and without creating a public token link. This feature adds **explicit, per-recipient,
read-only grants** on top of the existing isolation rules.

## Decisions

| Decision | Value |
|---|---|
| Permission | View/stream only. Recipients cannot write, rename, delete or re-share. |
| Recipients | Individual active accounts + an “All users” flag (future accounts included). |
| Groups | Not in v1 (see `TODO.md` F-03). The grant shape allows `groups: []` later. |
| Resource types | `path` (file/folder), `album` (Photos), `playlist` (Music). |
| Shareable roots | `/home`, `/mnt`, `/media` only, even for admins. `<DATA_DIR>` is never shareable. |
| Private Photos | Encrypted/private items are excluded from shared albums. |
| Notifications | Targeted events: only recipients see “Shared with you”. |
| Discovery | Central **Sharing** app, Settings → Sharing, and a per-app shared section/actions. |

## Data model

Store: `<DATA_DIR>/sharing.json` via `services/json_store.py` (`version: 1`).

```json
{
  "version": 1,
  "grants": [
    {
      "id": "shr_1a2b3c4d5e6f",
      "owner": "alice",
      "kind": "path",
      "resource": "/home/alice/Photos/Holiday",
      "label": "Holiday photos",
      "recipients": ["bob", "carol"],
      "all": false,
      "created_at": "2026-11-22T10:00:00+00:00",
      "updated_at": "2026-11-22T10:00:00+00:00"
    }
  ]
}
```

- One grant per `(owner, kind, resource)`; editing replaces the recipient list.
- `all: true` ignores `recipients` and covers every active account.
- `label` is resolved server-side from the resource (album name, playlist name, basename).

## Enforcement

- `services/path_scope.py`
  - `sanitize()` / `sanitize_for()` stay own-scope only — **writes are never share-aware**.
  - `sanitize_read()` = own scope **or** a valid received grant. Used only by read-only
    endpoints (`GET` file listing/raw/download/thumbnail, video streaming/transcoding,
    gallery ad-hoc scans).
  - `sanitize_share()` validates a grant target: `/home`, `/mnt`, `/media` only; never
    `<DATA_DIR>`; non-admins must still own the path.
  - `can_write()` answers “own scope only” so shared folders render read-only in the UI.
- `services/sharing_service.py`
  - grant CRUD, recipient/owner validation, caches (generation-bumped on change).
  - `allows_read(username, real_path)` checks path containment or resolved album/playlist
    paths, owner still active, and owner still allowed the path.
  - purge helper for account deletion.
- `services/sharing_resources.py` resolves resource → label + paths by reading the
  per-user stores directly (services never import routers):
  - `album` → `<DATA_DIR>/users/<owner>/albums.json` `paths`.
  - `playlist` → `<DATA_DIR>/users/<owner>/music_library.json` local track files.
  - `path` → the path itself (folder containment).

## API

Mounted at `/api/sharing` (`backend/routers/sharing.py`), every route needs a JWT.

| Method | Path | Notes |
|---|---|---|
| GET | `/users` | Active usernames + role, no uid/home/api_key. Available to every user. |
| GET | `/incoming` | Grants where the caller is a recipient (or `all`). Optional `?kind=`. |
| GET | `/outgoing` | Grants created by the caller. |
| GET | `/resource?kind=&resource=` | Current grant for one of the caller's resources (dialog prefill). |
| POST | `/grants` | `{kind, resource, recipients, all, label?}`; validates ownership. |
| PATCH | `/grants/{id}` | Update recipients/all (owner only). |
| DELETE | `/grants/{id}` | Revoke (owner only). |

Creating/updating a grant emits a targeted notification with `source="sharing"` and the
new recipients in `users`; `all` uses a broadcast event.

## App integration

| App | Resource | Read path |
|---|---|---|
| Files | any file/folder | `GET /api/files` + `/raw` + `/thumbnail` via `sanitize_read` |
| Photos | album | `GET /api/gallery/albums?owner=` and `/items?owner=&album=` |
| Video Station | library root / single video | `/api/videos/library?path=`, `/stream`, `/transcode` via `sanitize_read` |
| Music | playlist | `GET /api/music/playlists/shared?owner=&id=` rewrites local `file_url` to `/api/files/raw?path=` |
| Books | folder / individual book | shared roots in the Books service; bytes via `/api/files/raw` |

UI surfaces:

- **Sharing** desktop app (core): “Shared with me” / “Shared by me”.
- **Settings → Sharing** (core): the same tables for every account.
- **Files**: “Share with users…” context action, “Shared with me” sidebar section.
- **Photos/Video/Music/Books**: share actions in their own settings/rows and a
  “Shared with me” section. Native packages call `ctx.share.open(...)`.

## File map

Backend:
- `services/sharing_service.py`, `services/sharing_resources.py` (new)
- `services/path_scope.py`, `services/notifications_service.py`
- `routers/sharing.py` (new), `routers/notifications.py`, `routers/notifications_ws.py`
- `routers/files.py`, `routers/gallery.py`, `routers/video_station.py`, `routers/music.py`
- `services/user_accounts.py` (purge), `main.py` (mount)

Frontend:
- `core/share-dialog.js`, `core/sharing-ui.js` (new)
- `apps/sharing.js` (new), `core/registry.js`, `core/app-icons.js`
- `apps/settings.js`, `apps/files.js`, `app.js` (`ctx.share`)

Packages: `ethos-photos`, `ethos-video`, `ethos-music`, `ethos-books` (sibling repo).

## Status

- [x] Spec and decisions
- [x] Targeted notification audience (`users`, per-user read state, WS filtering)
- [x] Sharing service + resource resolvers
- [x] `path_scope.sanitize_read` / `sanitize_share` / `can_write`
- [x] `/api/sharing` router + account purge
- [x] Read-only endpoint opt-in (Files, Video, Gallery scans)
- [x] Core UI: share dialog, Sharing app, Settings section, Files integration
- [x] Photos owner-scoped albums
- [x] Video Station package
- [x] Music shared playlists
- [x] Books shared roots
- [x] Core tests (`test_sharing_api`, `test_path_scope_sharing`, gallery/video/music,
      notification targeting) and static guards
- [x] Browser verification: Sharing app, dialog, Files sidebar, Photos shared albums,
      Video shared folders, Music shared playlists, Books shared-roots service
- [ ] Package publish — blocked by the unrelated `ethos-sticky-notes` manifest in the
      package repo; publish after the 2.14.0 core release
- [ ] Android parity for shared albums/playlists (Moments/Music follow-up)

## Verification

- `tests/test_sharing_api.py` — directory, grants, incoming/outgoing, all-users,
  revoke, disabled accounts, DATA_DIR rejection, ownership checks.
- `tests/test_path_scope_sharing.py` — `sanitize_read` vs `sanitize`, no write
  amplification, static guard that write routes never use the read variant.
- `tests/test_gallery_sharing.py`, `tests/test_video_sharing.py`,
  `tests/test_music_sharing.py` — app-specific read paths.
- Notification tests — targeted delivery, per-user unread, WS filtering.
- `tests/test_app_ui.py`, `tests/test_notification_ui.py` — UI guards.
- Two-user manual pass on an isolated instance; revoke must return 403 immediately.

## Known limits

- No write permissions, no groups, no re-sharing by recipients.
- Shared media outside `/home|/mnt|/media` is impossible by design.
- Android Moments/Music do not show shared resources until a parity follow-up.
- Sharing “My List” from Video Station is a follow-up; v1 shares roots and files.
- The package releases for Photos/Video/Music/Books are prepared (`min_ethos_version`
  2.14.0) but not published; the sibling repo's signed index cannot be rebuilt until
  the unrelated `ethos-sticky-notes` package is valid again.
