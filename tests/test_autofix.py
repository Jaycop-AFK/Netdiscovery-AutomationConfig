import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netscope import ai, server, store  # noqa: E402


@pytest.fixture()
def inv(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    # 127.0.0.99:22 has nobody listening -> connection refused immediately
    d = store.upsert_device({"name": "R1", "kind": "router", "mgmt_ip": "127.0.0.99", "protocol": "ssh", "port": 22,
                             "username": "admin", "password": "x", "status": "online"})
    return d


def plan_for(dev, cmds=("show version",), kind="exec"):
    return {"device_id": dev["id"], "device": dev["name"], "kind": kind, "commands": list(cmds)}


def test_connection_failure_is_flagged_and_not_sent_to_ai(inv, monkeypatch):
    called = []
    monkeypatch.setattr(ai, "fix", lambda *a, **k: called.append(1))
    log = []
    out = server.job_autofix(log.append, "goal", [plan_for(inv)], True, 3)
    assert out["ok"] is False and out["results"][0]["conn"] is True
    assert not called and any("ต่ออุปกรณ์ไม่ได้" in l for l in log)


def test_autofix_loop_stops_on_repeat_and_after_fix(inv, monkeypatch):
    calls = {"n": 0}
    results = iter([
        {"results": [{"device": "R1", "ok": False, "errors": ["x -> % Invalid input"], "output": ""}], "ok": False},
        {"results": [{"device": "R1", "ok": True, "errors": [], "output": ""}], "ok": True},
    ])
    monkeypatch.setattr(server, "job_apply_plans", lambda log, plans, save: next(results))

    def fake_fix(goal, attempts, devs, topo, key, model, hint=""):
        calls["n"] += 1
        assert attempts[0]["errors"] and attempts[0]["ok"] is False
        return [plan_for(inv, ["interface Gi0/1", " no shutdown", "exit"], "config")], "", "fixed it"
    monkeypatch.setattr(ai, "fix", fake_fix)
    out = server.job_autofix(lambda s: None, "goal", [plan_for(inv, ["bad"], "config")], True, 3)
    assert out["ok"] is True and len(out["rounds"]) == 2 and calls["n"] == 1 and out["rounds"][1]["summary"] == "fixed it"


def test_autofix_gives_up_after_max_rounds(inv, monkeypatch):
    bad = {"results": [{"device": "R1", "ok": False, "errors": ["e"], "output": ""}], "ok": False}
    monkeypatch.setattr(server, "job_apply_plans", lambda log, plans, save: bad)
    n = {"i": 0}

    def fake_fix(*a, **k):
        n["i"] += 1
        return [plan_for(inv, [f"cmd{n['i']}"], "config")], "", "again"
    monkeypatch.setattr(ai, "fix", fake_fix)
    out = server.job_autofix(lambda s: None, "g", [plan_for(inv, ["cmd0"], "config")], True, 2)
    assert out["ok"] is False and len(out["rounds"]) == 3 and n["i"] == 2     # initial + 2 fixes


def test_autofix_stops_when_ai_repeats_itself(inv, monkeypatch):
    bad = {"results": [{"device": "R1", "ok": False, "errors": ["e"], "output": ""}], "ok": False}
    monkeypatch.setattr(server, "job_apply_plans", lambda log, plans, save: bad)
    monkeypatch.setattr(ai, "fix", lambda *a, **k: ([plan_for(inv, ["same"], "config")], "", ""))
    out = server.job_autofix(lambda s: None, "g", [plan_for(inv, ["same"], "config")], True, 3)
    assert len(out["rounds"]) == 1
