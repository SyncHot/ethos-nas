# Changelog

All notable, user-visible changes to EthOS NAS are documented here.

- Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
- Versioning: [SemVer](https://semver.org/) — one version is one `APP_VERSION` in
  `backend/config.py`, one git tag `v<APP_VERSION>` and one entry in the signed
  `releases/index.json`.
- The release ritual (bump → changelog → publish → tag) is mandatory for every push
  of runtime changes: skill `release-tag-changelog`.

## [Unreleased]

## [2.3.14] - 2026-11-20

### Fixed

- Files: video thumbnails are generated reliably again without hammering the CPU.
- Files: only one video stream runs at a time — thumbnail work yields to active
  playback, so playback no longer stutters.

## [2.3.13] - 2026-11-18

### Added

- Packages: **Update all** — every pending update runs sequentially, each with its
  own rollback, instead of one failed package blocking the rest.

Earlier releases were not maintained as a changelog; the notes for each version
live in `releases/index.json` and the full history in `git log`.
