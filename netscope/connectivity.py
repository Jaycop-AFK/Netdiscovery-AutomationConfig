"""Ping tests executed ON the network devices (not on the PC) - proves routing between devices works."""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from . import devices, parsers, store

_IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def resolve_target(target: str) -> str:
    """Accept an IP, or a device name (-> its loopback / first non-management IP from the last discovery)."""
    target = target.strip()
    if _IP_RE.match(target):
        return target
    topo = store.load("topology.json", {"nodes": []})
    for n in topo["nodes"]:
        if n["name"].lower() == target.lower():
            ips = _node_ips(n)
            if ips:
                return ips[0]
    raise ValueError(f"'{target}' is not an IP address or a known device with an IP")


def _node_ips(node: dict) -> list[str]:
    ifs = [i for i in node["interfaces"] if i.get("ip") and i["state"] == "up" and not i.get("mgmt")]
    ifs.sort(key=lambda i: (not i["name"].lower().startswith("loopback"), parsers.natural_key(i["name"])))
    return [i["ip"] for i in ifs]


def ping_from(dev: dict, target: str, count: int = 5, source: str | None = None) -> dict:
    ip = resolve_target(target)
    cmd = f"ping {ip} repeat {int(count)} timeout 2" + (f" source {source}" if source else "")
    with devices.session(dev) as s:
        out = s.run(cmd, timeout=30 + 2 * int(count))
    res = parsers.parse_ping(out)
    res.update({"from": dev["name"], "target": ip, "command": cmd})
    return res


def matrix(progress) -> dict:
    topo = store.load("topology.json", None)
    if not topo:
        raise ValueError("Run Auto Discovery first")
    inv = {d["name"].lower(): d for d in store.devices()}
    nodes = [n for n in topo["nodes"] if n["managed"] and n["name"].lower() in inv]
    targets = {n["name"]: _node_ips(n) for n in nodes}
    cells: dict[str, dict] = {n["name"]: {} for n in nodes}

    def run_source(src):
        dev = inv[src["name"].lower()]
        with devices.session(dev) as s:
            for dst in nodes:
                if dst["name"] == src["name"]:
                    continue
                results = []
                for ip in targets[dst["name"]]:
                    out = s.run(f"ping {ip} repeat 2 timeout 1", timeout=40)
                    r = parsers.parse_ping(out)
                    results.append({"ip": ip, "ok": r["ok"], "percent": r["percent"]})
                    progress(f"{src['name']} → {dst['name']} {ip}: {'OK' if r['ok'] else 'FAIL'} ({r['percent']}%)")
                cells[src["name"]][dst["name"]] = results

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(run_source, n) for n in nodes]
        for f in futs:
            f.result()
    ok = sum(1 for row in cells.values() for rs in row.values() for r in rs if r["ok"])
    total = sum(1 for row in cells.values() for rs in row.values() for _ in rs)
    store.log_event("Ping matrix", f"{ok}/{total} reachable", "success" if ok == total and total else "warning")
    return {"names": [n["name"] for n in nodes], "cells": cells, "ok": ok, "total": total}
