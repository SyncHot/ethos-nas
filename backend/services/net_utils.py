"""Small network helpers shared by routers (no third-party deps)."""

import logging

logger = logging.getLogger("ethos-nas.services.net_utils")


def port_is_listening(port: int) -> bool:
    """True when a LISTEN socket exists on `port`, parsed from /proc/net/tcp{,6}.

    Reading /proc needs no privileges; `psutil.net_connections()` raises
    AccessDenied for the unprivileged service user.
    """
    hex_port = f"{port:04X}"
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, "r", encoding="utf-8", errors="replace") as fh:
                next(fh, None)  # header row
                for line in fh:
                    parts = line.split()
                    if len(parts) < 4 or parts[3] != "0A":  # 0A = LISTEN
                        continue
                    if parts[1].rsplit(":", 1)[-1] == hex_port:
                        return True
        except OSError:
            continue
    return False
