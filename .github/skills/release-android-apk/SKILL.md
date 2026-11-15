---
name: release-android-apk
description: "Build and publish a Moments/Music/Video APK. Use after Android app changes or version bumps; bump versionCode, run build.sh, verify /api/app/version, check web parity."
argument-hint: "Which app changed (moments | music | video)?"
---

# Release an Android APK

## Why strict
Phones compare **only `versionCode`**, and need both the APK **and** `<apk>.update.json`.

## Procedure

### 1. Bump the version
In `ethos-<app>/app/build.gradle.kts`:
- increment `appVersionCode` (integer, strictly increasing)
- update `appVersionName` (human-readable)

### 2. Build (served channel = debug)
```bash
cd ethos-<app>
./build.sh
```
This runs `assembleDebug` + `writeUpdateManifest` and produces:
- `app/build/outputs/apk/debug/app-debug.apk`
- `app/build/outputs/apk/debug/app-debug.apk.update.json` (version code/name, sha256, size)

Do not hand-edit the manifest; do not copy the APK anywhere.

### 3. Confirm the NAS serves it
Serving paths live in `backend/config.py` → `APK_DEFAULTS` (overridable with
`ETHOS_MOMENTS_APK_PATH`, `ETHOS_MUSIC_APK_PATH`, `ETHOS_VIDEO_APK_PATH`).
- `ethos-video` also has a stable deploy dir: `apks/video/` in this repo — `build.sh` copies there.
- The build output directory **is** the served location for moments/music.

### 4. Web ⇄ Android parity (hard rule)
Every user-facing feature of the Android app must exist in the matching web app under
`frontend/static/apps/` (`music.js`, `video-station.js`, `gallery.js` for Moments).
Mirror the change in the **same task**; shared data comes from `/api/...`, never duplicated state.
Report the parity check explicitly.

### 5. Verify
```bash
curl -sf 'http://localhost:9000/api/app/version?app=<app>'
```
Expect HTTP 200 and the **new** `version_code`. Then check Settings → System: the app card shows
the live version.

### 6. Commit
Commit and push in the app repo with `feat(<scope>): …` / `fix(<scope>): …`, and in this repo if
the web side changed. Report the new `version_code`/`version_name` and which apps were rebuilt.

## Gotchas
- `release` wins over `debug` when both exist; this workflow produces the debug channel.
- `video` serves from `apks/video/` first — verify that copy was refreshed.
- 404 = APK or `.update.json` missing at the configured path.

## Done when
`/api/app/version?app=<app>` returns 200 with the bumped code, both build artifacts exist, the web
app has parity, and the app repo is pushed.
