"""End-to-end check of every routing option in the assignment (RIP, EIGRP, OSPF, static) against the built-in simulator.

Runs fully in-process with a throw-away data dir and its own simulator ports, so it never touches your real data.

    python tests/e2e_routing_protocols.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NETSCOPE_DATA"] = tempfile.mkdtemp(prefix="netscope_rt_")
os.environ.setdefault("NETSCOPE_DEMO_CONSOLE_BASE", "2501")
os.environ.setdefault("NETSCOPE_DEMO_IP_BASE", "31")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netscope import connectivity, discovery, fakelab, initconfig, prompt, store  # noqa: E402
from netscope.server import job_apply_plans  # noqa: E402

quiet = lambda s: None  # noqa: E731
fails = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def apply(text, rules=True):
    res = prompt.parse(text, store.devices(), store.load("topology.json", None))
    assert not res["problems"], (text, res["problems"])
    out = job_apply_plans(quiet, res["plans"], True)
    assert out["ok"], (text, [r.get("errors") for r in out["results"]])
    return res


def matrix():
    m = connectivity.matrix(quiet)
    return m["ok"], m["total"]


devs = fakelab.start()
for d in devs[:3]:                                  # R1, R2, R3
    r = initconfig.apply({"type": "telnet", "host": "127.0.0.1", "port": d.console_port},
                         {"hostname": d.key, "username": "admin", "password": "cisco123", "mgmt_if": "g0/0",
                          "mgmt_ip": d.listen_ip, "mgmt_prefix": 8, "device_type": d.kind}, quiet)
    check(r["ok"], f"initial config {d.key} over console + SSH verify")
topo = discovery.discover(quiet, crawl=False)
check(len(topo["nodes"]) >= 3 and len(topo["links"]) >= 3, f"discovery: {len(topo['nodes'])} nodes / {len(topo['links'])} links")
apply("auto address links from 10.10.0.0/16")
ok0, tot = matrix()
check(ok0 < tot, f"before routing only directly-connected pings work ({ok0}/{tot})")

for label, add, remove in [
    ("RIP v2", "rip on all devices network 10.0.0.0", "no router rip"),
    ("EIGRP 100", "eigrp 100 on all devices", "no router eigrp 100"),
    ("OSPF area 0", "ospf area 0 on all devices", "no router ospf 1"),
]:
    res = apply(add)
    ok, tot = matrix()
    check(ok == tot, f"{label}: all-to-all ping {ok}/{tot}")
    cmds = [c for p in res["plans"] for c in p["commands"]]
    print("     e.g. " + " | ".join(res["plans"][0]["commands"]))
    for d in store.devices():                      # remove the protocol again (raw commands, as a user could type in Preview)
        job_apply_plans(quiet, [{"device_id": d["id"], "device": d["name"], "kind": "config", "commands": [remove]}], True)
    ok, tot = matrix()
    check(ok < tot, f"{label} removed again -> reachability drops ({ok}/{tot})")

apply("static route 10.10.0.8/30 via 10.10.0.2 on R1")
apply("static route 10.10.0.0/30 via 10.10.0.5 on R3")
from netscope import store as _s  # noqa: E402
r1 = _s.find_device("R1")
pr = connectivity.ping_from(r1, "10.10.0.10")
check(pr["ok"], f"static route: R1 -> 10.10.0.10 (two hops away) ping {pr['percent']}%")
print("ALL ROUTING CHECKS PASSED" if not fails else f"{len(fails)} CHECK(S) FAILED")
sys.exit(1 if fails else 0)
