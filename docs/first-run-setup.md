# First-run setup ("claim this appliance")

How a freshly flashed EthOS OS box gets its admin password — without an HDMI
cable, and without a default credential that every downloaded image would share.

## The flow

```
ethos-firstboot (root, before ethos-nas)
  ├── admin account ………… locked in the image (no usable password)
  ├── random console password … single-use fallback printed on screen
  ├── claim code …………… 8 chars, no 0/O/1/I  → /etc/ethos-nas/setup.json
  └── banner ……………………… URL + setup code

browser  http://ethos-nas.local:9000
  ├── GET  /api/setup/status …… {required: true, user: "ethosadmin"}   (public)
  ├── wizard: setup code + new password (+ repeat)
  └── POST /api/setup/claim ……… code + password
        └── sudo -n /usr/local/bin/ethos-set-password
              ├── refuses unless setup.json exists without claimed_at
              ├── only for the account recorded in setup.json
              ├── chpasswd (password on stdin, never argv)
              ├── chage -d today  (the operator just chose it: not expired)
              └── rewrites setup.json with claimed_at → single use
        └── PAM login with the new password → JWT → dashboard
```

## Why a code and not just "first visitor wins"

The box is reachable at `ethos-nas.local` **before** it is claimed. Without a
secret that only someone physically at the appliance can read, the first browser
on the network could set its admin password. But the person who flashed the image
should not have to read a code off a screen they may not even have.

So the image records a **claim policy** in the state file (`claim`), set from
`ETHOS_SETUP_CLAIM` in `/etc/ethos-os/firstboot.env`:

| Policy | Who types the code | Use it when |
|---|---|---|
| `local` *(default)* | nobody on the appliance's own network; the code is required only when the request arrives through a reverse proxy (`X-Forwarded-For`/`Forwarded`) or from a public address | home/appliance behind a router — the normal case |
| `code` | everyone, always | the box is published on the internet, or you cannot trust the LAN |
| `open` | nobody, ever | a lab/VM where the first visitor is always you |

`local` keeps the anti-hijack property for the case that matters (an exposed
appliance) while making setup a two-field form for the normal case. A peer behind
a proxy looks private to the server, so proxied requests always need the code.

`/api/setup/status` returns `code_required` for the caller, and the wizard hides
the field when it is `false`.

## API

| Method | Path | Auth | Notes |
|---|---|---|---|
| `GET` | `/api/setup/status` | public | `{required, user, hostname, code_required}`; never the code |
| `POST` | `/api/setup/claim` | public | `{code, password}` → same payload as `/api/auth/login` |

Both paths are in `public_paths` in `backend/main.py` **as exact paths** — a
fresh appliance has no account, so the wizard has to work before login.

Guards on `/api/setup/claim`:

- `409` when the state file is missing or already claimed (setup happens once);
- `403` for a wrong code, `429` after 5 failures per client address in 10 min
  (success clears the counter) — the code itself is ~41 bits, the limit is about
  keeping a LAN scanner quiet;
- `400` for a password shorter than 10 characters or made of a single character
  class — checked in the router *and* in the helper;
- neither the code nor the password is ever logged; the response is a normal
  login payload, so the wizard lands the operator straight in the dashboard.

## Files

| File | Role |
|---|---|
| `deploy/ethos-set-password` | root helper — the only writer of the password DB |
| `backend/routers/setup.py` | `/api/setup/*`, rate limit, wizard backend |
| `backend/config.py` | `SETUP_STATE_FILE`, `SET_PASSWORD_HELPER`, `SET_PASSWORD_VIA_SUDO` |
| `frontend/index.html` | `#setupPanel` inside the login card |
| `setup_scripts/setup_service.sh` | installs the helper + its argument-free sudoers rule |
| `tests/test_setup_claim.py` | flow: status, claim, single use, rate limit |
| `tests/test_set_password_helper.py` | helper: refusals, stdin, marking claimed |
| `docs/first-run-setup.md` | this file |

Image side (`ethos-os-release`): `config/overlay/usr/local/sbin/ethos-firstboot`
writes the state file, and `config/overlay/etc/ethos-os/firstboot.env` controls
whether it runs (`ETHOS_FIRSTBOOT_RESET_PASSWORD=0` skips it for images
provisioned by another mechanism).

## Operating it

- **Lost the code?** `sudo rm /etc/ethos-nas/setup.json` and
  `sudo rm /etc/ethos-os/firstboot.done`, then
  `sudo systemctl restart ethos-firstboot` — a new code is printed.
- **Already set up, but the state file survived** (restored backup): the console
  password still works; claiming again just replaces it.
- **No screen at all** (cloud VM, remote rack): the credentials file
  `/etc/ethos-os/firstboot-credentials.txt` holds the same code; read it over SSH
  or from the hypervisor console.
- The state file lives in `/etc/ethos-nas/` (root:ethos-nas 0640) — the service
  can read it, nothing else can.

## Status

- [x] `/api/setup/status` + `/api/setup/claim`, public by exact path
- [x] claim policy: `local` (LAN claims without a code) / `code` / `open`
- [x] root helper with single-use state and account pinning
- [x] setup wizard in the login card (code + password + repeat)
- [x] image writes the claim code and prints it in the banner
- [x] flow and helper tests
- [ ] install-to-disk helper (flash once, install to the internal disk)
- [ ] website install page (flash instructions + published images)
