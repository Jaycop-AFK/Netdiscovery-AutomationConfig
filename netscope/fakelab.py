"""Fake Cisco-IOS lab for developing/testing NetScope WITHOUT EVE-NG.

Starts 4 emulated devices (R1, R2, R3 routers + SW1 switch), each with
  * a telnet "console"  : 127.0.0.1:2301..2304   (starts unconfigured, shows the IOS initial-config dialog)
  * an SSH server       : 127.0.0.11..14 : 22     (accepts logins only after the initial config created a user + RSA keys)
Cabling:  R1 Gi0/1 - R2 Gi0/1 | R2 Gi0/2 - R3 Gi0/1 | R3 Gi0/2 - SW1 Gi0/1 | R1 Gi0/2 - R3 Gi0/3
Supports enough IOS (interfaces, ip address, shutdown, ospf/rip/eigrp, static routes, cdp/lldp, ping) to exercise every
NetScope feature, including a small routing model so `ping` only succeeds once routing is configured.

    python tools/fake_lab.py
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time

import paramiko

IFACES = ["GigabitEthernet0/0", "GigabitEthernet0/1", "GigabitEthernet0/2", "GigabitEthernet0/3"]
WIRES = [("R1", "GigabitEthernet0/1", "R2", "GigabitEthernet0/1"), ("R2", "GigabitEthernet0/2", "R3", "GigabitEthernet0/1"),
         ("R3", "GigabitEthernet0/2", "SW1", "GigabitEthernet0/1"), ("R1", "GigabitEthernet0/2", "R3", "GigabitEthernet0/3")]
_CP, _IP = int(os.getenv("NETSCOPE_DEMO_CONSOLE_BASE", "2301")), int(os.getenv("NETSCOPE_DEMO_IP_BASE", "11"))   # overridable only to avoid port clashes
SPEC = [("R1", "router", _CP, f"127.0.0.{_IP}"), ("R2", "router", _CP + 1, f"127.0.0.{_IP + 1}"),
        ("R3", "router", _CP + 2, f"127.0.0.{_IP + 2}"), ("SW1", "switch", _CP + 3, f"127.0.0.{_IP + 3}")]
DEVICES: dict[str, "Dev"] = {}          # keyed by the *factory* name (R1..), hostname may change after config
LOCK = threading.RLock()


class Iface:
    def __init__(self, name):
        self.name, self.ip, self.mask, self.shut = name, None, None, True

    @property
    def net(self):
        return ipaddress.ip_network(f"{self.ip}/{self.mask}", strict=False) if self.ip else None


class Dev:
    def __init__(self, key, kind, console_port, mgmt_ip):
        self.key, self.kind, self.console_port, self.listen_ip = key, kind, console_port, mgmt_ip
        self.hostname = "Router" if kind == "router" else "Switch"
        self.domain = None
        self.users: dict[str, str] = {}
        self.enable = None
        self.keys = False
        self.fresh = True                   # initial config dialog not answered yet
        self.cdp = True
        self.lldp = False
        self.ifaces = {n: Iface(n) for n in IFACES}
        self.ospf: dict[int, list] = {}
        self.eigrp: dict[int, list] = {}
        self.rip: list = []
        self.statics: list = []
        self.started = time.time()

    @property
    def mgmt_ip(self):
        return self.ifaces["GigabitEthernet0/0"].ip

    def iface(self, name):
        if name not in self.ifaces:
            if name.lower().startswith("loopback"):
                self.ifaces[name] = Iface(name)
                self.ifaces[name].shut = False
            else:
                return None
        return self.ifaces[name]

    def peer(self, ifname):
        for a, ai, b, bi in WIRES:
            if a == self.key and ai == ifname:
                return DEVICES[b], bi
            if b == self.key and bi == ifname:
                return DEVICES[a], ai
        return None

    def oper(self, i: Iface):
        if i.shut:
            return "administratively down", "down"
        if i.name == "GigabitEthernet0/0" or i.name.lower().startswith("loopback"):
            return "up", "up"
        p = self.peer(i.name)
        if p and not p[0].ifaces[p[1]].shut:
            return "up", "up"
        return "down", "down"

    def is_up(self, i):
        return self.oper(i)[0] == "up"


# ------------------------------------------------------------------ routing model (for ping)
def covered(dev: Dev, proto, arg, i: Iface) -> bool:
    if not i.ip:
        return False
    nets = {"ospf": dev.ospf.get(arg, []), "eigrp": dev.eigrp.get(arg, []), "rip": dev.rip}[proto]
    for n, w in nets:
        try:
            wild = ipaddress.ip_address(w)
            net = ipaddress.ip_network(f"{n}/{ipaddress.ip_address(int(wild) ^ 0xFFFFFFFF)}", strict=False) if w else None
        except ValueError:
            continue
        if net and ipaddress.ip_address(i.ip) in net:
            return True
    return False


def owner_of(ip):
    for d in DEVICES.values():
        for i in d.ifaces.values():
            if i.ip == ip and d.is_up(i):
                return d
    return None


def adjacent(dev: Dev, i: Iface):
    """Neighbor device across interface i if the link is up and both ends are in the same subnet."""
    p = dev.peer(i.name)
    if not p or not i.ip or not dev.is_up(i):
        return None
    pd, pi = p
    j = pd.ifaces[pi]
    if j.ip and pd.is_up(j) and j.net == i.net:
        return pd, j
    return None


def routes(dev: Dev):
    """RIB: list of (network, next_device, egress_iface). next_device None => connected/local."""
    rib = []
    for i in dev.ifaces.values():
        if i.ip and dev.is_up(i):
            rib.append((i.net, None, i))
    for net, mask, nh in dev.statics:
        try:
            n = ipaddress.ip_network(f"{net}/{mask}", strict=False)
        except ValueError:
            continue
        for i in dev.ifaces.values():
            if i.ip and dev.is_up(i) and ipaddress.ip_address(nh) in i.net:
                tgt = owner_of(nh)
                rib.append((n, tgt, i))
    for proto, table in (("ospf", dev.ospf), ("eigrp", dev.eigrp), ("rip", {0: dev.rip} if dev.rip else {})):
        for arg in table:
            seen, frontier = {dev.key}, [(dev, None, None)]
            while frontier:
                cur, first_nh, first_if = frontier.pop(0)
                for i in cur.ifaces.values():
                    if not covered(cur, proto, arg if proto != "rip" else 0, i):
                        continue
                    if cur is not dev and i.net is not None:
                        rib.append((i.net, first_nh, first_if))
                    adj = adjacent(cur, i)
                    if adj and adj[0].key not in seen and any(
                            covered(adj[0], proto, a2, adj[1]) for a2 in ({"ospf": adj[0].ospf, "eigrp": adj[0].eigrp, "rip": {0: 1}}[proto])):
                        seen.add(adj[0].key)
                        frontier.append((adj[0], first_nh or adj[0], first_if or i))
    return rib


def lookup(dev: Dev, ip):
    addr = ipaddress.ip_address(ip)
    best = None
    for net, nxt, egress in routes(dev):
        if addr in net and (best is None or net.prefixlen > best[0].prefixlen):
            best = (net, nxt, egress)
    return best


def forward(dev: Dev, ip, ttl=16):
    """Returns the egress interface of the first hop if ip is reachable from dev, else None."""
    if ttl == 0:
        return None
    for i in dev.ifaces.values():
        if i.ip == ip and dev.is_up(i):
            return i
    r = lookup(dev, ip)
    if not r:
        return None
    _, nxt, egress = r
    if nxt is None:
        adj = adjacent(dev, egress)
        if adj and adj[1].ip == ip:
            return egress
        return None
    return egress if forward(nxt, ip, ttl - 1) else None


def ping_ok(dev: Dev, ip, source=None):
    egress = forward(dev, ip)
    if not egress:
        return False
    src_ip = source or egress.ip
    target = owner_of(ip)
    return bool(target and (target is dev or forward(target, src_ip)))


# ------------------------------------------------------------------ CLI engine
class Cli:
    def __init__(self, dev: Dev, write, authed_user=None, console=False):
        self.d, self.w, self.mode, self.cur, self.console = dev, write, "user", None, console
        self.priv = False
        self.pending = None      # callable(answer) for interactive questions
        self.user = authed_user

    def out(self, text=""):
        self.w((text + "\r\n").replace("\r\r", "\r"))

    @property
    def prompt(self):
        h = self.d.hostname
        return {"user": f"{h}>", "priv": f"{h}#", "config": f"{h}(config)#", "if": f"{h}(config-if)#",
                "router": f"{h}(config-router)#", "line": f"{h}(config-line)#"}[self.mode]

    def banner(self):
        d = self.d
        if self.console and d.fresh:
            self.w("\r\n         --- System Configuration Dialog ---\r\n\r\nWould you like to enter the initial configuration dialog? [yes/no]: ")
            self.pending = self._initial_dialog
        else:
            self.w(self.prompt)

    def _initial_dialog(self, ans):
        self.d.fresh = False
        self.out("\r\nPress RETURN to get started!")
        self.pending = lambda a: (setattr(self, "pending", None), setattr(self, "mode", "user"), self.w(self.prompt))
        self.mode = "user"

    def feed(self, line: str):
        if self.pending:
            fn, self.pending = self.pending, None
            fn(line.strip())
            return
        self.handle(line.strip())
        if not self.pending:
            self.w(self.prompt)

    # ---- dispatch
    def handle(self, line):
        if not line:
            return
        d, t = self.d, line.split()
        low = line.lower()
        bad = lambda: self.out("% Invalid input detected at '^' marker.")   # noqa: E731
        if t[0].lower() in ("exit", "end", "quit") or low == "logout":
            if self.mode == "user" or (self.mode == "priv" and t[0].lower() != "end"):
                self.mode = "user" if self.mode == "priv" else self.mode
                return
            if t[0].lower() == "end":
                self.mode = "priv"
            else:
                self.mode = {"if": "config", "router": "config", "line": "config", "config": "priv"}.get(self.mode, "priv")
            return
        if self.mode == "user":
            if low.startswith(("en", )) and len(t) == 1:
                if d.enable:
                    self.w("Password: ")
                    self.pending = lambda a: (self.setmode("priv") if a == d.enable else self.out("% Access denied"), self.w(self.prompt))
                    self.pending_noprompt = True
                    return
                self.mode = "priv"
                return
            if low.startswith("terminal"):
                return
            return self.show_or_ping(line) if t[0].lower() in ("show", "sh", "ping") else bad()
        if self.mode == "priv":
            if low.startswith(("conf",)):
                self.out("Enter configuration commands, one per line.  End with CNTL/Z.")
                self.mode = "config"
                return
            if low.startswith(("terminal", "disable")):
                if low.startswith("disable"):
                    self.mode = "user"
                return
            if low.startswith(("write", "wr", "copy run")):
                self.out("Building configuration...\r\n[OK]")
                return
            return self.show_or_ping(line) if t[0].lower() in ("show", "sh", "ping", "traceroute") else bad()
        return self.config(line, t, low, bad)

    def setmode(self, m):
        self.mode = m
        self.pending = None

    # ---- show / ping
    def show_or_ping(self, line):
        d = self.d
        t, low = line.split(), line.lower()
        if t[0].lower() == "ping":
            return self.ping(t)
        if low.startswith("show version") or low.startswith("sh ver"):
            up = int((time.time() - d.started) // 60)
            if d.kind == "router":
                self.out(f"Cisco IOS Software, IOSv Software (VIOS-ADVENTERPRISEK9-M), Version 15.6(2)T, RELEASE SOFTWARE (fc2)")
            else:
                self.out(f"Cisco IOS Software, vios_l2 Software (vios_l2-ADVENTERPRISEK9-M), Version 15.2(4.0.55)E, EARLY DEPLOYMENT")
            self.out(f"{d.hostname} uptime is {up} minutes\r\nProcessor board ID 9{abs(hash(d.key)) % 10**7:07d}")
            return
        if "ip interface brief" in low or "ip int br" in low:
            self.out(f"{'Interface':<27}{'IP-Address':<16}OK? Method Status                Protocol")
            for i in sorted(d.ifaces.values(), key=lambda x: x.name):
                st, pr = d.oper(i)
                self.out(f"{i.name:<27}{(i.ip or 'unassigned'):<16}YES {'manual' if i.ip else 'unset ':<6} {st:<21} {pr}")
            return
        if "ip interface" in low:
            for i in sorted(d.ifaces.values(), key=lambda x: x.name):
                st, pr = d.oper(i)
                self.out(f"{i.name} is {st}, line protocol is {pr}")
                if i.ip:
                    self.out(f"  Internet address is {i.ip}/{i.net.prefixlen}")
                else:
                    self.out("  Internet protocol processing disabled")
            return
        if "cdp neighbors detail" in low or "cdp neighbor detail" in low:
            if not d.cdp:
                return self.out("% CDP is not enabled")
            for i in sorted(d.ifaces.values(), key=lambda x: x.name):
                p = d.peer(i.name)
                if not p or not d.is_up(i) or not p[0].cdp:
                    continue
                pd, pi = p
                self.out("-------------------------")
                self.out(f"Device ID: {pd.hostname}.{pd.domain or ''}".rstrip("."))
                self.out(f"Entry address(es): \r\n  IP address: {pd.mgmt_ip}" if pd.mgmt_ip else "Entry address(es): ")
                caps = "Router Source-Route-Bridge" if pd.kind == "router" else "Switch IGMP"
                self.out(f"Platform: Cisco {'IOSv' if pd.kind == 'router' else ''},  Capabilities: {caps} ")
                self.out(f"Interface: {i.name},  Port ID (outgoing port): {pi}")
                self.out("Holdtime : 150 sec\r\n")
            return
        if "lldp neighbors detail" in low:
            if not d.lldp:
                return self.out("% LLDP is not enabled")
            for i in sorted(d.ifaces.values(), key=lambda x: x.name):
                p = d.peer(i.name)
                if p and d.is_up(i) and p[0].lldp:
                    self.out("------------------------------------------------")
                    self.out(f"Local Intf: {i.name.replace('GigabitEthernet', 'Gi')}\r\nPort id: {p[1].replace('GigabitEthernet', 'Gi')}\r\nSystem Name: {p[0].hostname}")
                    self.out(f"System Capabilities: B,R\r\nManagement Addresses:\r\n    IP: {p[0].mgmt_ip}")
            return
        if "running-config" in low or low.startswith("sh run"):
            self.out(f"hostname {d.hostname}")
            for i in d.ifaces.values():
                self.out(f"interface {i.name}\r\n ip address {i.ip} {i.mask}" if i.ip else f"interface {i.name}")
                self.out(" shutdown" if i.shut else " no shutdown")
            return
        if "ip route" in low:
            self.out("Codes: C - connected, S - static, O - OSPF, D - EIGRP, R - RIP")
            for net, nxt, eg in routes(d):
                self.out(f"{'C' if nxt is None and eg.net == net else 'R'}   {net} via {eg.name}")
            return
        self.out("% Invalid input detected at '^' marker.")

    def ping(self, t):
        ip = t[1] if len(t) > 1 else None
        if not ip or not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            return self.out("% Unrecognized host or address, or protocol not running.")
        count = int(t[t.index("repeat") + 1]) if "repeat" in t else 5
        src = None
        if "source" in t:
            nm = t[t.index("source") + 1]
            si = self.d.ifaces.get(nm)
            src = si.ip if si else nm
        ok = ping_ok(self.d, ip, src)
        self.out(f"Type escape sequence to abort.\r\nSending {count}, 100-byte ICMP Echos to {ip}, timeout is 2 seconds:")
        time.sleep(0.15)
        if ok:
            self.out("!" * count)
            self.out(f"Success rate is 100 percent ({count}/{count}), round-trip min/avg/max = 1/2/4 ms")
        else:
            self.out("." * count)
            self.out(f"Success rate is 0 percent (0/{count})")

    # ---- configuration
    def config(self, line, t, low, bad):
        d = self.d
        neg = low.startswith("no ")
        tt = t[1:] if neg else t
        if self.mode == "config":
            if t[0] == "hostname" and len(t) == 2:
                d.hostname = t[1]
            elif low.startswith("ip domain-name"):
                d.domain = t[2]
            elif low.startswith(("no ip domain", "ip domain-lookup")):
                pass
            elif low.startswith("enable secret"):
                d.enable = t[-1]
            elif low.startswith("username") and "secret" in t:
                d.users[t[1]] = t[-1]
            elif low.startswith("interface") and len(t) >= 2:
                nm = re.sub(r"^(gigabitethernet|gi)\s*", "GigabitEthernet", "".join(t[1:]), flags=re.I)
                nm = re.sub(r"^lo(opback)?", "Loopback", nm, flags=re.I)
                i = d.iface(nm)
                if not i:
                    return bad()
                self.cur, self.mode = i, "if"
            elif low.startswith("crypto key generate"):
                if not d.domain or d.hostname in ("Router", "Switch"):
                    return self.out("% Please define a hostname other than Router.")
                if d.keys:
                    self.out("% You already have RSA keys defined named %s.%s.\r\n%% Do you really want to replace them? [yes/no]: " % (d.hostname, d.domain))
                    self.pending = lambda a: (self.out(f"The name for the keys will be: {d.hostname}.{d.domain}\r\n% The key modulus size is 2048 bits\r\n% Generating 2048 bit RSA keys, keys will be non-exportable...\r\n[OK]"), self.w(self.prompt))
                    return
                time.sleep(1.0)
                d.keys = True
                self.out(f"The name for the keys will be: {d.hostname}.{d.domain}\r\n% The key modulus size is 2048 bits\r\n% Generating 2048 bit RSA keys, keys will be non-exportable...\r\n[OK] (elapsed time was 1 seconds)")
            elif low.startswith(("ip ssh", "cdp run", "lldp run", "no cdp run", "no lldp run")):
                if low.endswith("cdp run"):
                    d.cdp = not neg
                if low.endswith("lldp run"):
                    d.lldp = not neg
            elif low.startswith("line "):
                self.mode = "line"
            elif low.startswith("no router "):
                if t[2] == "ospf" and len(t) > 3:
                    d.ospf.pop(int(t[3]), None)
                elif t[2] == "eigrp" and len(t) > 3:
                    d.eigrp.pop(int(t[3]), None)
                elif t[2] == "rip":
                    d.rip.clear()
            elif low.startswith("router "):
                kind = t[1]
                if kind == "ospf":
                    d.ospf.setdefault(int(t[2]), [])
                    self.cur = ("ospf", int(t[2]))
                elif kind == "eigrp":
                    d.eigrp.setdefault(int(t[2]), [])
                    self.cur = ("eigrp", int(t[2]))
                elif kind == "rip":
                    self.cur = ("rip", 0)
                elif kind == "bgp" and len(t) == 3:
                    self.cur = ("bgp", int(t[2]))     # accepted but NOT simulated (no routes are exchanged)
                else:
                    return bad()
                self.mode = "router"
            elif low.startswith("ip route") and len(t) == 5:
                d.statics.append((t[2], t[3], t[4]))
            elif low.startswith("ip default-gateway"):
                pass
            else:
                bad()
            return
        if self.mode == "if":
            i = self.cur
            if low.startswith("ip address") and len(t) == 4:
                try:
                    ipaddress.ip_interface(f"{t[2]}/{t[3]}")
                except ValueError:
                    return self.out("% Invalid input detected at '^' marker.")
                i.ip, i.mask = t[2], t[3]
            elif low in ("no shutdown", "no shut"):
                i.shut = False
            elif low in ("shutdown", "shut"):
                i.shut = True
            elif low.startswith(("description", "no ip address", "duplex", "speed")):
                if low == "no ip address":
                    i.ip = i.mask = None
            elif low.startswith("interface"):
                self.mode = "config"
                return self.config(line, t, low, bad)
            else:
                bad()
            return
        if self.mode == "router":
            proto, arg = self.cur
            if t[0] == "network" and proto != "bgp":
                if proto == "ospf" and len(t) == 5 and t[3] == "area":
                    d.ospf[arg].append((t[1], t[2]))
                elif proto == "eigrp" and len(t) in (2, 3):
                    d.eigrp[arg].append((t[1], t[2] if len(t) == 3 else "0.0.0.0"))
                elif proto == "rip" and len(t) == 2:
                    first = int(t[1].split(".")[0])
                    cls = 8 if first < 128 else 16 if first < 192 else 24
                    d.rip.append((t[1], str(ipaddress.ip_address(int(ipaddress.ip_network(f"0.0.0.0/{cls}").hostmask)))))
                else:
                    return bad()
            elif low.startswith(("version", "no auto-summary", "router-id", "passive", "redistribute", "default-information",
                                 "maximum-paths", "auto-cost", "log-adjacency", "timers", "distance", "bgp ", "no neighbor",
                                 "address-family", "exit-address-family", "synchronization")) or (proto == "bgp" and low.startswith(("neighbor", "network"))):
                pass
            elif low.startswith(("router ", "interface")):
                self.mode = "config"
                return self.config(line, t, low, bad)
            else:
                bad()
            return
        if self.mode == "line":
            if low.startswith("line "):
                return
            if low.startswith(("login", "transport", "exec-timeout", "logging", "password", "no login", "privilege")):
                return
            return bad()


# ------------------------------------------------------------------ servers
def _loop(cli: Cli, read):
    buf = ""
    cli.banner()
    while True:
        data = read()
        if data is None:
            return
        for ch in data:
            if ch in ("\r", "\n"):
                if ch == "\n" and buf == "" and getattr(cli, "_last_cr", False):
                    cli._last_cr = False
                    continue
                cli._last_cr = ch == "\r"
                cli.w("\r\n")
                line, buf = buf, ""
                with LOCK:
                    cli.feed(line)
            elif ch in ("\x08", "\x7f"):
                buf = buf[:-1]
            elif ch >= " ":
                buf += ch
                cli.w(ch)       # echo (passwords echo too - acceptable for a fake)
                cli._last_cr = False


def _listen(host, port, backlog=8):
    srv = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):       # Windows: refuse a port another process already listens on
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(backlog)
    return srv


def console_server(dev: Dev, srv):

    def handle(conn):
        def w(s):
            try:
                conn.sendall(s.encode())
            except OSError:
                pass

        def rd():
            try:
                b = conn.recv(1024)
                return b.decode("latin1") if b else None
            except OSError:
                return None
        _loop(Cli(dev, w, console=True), rd)
        conn.close()
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


class SshIface(paramiko.ServerInterface):
    def __init__(self, dev):
        self.dev, self.user = dev, None

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_auth_password(self, username, password):
        d = self.dev
        if d.keys and d.users.get(username) == password:
            self.user = username
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_shell_request(self, channel):
        return True

    def check_channel_pty_request(self, *a):
        return True


def ssh_server(dev: Dev, host_key, srv):

    def handle(conn):
        t = paramiko.Transport(conn)
        t.add_server_key(host_key)
        iface = SshIface(dev)
        try:
            t.start_server(server=iface)
            ch = t.accept(20)
            if not ch:
                return
            cli = Cli(dev, lambda s: ch.send(s.encode()), authed_user=iface.user)
            cli.mode = "priv"       # privilege 15 user lands in enable mode

            def rd():
                try:
                    b = ch.recv(1024)
                    return b.decode("latin1") if b else None
                except Exception:
                    return None
            _loop(cli, rd)
        except Exception:
            pass
        finally:
            t.close()
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()


_started = False


def running() -> bool:
    return _started


def start() -> list[Dev]:
    """Start the emulated devices inside this process (idempotent). Raises OSError if a port is already taken."""
    global _started
    with LOCK:
        if _started:
            return list(DEVICES.values())
        devs = [Dev(key, kind, cport, ip) for key, kind, cport, ip in SPEC]
        socks = []
        try:
            for d in devs:
                socks.append((d, _listen("127.0.0.1", d.console_port), _listen(d.listen_ip, 22)))
        except OSError:
            for _, a, b in socks:
                a.close()
                b.close()
            raise
        for d in devs:
            DEVICES[d.key] = d
        host_key = paramiko.RSAKey.generate(2048)
        for d, c, s_ in socks:
            threading.Thread(target=console_server, args=(d, c), daemon=True).start()
            threading.Thread(target=ssh_server, args=(d, host_key, s_), daemon=True).start()
        _started = True
        return devs


def info() -> list[dict]:
    with LOCK:
        return [{"name": d.key, "kind": d.kind, "console_port": d.console_port, "mgmt_ip": d.listen_ip,
                 "factory": d.fresh and not d.users} for d in DEVICES.values()]


def reset() -> None:
    """Back to factory state (unconfigured devices) without restarting the listeners."""
    with LOCK:
        for key, kind, cport, ip in SPEC:
            if key in DEVICES:
                DEVICES[key].__init__(key, kind, cport, ip)


def main():
    devs = start()
    print("Fake lab running. Consoles (telnet):")
    for d in devs:
        print(f"  {d.key:<4} console 127.0.0.1:{d.console_port}   ssh {d.listen_ip}:22   kind={d.kind}")
    print("Suggested mgmt IP for each device = its ssh address above (interface GigabitEthernet0/0). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
