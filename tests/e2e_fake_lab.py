"""End-to-end test against tools/fake_lab.py (start it first). Uses a throw-away data dir.

    python tests/e2e_fake_lab.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["NETSCOPE_DATA"] = tempfile.mkdtemp(prefix="netscope_e2e_")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netscope import connectivity, devices, discovery, initconfig, prompt, store  # noqa: E402
from netscope.server import BLOCKED, job_apply_plans  # noqa: E402


def say(s):
    print(s, flush=True)


def check(cond, msg):
    say(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


SPEC = [("R1", 2301, "127.0.0.11"), ("R2", 2302, "127.0.0.12"), ("R3", 2303, "127.0.0.13")]
for name, port, ip in SPEC:
    r = initconfig.apply({"type": "telnet", "host": "127.0.0.1", "port": port},
                         {"hostname": name, "domain": "lab.local", "username": "admin", "password": "cisco123",
                          "enable_secret": "cisco123", "mgmt_if": "g0/0", "mgmt_ip": ip, "mgmt_prefix": 8,
                          "device_type": "router"}, say)
    check(r["ok"], f"initial config {name}")

topo = discovery.discover(say, crawl=False)
check(len(topo["nodes"]) >= 3, f"discovery found {len(topo['nodes'])} nodes ({[n['name'] for n in topo['nodes']]})")
check(len(topo["links"]) >= 2, f"discovery found {len(topo['links'])} links")

inv = store.devices()
res = prompt.parse("auto address links from 10.10.0.0/16", inv, topo)
check(not res["problems"] and res["plans"], f"auto address plan: {res['problems']}")
log = []
out = job_apply_plans(log.append, res["plans"], True)
check(out["ok"], f"applied auto addressing: {[r.get('errors') for r in out['results']]}")

topo = store.load("topology.json", None)
before = connectivity.matrix(say)
say(f"matrix before routing: {before['ok']}/{before['total']}")

res = prompt.parse("enable ospf area 0 on all devices", store.devices(), topo)
check(not res["problems"], f"ospf plan: {res['problems']}")
for p in res["plans"]:
    say(f"  {p['device']}: {p['commands']}")
out = job_apply_plans(log.append, res["plans"], True)
check(out["ok"], "applied ospf")
after = connectivity.matrix(say)
say(f"matrix after routing: {after['ok']}/{after['total']}")
check(after["ok"] == after["total"] and after["total"] > 0, "all device-to-device pings succeed after OSPF")
check(after["ok"] > before["ok"], "pings improved after routing config")

r1 = store.find_device("R1")
pr = connectivity.ping_from(r1, "R3")
check(pr["ok"], f"ping R1 -> R3 {pr['target']} {pr['percent']}%")
check(bool(BLOCKED.match("reload")), "dangerous commands blocked")
say("ALL E2E CHECKS PASSED")
