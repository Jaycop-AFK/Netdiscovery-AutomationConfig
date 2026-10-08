"""Initial configuration over console (EVE-NG telnet console or serial) so the device becomes reachable via SSH."""
from __future__ import annotations

import ipaddress
import re
import time
from typing import Callable

from . import devices, parsers, store
from .transport import AuthError, CliError

HOSTNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]{0,62}$")


class ValidationError(ValueError):
    pass


def _ip(value: str, what: str) -> str:
    try:
        return str(ipaddress.IPv4Address((value or "").strip()))
    except ValueError:
        raise ValidationError(f"{what}: '{value}' is not a valid IPv4 address") from None


def normalise(p: dict) -> dict:
    """Validate the form values and fill defaults. Raises ValidationError."""
    q = {k: (v.strip() if isinstance(v, str) else v) for k, v in p.items()}
    if not HOSTNAME_RE.match(q.get("hostname", "")):
        raise ValidationError("Hostname must start with a letter and contain only letters, digits and '-'")
    q["domain"] = q.get("domain") or "lab.local"
    if not q.get("username") or not q.get("password"):
        raise ValidationError("SSH username and password are required")
    if re.search(r"\s", q["username"]):
        raise ValidationError("Username must not contain spaces")
    q["enable_secret"] = q.get("enable_secret") or q["password"]
    q["mgmt_if"] = parsers.norm_if(q.get("mgmt_if") or "")
    if not q["mgmt_if"]:
        raise ValidationError("Management interface is required (e.g. GigabitEthernet0/0)")
    q["mgmt_ip"] = _ip(q.get("mgmt_ip"), "Management IP")
    mask = q.get("mgmt_mask") or q.get("mgmt_prefix") or "24"
    mask = str(mask).lstrip("/")
    if "." in mask:
        pref = parsers.mask_to_prefix(_ip(mask, "Subnet mask"))
    else:
        pref = int(mask)
    if not 8 <= pref <= 30:
        raise ValidationError("Prefix length must be between 8 and 30")
    q["mgmt_prefix"], q["mgmt_mask"] = pref, parsers.prefix_to_mask(pref)
    iface = ipaddress.IPv4Interface(f"{q['mgmt_ip']}/{pref}")
    if iface.ip in (iface.network.network_address, iface.network.broadcast_address):
        raise ValidationError(
            f"Management IP {q['mgmt_ip']}/{pref} เป็น "
            f"{'network address' if iface.ip == iface.network.network_address else 'broadcast address'} — "
            f"เลือก host IP เช่น {iface.network.network_address + 1}/{pref}"
        )
    if q.get("gateway"):
        q["gateway"] = _ip(q["gateway"], "Default gateway")
    q["rsa_bits"] = int(q.get("rsa_bits") or 2048)
    if q["rsa_bits"] not in (1024, 2048, 4096):
        raise ValidationError("RSA modulus must be 1024, 2048 or 4096")
    q["device_type"] = (q.get("device_type") or "router").lower()
    q["vty_transport"] = q.get("vty_transport") or "ssh"
    q["bring_up_all"] = q.get("bring_up_all", True) not in (False, "false", 0, "0")
    q["save_config"] = q.get("save_config", True) not in (False, "false", 0, "0")
    return q


def build_commands(p: dict) -> list[str]:
    # Layer-2 switches receive their management address on an SVI, not on a
    # physical switchport.  Keep the form compatible with older labs where
    # the user picked Ethernet0/0, but emit the valid IOS command sequence.
    mgmt_if = p["mgmt_if"]
    if p.get("device_type") == "switch" and not re.match(r"(?i)^vlan\d+$", mgmt_if):
        mgmt_if = "Vlan1"
    cmds = [
        "no ip domain-lookup",
        f"hostname {p['hostname']}",
        f"ip domain-name {p['domain']}",
        f"enable secret {p['enable_secret']}",
        f"username {p['username']} privilege 15 secret {p['password']}",
        f"interface {mgmt_if}",
        f" ip address {p['mgmt_ip']} {p['mgmt_mask']}",
        " no shutdown",
        "exit",
    ]
    if p.get("gateway"):
        if p["device_type"] == "switch":
            cmds.append(f"ip default-gateway {p['gateway']}")
        else:
            cmds.append(f"ip route 0.0.0.0 0.0.0.0 {p['gateway']}")
    cmds += [
        f"crypto key generate rsa modulus {p['rsa_bits']}",
        "ip ssh version 2",
        "line vty 0 4",
        " login local",
        f" transport input {p['vty_transport']}",
        " exec-timeout 30 0",
        "exit",
        "line con 0",
        " logging synchronous",
        " exec-timeout 0 0",
        "exit",
        "cdp run",
        "lldp run",
    ]
    cmds += [l.rstrip() for l in (p.get("extra") or "").splitlines() if l.strip()]
    return cmds


def _resolve_device_kind_and_interface(p: dict, version_text: str, interfaces: list[dict]) -> None:
    """Adapt the form's generic defaults to the actual EVE/IOS image.

    EVE labs commonly mix IOSv (Gi0/0), IOL (Et0/0), and IOSvL2 (Vlan1).
    Sending a guessed interface causes a cascade of misleading CLI errors,
    so resolve it before building the command list.
    """
    try:
        info = parsers.parse_version(version_text)
        detected = (info.get("kind") or "").lower()
        if detected in ("router", "switch"):
            # A detected L2 image is authoritative even when the form kept its
            # Router default (for example, after selecting SW1).
            if detected == "switch" or p.get("device_type") == "switch":
                p["device_type"] = "switch"
    except Exception:
        pass

    names = [i["name"] for i in interfaces]
    if not names:
        raise ValidationError("ไม่พบ Interface จากอุปกรณ์ — ตรวจสอบว่า Node boot เสร็จแล้ว")
    if p.get("device_type") == "switch":
        svi = next((n for n in names if re.match(r"(?i)^vlan1$", n)), None)
        if svi:
            p["mgmt_if"] = svi
            return
    if p["mgmt_if"] in names:
        return
    physical = [i for i in interfaces if re.match(r"(?i)^(GigabitEthernet|FastEthernet|Ethernet|Serial)", i["name"])]
    if not physical:
        raise ValidationError(f"ไม่พบ Interface ที่ใช้ตั้ง Management ได้ (เลือกไว้: {p['mgmt_if']})")
    # Prefer an interface already carrying an address (often DHCP on EVE's
    # Net/Cloud), then an up/up physical port, then the first physical port.
    chosen = next((i for i in physical if i.get("ip")), None)
    chosen = chosen or next((i for i in physical if i.get("state") == "up"), None) or physical[0]
    p["mgmt_if"] = chosen["name"]


def apply(console: dict, params: dict, progress: Callable[[str], None]) -> dict:
    """Run the full initial-config workflow. Returns {'ok', 'device', 'errors', 'verified'}."""
    p = normalise(params)
    if console.get("type", "telnet").lower() == "serial" and ipaddress.ip_address(p["mgmt_ip"]).is_loopback:
        raise ValidationError("Management IP 127.x ใช้ได้เฉพาะโหมด Demo — Router จริงต้องใช้ IP ที่คอมเข้าถึงได้ เช่น 192.168.50.11/24")
    cmds: list[str] = []
    errors: list[str] = []

    where = (f"{console['host']}:{console['port']}" if console.get("type", "telnet") == "telnet"
             else f"{console['port']} @ {console.get('baud', 9600)}")
    progress(f"[1/4] Connecting to console {where} ...")
    s = devices.open_console(console, console.get("username") or None, console.get("password") or None,
                             console.get("enable") or None, timeout=15)
    try:
        progress(f"      prompt: {s.prompt}")
        s.prepare()
        version_text = s.run("show version", 30)
        interfaces = parsers.parse_ip_int_brief(s.run("show ip interface brief", 30))
        _resolve_device_kind_and_interface(p, version_text, interfaces)
        cmds = build_commands(p)
        progress(f"      detected {p['device_type']} management interface: {p['mgmt_if']}")
        if p.get("bring_up_all", True):
            ports = [i["name"] for i in interfaces
                     if i["name"] != p["mgmt_if"] and not re.match(r"(?i)(loopback|vlan|tunnel|null|port-channel)", i["name"])]
            for port in ports:
                cmds += [f"interface {port}", " no shutdown", "exit"]
            progress(f"      will also enable {len(ports)} physical port(s): " + ", ".join(parsers.short_if(x) for x in ports))
        progress(f"[2/4] Sending {len(cmds)} configuration lines")
        res = s.config(cmds, progress=lambda c: progress(f"      {c}"))
        # LLDP is missing on some images (e.g. older IOL) - not an error for initial config
        errors += [e for e in res["errors"] if not e.startswith("lldp run")]
        if errors:
            progress("      ! device reported errors: " + "; ".join(errors))
        if p["save_config"]:
            progress("[3/4] Saving configuration (write memory)")
            s.save()
        else:
            progress("[3/4] Skipping write memory (config remains in running-config only)")
    finally:
        s.close()

    progress(f"[4/4] Verifying SSH to {p['mgmt_ip']} as {p['username']} ...")
    rec = {"name": p["hostname"], "kind": p["device_type"], "mgmt_ip": p["mgmt_ip"], "protocol": "ssh",
           "port": 22, "username": p["username"], "password": p["password"], "enable_secret": p["enable_secret"],
           "mgmt_if": p["mgmt_if"], "console": {k: v for k, v in console.items() if k not in ("password", "enable")},
           "status": "unreachable"}
    verified, last_err = False, ""
    deadline = time.time() + 45
    while time.time() < deadline and not verified:
        try:
            with devices.session(rec) as ss:
                info = parsers.parse_version(ss.run("show version", 30))
                rec.update({"model": info["model"], "version": info["version"], "status": "online"})
                verified = True
        except AuthError as e:
            last_err = str(e)
            break
        except CliError as e:
            last_err = str(e)
            time.sleep(4)
    if verified:
        progress("      SSH OK - device is ready for discovery")
    else:
        progress(f"      ! SSH verification failed: {last_err}")
        progress("      (check that this computer can reach the management IP, e.g. EVE-NG Cloud/pnet0 or host-only network)")
    saved = store.upsert_device(rec)
    store.log_event("Initial config", f"{p['hostname']} {p['mgmt_ip']} " + ("verified" if verified else "NOT verified"),
                    "success" if verified and not errors else "warning")
    return {"ok": verified and not errors, "verified": verified, "errors": errors,
            "device": store.public(saved), "commands": cmds}
