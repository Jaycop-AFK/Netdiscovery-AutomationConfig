"""Open CLI sessions to inventory devices (SSH/vty-telnet first, console as fallback) and common tasks."""
from __future__ import annotations

import threading
from contextlib import contextmanager

from . import parsers
from .transport import (CliError, CliSession, SerialTransport, SSHTransport, TelnetTransport)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def open_mgmt(dev: dict, timeout: float = 10) -> CliSession:
    """Session over the management IP (SSH by default, vty telnet if dev['protocol']=='telnet')."""
    host = dev.get("mgmt_ip")
    if not host:
        raise CliError("device has no management IP")
    proto = (dev.get("protocol") or "ssh").lower()
    if proto == "telnet":
        tr = TelnetTransport(host, int(dev.get("port") or 23), timeout)
        s = CliSession(tr)
        s.wake(dev.get("username"), dev.get("password"), dev.get("enable_secret") or dev.get("password"), timeout=30)
    else:
        tr = SSHTransport(host, int(dev.get("port") or 22), dev.get("username", ""), dev.get("password", ""), timeout)
        s = CliSession(tr)
        s.wake(dev.get("username"), dev.get("password"), dev.get("enable_secret") or dev.get("password"), timeout=30)
    s.prepare()
    return s


def open_console(console: dict, username: str | None = None, password: str | None = None,
                 enable: str | None = None, timeout: float = 90) -> CliSession:
    kind = (console.get("type") or "telnet").lower()
    if kind == "serial":
        tr = SerialTransport(console["port"], int(console.get("baud") or 9600))
    else:
        tr = TelnetTransport(console["host"], int(console["port"]), 10)
    s = CliSession(tr)
    try:
        s.wake(username, password, enable, timeout=timeout)
    except Exception:
        s.close()
        raise
    return s


@contextmanager
def session(dev: dict):
    """Serialised (one at a time per device) management session."""
    lock = _lock_for(str(dev.get("id") or dev.get("mgmt_ip")))
    with lock:
        s = open_mgmt(dev)
        try:
            yield s
        finally:
            s.close()


def collect_interfaces(s: CliSession) -> list[dict]:
    """Interface table with IP/prefix/state (show ip interface brief + prefix lengths)."""
    ifs = parsers.parse_ip_int_brief(s.run("show ip interface brief", 30))
    try:
        pref = parsers.parse_ip_interface_prefixes(
            s.run("show ip interface | include line protocol|Internet address", 30))
    except CliError:
        pref = {}
    for i in ifs:
        if i["ip"]:
            i["prefix"] = pref.get(i["name"])
    return sorted(ifs, key=lambda i: parsers.natural_key(i["name"]))


def collect_traffic(s: CliSession) -> list[dict]:
    """Read interface counters and five-minute rates without changing device config."""
    return parsers.parse_interface_traffic(s.run("show interfaces", 30))


def push_config(dev: dict, commands: list[str], save: bool = True, progress=None) -> dict:
    with session(dev) as s:
        res = s.config(commands, progress=progress)
        if save and not res["errors"]:
            res["saved"] = "[OK]" in s.save()
        return res


def run_exec(dev: dict, commands: list[str]) -> dict[str, str]:
    with session(dev) as s:
        return {c: s.run(c, timeout=90) for c in commands}
