"""Parsers for Cisco IOS / IOS-XE show-command output (pure functions, unit-tested)."""
from __future__ import annotations

import re

_IF_PREFIX = [
    ("tengigabitethernet", "TenGigabitEthernet"), ("gigabitethernet", "GigabitEthernet"),
    ("fastethernet", "FastEthernet"), ("port-channel", "Port-channel"),
    ("loopback", "Loopback"), ("ethernet", "Ethernet"), ("serial", "Serial"),
    ("tunnel", "Tunnel"), ("vlan", "Vlan"), ("mgmt", "mgmt"),
    ("gigabit", "GigabitEthernet"),
    ("gig", "GigabitEthernet"), ("te", "TenGigabitEthernet"), ("gi", "GigabitEthernet"),
    ("fa", "FastEthernet"), ("eth", "Ethernet"), ("et", "Ethernet"), ("lo", "Loopback"),
    ("se", "Serial"), ("tu", "Tunnel"), ("vl", "Vlan"), ("po", "Port-channel"),
    ("g", "GigabitEthernet"), ("f", "FastEthernet"), ("e", "Ethernet"), ("s", "Serial"),
]
_SHORT = {"GigabitEthernet": "Gi", "FastEthernet": "Fa", "Ethernet": "Et", "TenGigabitEthernet": "Te",
          "Loopback": "Lo", "Serial": "Se", "Vlan": "Vl", "Port-channel": "Po", "Tunnel": "Tu"}


def norm_if(name: str) -> str:
    """'g0/1' / 'Gi0/1' / 'gigabitethernet 0/1' -> 'GigabitEthernet0/1'."""
    m = re.match(r"^\s*([A-Za-z][A-Za-z\-]*)\s*([\d/.:]+)\s*$", name or "")
    if not m:
        return (name or "").strip()
    word, num = m.group(1).lower(), m.group(2)
    for prefix, full in _IF_PREFIX:
        if word == prefix:
            return full + num
    return (name or "").strip()


def short_if(name: str) -> str:
    full = norm_if(name)
    m = re.match(r"^([A-Za-z\-]+)([\d/.:]+)$", full)
    if not m:
        return full
    return _SHORT.get(m.group(1), m.group(1)) + m.group(2)


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def base_hostname(name: str) -> str:
    """'R1.lab.local' -> 'R1' (CDP/LLDP report FQDNs; a '(serial)' suffix is removed)."""
    name = re.sub(r"\(.*?\)$", "", (name or "").strip())
    if re.match(r"^\d+(\.\d+){3}$", name):
        return name
    return name.split(".")[0]


def prefix_to_mask(prefix: int) -> str:
    prefix = int(prefix)
    bits = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix else 0
    return ".".join(str((bits >> s) & 255) for s in (24, 16, 8, 0))


def mask_to_prefix(mask: str) -> int:
    return sum(bin(int(o)).count("1") for o in mask.split("."))


def interface_state(status: str, protocol: str) -> str:
    s, p = (status or "").lower(), (protocol or "").lower()
    if s.startswith("administratively"):
        return "admin_down"
    return "up" if s == "up" and p == "up" else "down"


_BRIEF = re.compile(
    r"^(\S+)\s+(\S+)\s+(?:YES|NO)\s+\S+\s+(administratively down|up|down|deleted|\S+)\s+(up|down|\S+)\s*$", re.I)


def parse_ip_int_brief(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = _BRIEF.match(line.rstrip())
        if not m or m.group(1).lower() == "interface":
            continue
        name, ip, status, proto = m.groups()
        out.append({"name": norm_if(name), "short": short_if(name),
                    "ip": None if ip.lower() == "unassigned" else ip, "prefix": None,
                    "status": status.lower(), "protocol": proto.lower(),
                    "state": interface_state(status, proto)})
    return out


def parse_ip_interface_prefixes(text: str) -> dict[str, int]:
    """`show ip interface | include line protocol|Internet address` -> {ifname: prefixlen}."""
    res, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^(\S+) is .+?, line protocol is \S+", line)
        if m:
            cur = norm_if(m.group(1))
            continue
        m = re.search(r"Internet address is (\d+\.\d+\.\d+\.\d+)/(\d+)", line)
        if m and cur:
            res[cur] = int(m.group(2))
    return res


def parse_interface_traffic(text: str) -> list[dict]:
    """Parse the useful counters/rates from Cisco ``show interfaces`` output."""
    headers = list(re.finditer(r"(?m)^([^\s]+) is (?:administratively down|up|down), line protocol is \S+", text))
    out = []
    for idx, match in enumerate(headers):
        block = text[match.start(): headers[idx + 1].start() if idx + 1 < len(headers) else len(text)]
        def number(pattern):
            found = re.search(pattern, block, re.I)
            return int(found.group(1)) if found else 0
        out.append({
            "name": norm_if(match.group(1)), "short": short_if(match.group(1)),
            "input_packets": number(r"(\d+) packets input"),
            "input_bytes": number(r"\d+ packets input,\s*(\d+) bytes"),
            "output_packets": number(r"(\d+) packets output"),
            "output_bytes": number(r"\d+ packets output,\s*(\d+) bytes"),
            "input_errors": number(r"(\d+) input errors"),
            "output_errors": number(r"(\d+) output errors"),
            "input_rate_bps": number(r"5 minute input rate (\d+) bits/sec"),
            "output_rate_bps": number(r"5 minute output rate (\d+) bits/sec"),
        })
    return out


def parse_cdp_detail(text: str) -> list[dict]:
    res = []
    for block in re.split(r"-{10,}", text):
        dev = re.search(r"Device ID:\s*(\S+)", block)
        if not dev:
            continue
        ip = re.search(r"IP(?:v4)? [Aa]ddress:\s*(\d+\.\d+\.\d+\.\d+)", block)
        plat = re.search(r"Platform:\s*(.*?),\s*Capabilities:\s*(.*)", block)
        ifs = re.search(r"Interface:\s*([^,\s]+),\s*Port ID \(outgoing port\):\s*(\S+)", block)
        if not ifs:
            continue
        res.append({"protocol": "cdp", "device_id": dev.group(1), "name": base_hostname(dev.group(1)),
                    "ip": ip.group(1) if ip else None,
                    "platform": (plat.group(1).strip() if plat else ""),
                    "capabilities": (plat.group(2).split() if plat else []),
                    "local_if": norm_if(ifs.group(1)), "remote_if": norm_if(ifs.group(2))})
    return res


def parse_lldp_detail(text: str) -> list[dict]:
    res = []
    for block in re.split(r"-{20,}", text):
        loc = re.search(r"Local Intf:\s*(\S+)", block)
        if not loc:
            continue
        port = re.search(r"Port id:\s*(\S+)", block)
        desc = re.search(r"Port Description:\s*(\S+)", block)
        rport = port.group(1) if port else ""
        if re.match(r"^[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}$", rport, re.I) and desc:
            rport = desc.group(1)
        name = re.search(r"System Name:\s*(\S+)", block)
        ip = re.search(r"(?:IP|IPv4):\s*(\d+\.\d+\.\d+\.\d+)", block)
        caps = re.search(r"System Capabilities:\s*(.*)", block)
        if not name:
            continue
        res.append({"protocol": "lldp", "device_id": name.group(1), "name": base_hostname(name.group(1)),
                    "ip": ip.group(1) if ip else None, "platform": "",
                    "capabilities": [c.strip() for c in caps.group(1).split(",")] if caps else [],
                    "local_if": norm_if(loc.group(1)), "remote_if": norm_if(rport)})
    return res


def parse_version(text: str) -> dict:
    info = {"hostname": None, "version": None, "model": None, "serial": None, "uptime": None, "kind": "router"}
    m = re.search(r"^(\S+) uptime is (.+)$", text, re.M)
    if m:
        info["hostname"], info["uptime"] = m.group(1), m.group(2).strip()
    m = re.search(r"Version\s+([0-9][^\s,]*)", text)
    if m:
        info["version"] = m.group(1)
    m = (re.search(r"Model [Nn]umber\s*:\s*(\S+)", text)
         or re.search(r"\((I86BI_[A-Z0-9_\-]+|vios[\w\-]*|VIOS[\w\-]*)\)", text)
         or re.search(r"^[Cc]isco\s+(\S+)\s+\(.*?\)\s+(?:processor|with)", text, re.M)
         or re.search(r"[Ss]oftware \(([^)]+)\)", text))
    if m:
        info["model"] = m.group(1)
    m = re.search(r"Processor board ID\s+(\S+)", text)
    if m:
        info["serial"] = m.group(1)
    blob = (text[:2000] + " " + (info["model"] or "")).lower()
    if re.search(r"linuxl2|iosvl2|vios_l2|vios-l2|ws-c|catalyst|l2-|switch", blob):
        info["kind"] = "switch"
    return info


def parse_ping(text: str) -> dict:
    m = re.search(r"Success rate is (\d+) percent(?: \((\d+)/(\d+)\))?", text)
    rtt = re.search(r"min/avg/max\s*=\s*(\d+)/(\d+)/(\d+)", text)
    if not m:
        return {"ok": False, "percent": 0, "sent": 0, "received": 0, "avg_ms": None, "raw": text}
    pct = int(m.group(1))
    return {"ok": pct > 0, "percent": pct, "sent": int(m.group(3) or 0), "received": int(m.group(2) or 0),
            "avg_ms": int(rtt.group(2)) if rtt else None, "raw": text}


_SYSLOG = re.compile(r"^\*?[A-Za-z]{3}\s+\d+\s+[\d:.]+:?\s+%[A-Z0-9_\-]+-\d-[A-Z0-9_\-]+:|^\*?\d+:\s.*%[A-Z0-9_\-]+-\d-[A-Z0-9_\-]+:")


def strip_syslog(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if not _SYSLOG.match(l.strip()))


_ERROR_RE = re.compile(
    r"^% (Invalid input|Incomplete command|Ambiguous command|Unknown command|Bad IP|Invalid|Cannot|Wrong|"
    r"Unrecognized|Access denied|Command rejected|.*overlaps)", re.I)


def find_errors(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if _ERROR_RE.match(l.strip())]
