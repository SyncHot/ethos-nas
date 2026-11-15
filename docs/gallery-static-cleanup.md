# Gallery static-analysis cleanup (T-0010)

Cleanup pass over the 14 pyright findings reported in T-0010 (original run on
7826b93). Re-ran `pyright 1.1.390` on the current file before and after:
**13 errors before → 0 errors after** (the 14th, `cur` possibly-unbound in the
rescan path, had already been fixed in 7ada366).

## Fixed in this pass

| Finding | Fix |
|---|---|
| `Image.LANCZOS` — "LANCZOS is not a known attribute of module PIL.Image" (removed from Pillow ≥10 stubs; runtime alias still present in Pillow 12.3.0 but deprecated) | `Image.Resampling.LANCZOS` in `_private_image_thumb` |
| `_eff_date` — return could be non-float (`it.get(...) or ... or 0.0` passes values through untyped); flagged at the `dstart <= _eff_date(it) < dend` comparison | `_eff_date` now coerces via `float()` and is total: always returns a float (0.0 when unknown/malformed) |
| `_day_range_local` day filter — `dend` is `float \| None` but only `dstart` was guarded | guard both: `if dstart is not None and dend is not None` |
| `_read_meta` — `meta = {"path": orig}` inferred as `dict[str, str]`, then int/float values assigned (10 × `__setitem__` errors) | annotate `meta: dict = {"path": orig}` |
| `_read_exif_date` — `sources = [exif]` inferred as `list[Exif]`, `append(sub)` (a sub-IFD dict) rejected | annotate `sources: list = [exif]` |

## Verified

- `pyright 1.1.390 backend/routers/gallery.py` → 0 errors / 0 warnings.
- Function smoke (venv python): `_eff_date` 7 cases (always float, garbage
  input falls through); `_private_image_thumb` on an encrypted PNG → WebP
  thumbnail through the `Resampling.LANCZOS` path; `_read_meta` on a plain PNG.
- Live isolated instance (port 9104): folder scan indexed 2 files,
  `GET /items` OK, `GET /items?day=<today>` OK (the guarded path),
  `GET /items?day=not-a-date` → 200 unfiltered (no crash).
- No public API or behavior change: all fixes are internal helpers / type
  annotations; the day-filter guard is behavior-identical (`_day_range_local`
  returns either both values or `None, None`).

## Waived (pre-existing, out of scope)

The pi-lens lint runners also flag ~54 **style** issues in this file (import
ordering, `Optional[X]` → `X | None`, single-line `if` statements,
`try/except: pass` without logging, `typing.List` deprecation, unused `Path`
import, …). These predate T-0010, are file-wide, and are **explicitly waived**
for this pass per the task acceptance ("fixed or explicitly waived") — fixing
them here would be an unrelated refactor of a 2000-line production file.
Tracked as a separate backlog task (style cleanup pass) instead.

The ast-grep `unchecked-throwing-call-python` rule also flags 14 `open()`/IO
calls in this file. All 14 were reviewed and **waived**: they are either
intentional raise sites whose callers handle the exception by design
(`_encrypt_file`/`_decrypt_file`/`_reencrypt_file`/`_decrypt_to_temp`/`_save_config`
— atomic write/encrypt helpers whose callers convert the failure into a 4xx/5xx),
or already wrapped in `try/except` that the single-line rule cannot see
(albums JSON loader). Wrapping them would swallow exceptions the call chain
deliberately handles.

Semgrep advisories on this file are false positives: `0o700` is the most
restrictive directory mode, and the sqlite3 calls use parameterized queries.
