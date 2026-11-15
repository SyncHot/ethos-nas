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

The public repository is created from a **single clean commit** — the existing
private history (runtime data, API keys, tokens) is never published.

1. Prepare and verify the working tree (see "Checklist" below).
2. Create an empty public repository `SyncHot/ethos-nas`.
3. From a clean checkout of the private repo:

   ```bash
   git checkout --orphan public
   git rm -r --cached . >/dev/null
   rm -rf docs/archive ETHOS_LOOP_STATUS.md
   git add -A
   git commit -m "Initial public release"
   git remote add public https://github.com/SyncHot/ethos-nas.git
   git push public public:master
   ```

4. Tag the release (`git tag -a v2.0.0 -m "EthOS NAS 2.0.0"` + push the tag).
5. GitHub settings: branch protection on `master` (required CI), secret
   scanning + push protection, private vulnerability reporting, Dependabot.
6. Archive the old private repository on GitHub so the URL redirects readers to
   the public one.

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
