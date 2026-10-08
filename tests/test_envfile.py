import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netscope import envfile  # noqa: E402


def test_dotenv(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text('# comment\nOPENROUTER_API_KEY="sk-or-abc"\nexport OPENROUTER_MODEL=openai/gpt-4o-mini # small\nEMPTY=\nEXISTING=new\n',
                 encoding="utf-8-sig")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setenv("EXISTING", "keep")
    loaded = envfile.load_dotenv(f)
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-abc"
    assert os.environ["OPENROUTER_MODEL"] == "openai/gpt-4o-mini"
    assert os.environ["EXISTING"] == "keep" and "EMPTY" not in loaded
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.delenv("OPENROUTER_MODEL")


def test_missing_file(tmp_path):
    assert envfile.load_dotenv(tmp_path / "nope.env") == []


def test_ngrok_url_pick():
    from netscope import tunnel
    payload = {"tunnels": [{"public_url": "http://abc.ngrok-free.app"}, {"public_url": "https://abc.ngrok-free.app"}]}
    assert tunnel.pick_https_url(payload) == "https://abc.ngrok-free.app"
    assert tunnel.pick_https_url({"tunnels": [{"public_url": "http://x"}]}) == "http://x"
    assert tunnel.pick_https_url({"tunnels": []}) is None


def test_serial_rejects_demo_loopback_before_opening_console():
    import pytest
    from netscope import initconfig

    with pytest.raises(initconfig.ValidationError, match="127.x"):
        initconfig.apply(
            {"type": "serial", "port": "COM3", "baud": 9600},
            {"hostname": "R1", "username": "admin", "password": "Secret123",
             "mgmt_if": "GigabitEthernet0/0", "mgmt_ip": "127.0.0.11", "mgmt_prefix": 8},
            lambda _line: None,
        )


def test_initial_config_rejects_network_and_broadcast_addresses():
    import pytest
    from netscope import initconfig

    base = {"hostname": "R1", "username": "admin", "password": "Secret123",
            "mgmt_if": "GigabitEthernet0/0", "mgmt_prefix": 24}
    for bad in ("192.168.20.0", "192.168.20.255"):
        with pytest.raises(initconfig.ValidationError, match="address"):
            initconfig.normalise({**base, "mgmt_ip": bad})


def test_initial_config_write_memory_is_optional(monkeypatch):
    from netscope import initconfig

    class FakeSession:
        prompt = "Router#"
        def prepare(self): pass
        def run(self, command, timeout=30):
            if command == "show ip interface brief":
                return "GigabitEthernet0/0 unassigned YES unset administratively down down"
            return "Cisco IOS Software, Version 15.6\nRouter uptime is 1 minute"
        def config(self, commands, progress=None): return {"errors": [], "output": "ok"}
        def save(self): raise AssertionError("write memory must not run")
        def close(self): pass

    monkeypatch.setattr(initconfig.devices, "open_console", lambda *a, **k: FakeSession())
    monkeypatch.setattr(initconfig.devices, "session", lambda *a, **k: (_ for _ in ()).throw(initconfig.CliError("offline")))
    monkeypatch.setattr(initconfig.time, "sleep", lambda *_: None)
    # Stop SSH verification immediately while still proving save() was skipped.
    ticks = iter((0, 46, 46))
    monkeypatch.setattr(initconfig.time, "time", lambda: next(ticks, 46))
    out = initconfig.apply(
        {"type": "telnet", "host": "127.0.0.1", "port": 2301},
        {"hostname": "R1", "username": "admin", "password": "Secret123", "mgmt_if": "GigabitEthernet0/0",
         "mgmt_ip": "192.168.20.1", "mgmt_prefix": 24, "save_config": False, "bring_up_all": False},
        lambda _line: None,
    )
    assert out["verified"] is False
