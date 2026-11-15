# Publishing EthOS NAS (open-source release notes)

Status: in progress (see `TODO.md` P5-11).

## What is public

- The application source: `backend/`, `frontend/`, `scripts/`, `deploy/`, `setup_scripts/`, `tests/`.
- Operator documentation: `docs/*.md` (except `docs/archive/`).
- License: MIT (`LICENSE`). Third-party notices: `THIRD-PARTY.md`.

## What is deliberately kept private (the upstream archive)

- `docs/archive/**` — historical audits, incident logs and superseded plans.
  These contain personal paths and out-of-date security notes; they stay in the
  private archive repository.
- `ETHOS_LOOP_STATUS.md` — agent working state.
- `backend/data/**`, `backend/users.json`, `backend/routers/users.json`,
  `apks/**`, `.test_token`, `.env` — runtime state and credentials (gitignored;
  also absent from the public history by construction).

## Trust anchors

- The **package signing key** (`repo.key`) never enters any repository. Only the
  public half is committed (`repo.pub`, `backend/config.py: PACKAGES_DEFAULT_SOURCE_KEY`).
- The **core update index** (when the updater lands) is signed with the same
  offline key; the public half is installed to `/etc/ethos-nas/update.conf` by
  `setup_scripts/setup_service.sh`.

## Publishing procedure

The public repository was created from a **single clean commit** — the private
history (runtime data, API keys, tokens) is never published. Status: done
2026-11-15, `SyncHot/ethos-nas`, tag `v2.0.0`, MIT, secret scanning and push
protection enabled. The JWT secret was rotated on the production host
(`systemd` drop-in `10-jwt-secret.conf`) so the token that leaked into the old
history can no longer be used.

The two repositories have **unrelated histories**. Keep the private checkout as
the working repo and publish each release as one additional snapshot commit:

```bash
# 1. build the publishable tree from the current HEAD (tracked files only)
rm -rf /tmp/ethos-pub && git clone -q --depth 1 https://github.com/SyncHot/ethos-nas /tmp/ethos-pub
cd /path/to/private/ethos-nas
find /tmp/ethos-pub -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
git archive HEAD | tar -x -C /tmp/ethos-pub            # snapshot of HEAD
rm -rf /tmp/ethos-pub/docs/archive /tmp/ethos-pub/ETHOS_LOOP_STATUS.md

# 2. verify, then commit on top of the public history (never force-push)
cd /tmp/ethos-pub
git status --short                                     # review what is new
git add -A
git commit -m "Release 2.1.0"                          # or a plain sync commit
git tag -a v2.1.0 -m "EthOS NAS 2.1.0" && git push origin master --follow-tags
```

`git archive HEAD` only ships tracked files, so `backend/data/`, `users.json`,
`apks/`, `.env` and the venv can never leak this way; the `rm` step drops the
archive directory. Do **not** run `git push --force` on the public repo.

## First-release record (what was done)

1. `git archive HEAD` from the private repo into a temporary directory.
2. `rm -rf docs/archive ETHOS_LOOP_STATUS.md`.
3. Scans: no `backend/data/`, `users.json`, `.test_token`, `.env`, `*.pyc`,
   no `/home/<user>`, no `192.168.x` LAN defaults, no private-key/token patterns.
4. `git init -b master` + one commit authored as `SyncHot
   <SyncHot@users.noreply.github.com>` (the maintainer's real address is not
   published).
5. `gh repo create ethos-nas --public --source . --remote public --push`.
6. `git tag -a v2.0.0` + push; topics added; secret scanning, push protection
   and private vulnerability reporting enabled.

Remaining manual steps: branch protection on `master` (requires the `ci` check),
and archiving the old private repository **only after** every server checkout
has been repointed — the private repo is still the push target for day-to-day
work.


## Checklist before the first public push

- [ ] `git ls-files | grep -E '^backend/data/|users\.json$|\.test_token|^\.env$|\.pyc$'` → empty
- [ ] `grep -rE '/home/[a-z]+|192\.168\.[0-9]+\.' --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data .` → nothing personal
- [ ] `gitleaks detect --source . --no-git` → clean
- [ ] `LICENSE`, `SECURITY.md`, `CONTRIBUTING.md`, `THIRD-PARTY.md`, `.env.example` present
- [ ] `run_tests.py` green with a non-maintainer `ETHOS_TEST_USER`
- [ ] CI green on a fork-style clone (no secrets required)
- [ ] `install.sh` runs end-to-end in a disposable Ubuntu 24.04 VM

## Credentials to rotate regardless of the publish strategy

The private history contains an admin API key, a JWT and an old test password.
Rotate them even though the new repository excludes history:

```bash
sudo bash setup_scripts/rotate_jwt_secret.sh --apply   # new JWT secret + restart
# Settings → Security → API Key: generate a new key
# change the Linux account password if it was ever the test password
```
