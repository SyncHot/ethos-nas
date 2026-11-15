"""Network camera discovery — ONVIF WS-Discovery + subnet port scan.

Pure standard library (socket/subprocess/threading). No root required:
  - ONVIF probe: UDP multicast to 239.255.255.250:3702 (WS-Discovery "Probe"
    for dn:Device); compliant IP cameras reply with their XAddr endpoint.
  - Subnet scan: threaded TCP connect against common camera ports on the local
    network; port 554 hits are confirmed as RTSP servers via an OPTIONS request.

Everything is best-effort and never raises out of `discover()` — a failed probe
or unreachable host simply yields no result for it.
"""

import ipaddress
import logging
import re
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

logger = logging.getLogger("ethos-nas.services.camera_discovery")

ONVIF_MULTICAST_GROUP = "239.255.255.250"
ONVIF_PORT = 3702
CAMERA_TCP_PORTS = (554, 80, 8080)   # RTSP, HTTP web UI / MJPEG
RTSP_CONFIRM_TIMEOUT_S = 1.0

_PROBE_XML = """<?xml version="1.0" encoding="utf-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:tns="http://docs.oasis-open.org/wsn/b-2">
  <env:Header>
    <wsa:MessageID>uuid:{mid}</wsa:MessageID>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:addressing</wsa:To>
    <wsa:ReplyTo><wsa:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/anonymous</wsa:Address></wsa:ReplyTo>
  </env:Header>
  <env:Body>
    <tns:Probe>
      <tns:Types>dn:Device</tns:Types>
    </tns:Probe>
  </env:Body>
</env:Envelope>"""

_XADDR_RE = re.compile(rb"<XAddrs[^>]*>(.*?)</XAddrs>", re.DOTALL | re.IGNORECASE)


# ── Local network detection ─────────────────────────────────────────────────

def local_ip() -> str:
    """Best-effort local IPv4 (UDP connect trick — sends no packets)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_subnet() -> tuple[str, int]:
    """(network_address, prefix) of the default interface; falls back to /24."""
    ip = local_ip()
    try:
        out = subprocess.run(["ip", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=5).stdout
        dev_m = re.search(r"dev (\S+)", out)
        if dev_m:
            addr_out = subprocess.run(
                ["ip", "-4", "addr", "show", "dev", dev_m.group(1)],
                capture_output=True, text=True, timeout=5).stdout
            inet_m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/(\d+)", addr_out)
            if inet_m:
                return inet_m.group(1), int(inet_m.group(2))
    except (OSError, subprocess.SubprocessError):
        pass  # intentional: a failure here must not break the caller
    try:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        return str(net.network_address), net.prefixlen
    except ValueError:
        return "192.168.1.0", 24


def subnet_ips(window: int = 300) -> tuple[list[str], str]:
    """Host IPs of the local subnet to scan (network/broadcast excluded).

    Subnets larger than `window` hosts (e.g. a /16) are reduced to a bounded
    window around the local IP so discovery stays fast. Returns (ips, label).
    """
    ip_str, prefix = local_subnet()
    net = ipaddress.ip_network(f"{ip_str}/{prefix}", strict=False)
    hosts = [str(h) for h in net.hosts()]
    if len(hosts) > window:
        base = int(ipaddress.ip_address(ip_str))
        lo = max(int(net.network_address), base - window // 2)
        hi = min(int(net.broadcast_address), base + window)
        hosts = [str(ipaddress.ip_address(i)) for i in range(lo, hi + 1)]
    return hosts, f"{net.network_address}/{prefix}"


# ── ONVIF WS-Discovery ──────────────────────────────────────────────────────

def parse_onvif_response(data: bytes) -> Optional[str]:
    """Extract the first XAddr from a ProbeMatch datagram; None if not one."""
    m = _XADDR_RE.search(data)
    if not m:
        return None
    xaddr = m.group(1).decode(errors="replace").strip()
    # Cameras sometimes list several endpoints separated by whitespace.
    first = re.split(r"[\s,]+", xaddr)[0].rstrip("/")
    return first or None


def onvif_probe(timeout_s: float = 3.0) -> dict[str, str]:
    """Send an ONVIF Probe and collect replies until the deadline.

    Returns {source_ip: xaddr}. Never raises — multicast setup failures yield {}.
    """
    found: dict[str, str] = {}
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass  # intentional: a failure here must not break the caller
        sock.bind(("", ONVIF_PORT))
        mreq = socket.inet_aton(ONVIF_MULTICAST_GROUP) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

        probe = _PROBE_XML.format(mid=uuid.uuid4()).encode()
        sock.sendto(probe, (ONVIF_MULTICAST_GROUP, ONVIF_PORT))
        # A second burst ~1 s later catches slow cameras.
        deadline = time.monotonic() + timeout_s
        resent = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(min(remaining, 0.5))
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                if not resent and timeout_s >= 2:
                    sock.sendto(probe, (ONVIF_MULTICAST_GROUP, ONVIF_PORT))
                    resent = True
                continue
            xaddr = parse_onvif_response(data)
            if xaddr:
                found[addr[0]] = xaddr
    except OSError as exc:
        logger.warning("ONVIF probe unavailable: %s", exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass  # best-effort cleanup — nothing to report
    return found


# ── Subnet TCP scan + RTSP confirmation ─────────────────────────────────────

def _tcp_open(ip: str, port: int, timeout_s: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def rtsp_confirmed(ip: str, port: int = 554, timeout_s: float = RTSP_CONFIRM_TIMEOUT_S) -> bool:
    """True if the host answers an RTSP OPTIONS with an RTSP status line."""
    try:
        with socket.create_connection((ip, port), timeout=timeout_s) as s:
            req = (f"OPTIONS rtsp://{ip}/ RTSP/1.0\r\nCSeq: 1\r\n"
                   f"User-Agent: EthOS-NAS\r\n\r\n").encode()
            s.sendall(req)
            head = s.recv(256).decode(errors="replace").split("\r\n", 1)[0]
        return head.startswith("RTSP/")
    except OSError:
        return False


def scan_ips(ips: list[str], ports: tuple[int, ...] = CAMERA_TCP_PORTS,
             rtsp_ports: tuple[int, ...] = (554,), timeout_s: float = 0.35) -> dict[str, dict]:
    """Threaded TCP connect scan. Returns {ip: {"ports": [...], "rtsp": bool}}.

    Open ports in `rtsp_ports` are confirmed as RTSP servers via OPTIONS.
    """
    open_ports: dict[str, set[int]] = {}
    with ThreadPoolExecutor(max_workers=64) as pool:
        futs = {pool.submit(_tcp_open, ip, port, timeout_s): (ip, port) for ip in ips for port in ports}
        for fut in as_completed(futs):
            ip, port = futs[fut]
            if fut.result():
                open_ports.setdefault(ip, set()).add(port)

    results: dict[str, dict] = {}
    for ip, ports_open in open_ports.items():
        entry = {"ports": sorted(ports_open), "rtsp": False}
        for p in rtsp_ports:
            if p in ports_open and rtsp_confirmed(ip, port=p):
                entry["rtsp"] = True
                break
        results[ip] = entry
    return results


# ── Combined discovery ──────────────────────────────────────────────────────

def _suggested_url(ip: str, onvif: bool, xaddr: Optional[str], ports: list[int]) -> str:
    if 554 in ports or (onvif and not xaddr):
        return f"rtsp://{ip}:554/"
    if onvif and xaddr:
        # ONVIF endpoint found; RTSP port unknown — offer the common default.
        return f"rtsp://{ip}:554/"
    for p in ports:
        if p in (80, 8080):
            return f"http://{ip}:{p}/" if p != 80 else f"http://{ip}/"
    return f"rtsp://{ip}:554/"


def discover(timeout_s: float = 3.0, scan_subnets: bool = True) -> dict:
    """Run ONVIF probe (+ optional subnet scan). Never raises."""
    t0 = time.monotonic()
    onvif: dict[str, str] = {}
    try:
        onvif = onvif_probe(timeout_s)
    except Exception as exc:  # defensive — probe already guards itself
        logger.warning("ONVIF probe failed: %s", exc)

    scanned_network = None
    scan_hosts = 0
    tcp: dict[str, dict] = {}
    if scan_subnets:
        try:
            ips, label = subnet_ips()
            scanned_network = label
            scan_hosts = len(ips)
            tcp = scan_ips(ips)
        except Exception as exc:
            logger.warning("Subnet scan failed: %s", exc)

    all_ips = sorted(set(onvif) | set(tcp), key=lambda i: tuple(map(int, i.split("."))))
    results = []
    for ip in all_ips:
        ports = tcp.get(ip, {}).get("ports", [])
        rtsp = bool(tcp.get(ip, {}).get("rtsp"))
        xaddr = onvif.get(ip)
        results.append({
            "ip": ip,
            "onvif": ip in onvif,
            "xaddr": xaddr,
            "rtsp": rtsp,
            "ports": ports,
            "suggested_url": _suggested_url(ip, ip in onvif, xaddr, ports),
        })

    return {
        "results": results,
        "scanned": {"network": scanned_network, "hosts": scan_hosts},
        "onvif_replies": len(onvif),
        "duration_s": round(time.monotonic() - t0, 2),
    }
