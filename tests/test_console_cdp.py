from netscope import discovery, store


def test_console_cdp_builds_link_from_each_device_without_ssh(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    observations = {
        32771: ("R1", "R2", "Ethernet0/1", "Ethernet0/1", "192.168.24.131"),
        32770: ("R2", "R1", "Ethernet0/1", "Ethernet0/1", "192.168.24.132"),
    }

    class Console:
        def __init__(self, port):
            self.name, self.peer, self.local, self.remote, self.ip = observations[port]

        def prepare(self, timeout=5):
            pass

        def run(self, cmd, timeout=15):
            if cmd == "show version":
                return f"Cisco IOS Software, Version 15.6\n{self.name} uptime is 1 minute"
            if cmd == "show ip interface brief":
                return f"Ethernet0/0 {self.ip} YES manual up up\n{self.local} unassigned YES unset up up"
            if cmd == "show cdp neighbors detail":
                return (f"Device ID: {self.peer}.lab.local\nIP address: 192.168.24.132\n"
                        f"Platform: Cisco IOL, Capabilities: Router\n"
                        f"Interface: {self.local}, Port ID (outgoing port): {self.remote}\n")
            raise AssertionError(cmd)

        def close(self):
            pass

    monkeypatch.setattr(discovery.devices, "open_console", lambda c, *args, **kwargs: Console(c["port"]))
    r1 = discovery.scan_console_cdp({"type": "telnet", "host": "192.168.24.129", "port": 32771,
                                     "enable": "secret"}, "R1", "Lab.unl")
    assert r1["topology"]["links"][0]["a"] == {"node": "R1", "port": "Ethernet0/1"}
    assert r1["topology"]["links"][0]["b"] == {"node": "R2", "port": "Ethernet0/1"}
    r2 = discovery.scan_console_cdp({"type": "telnet", "host": "192.168.24.129", "port": 32770},
                                    "R2", "Lab.unl")
    assert r2["topology"]["scanned_devices"] == 2
    assert len(r2["topology"]["links"]) == 1
    assert "secret" not in (tmp_path / "console_cdp.json").read_text()


def test_console_cdp_rejects_wrong_node_before_saving(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)

    class Console:
        def prepare(self, timeout=5):
            pass

        def run(self, cmd, timeout=15):
            return "R1 uptime is 1 minute" if cmd == "show version" else ""

        def close(self):
            pass

    monkeypatch.setattr(discovery.devices, "open_console", lambda *args, **kwargs: Console())
    import pytest
    with pytest.raises(Exception, match="เลือก Node R2"):
        discovery.scan_console_cdp({"type": "telnet", "host": "127.0.0.1", "port": 2301}, "R2")
    assert not (tmp_path / "console_cdp.json").exists()


def test_console_cdp_accepts_factory_hostname_for_selected_eve_node(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)

    class Console:
        def prepare(self, timeout=5): pass
        def run(self, cmd, timeout=15):
            if cmd == "show version": return "Cisco IOS Software, Version 15.4\nRouter uptime is 1 minute"
            if cmd == "show ip interface brief": return "Ethernet0/0 192.168.24.130 YES manual up up"
            if cmd == "show cdp neighbors detail": return ""
            raise AssertionError(cmd)
        def close(self): pass

    monkeypatch.setattr(discovery.devices, "open_console", lambda *args, **kwargs: Console())
    result = discovery.scan_console_cdp({"type": "telnet", "host": "127.0.0.1", "port": 32771}, "R1")
    assert result["name"] == "R1"
    assert result["topology"]["nodes"][0]["name"] == "R1"


def test_recent_console_links_survive_failed_ssh_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store.save("inventory.json", [{"id": 1, "name": "R1", "mgmt_ip": "192.168.24.131",
                                    "username": "admin", "password": "test"}])
    neighbor = {"protocol": "cdp", "name": "R2", "ip": None, "platform": "Cisco IOS",
                "capabilities": ["Router"], "local_if": "Ethernet0/1", "remote_if": "Ethernet0/1"}
    store.save("console_cdp.json", {"lab": "lab.unl", "devices": {"r1": {
        "name": "R1", "info": {"kind": "router"}, "interfaces": [],
        "neighbors": [neighbor], "scanned_at": discovery.time.time()}}})
    monkeypatch.setattr(discovery, "collect_device", lambda _: (_ for _ in ()).throw(OSError("SSH down")))
    result = discovery.discover(lambda _: None, crawl=False)
    assert result["source"] == "console_cdp"
    assert len(result["links"]) == 1
    assert store.load("topology.json", {})["links"] == result["links"]


def test_expired_console_observations_are_not_reused():
    old = discovery.time.time() - discovery.CONSOLE_CDP_MAX_AGE - 1
    assert discovery._fresh_console_observations({"devices": {"r1": {"scanned_at": old}}}) == []


def test_console_topology_resolves_factory_cdp_names_by_ip_and_kind():
    observations = [
        {"name": "R1", "info": {"kind": "router"},
         "interfaces": [{"name": "Ethernet0/0", "ip": "192.168.24.130", "state": "up"}],
         "neighbors": [{"name": "Router", "ip": "192.168.24.131", "local_if": "Ethernet0/0",
                         "remote_if": "Ethernet0/0", "protocol": "cdp", "platform": "", "capabilities": []}]},
        {"name": "R2", "info": {"kind": "router"},
         "interfaces": [{"name": "Ethernet0/0", "ip": "192.168.24.131", "state": "up"}],
         "neighbors": [{"name": "Switch", "ip": None, "local_if": "Ethernet0/0",
                         "remote_if": "Ethernet0/0", "protocol": "cdp", "platform": "", "capabilities": []}]},
        {"name": "SW1", "info": {"kind": "switch"},
         "interfaces": [{"name": "Ethernet0/0", "ip": None, "state": "up"}], "neighbors": []},
    ]
    topo = discovery._console_topology(observations)
    assert {n["name"] for n in topo["nodes"]} == {"R1", "R2", "SW1"}
    assert {link["b"]["node"] for link in topo["links"]} == {"R2", "SW1"}


def test_console_topology_reattaches_registered_devices(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    store.save("inventory.json", [{"id": 7, "name": "R1", "mgmt_ip": "192.168.24.130",
                                    "kind": "router", "status": "online", "model": "IOSv"}])
    topo = discovery._console_topology([{
        "name": "R1", "info": {"kind": "router", "model": "IOSv"},
        "interfaces": [{"name": "Ethernet0/0", "ip": "192.168.24.130", "state": "up"}],
        "neighbors": [],
    }])
    node = topo["nodes"][0]
    assert node["device_id"] == 7 and node["managed"] is True
