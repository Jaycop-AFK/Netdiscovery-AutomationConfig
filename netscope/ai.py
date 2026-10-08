"""Optional AI assistant: turns free-form requests the rule-based parser could not understand into IOS command plans.

Uses OpenRouter's OpenAI-compatible API (any chat model, a small/cheap one is enough). Only device names, interface
names/IPs and link topology are sent - never passwords. Output is validated and still goes through the preview/confirm step.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from .parsers import norm_if
from .safety import BLOCKED, EXEC_OK

URL = os.getenv("NETSCOPE_AI_URL", "https://openrouter.ai/api/v1/chat/completions")   # override only for tests
DEFAULT_MODEL = "openai/gpt-4o-mini"
RAG_RULES_PATH = Path(__file__).resolve().parents[1] / "RAG_RULES.md"

SYSTEM = """You are the planning engine of a network-automation tool for Cisco IOS lab devices.
Read the engineer's goal (Thai or English), look at the devices/interfaces/links in the context, and plan the configuration for each device that must change.
Reply with ONLY a JSON object:
{"summary":"<1-3 short sentences in the engineer's language describing the whole plan>",
 "plans":[{"device":"<exact device name>","kind":"config"|"exec","commands":["..."],"explain":"<one short sentence>"}],
 "unclear":"<question for the engineer, only if the goal cannot be done safely>"}
Rules:
- kind "config": commands are entered in global configuration mode. Do NOT include "configure terminal", "end" or "write memory".
  Put sub-mode commands on their own lines indented by one space and finish a sub-mode with "exit"
  (e.g. "interface GigabitEthernet0/1", " ip address 10.0.0.1 255.255.255.0", " no shutdown", "exit").
- kind "exec": only show / ping / traceroute commands (use these to verify, e.g. ping after configuring).
- Use only device names and interface names that appear in the context; full interface names; dotted masks and wildcard masks.
- Never touch the interface marked MGMT. Do not re-address interfaces that already have the right IP. Prefer the smallest change that achieves the goal.
- When asked to make devices reach each other: address the point-to-point links (/30 from a private pool if no addressing exists), enable the requested (or OSPF area 0 by default) routing on the right interfaces, and add exec pings to verify.
- Never output reload, erase, delete, username, enable secret/password, crypto, line vty/con, or copy commands.
- If the goal is ambiguous or refers to something not in the context, return {"summary":"","plans":[],"unclear":"..."}.
No markdown, no commentary."""


def rag_system_prompt(extra: str = "") -> str:
    """Load the project RAG rules and apply them to every planning request."""
    try:
        rules = RAG_RULES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        rules = "ตอบเฉพาะคำถามและข้อมูลใน Context/RAG; ห้ามเดาและห้ามตอบนอกเรื่อง"
    return SYSTEM + "\n\nPROJECT RAG RULES (highest priority):\n" + rules + ("\n" + extra if extra else "")


NL = chr(10)


class AiError(Exception):
    pass


def _context(devs: list[dict], topo: dict | None) -> str:
    lines = []
    nodes = {n["name"].lower(): n for n in (topo or {}).get("nodes", [])}
    for d in devs:
        n = nodes.get(d["name"].lower())
        head = f"- {d['name']} ({d.get('kind', 'router')})"
        if n:
            ports = ", ".join(f"{i['name']} {i['ip'] + '/' + str(i['prefix']) if i.get('ip') and i.get('prefix') else (i.get('ip') or 'no-ip')} {i['state']}{' MGMT' if i.get('mgmt') else ''}"
                              for i in n["interfaces"])
            head += ": " + ports
        lines.append(head)
    if topo and topo.get("links"):
        lines.append("Links: " + "; ".join(f"{l['a']['node']} {l['a']['port']} <-> {l['b']['node']} {l['b']['port']}" for l in topo["links"]))
    return "\n".join(lines)


def _post(key: str, model: str, messages: list[dict], json_mode: bool) -> str:
    body = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 1500}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "X-Title": "NetScope", "HTTP-Referer": "http://localhost"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", {}).get("message", e.reason)
        except Exception:
            msg = e.reason
        raise AiError(f"OpenRouter HTTP {e.code}: {msg}") from e
    except (urllib.error.URLError, OSError) as e:
        raise AiError(f"Cannot reach OpenRouter: {e}") from e
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AiError(f"Unexpected OpenRouter response: {str(data)[:200]}") from None


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise AiError("AI did not return JSON") from None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            raise AiError("AI returned malformed JSON") from None


def _warnings(dev_name: str, cmds: list[str], topo: dict | None) -> list[str]:
    """Sanity checks of AI output against what discovery knows (interfaces that do not exist, bad IPs, MGMT port)."""
    node = next((n for n in (topo or {}).get("nodes", []) if n["name"].lower() == dev_name.lower()), None)
    out = []
    known = {i["name"]: i for i in node["interfaces"]} if node else {}
    cur = None
    for c in cmds:
        m = re.match(r"^\s*interface\s+(\S.*)$", c, re.I)
        if m:
            cur = norm_if(m.group(1).replace(" ", ""))
            if known and cur not in known and not re.match(r"(?i)(loopback|vlan|tunnel)", cur):
                out.append(f"{dev_name}: ไม่พบ interface {cur} บนอุปกรณ์ (จาก discovery ล่าสุด)")
            elif known.get(cur, {}).get("mgmt"):
                out.append(f"{dev_name}: คำสั่งแตะ interface management {cur} — ระวังหลุดการเชื่อมต่อ")
            continue
        m = re.match(r"^\s*ip address\s+(\S+)\s+(\S+)", c, re.I)
        if m:
            try:
                ipaddress.ip_interface(f"{m.group(1)}/{m.group(2)}")
            except ValueError:
                out.append(f"{dev_name}: ค่า IP/mask ไม่ถูกต้อง: {c.strip()}")
    return out


FIX_RULES = """
A previous attempt was already sent to the devices and some commands were rejected. Configuration that succeeded is NOT rolled back.
Produce ONLY the corrective plan: do not repeat commands that succeeded; remove leftovers only if they would cause harm.
Errors that appear right after a rejected mode-entry command (e.g. "router bgp 1") are usually cascading - fix the root cause.
"% Invalid input" on a feature command means this device/image may not support that feature: prefer an alternative the device supports
(e.g. OSPF/EIGRP/RIP/static routes instead of BGP) or, if there is no sensible alternative, return plans:[] with an explanation in "unclear".
The engineer's extra instruction (if any) overrides your own preference."""


def translate(statements: list[str], devs: list[dict], topo: dict | None, key: str, model: str | None) -> tuple[list[dict], str, str]:
    """Plan with the LLM. Returns (plans in prompt.parse format, notes, summary). Raises AiError."""
    msgs = [{"role": "system", "content": rag_system_prompt()},
            {"role": "user", "content": "Context:" + NL + "" + _context(devs, topo) + NL + "" + NL + "Goal:" + NL + "" + (NL + "").join(statements)}]
    return _plan(msgs, devs, topo, key, model)


def fix(goal: str, attempts: list[dict], devs: list[dict], topo: dict | None, key: str, model: str | None,
        hint: str = "") -> tuple[list[dict], str, str]:
    """Ask the LLM for a corrective plan after a failed apply. attempts: [{device, kind, commands, ok, errors, output}]."""
    lines = []
    for a in attempts:
        cmds = [c for c in (a.get("commands") or []) if isinstance(c, str)][:60]
        lines.append(f"- {a.get('device')} [{'OK' if a.get('ok') else 'ERROR'}] sent:" + NL + "    " + (NL + "    ").join(cmds))
        if a.get("errors"):
            lines.append("  device errors: " + " | ".join(str(e) for e in a["errors"][:8]))
        elif not a.get("ok") and a.get("output"):
            lines.append("  device output tail: " + str(a["output"])[-500:])
    user = ("Context (fresh state after the failed attempt):" + NL + "" + _context(devs, topo)
            + NL + "" + NL + "Original goal:" + NL + "" + (goal or "(not recorded)")
            + NL + "" + NL + "Previous attempt results:" + NL + "" + (NL + "").join(lines)
            + (NL + "" + NL + "Engineer's extra instruction:" + NL + "" + hint if hint.strip() else "")
            + NL + "" + NL + "Produce the corrective plan now.")
    msgs = [{"role": "system", "content": rag_system_prompt(FIX_RULES)}, {"role": "user", "content": user}]
    return _plan(msgs, devs, topo, key, model)


def _plan(msgs: list[dict], devs: list[dict], topo: dict | None, key: str, model: str | None) -> tuple[list[dict], str, str]:
    model = model or DEFAULT_MODEL
    try:
        raw = _post(key, model, msgs, json_mode=True)
    except AiError as e:
        if "response_format" in str(e).lower() or "json" in str(e).lower():
            raw = _post(key, model, msgs, json_mode=False)
        else:
            raise
    data = _extract_json(raw)
    by_name = {d["name"].lower(): d for d in devs}
    plans, notes = [], []
    for p in data.get("plans") or []:
        dev = by_name.get(str(p.get("device", "")).lower())
        cmds = [c.rstrip() for c in (p.get("commands") or []) if isinstance(c, str) and c.strip()]
        if not dev or not cmds:
            notes.append(f"AI ระบุอุปกรณ์ที่ไม่มีอยู่: {p.get('device')}")
            continue
        kind = "exec" if p.get("kind") == "exec" else "config"
        bad = [c for c in cmds if BLOCKED.match(c) or (kind == "exec" and not EXEC_OK.match(c)) or "\n" in c]
        if bad:
            notes.append(f"AI เสนอคำสั่งที่ไม่อนุญาต ถูกตัดทิ้ง: {bad[0]}")
            continue
        if len(cmds) > 80:
            notes.append("AI เสนอคำสั่งมากเกินไป ถูกตัดทิ้ง")
            continue
        plans.append({"device_id": dev["id"], "device": dev["name"], "kind": kind, "commands": cmds,
                      "explain": [f"🤖 {p.get('explain') or 'AI generated'}"], "ai": True,
                      "warnings": _warnings(dev["name"], cmds, topo) if kind == "config" else []})
    if data.get("unclear"):
        notes.append("AI ถามกลับ: " + str(data["unclear"]))
    return plans, "; ".join(notes), str(data.get("summary") or "")


def test(key: str, model: str | None) -> str:
    out = _post(key, model or DEFAULT_MODEL,
                [{"role": "system", "content": rag_system_prompt()},
                 {"role": "user", "content": 'Reply with the JSON {"ok":true}'}], json_mode=False)
    return out.strip()[:80]
