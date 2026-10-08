"""Natural-language-ish prompt -> per-device IOS command plans (rule based, Thai + English).

One instruction per line (or separated by ';'). Examples:
    set ip 10.0.0.1/24 on g0/1 of R1
    no shutdown g0/1 on R2
    ospf area 0 on R1 R2 network 10.0.0.0/24
    enable ospf on all devices            (networks are derived from discovered interface IPs)
    rip on R1 network 10.0.0.0
    eigrp 100 on R3 network 10.0.0.0/24
    static route 192.168.2.0/24 via 10.0.0.2 on R1
    auto address links from 10.10.0.0/16  (/30 per discovered link)
    ping 10.0.0.2 from R1
"""
from __future__ import annotations

import ipaddress
import re

from . import parsers

IP = r"\d{1,3}(?:\.\d{1,3}){3}"
IF_RE = re.compile(
    r"\b(gigabitethernet|gigabit|gig|fastethernet|ethernet|eth|loopback|vlan|serial|tengigabitethernet|tunnel|port-channel|"
    r"gi|fa|te|lo|se|tu|vl|po)\s?(\d+(?:/\d+)*(?:\.\d+)?)\b|\b([gfes])(\d+(?:/\d+)+|\d+)\b", re.I)
ALL_PORTS_RE = re.compile(r"\ball\s+(?:ports?|interfaces?|int)\b|ทุกพอร์ต|ทุกอินเทอร์เฟซ", re.I)
ALL_RE = re.compile(r"\b(all|every|everything)\b|ทุกตัว|ทั้งหมด|ทุกอุปกรณ์", re.I)


class PromptError(ValueError):
    pass


def _wild(second: str) -> str:
    """mask or wildcard (dotted) -> wildcard."""
    octs = [int(x) for x in second.split(".")]
    return second if octs[0] == 0 else ".".join(str(255 - o) for o in octs)


def _networks(text: str) -> list[tuple[str, str]]:
    """[(network, wildcard)] from 'A.B.C.D/N' or 'A.B.C.D mask|wildcard' mentions."""
    out = []
    for m in re.finditer(rf"({IP})/(\d{{1,2}})\b", text):
        net = ipaddress.ip_network(f"{m.group(1)}/{m.group(2)}", strict=False)
        out.append((str(net.network_address), str(net.hostmask)))
    spans = [m.span() for m in re.finditer(rf"({IP})/(\d{{1,2}})\b", text)]
    for m in re.finditer(rf"\b({IP})\s+({IP})\b", text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        out.append((m.group(1), _wild(m.group(2))))
    return out


def _classful(net: str) -> str:
    first = int(net.split(".")[0])
    n = 8 if first < 128 else 16 if first < 192 else 24
    return str(ipaddress.ip_network(f"{net}/{n}", strict=False).network_address)


def _iface(text: str) -> str | None:
    clean = re.sub(rf"{IP}(?:/\d+)?", " ", text)
    m = IF_RE.search(clean)
    if not m:
        return None
    return parsers.norm_if((m.group(1) or m.group(3)) + (m.group(2) or m.group(4)))


def _int_after(text: str, *words: str) -> int | None:
    m = re.search(rf"\b(?:{'|'.join(words)})\s*[:=]?\s*(\d+)\b", text, re.I)
    return int(m.group(1)) if m else None


def split_statements(text: str) -> list[str]:
    parts = []
    for line in re.split(r"[\n;]+", text):
        for p in re.split(r"\s+then\s+|\s+แล้ว\s+", line, flags=re.I):
            if p.strip():
                parts.append(p.strip())
    return parts


def _targets(stmt: str, devs: list[dict], default: list[dict]) -> list[dict]:
    if ALL_RE.search(ALL_PORTS_RE.sub(" ", stmt)):
        return list(devs)
    found = [d for d in devs if re.search(rf"(?<![\w\-]){re.escape(d['name'])}(?![\w\-])", stmt, re.I)]
    return found or default


def _derived_networks(dev: dict, topo_nodes: dict) -> list[tuple[str, str]]:
    node = topo_nodes.get(dev["name"].lower())
    nets = []
    for i in (node or {}).get("interfaces", []):
        if i.get("ip") and i.get("prefix") and not i.get("mgmt") and i["state"] != "admin_down":
            n = ipaddress.ip_network(f"{i['ip']}/{i['prefix']}", strict=False)
            nets.append((str(n.network_address), str(n.hostmask)))
    return sorted(set(nets))


class Plan:
    def __init__(self):
        self.items: dict[str, dict] = {}   # device name -> plan item (ordered)

    def add(self, dev: dict, commands: list[str], why: str, kind: str = "config") -> None:
        key = (dev["name"], kind)
        item = self.items.setdefault(f"{key[0]}|{kind}", {"device_id": dev["id"], "device": dev["name"],
                                                           "kind": kind, "commands": [], "explain": []})
        item["commands"] += commands
        item["explain"].append(why)

    def result(self) -> list[dict]:
        return list(self.items.values())


def parse(text: str, devs: list[dict], topology: dict | None = None) -> dict:
    """Return {'plans': [...], 'problems': [...]} . Raises nothing; problems are reported per statement."""
    topo_nodes = {n["name"].lower(): n for n in (topology or {}).get("nodes", [])}
    plan, problems, failed = Plan(), [], []
    last: list[dict] = []
    if not devs:
        return {"plans": [], "failed": [], "problems": ["ยังไม่มีอุปกรณ์ที่จัดการได้ — ทำ Initial Config หรือเพิ่มอุปกรณ์ก่อน"]}
    for stmt in split_statements(text):
        try:
            targets = _targets(stmt, devs, last)
            if not targets and "auto" in stmt.lower():
                targets = list(devs)
            if not targets:
                raise PromptError("ไม่พบชื่ออุปกรณ์ในคำสั่ง (ระบุเช่น 'on R1') — ชื่อที่มี: " + ", ".join(d["name"] for d in devs))
            last = targets
            _statement(stmt, targets, plan, topo_nodes, topology, devs)
        except PromptError as e:
            problems.append(f"“{stmt}” → {e}")
            failed.append(stmt)
        except ValueError as e:
            problems.append(f"“{stmt}” → ค่าที่ระบุไม่ถูกต้อง: {e}")
    return {"plans": plan.result(), "problems": problems, "failed": failed}


def _statement(stmt, targets, plan, topo_nodes, topology, all_devs):
    s = stmt.lower()
    nets = _networks(stmt)
    iface = _iface(stmt)

    # ---- exec-only commands (show / ping / traceroute)
    cmd = stmt
    for d in all_devs:
        cmd = re.sub(rf"\s*(?:\b(?:on|from|at|of)\b|บน|จาก)?\s*(?<![\w\-]){re.escape(d['name'])}(?![\w\-])\s*:?", " ", cmd, flags=re.I)
    cmd = " ".join(re.sub(r"\s*(?:\b(?:on|from|at)\b|บน|จาก)?\s*\b(?:all|every)\b(?:\s+devices?)?", " ", cmd, flags=re.I).split())
    if re.match(r"^(show|ping|traceroute)\b", cmd, re.I):
        for d in targets:
            plan.add(d, [cmd], f"run `{cmd}`", "exec")
        return

    # ---- static / default route
    if re.search(r"\bstatic\b|\broute\b|สแตติก|เส้นทาง", s) and not re.search(r"\b(ospf|rip|eigrp)\b", s):
        ips = re.findall(IP, stmt)
        via = re.search(rf"\b(?:via|next-?hop|nh|ผ่าน)\s+({IP})", stmt, re.I)
        cidr = re.search(rf"({IP})/(\d{{1,2}})", stmt)
        if re.search(r"\bdefault\b|ดีฟอลต์", s):
            dest, mask = "0.0.0.0", "0.0.0.0"
            nh = via.group(1) if via else (ips[0] if ips else None)
        elif cidr:
            n = ipaddress.ip_network(f"{cidr.group(1)}/{cidr.group(2)}", strict=False)
            dest, mask = str(n.network_address), str(n.netmask)
            rest = re.findall(IP, re.sub(rf"{IP}/\d+", " ", stmt))
            nh = via.group(1) if via else (rest[0] if rest else None)
        elif len(ips) >= 2:
            dest, mask = ips[0], ips[1]
            nh = via.group(1) if via else (ips[2] if len(ips) > 2 else None)
        else:
            raise PromptError("ต้องระบุปลายทางพร้อม mask เช่น `static route 192.168.2.0/24 via 10.0.0.2`")
        if not nh:
            raise PromptError("ต้องระบุ next-hop เช่น `via 10.0.0.2`")
        ipaddress.IPv4Address(nh)
        for d in targets:
            plan.add(d, [f"ip route {dest} {mask} {nh}"], f"static route {dest}/{parsers.mask_to_prefix(mask)} via {nh}")
        return

    # ---- dynamic routing
    proto = next((p for p in ("ospf", "eigrp", "rip") if re.search(rf"\b{p}\b", s)), None)
    if proto:
        auto = not nets
        for d in targets:
            use = nets or _derived_networks(d, topo_nodes)
            if not use:
                raise PromptError(f"{d['name']}: ไม่มี network ให้ประกาศ — ระบุ `network 10.0.0.0/24` หรือทำ Auto Discovery/กำหนด IP ก่อน")
            cmds, why = [], f"{proto.upper()} " + ("(networks from discovered interfaces)" if auto else "")
            if proto == "ospf":
                pid, area = _int_after(stmt, "process", "pid", "ospf") or 1, _int_after(stmt, "area") or 0
                cmds.append(f"router ospf {pid}")
                rid = re.search(rf"router-?id\s+({IP})", stmt, re.I)
                if rid:
                    cmds.append(f" router-id {rid.group(1)}")
                cmds += [f" network {n} {w} area {area}" for n, w in use]
            elif proto == "eigrp":
                asn = _int_after(stmt, "as", "eigrp", "asn") or 100
                cmds += [f"router eigrp {asn}"] + [f" network {n} {w}" for n, w in use] + [" no auto-summary"]
            else:
                cmds += ["router rip", " version 2"] + [f" network {n}" for n in dict.fromkeys(_classful(n) for n, _ in use)] + [" no auto-summary"]
            cmds.append("exit")
            plan.add(d, cmds, why.strip())
        return

    # ---- automatic point-to-point addressing
    if re.search(r"\bauto\b|อัตโนมัติ", s) and re.search(r"\b(address|ip)\b|ไอพี", s):
        pool = re.search(rf"({IP})/(\d{{1,2}})", stmt)
        if not pool:
            raise PromptError("ต้องระบุ pool เช่น `auto address links from 10.10.0.0/16`")
        _auto_address(ipaddress.ip_network(f"{pool.group(1)}/{pool.group(2)}", strict=False), plan, topology, all_devs)
        return

    # ---- hostname
    m = re.search(r"\bhostname\s+([A-Za-z][\w\-]*)", stmt, re.I)
    if m:
        for d in targets[:1]:
            plan.add(d, [f"hostname {m.group(1)}"], f"rename to {m.group(1)}")
        return

    # ---- interface IP
    ip_m = re.search(rf"({IP})(?:/(\d{{1,2}})|\s+({IP}))?", stmt)
    wants_down = re.search(r"\b(shut(down)?|disable|down)\b|ปิด", s) and not re.search(r"\bno\s+shut", s)
    wants_up = re.search(r"\bno\s*shut(down)?\b|\bup\b|\benable\b|เปิด|bring up", s)
    if iface and ip_m and not wants_down:
        if len(targets) != 1:
            raise PromptError("การกำหนด IP ต้องระบุอุปกรณ์เดียวต่อคำสั่ง")
        ip = ip_m.group(1)
        ipaddress.IPv4Address(ip)
        if ip_m.group(2):
            mask = parsers.prefix_to_mask(int(ip_m.group(2)))
        elif ip_m.group(3):
            mask = ip_m.group(3)
        else:
            mask = "255.255.255.255" if iface.lower().startswith("loopback") else "255.255.255.0"
        cmds = [f"interface {iface}", f" ip address {ip} {mask}", " no shutdown", "exit"]
        plan.add(targets[0], cmds, f"{iface} = {ip}/{parsers.mask_to_prefix(mask)} + no shutdown")
        return

    # ---- interface up / down
    all_ports = ALL_PORTS_RE.search(stmt)
    if (iface or all_ports) and (wants_up or wants_down):
        for d in targets:
            if all_ports and not iface:
                ports = [i["name"] for i in topo_nodes.get(d["name"].lower(), {}).get("interfaces", [])
                         if not re.match(r"(?i)(loopback|vlan|tunnel|null|port-channel)", i["name"]) and not i.get("mgmt")]
                if not ports:
                    raise PromptError(f"{d['name']}: ยังไม่รู้รายการพอร์ต — รัน Auto Discovery ก่อน")
                verb = " shutdown" if wants_down and not wants_up else " no shutdown"
                plan.add(d, [x for p in ports for x in (f"interface {p}", verb, "exit")], f"{verb.strip()} all {len(ports)} ports")
                continue
            if wants_down and not wants_up:
                plan.add(d, [f"interface {iface}", " shutdown", "exit"], f"shutdown {iface}")
            else:
                plan.add(d, [f"interface {iface}", " no shutdown", "exit"], f"no shutdown {iface}")
        return

    raise PromptError("ไม่เข้าใจคำสั่ง — ลองรูปแบบเช่น `set ip 10.0.0.1/24 on g0/1 of R1`, `ospf on all devices`, `static route 10.1.0.0/24 via 10.0.0.2 on R1`")


def _auto_address(pool, plan, topology, all_devs):
    if not topology or not topology.get("links"):
        raise PromptError("ยังไม่มี topology — รัน Auto Discovery ก่อน")
    by_name = {d["name"].lower(): d for d in all_devs}
    nodes = {n["name"].lower(): n for n in topology["nodes"]}
    subnets = pool.subnets(new_prefix=30)
    used = 0
    for link in topology["links"]:
        ends = []
        for e in (link["a"], link["b"]):
            node = nodes.get(e["node"].lower())
            port = next((i for i in (node or {}).get("interfaces", []) if i["name"] == e["port"]), None)
            ends.append((e, port))
        if any(p and p.get("ip") for _, p in ends):
            continue
        if not all(e["node"].lower() in by_name for e, _ in ends):
            continue   # can only configure managed devices
        try:
            sn = next(subnets)
        except StopIteration:
            raise PromptError("pool ไม่พอสำหรับทุกลิงก์") from None
        hosts = list(sn.hosts())
        for (e, _), host in zip(ends, hosts):
            plan.add(by_name[e["node"].lower()],
                     [f"interface {e['port']}", f" ip address {host} {sn.netmask}", " no shutdown", "exit"],
                     f"{e['port']} = {host}/30 (link {link['a']['node']}–{link['b']['node']})")
        used += 1
    if not used:
        raise PromptError("ทุกลิงก์มี IP อยู่แล้ว หรือหา link ที่กำหนดได้ไม่เจอ")
