"""Provider registry for the Downloads engine.

One place that describes every download provider the engine can use:

- **torrent providers** (Real-Debrid, AllDebrid, TorBox) cache a magnet and
  expose direct file links;
- **link providers** (AllDebrid, tb7.pl) turn a premium file-hoster URL into a
  direct link.

The router, config masking, `/api/downloads/verify`, `/api/downloads/providers`
and the package UI all read this registry instead of carrying hardcoded
provider/key lists. Adding a provider means adding one `ProviderSpec` here (and
a client module when it speaks its own protocol).

Providers are configured in two independent slots (`debrid.provider` for
torrents, `debrid.link_provider` for hoster links), so a torrent debrid and a
multihoster can be active at the same time.

The existing Real-Debrid / AllDebrid / TorBox clients live in
`backend/services/debrid.py`; tb7.pl lives in `backend/services/tb7.py`.
"""

import dataclasses
import logging
import urllib.parse

from backend.services import debrid, tb7

logger = logging.getLogger("ethos-nas.services.download_providers")

CAP_TORRENTS = "torrents"
CAP_LINK_UNLOCK = "link_unlock"

HOSTER_MATCH_NONE = "none"
HOSTER_MATCH_ANY = "any"
HOSTER_MATCH_LIST = "list"

SLOT_TORRENT = "torrent"
SLOT_LINK = "link"


@dataclasses.dataclass(frozen=True)
class CredentialField:
    """One credential input (stored under `debrid.<name>` in the config)."""

    name: str
    label: str
    secret: bool = True
    kind: str = "text"          # "text" | "password"
    placeholder: str = ""


@dataclasses.dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    kind: str                   # "debrid" | "multihoster"
    homepage: str
    credentials: tuple
    capabilities: frozenset
    hoster_match: str = HOSTER_MATCH_NONE
    description: str = ""


_AD_KEY = CredentialField("alldebrid_api_key", "API key", secret=True, kind="password")

_PROVIDERS = (
    ProviderSpec(
        id="realdebrid",
        label="Real-Debrid",
        kind="debrid",
        homepage="https://real-debrid.com",
        credentials=(CredentialField("realdebrid_api_token", "API token", secret=True, kind="password"),),
        capabilities=frozenset({CAP_TORRENTS}),
        description="Torrents and magnets; cached files are streamed to the NAS.",
    ),
    ProviderSpec(
        id="alldebrid",
        label="AllDebrid",
        kind="debrid",
        homepage="https://alldebrid.com",
        credentials=(_AD_KEY,),
        capabilities=frozenset({CAP_TORRENTS, CAP_LINK_UNLOCK}),
        hoster_match=HOSTER_MATCH_ANY,
        description="Torrents, magnets and premium hoster links.",
    ),
    ProviderSpec(
        id="torbox",
        label="TorBox",
        kind="debrid",
        homepage="https://torbox.app",
        credentials=(CredentialField("torbox_api_key", "API key", secret=True, kind="password"),),
        capabilities=frozenset({CAP_TORRENTS}),
        description="Torrents and magnets (all files are downloaded).",
    ),
    ProviderSpec(
        id="tb7",
        label="tb7.pl",
        kind="multihoster",
        homepage="https://tb7.pl",
        credentials=(
            CredentialField("tb7_username", "Username", secret=False, kind="text"),
            CredentialField("tb7_password", "Password", secret=True, kind="password"),
        ),
        capabilities=frozenset({CAP_LINK_UNLOCK}),
        hoster_match=HOSTER_MATCH_LIST,
        description="Polish multihoster: premium links for the hosters it lists (10 GB/24 h).",
    ),
)

PROVIDERS = {spec.id: spec for spec in _PROVIDERS}


# ── Metadata ─────────────────────────────────────────────────────────────────

def all_specs() -> tuple:
    """Every provider in display order."""
    return _PROVIDERS


def get(provider: str):
    """ProviderSpec or None."""
    return PROVIDERS.get(str(provider or "").strip())


def provider_ids() -> tuple:
    return tuple(spec.id for spec in _PROVIDERS)


def ids_by_capability(capability: str) -> tuple:
    return tuple(spec.id for spec in _PROVIDERS if capability in spec.capabilities)


def supports(provider: str, capability: str) -> bool:
    spec = get(provider)
    return bool(spec and capability in spec.capabilities)


def is_torrent_provider(provider: str) -> bool:
    return supports(provider, CAP_TORRENTS)


def is_link_provider(provider: str) -> bool:
    return supports(provider, CAP_LINK_UNLOCK)


def eligible_slots(provider: str) -> list:
    slots = []
    if is_torrent_provider(provider):
        slots.append(SLOT_TORRENT)
    if is_link_provider(provider):
        slots.append(SLOT_LINK)
    return slots


def credential_fields(provider: str) -> tuple:
    spec = get(provider)
    return spec.credentials if spec else ()


# ── Credentials ──────────────────────────────────────────────────────────────

def provider_credentials(cfg: dict, provider: str) -> dict:
    """Credential values for `provider` from the downloads config."""
    spec = get(provider)
    db = (cfg or {}).get("debrid") or {}
    if not spec or not isinstance(db, dict):
        return {}
    return {f.name: str(db.get(f.name) or "").strip() for f in spec.credentials}


def credentials_complete(provider: str, creds: dict) -> bool:
    """True when every declared credential field has a value."""
    fields = credential_fields(provider)
    return bool(fields) and all(str((creds or {}).get(f.name) or "").strip() for f in fields)


def primary_secret(provider: str, creds: dict) -> str:
    """First secret credential (the legacy single-key providers' key)."""
    for field in credential_fields(provider):
        if field.secret:
            return str((creds or {}).get(field.name) or "").strip()
    return ""


def credentials_from_secret(provider: str, secret: str) -> dict:
    """Build a credentials dict from one secret (legacy `_debrid_key` tasks).

    Only works for providers with exactly one credential field; a multihoster
    that also needs a username has to be re-read from the config.
    """
    fields = credential_fields(provider)
    if len(fields) == 1 and fields[0].secret:
        return {fields[0].name: str(secret or "").strip()}
    return {}


def mask_credentials(db: dict) -> dict:
    """Copy of the `debrid` config block with every secret value masked."""
    out = dict(db) if isinstance(db, dict) else {}
    for spec in _PROVIDERS:
        for field in spec.credentials:
            if not field.secret:
                continue
            value = out.get(field.name)
            if isinstance(value, str) and value:
                out[field.name] = ("****" + value[-4:]) if len(value) > 6 else "****"
    return out


def merge_credential_values(current: dict, incoming: dict) -> dict:
    """Merge a config POST into the stored block, keeping masked/absent secrets."""
    out = dict(current) if isinstance(current, dict) else {}
    for spec in _PROVIDERS:
        for field in spec.credentials:
            if field.name not in (incoming or {}):
                continue
            value = incoming.get(field.name)
            if not isinstance(value, str):
                continue
            value = value.strip()
            if field.secret and "****" in value:
                continue
            out[field.name] = value
    return out


# ── Slot resolution ──────────────────────────────────────────────────────────

def torrent_provider(cfg: dict) -> tuple:
    """(provider_id, credentials) for the torrent slot; ("", {}) when unset."""
    name = str(((cfg or {}).get("debrid") or {}).get("provider") or "").strip()
    if not is_torrent_provider(name):
        return "", {}
    return name, provider_credentials(cfg, name)


def link_provider(cfg: dict) -> tuple:
    """(provider_id, credentials) for the hoster-link slot; ("", {}) when unset."""
    name = str(((cfg or {}).get("debrid") or {}).get("link_provider") or "").strip()
    if not is_link_provider(name):
        return "", {}
    return name, provider_credentials(cfg, name)


# Premium file hosters serve an HTML landing page, not the file itself: such a
# link can only be downloaded through a link provider. Used to refuse routing
# them to the plain HTTP engine (which would save the web page).
KNOWN_HOSTERS = (
    "rapidgator.net", "nitroflare.com", "turbobit.net", "hitfile.net",
    "katfile.com", "1fichier.com", "ddownload.com", "filefactory.com",
    "uploadgig.com", "k2s.cc", "keep2share.cc", "filejoker.net", "mega.nz",
)


# ── Hoster routing ───────────────────────────────────────────────────────────

def _url_host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""


def host_matches(provider: str, url: str) -> bool:
    """True when `provider` can unlock links from this URL's host."""
    spec = get(provider)
    if not spec or CAP_LINK_UNLOCK not in spec.capabilities:
        return False
    host = _url_host(url)
    if not host:
        return False
    if spec.hoster_match == HOSTER_MATCH_ANY:
        return True
    if spec.hoster_match != HOSTER_MATCH_LIST:
        return False
    for known in hosters(provider):
        if host == known or host.endswith("." + known):
            return True
    return False


def hosters(provider: str) -> list:
    """Hoster domains a provider supports ([] when it accepts any host)."""
    if provider == "tb7":
        return tb7.hosters()
    return []


def is_known_hoster(url: str) -> bool:
    """True for a premium file-host landing page (never a direct file URL)."""
    host = _url_host(url)
    if not host:
        return False
    return any(host == known or host.endswith("." + known) for known in KNOWN_HOSTERS)


def configured_link_providers(cfg: dict) -> list:
    """Every link provider with complete credentials, primary slot first."""
    preferred = str(((cfg or {}).get("debrid") or {}).get("link_provider") or "").strip()
    found = []
    for spec in _PROVIDERS:
        if CAP_LINK_UNLOCK not in spec.capabilities:
            continue
        creds = provider_credentials(cfg, spec.id)
        if credentials_complete(spec.id, creds):
            found.append((spec.id, creds))
    # Stable sort: the configured slot stays first, the rest keep registry order.
    found.sort(key=lambda pair: 0 if pair[0] == preferred else 1)
    return found


def link_candidates(cfg: dict, url: str) -> list:
    """Configured link providers that can unlock `url`, primary slot first.

    Every configured provider is a candidate, not only the one in
    `debrid.link_provider`: a second account (AllDebrid unlocks any host) takes
    over when the primary one does not list the hoster or its unlock fails.
    """
    return [(pid, creds) for pid, creds in configured_link_providers(cfg)
            if host_matches(pid, url)]


# ── Dispatch ─────────────────────────────────────────────────────────────────

def resolve_link(provider: str, creds: dict, url: str, stop=None) -> dict:
    """Resolve a premium hoster URL into a direct link.

    Returns `{url, name, size, headers}`; `headers` is optional request metadata
    (Referer/Cookie) that must be sent with the download, and only for hosts
    that actually need it.
    """
    if provider == "alldebrid":
        key = str((creds or {}).get("alldebrid_api_key") or "").strip()
        info = debrid.ad_link_info(key, url)
        link = debrid.ad_unlock(key, url, stop)
        return {
            "url": str(link.get("url") or ""),
            "name": str(link.get("name") or info.get("name") or ""),
            "size": int(link.get("size") or info.get("size") or 0),
            "headers": {},
        }
    if provider == "tb7":
        return tb7.resolve_link(creds, url, stop)
    raise RuntimeError(f"Provider {provider!r} does not support hoster links")


def verify(provider: str, creds: dict) -> dict:
    """Probe credentials. Returns `{ok, account?}`.

    Provider-side rejections raise `ProviderHTTPError(401, …)` so the router can
    keep mapping them to HTTP 401; network/API failures propagate as 502.
    """
    spec = get(provider)
    if not spec:
        return {"ok": False}
    if provider == "tb7":
        return tb7.verify(creds)
    ok = debrid.verify(provider, primary_secret(provider, creds))
    return {"ok": bool(ok)}


def account_info(provider: str, creds: dict) -> dict:
    """Live account summary (tb7.pl only today); {} for other providers."""
    if provider == "tb7":
        return tb7.account_info(creds)
    return {}


def cached_account(provider: str, creds: dict) -> dict:
    """Last known account summary without a network call."""
    if provider == "tb7":
        return tb7.cached_account(creds)
    return {}
