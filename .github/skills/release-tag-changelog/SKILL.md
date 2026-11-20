---
name: release-tag-changelog
description: "Mandatory release ritual for every ethos-nas push: bump APP_VERSION, write the CHANGELOG entry from git log, publish the signed releases/index.json, then create and push the annotated tag vX.Y.Z on the released sha. Use after pushing any runtime change (feature, fix, UX, package), when asked to 'tag it', 'cut a version', 'release this', or when a push skipped its changelog. Not for Android APKs (release-android-apk) or updater internals (release-core-update)."
argument-hint: "What shipped? (one user-facing line for the release notes)"
---

# Release on every push — version, CHANGELOG, tag

Every push of **runtime** changes to `ethos-nas` ends with this ritual. A release is
not done until all four are true and on `origin`:

1. `APP_VERSION` bumped in `backend/config.py`,
2. the matching section in `CHANGELOG.md`,
3. the signed `releases/index.json` entry for that version,
4. an annotated tag `vX.Y.Z` pointing at the released sha.

Docs-only / CI-only pushes (nothing users run) keep the current version — if you skip
the ritual, say so explicitly in the report; never skip it silently.

Related: spec `docs/updates.md` · index publishing: skill `release` · updater changes:
skill `release-core-update` · Android APKs: skill `release-android-apk` (own
`versionCode` + `build.sh` — the core tag does not replace it).

## Hard rules

- **One version = one tag = one index entry = one changelog section.** Any pair
  without the others is a broken release.
- **Tag the released sha**, not the index commit: `releases/index.json` stores
  `git_sha_full` — that is the commit servers `git reset --hard` to, so it is what
  the tag must describe. (The index commit is deliberately one build ahead.)
- **Never move or reuse a published tag.** Builds are `git rev-list --count HEAD`;
  servers cache them. A mistake means a *new* version.
- **CHANGELOG is written before publishing.** Amending the code commit after
  `publish` changes its sha and breaks the index.
- Never hand-edit `releases/index.json` / `.sig`, never commit the signing key,
  never restart the service yourself (Settings → Restart or `sudo systemctl restart`).
- Publish only from a clean tree and a sha already on `origin/master`.

## 1. Gate

```bash
git status --short                  # only your files; no unrelated changes staged
git fetch origin && git status -sb  # not behind origin/master
```

Run the `verify-changes` skill first (compile, import smoke, ruff, boundaries,
logging rule, JS syntax, relevant tests). A red gate ships a broken update: the
server applies it with automatic rollback, but users still see a failed update.

## 2. Pick the version

```bash
grep -m1 '^APP_VERSION' backend/config.py
git describe --tags --abbrev=0 2>/dev/null || echo "no tags yet"
PREV="$(git describe --tags --abbrev=0 2>/dev/null || true)"
git log --no-merges --pretty='- %s (%h)' ${PREV:+"$PREV..HEAD"} | head -40
```

| Change since the last tag | Bump |
|---|---|
| breaking API/behaviour change | major |
| new user-facing capability (new tab, app, endpoint families) | minor |
| fixes, polish, docs that ship with the UI | patch |

Use `APP_VERSION` as the source of truth for the number itself (it may already be
bumped by the feature commit — then reuse it, don't bump twice).

## 3. CHANGELOG entry

`CHANGELOG.md` follows Keep a Changelog. At release time:

1. rename the top `## [Unreleased]` heading to `## [X.Y.Z] - YYYY-MM-DD`,
2. keep only sections that have content (`Added`, `Changed`, `Removed`, `Fixed`,
   `Security`),
3. add a fresh empty `## [Unreleased]` above it,
4. write bullets in **user language** ("Code Editor: bottom panel starts collapsed"),
   not raw commit subjects, and keep the short sha when it helps.

If `CHANGELOG.md` is missing, recreate it from git history — the release still needs
its section.

## 4. Commit and push the code

The bump + changelog travel with the feature commit or as their own commit; either
way they must be **pushed before publishing**:

```bash
git add backend/config.py CHANGELOG.md <files>
git commit -m "chore(release): vX.Y.Z — <one line>"   # or amend the feature commit
git push origin master
```

## 5. Publish the signed index

Release notes: one user-facing line, present tense, `Area: what changed`.

```bash
.venv/bin/python scripts/ethos-release publish --notes "Area: what the user can now do"
.venv/bin/python scripts/ethos-release verify
# OK: signature valid — vX.Y.Z · <sha> · build N · M release(s)

git add releases && git commit -m "release: vX.Y.Z" && git push origin master
```

The index commit contains **only** `releases/`. Do not "fix" the one-build offset.

## 6. Tag the released sha and push the tag

```bash
VERSION="$(grep -m1 '^APP_VERSION' backend/config.py | cut -d'"' -f2)"
CODE_SHA="$(.venv/bin/python -c "import json;print(json.load(open('releases/index.json'))['releases'][0]['git_sha_full'])")"

git tag -a "v$VERSION" -m "EthOS NAS v$VERSION" "$CODE_SHA"
git push origin "v$VERSION"
```

Annotated tag (`-a`) only — lightweight tags carry no message/release metadata.
`git describe` on `master` will then report `vX.Y.Z-1-g…` (the index commit ahead),
which is expected.

## 7. Verify the chain

```bash
git cat-file -t "v$VERSION"                    # → tag (annotated, not commit)
git show -s --format='%H %s' "v$VERSION"       # sha == index git_sha_full
git ls-remote --tags origin "v$VERSION"        # tag is on origin
grep -m1 '^APP_VERSION' backend/config.py      # == ${VERSION}
.venv/bin/python scripts/ethos-release verify
grep -m1 -A2 "## \[$VERSION\]" CHANGELOG.md # section exists, date filled
```

Report: version, tag, released sha, build number, the notes line, and whether the
user must restart the server (they must, for backend changes — via Settings → Restart).

## If something fails

| Symptom | Cause / fix |
|---|---|
| `tag 'vX.Y.Z' already exists` | `APP_VERSION` was not bumped — bump it, never move the tag |
| `sha is not on origin` | push the code commit first |
| `dirty working tree` from `publish` | commit or stash; the index must describe a real commit |
| signature invalid after `verify` | re-run `publish`; never patch the `.sig` |
| changelog has no section for the version | add it and commit **before** publishing |
| the index / APP_VERSION / tag disagree | stop, fix the version choice, publish a **new** version |
| updater files touched (`deploy/ethos-update`, `update_service.py`, `system_update.py`, Settings update card, `setup_service.sh`) | also run `release-core-update` checks |
| an Android app changed | also run `release-android-apk` (versionCode + APK + `/api/app/version`) |

## Done when

- `verify-changes` is green,
- the code commit (with `APP_VERSION` + `CHANGELOG.md`) and the `release: vX.Y.Z`
  index commit are on `origin/master`,
- `scripts/ethos-release verify` prints OK for `vX.Y.Z`,
- `git ls-remote --tags origin` lists `vX.Y.Z` and it points at the indexed sha,
- the report names the version, tag, sha, build and the one-line notes.
