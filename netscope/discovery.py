"""Auto discovery: collect facts + CDP/LLDP neighbours from every managed device, crawl to new neighbours, build topology."""
from __future__ import annotations

import time
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import devices, parsers, store
from .transport import AuthError, CliError

CONSOLE_CDP_MAX_AGE = 600


def _fresh_console_observations(cache: dict, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return [o for o in cache.get("devices", {}).values()
            if 0 <= now - o.get("scanned_at", 0) <= CONSOLE_CDP_MAX_AGE]


def _console_topology(observations: list[dict]) -> dict:
    # Fresh EVE nodes often advertise factory names (Router/Switch), and some
    # CDP blocks omit the management IP. Resolve IP-bearing neighbors first,
    # then map a unique scanned switch for the remaining factory Switch name.
    ip_to_name = {p.get("ip"): o["name"]
                  for o in observations for p in o.get("interfaces", []) if p.get("ip")}
    switches = [o["name"] for o in observations
                if (o.get("info", {}).get("kind") or "").lower() == "switch"]
    for observation in observations:
        for neighbor in observation.get("neighbors", []):
            if neighbor.get("ip") in ip_to_name and ip_to_name[neighbor["ip"]].lower() != observation["name"].lower():
                neighbor["name"] = ip_to_name[neighbor["ip"]]
            elif neighbor.get("name", "").lower() == "switch" and len(switches) == 1:
                neighbor["name"] = switches[0]
    inv = [{"id": -(i + 1), "name": o["name"], "kind": o["info"].get("kind") or "router",
            "mgmt_ip": next((p["ip"] for p in o["interfaces"] if p.get("ip")), None)}
           for i, o in enumerate(observations)]
    results = {d["id"]: {"info": o["info"], "interfaces": o["interfaces"],
                          "neighbors": o["neighbors"]}
               for d, o in zip(inv, observations)}
    topo = build_topology(inv, results, {})
    # Console/CDP observations use synthetic IDs so they can be built even
    # when SSH is unavailable. Reattach nodes to the real inventory whenever
    # the scanned hostname or management IP matches a registered device; the
    # UI can then expose read-only actions such as refresh, traffic and ping.
    registered = store.devices()
    by_name = {d.get("name", "").lower(): d for d in registered if d.get("name")}
    by_ip = {d.get("mgmt_ip"): d for d in registered if d.get("mgmt_ip")}
    for node in topo["nodes"]:
        match = by_name.get(node["name"].lower()) or by_ip.get(node.get("mgmt_ip"))
        node["device_id"] = match.get("id") if match else None
        node["managed"] = bool(match)
        if match:
            node["status"] = match.get("status") or node.get("status")
            node["kind"] = match.get("kind") or node.get("kind")
            node["model"] = match.get("model") or node.get("model")
        node["source"] = "console_cdp"
    topo["source"] = "console_cdp"
    topo["scanned_devices"] = len(observations)
    return topo


def scan_console_cdp(console: dict, name: str, lab: str = "") -> dict:
    """Read CDP over a node's console and assemble a topology without SSH.

    Only observations are saved; console passwords and enable secrets are not.
    Each additional node scan updates the same lab's graph.
    """
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,62}", name):
        raise ValueError("เลือก Node ที่ต้องการอ่าน CDP ก่อน")
    if not console.get("port") or (console.get("type", "telnet") == "telnet" and not console.get("host")):
        raise ValueError("เลือก Console Host/Port ของ Node ก่อน")
    with_console = devices.open_console(console, console.get("username") or None,
                                        console.get("password") or None, console.get("enable") or None,
                                        timeout=15)
    try:
        with_console.prepare(timeout=5)
        version = parsers.parse_version(with_console.run("show version", 15))
        interfaces = parsers.parse_ip_int_brief(with_console.run("show ip interface brief", 15))
        raw = with_console.run("show cdp neighbors detail", 20)
        command_errors = parsers.find_errors(raw)
        if command_errors:
            raise CliError("อุปกรณ์อ่าน CDP ไม่ได้: " + "; ".join(command_errors))
        neighbors = parsers.parse_cdp_detail(raw)
    finally:
        with_console.close()
    observed_name = parsers.base_hostname(version.get("hostname") or name)
    # A fresh EVE node normally answers as the IOS defaults "Router" or
    # "Switch" until Initial Config assigns its hostname. The selected EVE
    # node/console port is the reliable identity in that state.
    default_hostnames = {"router", "switch", "ios", "cisco"}
    if observed_name.lower() != name.lower() and observed_name.lower() not in default_hostnames:
        raise CliError(f"Console นี้ตอบเป็น {observed_name} แต่เลือก Node {name} — ตรวจ Port ก่อน")
    graph_name = name if observed_name.lower() in default_hostnames else observed_name
    lab = (lab or "").strip().lower()
    cache = store.load("console_cdp.json", {"lab": lab, "devices": {}})
    if cache.get("lab") != lab:
        cache = {"lab": lab, "devices": {}}
    now = time.time()
    cache["devices"] = {k: o for k, o in cache.get("devices", {}).items()
                        if 0 <= now - o.get("scanned_at", 0) <= CONSOLE_CDP_MAX_AGE}
    cache["devices"][name.lower()] = {
        "name": graph_name, "info": version, "interfaces": interfaces,
        "neighbors": neighbors, "scanned_at": now,
    }
    store.save("console_cdp.json", cache)
    topo = _console_topology(list(cache["devices"].values()))
    store.save("topology.json", topo)
    store.log_event("CDP console scan", f"{graph_name}: {len(neighbors)} neighbors, {len(topo['links'])} links", "info")
    return {"name": graph_name, "neighbors": len(neighbors), "topology": topo}


def collect_device(dev: dict) -> dict:
    """One SSH session: version, interfaces, neighbours."""
    with devices.session(dev) as s:
        ver = parsers.parse_version(s.run("show version", 40))
        ifs = devices.collect_interfaces(s)
        neigh = parsers.parse_cdp_detail(s.run("show cdp neighbors detail", 40))
        try:
            lldp = parsers.parse_lldp_detail(s.run("show lldp neighbors detail", 40))
        except CliError:
            lldp = []
        # prefer CDP; LLDP only adds links CDP did not report
        seen = {(n["local_if"]) for n in neigh}
        neigh += [n for n in lldp if n["local_if"] not in seen]
    return {"info": ver, "interfaces": ifs, "neighbors": neigh}


def _kind_from_caps(caps: list[str], platform: str) -> str:
    c = " ".join(caps).lower() + " " + (platform or "").lower()
    if "switch" in c and "router" not in c:
        return "switch"
    if "l2" in c or "ws-c" in c:
        return "switch"
    return "router"


def _try_login(ip: str, creds: list[dict], progress) -> dict | None:
    for c in creds:
        probe = {"mgmt_ip": ip, "username": c["username"], "password": c["password"],
                 "enable_secret": c.get("enable_secret") or c["password"], "protocol": "ssh", "port": 22}
        try:
            with devices.session(probe) as s:
                info = parsers.parse_version(s.run("show version", 30))
            probe.update({"name": info["hostname"] or ip, "kind": info["kind"], "model": info["model"],
                          "version": info["version"], "status": "online", "auto_discovered": True})
            return probe
        except AuthError:
            continue
        except CliError as e:
            progress(f"      cannot log in to {ip}: {e}")
            return None
    progress(f"      {ip}: no credential worked - shown as unmanaged neighbour")
    return None


def discover(progress: Callable[[str], None], crawl: bool = True, extra_creds: list[dict] | None = None,
             max_devices: int = 40) -> dict:
    inv = store.devices()
    if not inv:
        raise CliError("No devices registered yet - run Initial Config or add a device first")
    started = time.time()
    results: dict[int, dict] = {}
    errors: dict[int, str] = {}
    tried_ips = {d["mgmt_ip"] for d in inv if d.get("mgmt_ip")}
    queue = list(inv)
    shared_creds = [{"username": d["username"], "password": d["password"], "enable_secret": d.get("enable_secret")}
                    for d in inv if d.get("username")]
    for c in extra_creds or []:
        if c.get("username"):
            shared_creds.append(c)

    while queue:
        progress(f"Scanning {len(queue)} device(s): " + ", ".join(d["name"] for d in queue))
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {d["id"]: (d, pool.submit(collect_device, d)) for d in queue}
        queue = []
        for did, (dev, fut) in futs.items():
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001 - report any device failure but keep going
                errors[did] = str(e)
                store.upsert_device({"id": did, "status": "unreachable"})
                progress(f"  ✗ {dev['name']}: {e}")
                continue
            results[did] = res
            hostname = res["info"]["hostname"] or dev["name"]
            store.upsert_device({"id": did, "name": hostname, "model": res["info"]["model"],
                                 "version": res["info"]["version"], "kind": res["info"]["kind"] or dev.get("kind"),
                                 "status": "online", "last_seen": time.strftime("%H:%M:%S")})
            progress(f"  ✓ {hostname}: {len(res['interfaces'])} interfaces, {len(res['neighbors'])} neighbour(s)")
            if not crawl:
                continue
            for n in res["neighbors"]:
                ip = n.get("ip")
                if not ip or ip in tried_ips or len(results) + len(queue) >= max_devices:
                    continue
                tried_ips.add(ip)
                progress(f"  → new neighbour {n['name']} ({ip}), trying to log in")
                found = _try_login(ip, shared_creds, progress)
                if found:
                    found = store.upsert_device(found)
                    progress(f"      added {found['name']} to inventory")
                    queue.append(found)
    console_observations = _fresh_console_observations(store.load("console_cdp.json", {}))
    if not results and console_observations:
        progress("SSH unavailable; showing recent CDP links collected through Console")
        topo = _console_topology(console_observations)
        topo["errors"] = errors
    else:
        topo = build_topology(store.devices(), results, errors)
    topo["took"] = round(time.time() - started, 1)
    store.save("topology.json", topo)
    store.log_event("Auto discovery", f"{len(topo['nodes'])} nodes, {len(topo['links'])} links", "success")
    progress(f"Done: {len(topo['nodes'])} nodes, {len(topo['links'])} links in {topo['took']}s")
    return topo


def build_topology(inv: list[dict], results: dict[int, dict], errors: dict[int, str]) -> dict:
    nodes: dict[str, dict] = {}
    for dev in inv:
        key = dev["name"].lower()
        res = results.get(dev["id"])
        node = {"id": dev["name"], "name": dev["name"], "device_id": dev["id"], "managed": True,
                "mgmt_ip": dev.get("mgmt_ip"), "kind": dev.get("kind") or "router", "model": dev.get("model"),
                "version": dev.get("version"), "status": "online" if res else "unreachable",
                "error": errors.get(dev["id"]), "interfaces": []}
        if res:
            for i in res["interfaces"]:
                i = dict(i)
                i["mgmt"] = bool(i["ip"] and i["ip"] == dev.get("mgmt_ip"))
                node["interfaces"].append(i)
        nodes[key] = node

    links: dict[tuple, dict] = {}
    for dev in inv:
        res = results.get(dev["id"])
        if not res:
            continue
        a = dev["name"]
        for n in res["neighbors"]:
            b_key = n["name"].lower()
            if b_key not in nodes:
                nodes[b_key] = {"id": n["name"], "name": n["name"], "device_id": None, "managed": False,
                                "mgmt_ip": n.get("ip"), "kind": _kind_from_caps(n["capabilities"], n["platform"]),
                                "model": n["platform"] or None, "version": None, "status": "unmanaged",
                                "error": None, "interfaces": []}
            b = nodes[b_key]
            if not any(i["name"] == n["remote_if"] for i in b["interfaces"]) and not b["managed"]:
                b["interfaces"].append({"name": n["remote_if"], "short": parsers.short_if(n["remote_if"]),
                                        "ip": None, "prefix": None, "status": "up", "protocol": "up",
                                        "state": "up", "mgmt": False})
            ends = sorted([(a.lower(), n["local_if"]), (b_key, n["remote_if"])])
            key = tuple(ends)
            if key in links:
                continue
            links[key] = {"id": f"{ends[0][0]}:{ends[0][1]}--{ends[1][0]}:{ends[1][1]}",
                          "a": {"node": a if a.lower() == ends[0][0] else b["id"], "port": ends[0][1]},
                          "b": {"node": a if a.lower() == ends[1][0] else b["id"], "port": ends[1][1]},
                          "protocol": n["protocol"]}
    node_by_id = {n["id"].lower(): n for n in nodes.values()}
    for link in links.values():
        states = []
        for end in (link["a"], link["b"]):
            node = node_by_id[end["node"].lower()]
            for i in node["interfaces"]:
                if i["name"] == end["port"]:
                    states.append(i["state"])
        link["state"] = "up" if states and all(s == "up" for s in states) else ("down" if states else "unknown")
        # annotate each interface with the neighbour it connects to
        for me, other in ((link["a"], link["b"]), (link["b"], link["a"])):
            for i in node_by_id[me["node"].lower()]["interfaces"]:
                if i["name"] == me["port"]:
                    i["neighbor"] = {"node": other["node"], "port": other["port"]}
    return {"nodes": list(nodes.values()), "links": list(links.values()),
            "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"), "errors": errors}


def refresh_device(dev: dict) -> dict:
    """Fast interface-only refresh used by the front-panel view; patches the stored topology."""
    with devices.session(dev) as s:
        ifs = devices.collect_interfaces(s)
    topo = store.load("topology.json", None)
    if topo:
        for n in topo["nodes"]:
            if n.get("device_id") == dev["id"]:
                old = {i["name"]: i for i in n["interfaces"]}
                for i in ifs:
                    i["mgmt"] = bool(i["ip"] and i["ip"] == dev.get("mgmt_ip"))
                    if old.get(i["name"], {}).get("neighbor"):
                        i["neighbor"] = old[i["name"]]["neighbor"]
                n["interfaces"], n["status"] = ifs, "online"
        # recompute link state from fresh interface states
        by_id = {n["id"].lower(): n for n in topo["nodes"]}
        for link in topo["links"]:
            st = [i["state"] for e in (link["a"], link["b"]) for i in by_id[e["node"].lower()]["interfaces"]
                  if i["name"] == e["port"]]
            link["state"] = "up" if st and all(x == "up" for x in st) else ("down" if st else "unknown")
        store.save("topology.json", topo)
    return {"interfaces": ifs}
