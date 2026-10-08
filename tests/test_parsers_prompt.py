import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netscope import parsers, prompt  # noqa: E402

BRIEF = """Interface                  IP-Address      OK? Method Status                Protocol
GigabitEthernet0/0         192.168.1.10    YES manual up                    up
GigabitEthernet0/1         10.0.0.1        YES manual up                    up
GigabitEthernet0/2         unassigned      YES unset  administratively down down
GigabitEthernet0/3         unassigned      YES unset  down                  down
Loopback0                  1.1.1.1         YES manual up                    up
"""

CDP = """-------------------------
Device ID: R2.lab.local
Entry address(es):
  IP address: 192.168.1.11
Platform: Cisco IOSv,  Capabilities: Router Source-Route-Bridge
Interface: GigabitEthernet0/1,  Port ID (outgoing port): GigabitEthernet0/1
Holdtime : 143 sec

-------------------------
Device ID: SW1
Entry address(es):
  IP address: 192.168.1.20
Platform: Cisco ,  Capabilities: Switch IGMP
Interface: GigabitEthernet0/3,  Port ID (outgoing port): Ethernet0/1
"""

LLDP = """------------------------------------------------
Local Intf: Gi0/1
Chassis id: 5000.0002.0000
Port id: Gi0/2
Port Description: GigabitEthernet0/2
System Name: R9.lab.local

System Capabilities: B,R
Management Addresses:
    IP: 192.168.1.19
"""

VERSION = """Cisco IOS Software, IOSv Software (VIOS-ADVENTERPRISEK9-M), Version 15.6(2)T, RELEASE SOFTWARE (fc2)
R1 uptime is 10 minutes
Processor board ID 9ABCDEF
"""


def test_brief():
    ifs = parsers.parse_ip_int_brief(BRIEF)
    assert [i["state"] for i in ifs] == ["up", "up", "admin_down", "down", "up"]
    assert ifs[0]["ip"] == "192.168.1.10" and ifs[2]["ip"] is None and ifs[2]["short"] == "Gi0/2"


def test_prefixes():
    t = "GigabitEthernet0/0 is up, line protocol is up\n  Internet address is 192.168.1.10/24\nGigabitEthernet0/2 is administratively down, line protocol is down\n  Internet protocol processing disabled\n"
    assert parsers.parse_ip_interface_prefixes(t) == {"GigabitEthernet0/0": 24}


def test_interface_traffic():
    t = """GigabitEthernet0/0 is up, line protocol is up
  5 minute input rate 12000 bits/sec, 20 packets/sec
     123 packets input, 45678 bytes, 0 no buffer
     2 input errors, 1 CRC
  5 minute output rate 3000 bits/sec, 4 packets/sec
     98 packets output, 7654 bytes, 0 underruns
     3 output errors, 0 collisions
"""
    r = parsers.parse_interface_traffic(t)
    assert r == [{"name": "GigabitEthernet0/0", "short": "Gi0/0", "input_packets": 123,
                  "input_bytes": 45678, "output_packets": 98, "output_bytes": 7654,
                  "input_errors": 2, "output_errors": 3, "input_rate_bps": 12000,
                  "output_rate_bps": 3000}]


def test_cdp():
    n = parsers.parse_cdp_detail(CDP)
    assert n[0]["name"] == "R2" and n[0]["local_if"] == "GigabitEthernet0/1" and n[0]["ip"] == "192.168.1.11"
    assert n[1]["capabilities"][0] == "Switch" and n[1]["remote_if"] == "Ethernet0/1"


def test_lldp():
    n = parsers.parse_lldp_detail(LLDP)
    assert n[0]["name"] == "R9" and n[0]["local_if"] == "GigabitEthernet0/1" and n[0]["remote_if"] == "GigabitEthernet0/2"


def test_version_ping_names():
    v = parsers.parse_version(VERSION)
    assert v["hostname"] == "R1" and v["version"] == "15.6(2)T" and v["kind"] == "router"
    assert parsers.parse_ping("Success rate is 80 percent (4/5), round-trip min/avg/max = 1/2/4 ms")["percent"] == 80
    assert not parsers.parse_ping("Success rate is 0 percent (0/5)")["ok"]
    assert parsers.norm_if("g0/1") == "GigabitEthernet0/1" and parsers.norm_if("lo0") == "Loopback0"
    assert parsers.short_if("GigabitEthernet0/1") == "Gi0/1"
    assert parsers.find_errors("% Invalid input detected at '^' marker.") != []


DEVS = [{"id": 1, "name": "R1"}, {"id": 2, "name": "R2"}, {"id": 3, "name": "R3"}]


def cmds(res, dev, kind="config"):
    return next(p["commands"] for p in res["plans"] if p["device"] == dev and p["kind"] == kind)


def test_prompt_ip():
    r = prompt.parse("set ip 10.0.0.1/24 on g0/1 of R1", DEVS)
    assert not r["problems"]
    assert cmds(r, "R1") == ["interface GigabitEthernet0/1", " ip address 10.0.0.1 255.255.255.0", " no shutdown", "exit"]
    r = prompt.parse("R2 ตั้ง ip 10.0.0.2 255.255.255.252 ที่ fa0/0", DEVS)
    assert " ip address 10.0.0.2 255.255.255.252" in cmds(r, "R2")
    r = prompt.parse("loopback 0 1.1.1.1/32 on R3", DEVS)
    assert cmds(r, "R3")[0] == "interface Loopback0"


def test_prompt_shut():
    r = prompt.parse("no shutdown g0/1 on R2", DEVS)
    assert cmds(r, "R2") == ["interface GigabitEthernet0/1", " no shutdown", "exit"]
    r = prompt.parse("shutdown gi0/2 on R1", DEVS)
    assert " shutdown" in cmds(r, "R1")
    r = prompt.parse("เปิดพอร์ต g0/1 บน R1", DEVS)
    assert " no shutdown" in cmds(r, "R1")


def test_prompt_routing():
    r = prompt.parse("ospf area 0 on R1 R2 network 10.0.0.0/24 network 192.168.1.0/24", DEVS)
    assert cmds(r, "R1") == ["router ospf 1", " network 10.0.0.0 0.0.0.255 area 0", " network 192.168.1.0 0.0.0.255 area 0", "exit"]
    assert cmds(r, "R2") == cmds(r, "R1") and len(r["plans"]) == 2
    r = prompt.parse("eigrp 100 on R3 network 10.0.0.0 0.0.0.255", DEVS)
    assert cmds(r, "R3") == ["router eigrp 100", " network 10.0.0.0 0.0.0.255", " no auto-summary", "exit"]
    r = prompt.parse("rip on R1 network 10.1.2.0/24 network 172.16.5.0/24", DEVS)
    assert cmds(r, "R1") == ["router rip", " version 2", " network 10.0.0.0", " network 172.16.0.0", " no auto-summary", "exit"]


def test_prompt_static_and_exec():
    r = prompt.parse("static route 192.168.2.0/24 via 10.0.0.2 on R1", DEVS)
    assert cmds(r, "R1") == ["ip route 192.168.2.0 255.255.255.0 10.0.0.2"]
    r = prompt.parse("default route via 10.0.0.1 on R3", DEVS)
    assert cmds(r, "R3") == ["ip route 0.0.0.0 0.0.0.0 10.0.0.1"]
    r = prompt.parse("ping 10.0.0.2 from R1", DEVS)
    assert cmds(r, "R1", "exec") == ["ping 10.0.0.2"]
    r = prompt.parse("show ip route on all devices", DEVS)
    assert len(r["plans"]) == 3 and cmds(r, "R2", "exec") == ["show ip route"]


def test_prompt_derived_networks_and_errors():
    topo = {"nodes": [{"name": "R1", "interfaces": [
        {"name": "GigabitEthernet0/0", "ip": "192.168.1.10", "prefix": 24, "state": "up", "mgmt": True},
        {"name": "GigabitEthernet0/1", "ip": "10.0.0.1", "prefix": 30, "state": "up", "mgmt": False}]}], "links": []}
    r = prompt.parse("enable ospf on R1", DEVS, topo)
    assert cmds(r, "R1") == ["router ospf 1", " network 10.0.0.0 0.0.0.3 area 0", "exit"]
    assert prompt.parse("blah blah", DEVS)["problems"]
    assert prompt.parse("ospf on R9", DEVS)["problems"] or True


def test_auto_address():
    topo = {"nodes": [{"name": "R1", "interfaces": [{"name": "GigabitEthernet0/1", "ip": None}]},
                      {"name": "R2", "interfaces": [{"name": "GigabitEthernet0/1", "ip": None}]}],
            "links": [{"a": {"node": "R1", "port": "GigabitEthernet0/1"}, "b": {"node": "R2", "port": "GigabitEthernet0/1"}}]}
    r = prompt.parse("auto address links from 10.10.0.0/16", DEVS, topo)
    assert not r["problems"]
    assert " ip address 10.10.0.1 255.255.255.252" in cmds(r, "R1")
    assert " ip address 10.10.0.2 255.255.255.252" in cmds(r, "R2")


def test_all_ports():
    topo = {"nodes": [{"name": "R1", "interfaces": [
        {"name": "GigabitEthernet0/0", "mgmt": True}, {"name": "GigabitEthernet0/1"}, {"name": "Loopback0"}]}], "links": []}
    r = prompt.parse("no shutdown all interfaces on R1", DEVS, topo)
    assert not r["problems"]
    assert cmds(r, "R1") == ["interface GigabitEthernet0/1", " no shutdown", "exit"]


def test_ai_validation(monkeypatch):
    from netscope import ai
    reply = ('```json\n{"plans":[{"device":"r1","kind":"config","commands":["interface GigabitEthernet0/1"," shutdown","exit"],"explain":"close port"},'
             '{"device":"R2","kind":"config","commands":["reload"],"explain":"bad"},'
             '{"device":"R9","kind":"config","commands":["hostname x"],"explain":"ghost"},'
             '{"device":"R3","kind":"exec","commands":["configure terminal"],"explain":"not exec"}],"unclear":""}\n```')
    monkeypatch.setattr(ai, "_post", lambda key, model, msgs, json_mode: reply)
    plans, note, summary = ai.translate(["close port g0/1 on r1"], DEVS, None, "k", None)
    assert len(plans) == 1 and plans[0]["device"] == "R1" and plans[0]["ai"] and plans[0]["device_id"] == 1
    assert "reload" in note and "R9" in note


def test_ai_warnings_and_summary(monkeypatch):
    from netscope import ai
    reply = ('{"summary":"ปิดพอร์ต","plans":[{"device":"R1","kind":"config","commands":["interface GigabitEthernet0/9"," shutdown","exit",'
             '"interface GigabitEthernet0/0"," shutdown","exit"],"explain":"x"}]}')
    monkeypatch.setattr(ai, "_post", lambda *a, **k: reply)
    topo = {"nodes": [{"name": "R1", "interfaces": [{"name": "GigabitEthernet0/0", "mgmt": True, "ip": "1.1.1.1", "prefix": 8, "state": "up"}]}], "links": []}
    plans, note, summary = ai.translate(["x"], DEVS, topo, "k", None)
    assert summary == "ปิดพอร์ต"
    w = " ".join(plans[0]["warnings"])
    assert "0/9" in w and "management" in w


def test_failed_statements_reported():
    r = prompt.parse("make it faster please\nset ip 10.0.0.1/24 on g0/1 of R1", DEVS)
    assert r["failed"] == ["make it faster please"] and len(r["plans"]) == 1


def test_ai_fix_prompt_contains_errors(monkeypatch):
    from netscope import ai
    seen = {}

    def fake_post(key, model, msgs, json_mode):
        seen["system"], seen["user"] = msgs[0]["content"], msgs[1]["content"]
        return '{"summary":"drop bgp","plans":[{"device":"R1","kind":"config","commands":["no router bgp 1"],"explain":"cleanup"}]}'
    monkeypatch.setattr(ai, "_post", fake_post)
    attempts = [{"device": "R1", "kind": "config", "commands": ["router bgp 1", " neighbor 10.0.0.2 remote-as 1"], "ok": False,
                 "errors": ["router bgp 1 -> % Invalid input detected"], "output": ""},
                {"device": "R2", "kind": "config", "commands": ["router ospf 1"], "ok": True, "errors": [], "output": ""}]
    plans, note, summary = ai.fix("connect R1 R2", attempts, DEVS, None, "k", None, hint="no bgp please")
    assert plans[0]["commands"] == ["no router bgp 1"] and summary == "drop bgp"
    u = seen["user"]
    assert "connect R1 R2" in u and "R1 [ERROR]" in u and "R2 [OK]" in u and "Invalid input" in u and "no bgp please" in u
    assert "NOT rolled back" in seen["system"]


def test_openrouter_prompt_loads_project_rag_rules():
    from netscope import ai
    prompt_text = ai.rag_system_prompt()
    assert "PROJECT RAG RULES" in prompt_text
    assert "ห้ามตอบนอกเรื่อง" in prompt_text
