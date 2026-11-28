"""Compose interpolation variables for one installed package.

The signed compose file never changes; per-install values reach it as `${VAR}`
environment variables when the core runs `docker compose`. Two sources exist:

* manifest `settings` — user-supplied values stored in `settings_values`
  (passwords included; never log this dict);
* manifest `data` — the host folder chosen for the service's data, exposed as
  `${ETHOS_PACKAGE_DATA_DIR}`. Its default is `packages/data/<id>`.

Keeping the mapping here means install, update, settings, health probes and the
uninstall cleanup all interpolate the same values without importing the
installer (which imports the cleanup module itself).
"""

import logging
import os
from typing import Optional

from backend.config import settings
from backend.services import gpu_host, packages_store

logger = logging.getLogger("ethos-nas.services.package_env")


def compose_env(pkg_id: Optional[str] = None, record: Optional[dict] = None) -> dict[str, str]:
    """Environment for `docker compose`, including the EthOS service identity.

    Compose interpolates `${VAR}` from this environment, so a manifest can run its
    container with the same uid/gid as EthOS (`PUID: "${ETHOS_SERVICE_UID}"`) and
    files it creates on /mnt or /media stay editable by the NAS and its accounts.
    A package's settings and data folder are added too, so `${GF_SECURITY_ADMIN_PASSWORD}`
    and `${ETHOS_PACKAGE_DATA_DIR}` in the signed compose file resolve for *every*
    compose call — the installer, the health probes, the Docker Manager and cleanup.

    Without this, `docker compose down` for a package that binds its data folder
    dies with `invalid spec: :/var/lib/grafana: empty section between colons`.
    """
    env = dict(os.environ)
    env.setdefault("ETHOS_SERVICE_UID", str(os.getuid()))
    env.setdefault("ETHOS_SERVICE_GID", str(os.getgid()))
    env.setdefault("ETHOS_DATA_DIR", str(settings.DATA_DIR))
    # GPU facts a signed compose file may reference with `${VAR:-}`: the render
    # node's group ids (a container must join them to open /dev/dri) and two
    # booleans. Missing hardware simply exports 0, so the package still starts.
    try:
        env.update(gpu_host.compose_env())
    except Exception:  # noqa: BLE001 — compose must not fail on a probe error
        logger.warning("GPU probe failed while building the compose environment", exc_info=True)
    if record is None and pkg_id:
        record = packages_store.get_package(pkg_id)
    if record:
        env.update(compose_vars(record))
    return env


def compose_vars(record: Optional[dict]) -> dict[str, str]:
    """Settings + data folder of a package as compose interpolation variables."""
    if not record:
        return {}
    values = record.get("settings_values") or {}
    env: dict[str, str] = {}
    for definition in record.get("settings") or []:
        if not isinstance(definition, dict):
            continue
        key = str(definition.get("key") or "")
        name = str(definition.get("env") or "")
        if not key or not name:
            continue
        value = values.get(key)
        if value is None or value == "":
            value = definition.get("default") or ""
        env[name] = str(value)
    if record.get("data"):
        pkg_id = str(record.get("id") or "")
        chosen = str(record.get("data_dir") or "")
        if not chosen and pkg_id:
            try:
                # The same default the installer records: the admin's root from
                # Settings → Packages (built-in <DATA_DIR>/packages/data).
                chosen = str(packages_store.data_root() / pkg_id)
            except Exception:
                logger.warning("Cannot resolve the default data folder for %s", pkg_id, exc_info=True)
        if chosen:
            env["ETHOS_PACKAGE_DATA_DIR"] = chosen
    return env
