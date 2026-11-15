# EthOS NAS

EthOS NAS is a self-hosted NAS management UI and API. It runs on Linux as a
FastAPI service and serves its web UI directly; there is no frontend build step.

## Quick install (Ubuntu Server 64-bit)

The installer supports Ubuntu Server on 64-bit ARM (including Raspberry Pi 4)
and x86_64, and requires Python 3.14 or newer. When the distribution ships an
older interpreter (Ubuntu 24.04 has 3.12), it enables the deadsnakes PPA and
installs `python3.14` before creating the venv. It checks the operating system,
architecture, existing install path, systemd service, and port before
proceeding, and grants the `ethos-nas` service account ACL access to the admin
home's non-dot entries so Files and the code editor can open it (dot
directories such as `.ssh` stay private; shares are not touched).

Review the installer before running it:

```bash
curl -fsSLo /tmp/ethos-nas-install.sh \
  https://raw.githubusercontent.com/SyncHot/ethos-nas/master/setup_scripts/install.sh &&
  less /tmp/ethos-nas-install.sh &&
  sudo bash /tmp/ethos-nas-install.sh
```

Or, for a one-line install:

```bash
bash -o pipefail -c 'curl -fsSL https://raw.githubusercontent.com/SyncHot/ethos-nas/master/setup_scripts/install.sh | sudo bash'
```

**Security note:** `curl | sudo bash` downloads code and immediately runs it as
root. Use it only if you trust this repository and its maintainers; for a more
reviewable install, download and inspect the script first. The installer asks
for confirmation, installs Ubuntu packages, clones the repository to
`/opt/ethos-nas`, creates a Python virtual environment, configures the dedicated
`ethos-nas` system account, generates a private JWT signing secret, and starts
the systemd service.

The installer does **not** format, partition, mount, or change permissions on
storage disks and shares. It also does not grant the service access to your
home directory. Configure storage only after identifying the intended mounted
path. Existing install directories, service units, secrets, or port 9000
listeners cause the installer to stop rather than overwrite or terminate them.

The one-line form always follows `master`. For a reproducible install, clone a
tagged release instead and use the checkout installer:

```bash
sudo install -d -o "$USER" -g "$(id -gn)" /opt/ethos-nas
git clone --branch <tag> https://github.com/SyncHot/ethos-nas.git /opt/ethos-nas
sudo bash /opt/ethos-nas/setup_scripts/install.sh --from-checkout
```

After installation, open the URL printed by the installer, usually
`http://<server-ip>:9000`. If you use UFW or another network firewall, allow TCP
port 9000 only on the network where you intend to use EthOS NAS. The first
eligible Linux account (UID 1000 or higher, with an interactive shell) becomes
the initial administrator when it first signs in. Make sure the intended admin
account exists before installation; other PAM-authenticated Linux accounts are
registered as regular users.

Useful service commands:

```bash
sudo systemctl status ethos-nas
sudo journalctl -u ethos-nas -f
curl -i http://localhost:9000/api/system/status  # HTTP 401 means the API is up
```

## Manual installation

Use Ubuntu Server 64-bit with Python 3.14 or newer, then:

```bash
sudo apt update
sudo apt install -y 7zip acl btrfs-progs build-essential ca-certificates cron \
  curl dosfstools exfatprogs ffmpeg git iptables libarchive-tools libffi-dev \
  libpam0g-dev libssl-dev nftables ntfs-3g openssl parted psmisc python3-dev \
  python3-venv rsync samba smartmontools sudo ufw xfsprogs zip
# Ubuntu 24.04 ships Python 3.12: install 3.14 from deadsnakes first.
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.14 python3.14-dev python3.14-venv
sudo install -d -o "$USER" -g "$(id -gn)" /opt/ethos-nas
git clone https://github.com/SyncHot/ethos-nas.git /opt/ethos-nas
cd /opt/ethos-nas
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Create a strong secret file before starting the service:

```bash
sudo install -d -o root -g root -m 0755 /etc/ethos-nas
sudo bash -c 'umask 077; printf "ETHOS_JWT_SECRET=%s\n" "$(openssl rand -hex 32)" > /etc/ethos-nas/ethos.env'
```

Then install the service. File operations run as the service user, so the
installer grants it ACL access to the admin home (non-dot entries); shares are
left untouched. Add `ETHOS_INSTALL_DOCKER=1` to install Docker Engine + Compose
v2 in the same pass (required for Packages apps and the Docker Manager):

```bash
sudo env ETHOS_SHARES= bash setup_scripts/setup_service.sh
```

Use `ETHOS_GRANT_HOME_ACL=0` if the service account must not reach your home;
Files will then return 403 for it.

The service installer always installs `/usr/local/bin/ethos-docker-install`, the
only privileged path used to provision Docker; after that the engine can also be
installed from **Docker Manager → Install Docker Engine** in the UI.

For manual installation outside `/opt`, the service installer adapts the systemd
working directory and Python executable to the current checkout path.

## Upgrade

Back up `backend/data` before upgrading. For the default installation:

```bash
cd /opt/ethos-nas
sudo tar czf "/var/backups/ethos-data-$(date +%F).tgz" backend/data
git pull --ff-only
.venv/bin/pip install -r requirements.txt
sudo systemctl restart ethos-nas
sudo journalctl -u ethos-nas -n 50 --no-pager
```

See [docs/ops.md](docs/ops.md) for backup, restore, rollback, secrets, and
troubleshooting guidance. Feature-specific dependencies and limitations are
described in the [documentation](docs/).

## Requirements

- Ubuntu Server 24.04 (x86_64 or arm64), Python 3.14+
- systemd, and the packages installed by `setup_scripts/install.sh`
- Port 9000 (and 80/443 if you use the Domains package)
- Administrator access is granted to the first interactive Linux account; all
  authentication goes through PAM

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
./start_server.sh                      # http://127.0.0.1:9001, isolated data
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests, guards and conventions.

## Security

Report vulnerabilities through GitHub's private advisory form — see
[SECURITY.md](SECURITY.md). The service user holds a small set of `NOPASSWD`
sudo rules; the full model is documented in [docs/service-user.md](docs/service-user.md).

## License

MIT — see [LICENSE](LICENSE). Third-party components and data sources are
listed in [THIRD-PARTY.md](THIRD-PARTY.md). EthOS NAS is not affiliated with,
endorsed by, or sponsored by Synology Inc. or any other storage vendor.

## Contributing

Issues and pull requests are welcome. Run the CI gate locally
(`compileall`, `ruff`, `scripts/check_*.py`, the fast test suite) before
opening a PR; it is described in [CONTRIBUTING.md](CONTRIBUTING.md).
