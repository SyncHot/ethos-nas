# Recycle Bin (per-user Trash) — Spec & Status

Files, Photos and the desktop Trash shortcut share one recycle bin.
Each account gets its own namespace; an admin can switch the Trash view to
"all users". Nothing is stored in XDG trash (`~/.local/share/Trash`) — this is
an application trash under `ETHOS_DATA_DIR`. Verified against the code on
2026-11-13.

## File map

| What | Where |
|---|---|
| Storage, ownership, migration, retention helpers | `backend/services/trash_service.py` |
| HTTP endpoints (auth, path scope, events) | `backend/routers/files.py` — Trash section |
| Photos soft-delete (writes into the caller's bin) | `backend/routers/gallery.py` — `gallery_trash` |
| Batch soft delete | `backend/services/files_service.py` — `soft_delete_items(paths, owner)` |
| Retention sweep/usage | `backend/services/retention_service.py` → `trash_service.sweep_retention` / `.usage` |
| Startup migration | `backend/main.py` — lifespan calls `trash_service.migrate_legacy()` |
| Files Trash view (+ admin all-users toggle) | `frontend/static/apps/files.js` — `fmOpenTrash`, `fmTrashToggleScope` |
| Desktop shortcut / badge | `frontend/static/core/desktop-trash.js` (see `docs/desktop-trash.md`) |
| Tests | `tests/test_trash_service.py`, `tests/test_trash_multiuser_api.py`, `tests/test_retention.py`, `tests/test_file_station_broadcasts.py` |

## Data model

```
DATA_DIR/trash/<username>/<item>              item deleted by <username>
DATA_DIR/trash/<username>/<item>.meta.json    metadata sidecar
DATA_DIR/trash/<username>/.namespace          marker ("this dir is a namespace")
DATA_DIR/trash/_legacy/…                      pre-multi-user items (admin-only)
```

Sidecar fields (written atomically — tmp file + `os.replace`):

```jsonc
{
  "original_path": "/home/alice/docs/report.pdf",
  "trashed_name": "report.pdf",       // after collision suffixes
  "owner": "alice",
  "deleted_at": "2026-11-13T16:10:00+00:00",
  "is_dir": false,
  "size": 812345                      // bytes for files, -1 for directories
}
```

- The `.namespace` marker is what distinguishes a real user bin from a trashed
  **directory** that happens to be named like a user.
- Item names are collision-free **per user** (`report.pdf` → `report_1.pdf`),
  so two accounts can both keep a `report.pdf`.
- `_legacy` is a reserved pseudo-owner: items whose original `/home/<user>`
  could not be determined during migration. Only the admin all-users view sees
  it; admins can restore/delete those items by passing `owner=_legacy`.
- `normalize_owner()` (letters/digits/`._@+-`, max 64) prevents path traversal
  through the username; `valid_item_name()` rejects `/`, `\`, `.`, `..`,
  `.meta.json` and `.namespace`.

## API

All endpoints require a JWT. The username always comes from
`request.state.current_user` — never from the body.

| Method | Path | Scope | Notes |
|---|---|---|---|
| POST | `/api/files/trash` | own | `{path}`; source checked with `path_scope.sanitize`; broadcasts `delete_soft` |
| GET | `/api/files/trash` | own | `{items,total,limit,offset}`; `?all=1` = every namespace (admin) |
| POST | `/api/files/trash/restore` | own | `{name}`; `?owner=<user>` for admins; re-validates the stored `original_path` through `path_scope.sanitize` |
| DELETE | `/api/files/trash` | own | empties the caller's items; `?all=1` = every namespace (admin); emits a Notification Center event |
| DELETE | `/api/files/trash/item/{name}` | own | `?owner=<user>` for admins |

Isolation rules proven by `tests/test_trash_multiuser_api.py`:

- A user cannot list, restore, delete or empty another user's items (404/403).
- `?owner=` impersonation is admin-only.
- A non-admin cannot restore an item whose stored `original_path` is outside
  `/home`, `/mnt`, `/media` (this closed the old scope bypass).
- Admin `?all=1` returns items from every namespace with the `owner` field.

Restore fallback: when the original parent directory is gone, the item is
restored into `/home/<owner>` (the deleting account's home, not the daemon's);
`Path.home()` is only a last resort for `_legacy` items.

## Migration

`trash_service.migrate_legacy()` runs at startup (and lazily on first use),
idempotently:

1. Legacy flat item with `original_path` under `/home/<user>` → `<user>` namespace.
2. Anything else (unknown/missing metadata) → `_legacy` (admin-only view).
3. Sidecars are rewritten with `owner`, `deleted_at`, `is_dir`, `size`.

`_legacy` keeps old items reachable; they are removed by retention or by an
admin emptying "all users".

## Retention

`retention_service.sweep()` delegates the `trash` area to
`trash_service.sweep_retention(TRASH_RETENTION_DAYS)`: the age policy applies to
**individual items**, never to a whole user namespace (an idle user's bin must
not disappear). `usage()` reports item count and bytes across namespaces with
sidecars/markers excluded.

## Decisions & constraints

- Per-user namespaces rather than a flat directory with an owner field:
  collision-free names, cheap listing, no cross-user filename leaks, atomic
  per-user empty.
- Ownership means "who deleted it". A shared `/mnt` file trashed by alice is
  invisible to bob and only alice/admin can restore or purge it. A per-share
  shared-trash option is deliberately not implemented.
- Not XDG trash: files come from `/mnt`/`/media`, the daemon runs as a service
  user, and there is no cross-user desktop-session context. The Disk Cleaner
  still scans real XDG trash separately (admin-only).
- No hard quota yet: retention (30 days by default) is the only bound. A
  per-user cap can be layered on `sweep_retention` later.
- `empty`/`delete_item` emit a Notification Center `emit_event`, but there is
  still no `delete_hard` WS broadcast; other clients refresh on their next poll.
  The Trash view and desktop badge refresh themselves locally after their own
  actions.

## Status

- [x] Per-user storage layout + atomic sidecars
- [x] Endpoint isolation (list/restore/delete/empty) + admin `all=1`
- [x] Restore re-validates path scope; owner-home fallback
- [x] Gallery + batch soft delete write into the caller's bin
- [x] Legacy flat-trash migration (`/home/<user>` inference → `_legacy`)
- [x] Retention per item, usage excludes sidecars
- [x] Files admin "Show all users" toggle
- [x] Service + API tests
- [ ] WS broadcast for permanent deletion (optional)

## Known gaps / next steps

- Add a `delete_hard`-style broadcast after empty/item-delete so all clients
  refresh immediately.
- Optional per-user quota (`ETHOS_TRASH_MAX_BYTES` / `..._MAX_FILES`).
- Optional per-share "shared trash" view for NAS shares where users expect to
  see each other's deletions.
