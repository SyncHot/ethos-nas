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
from typing import Optional

from backend.services import packages_store

logger = logging.getLogger("ethos-nas.services.package_env")


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
                logger.warning("Cannot resolve the default data folder for %s", pkg_id,
                               exc_info=True)
        if chosen:
            env["ETHOS_PACKAGE_DATA_DIR"] = chosen
    return env
