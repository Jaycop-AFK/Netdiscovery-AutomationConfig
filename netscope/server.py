"""HTTP API + static web UI (stdlib only). Run:  python run.py [--host 127.0.0.1] [--port 8080]"""
from __future__ import annotations

import json
import mimetypes
import os
import errno
import re
import socket
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import ai, auth, connectivity, devices, discovery, eve, fakelab, initconfig, parsers, prompt, store
from .safety import BLOCKED
from .transport import AuthError, CliError

WEB = Path(__file__).resolve().parents[1] / "web"
# ------------------------------------------------------------------ background jobs
class Job:
    def __init__(self, title: str):
        self.id, self.title, self.status = uuid.uuid4().hex[:10], title, "running"
        self.lines: list[str] = []
        self.result, self.error, self.started = None, None, time.time()

    def log(self, line: str) -> None:
        self.lines.append(line)

    def view(self, since: int = 0) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status, "lines": self.lines[since:],
                "next": len(self.lines), "result": self.result, "error": self.error}


JOBS: dict[str, Job] = {}


def start_job(title: str, fn, *args, **kw) -> Job:
    job = Job(title)
    JOBS[job.id] = job
    for old in [k for k, j in JOBS.items() if j.status != "running" and time.time() - j.started > 3600]:
        JOBS.pop(old, None)

    def runner():
        try:
            job.result = fn(job.log, *args, **kw)
            job.status = "done"
        except Exception as e:  # noqa: BLE001
            job.error = str(e) or e.__class__.__name__
            job.log(f"ERROR: {job.error}")
            job.status = "error"
            if not isinstance(e, (CliError, ValueError, eve.EveError, initconfig.ValidationError)):
                traceback.print_exc()
    threading.Thread(target=runner, daemon=True).start()
    return job


# ------------------------------------------------------------------ job bodies
def _sim_ips() -> set[str]:
    return {spec[3] for spec in fakelab.SPEC}


def job_apply_plans(log, plans: list[dict], save: bool) -> dict:
    """Send the confirmed plans. A result is not OK if the device rejected a command, could not be reached,
    or a verification ping got 0% (after letting routing converge when the batch changed config)."""
    results = []
    changed = any(p.get("kind") != "exec" for p in plans)
    for p in plans:
        dev = store.get_device(p["device_id"])
        if not dev:
            results.append({"device": p.get("device"), "ok": False, "errors": ["device not found"]})
            continue
        cmds = [c for c in p["commands"] if c.strip()]
        bad = [c for c in cmds if BLOCKED.match(c)]
        if bad:
            results.append({"device": dev["name"], "ok": False, "errors": [f"blocked command: {b}" for b in bad]})
            log(f"✗ {dev['name']}: blocked dangerous command(s) {bad}")
            continue
        log(f"→ {dev['name']}: sending {len(cmds)} line(s) [{p.get('kind', 'config')}]")
        try:
            if p.get("kind") == "exec":
                res = run_exec_checked(log, dev, cmds, settle=changed)
            else:
                r = devices.push_config(dev, cmds, save=save, progress=lambda c: log(f"    {c}"))
                res = {"device": dev["name"], "ok": not r["errors"], "output": r["output"], "errors": r["errors"],
                       "saved": r.get("saved")}
            log(("✓ " if res["ok"] else "✗ ") + dev["name"] + ("" if res["ok"] else ": " + "; ".join(res["errors"])))
        except (CliError, AuthError) as e:
            res = {"device": dev["name"], "ok": False, "errors": [str(e)], "output": "", "conn": True}
            if dev.get("mgmt_ip") in _sim_ips() and not (DEMO["active"] and fakelab.running()):
                res["hint"] = "อุปกรณ์จำลองไม่ได้รันอยู่ (โปรแกรมถูกรีสตาร์ท) — กดปุ่ม 🧪 Demo เพื่อเริ่มใหม่"
            log(f"✗ {dev['name']}: {e}")
        results.append(res)
    ok = all(r["ok"] for r in results)
    store.log_event("Config applied", ", ".join(r["device"] or "?" for r in results), "success" if ok else "warning")
    if changed:
        log("Refreshing interface status ...")
        try:
            discovery.discover(log, crawl=False)
        except Exception as e:  # noqa: BLE001
            log(f"(refresh skipped: {e})")
    return {"results": results, "ok": ok}


_PING = re.compile(r"^\s*ping\b", re.I)


def run_exec_checked(log, dev: dict, cmds: list[str], settle: bool) -> dict:
    """Run show/ping commands; a ping with 0% success counts as a failure (retried while routing converges)."""
    tries = 1 if (DEMO["active"] and fakelab.running()) or not settle else 4
    text, errors = [], []
    for c in cmds:
        out = ""
        for attempt in range(tries):
            out = devices.run_exec(dev, [c])[c]
            if not _PING.match(c) or parsers.parse_ping(out)["ok"] or attempt == tries - 1:
                break
            log(f"    ping 0% — รอ routing converge แล้วลองใหม่ ({attempt + 1}/{tries - 1})")
            time.sleep(8)
        text.append(f"{dev['name']}# {c}\n{out}")
        if _PING.match(c) and not parsers.parse_ping(out)["ok"]:
            errors.append(f"{c}  →  ping ไม่ผ่าน (0% success)")
    return {"device": dev["name"], "ok": not errors, "output": "\n".join(text), "errors": errors}


def _sig(plans: list[dict]) -> str:
    return json.dumps([[p.get("device"), p.get("commands")] for p in plans], sort_keys=True)


def job_autofix(log, goal: str, plans: list[dict], save: bool, max_rounds: int = 3, hint: str = "",
                prior_results: list[dict] | None = None) -> dict:
    """Apply -> verify -> if something failed ask the AI for a correction -> apply again (bounded). Every round is returned."""
    st = ai_settings()
    if not st.get("ai_key"):
        raise ValueError("ต้องมี OpenRouter API key")
    max_rounds = max(1, min(int(max_rounds or 3), 5))
    rounds, cur, seen = [], plans, {_sig(plans)}
    summary = ""
    out = {"results": prior_results, "ok": False} if prior_results else None
    n = 0
    while True:
        n += 1
        if out is None:
            log(f"=== รอบที่ {n}: ส่งคำสั่ง ===")
            out = job_apply_plans(log, cur, save)
        rounds.append({"n": n, "summary": summary, "plans": cur, "results": out["results"], "ok": out["ok"]})
        if out["ok"]:
            log(f"✔ ผ่านแล้ว (รอบที่ {n})")
            break
        if any(r.get("conn") for r in out["results"]):
            log("✗ ต่ออุปกรณ์ไม่ได้ — เป็นปัญหาการเชื่อมต่อ AI แก้ให้ไม่ได้ จึงหยุด")
            break
        if len(rounds) > max_rounds:
            log(f"✗ ครบ {max_rounds} รอบแล้วยังไม่ผ่าน — หยุด")
            break
        if not auth.AI_QUOTA.take(auth.ai_limit()):
            log("✗ โควตา AI ของโหมดแชร์ครบแล้ว — หยุด")
            break
        log("🤖 ส่ง error ให้ AI วิเคราะห์และวางแผนแก้ ...")
        attempts = [{"device": p.get("device"), "kind": p.get("kind"), "commands": p.get("commands"), "ok": r.get("ok"),
                     "errors": r.get("errors"), "output": r.get("output", "")} for p, r in zip(cur, out["results"])]
        try:
            fixed, note, summary = ai.fix(goal, attempts, store.devices(), store.load("topology.json", None),
                                          st["ai_key"], st.get("ai_model"), hint)
        except ai.AiError as e:
            log(f"✗ เรียก AI ไม่สำเร็จ: {e}")
            break
        if not fixed:
            log("✗ AI ไม่เสนอแผนแก้เพิ่ม" + (f": {note}" if note else ""))
            break
        if _sig(fixed) in seen:
            log("✗ AI เสนอแผนซ้ำกับที่เคยลองแล้ว — หยุด")
            break
        seen.add(_sig(fixed))
        log(f"🤖 แผนแก้ (รอบที่ {n + 1}): {summary or '-'}")
        cur, out = fixed, None
    return {"rounds": rounds, "results": rounds[-1]["results"], "ok": rounds[-1]["ok"]}


def job_ping(log, dev_id: int, target: str, count: int, source: str | None) -> dict:
    dev = store.get_device(dev_id)
    if not dev:
        raise ValueError("device not found")
    log(f"{dev['name']}# ping {target}")
    r = connectivity.ping_from(dev, target, count, source)
    log(f"success {r['percent']}% ({r['received']}/{r['sent']})")
    return r


def job_matrix(log) -> dict:
    log("Refreshing interfaces ...")
    discovery.discover(log, crawl=False)
    return connectivity.matrix(log)


def job_initconfig(log, console: dict, params: dict) -> dict:
    return initconfig.apply(console, params, log)


def ai_settings() -> dict:
    """AI settings from data/settings.json; OPENROUTER_API_KEY in the environment is used if no key was saved."""
    st = dict(store.load("settings.json", {}))
    if not st.get("ai_key") and os.getenv("OPENROUTER_API_KEY"):
        st["ai_key"] = os.environ["OPENROUTER_API_KEY"]
    if not st.get("ai_model") and os.getenv("OPENROUTER_MODEL"):
        st["ai_model"] = os.environ["OPENROUTER_MODEL"]
    return st


def wipe_demo() -> None:
    sim_ips = _sim_ips()
    for d in store.devices():
        if d.get("demo") or d.get("mgmt_ip") in sim_ips:
            store.delete_device(d["id"])
    if not store.devices():
        store.save("topology.json", None)
    else:
        topo = store.load("topology.json", None)
        if topo:
            names = {d["name"].lower() for d in store.devices()}
            topo["nodes"] = [n for n in topo["nodes"] if n["name"].lower() in names]
            ids = {n["id"].lower() for n in topo["nodes"]}
            topo["links"] = [l for l in topo["links"] if l["a"]["node"].lower() in ids and l["b"]["node"].lower() in ids]
            store.save("topology.json", topo)


DEMO = {"active": False}      # UI-level flag; the simulator threads themselves cannot be stopped, only reset


def share_block(what: str = "") -> None:
    if auth.SHARE:
        raise PermissionError("โหมดแชร์: ใช้ได้เฉพาะอุปกรณ์จำลอง " + what)


def share_check_console(console: dict) -> None:
    if not auth.SHARE:
        return
    ok = (console.get("type", "telnet") == "telnet" and console.get("host") in ("127.0.0.1", "localhost")
          and int(console.get("port") or 0) in {spec[2] for spec in fakelab.SPEC})
    if not ok:
        raise PermissionError("โหมดแชร์: console ต้องเป็นอุปกรณ์จำลอง (127.0.0.1 พอร์ตจำลอง) เท่านั้น")


def start_sim() -> None:
    try:
        fakelab.start()
        DEMO["active"] = True
    except OSError as e:
        raise CliError("เปิดโหมดจำลองไม่ได้ เพราะ port ถูกใช้อยู่ (ปิด tools/fake_lab.py หรือโปรแกรม NetScope อีกตัวที่เปิดค้างไว้ก่อน — ต้องว่าง 127.0.0.1:2301-2304 และ 127.0.0.11-14:22): "
                       + str(e)) from e


def job_demo(log, reset: bool) -> dict:
    """Start the built-in Cisco IOS simulator, run Initial Config on its 4 devices and discover the topology."""
    was_running = fakelab.running()
    start_sim()
    if not was_running:
        wipe_demo()          # entries left from a previous run point at a simulator that no longer exists
    if reset:
        log("รีเซ็ตอุปกรณ์จำลองกลับเป็นค่าโรงงาน ...")
        wipe_demo()
        fakelab.reset()
    have = {d["name"] for d in store.devices()}
    for d in fakelab.DEVICES.values():
        if d.key in have:
            continue
        log(f"=== {d.key} ({d.kind}) : console 127.0.0.1:{d.console_port} ===")
        initconfig.apply({"type": "telnet", "host": "127.0.0.1", "port": d.console_port},
                         {"hostname": d.key, "domain": "lab.local", "username": "admin", "password": "cisco123",
                          "mgmt_if": "GigabitEthernet0/0", "mgmt_ip": d.listen_ip, "mgmt_prefix": 8, "device_type": d.kind}, log)
        rec = store.find_device(d.key)
        if rec:
            store.upsert_device({"id": rec["id"], "demo": True})
    log("ค้นหา topology ...")
    topo = discovery.discover(log, crawl=False)
    log("พร้อมแล้ว — ลองสั่ง `auto address links from 10.10.0.0/16` แล้ว `enable ospf area 0 on all devices` ในกล่อง Prompt")
    return {"nodes": len(topo["nodes"]), "links": len(topo["links"])}


def job_discovery(log, crawl: bool, creds: list[dict]) -> dict:
    return discovery.discover(log, crawl=crawl, extra_creds=creds)


# ------------------------------------------------------------------ serial helpers
def list_serial_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    likely = re.compile(r"ftdi|prolific|silicon labs|cp210|ch340|ch341|usb[- ]serial|cisco|console", re.I)
    out = [{"port": p.device, "description": p.description or "", "likely_console": bool(likely.search(f"{p.description} {p.manufacturer or ''}"))}
           for p in list_ports.comports()]
    return sorted(out, key=lambda x: (not x["likely_console"], parsers.natural_key(x["port"])))


def probe_console(b: dict) -> dict:
    """Open the console, wake the device and report what we see (hostname/prompt) - used by the 'test console' button."""
    c = b.get("console") or {}
    try:
        # A running EVE node should present a prompt quickly.  Keep a short
        # preflight timeout so a wrong console port/password does not leave
        # the browser spinner stuck for 40-90 seconds.
        s = devices.open_console(c, c.get("username") or None, c.get("password") or None, c.get("enable") or None, timeout=10)
    except AuthError as e:
        return {"ok": False, "error": str(e)}
    except CliError as e:
        return {"ok": False, "error": str(e)}
    try:
        prompt_ = s.prompt
        ver = parsers.parse_version(s.run("show version", 8))
        interfaces = parsers.parse_ip_int_brief(s.run("show ip interface brief", 8))
        return {"ok": True, "prompt": prompt_, "hostname": ver["hostname"], "version": ver["version"], "model": ver["model"],
                "kind": ver["kind"], "factory": prompt_.rstrip("#>") in ("Router", "Switch"),
                "interfaces": interfaces}
    except CliError as e:
        return {"ok": True, "prompt": s.prompt, "warning": str(e)}
    finally:
        s.close()


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "NetScope/1.0"

    def log_message(self, *a):
        pass

    # helpers
    def send_json(self, obj, code=200, headers=None):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except json.JSONDecodeError:
            raise ValueError("invalid JSON body") from None

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method: str):
        url = urlparse(self.path)
        path, q = url.path, parse_qs(url.query)
        try:
            if not self.gate(method, path):
                return
            if path.startswith("/api/"):
                return self.api(method, path, q)
            if method != "GET":
                return self.send_json({"error": "not found"}, 404)
            return self.static(path)
        except PermissionError as e:
            self.send_json({"error": str(e)}, 403)
        except (ValueError, initconfig.ValidationError) as e:
            self.send_json({"error": str(e)}, 400)
        except (CliError, eve.EveError) as e:
            self.send_json({"error": str(e)}, 502)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.send_json({"error": f"{e.__class__.__name__}: {e}"}, 500)

    OPEN_PATHS = ("/login", "/login.html", "/style.css", "/api/login", "/api/auth")

    def gate(self, method: str, path: str) -> bool:
        """Access control (see auth.py): remote requests need a password, and the whole app needs it when one is set."""
        if path in ("/api/login", "/api/auth"):
            if path == "/api/login" and method == "POST":
                self.login()
            else:
                self.send_json({"required": bool(auth.PASSWORD), "share": auth.SHARE})
            return False
        if not auth.PASSWORD:
            if auth.is_remote(self):
                msg = ("เข้าถึงจากภายนอกต้องมีรหัสผ่าน: เปิดโปรแกรมด้วย start.bat --share (แนะนำ — ใช้ได้เฉพาะอุปกรณ์จำลอง) "
                       "หรือใส่ NETSCOPE_PASSWORD=รหัส ในไฟล์ .env")
                if path.startswith("/api/"):
                    self.send_json({"error": msg}, 403)
                else:
                    raw = ("<meta charset=utf-8><body style=\"font:16px sans-serif;background:#0d1218;color:#e6edf5;padding:40px\">"
                           "<h2>🔒 NetScope</h2><p>" + msg + "</p>").encode()
                    self.send_response(403)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                return False
            return True
        if path in self.OPEN_PATHS or auth.valid_cookie(self.headers.get("Cookie", "")):
            return True
        if path.startswith("/api/"):
            self.send_json({"error": "login required", "login": True}, 401)
        else:
            self.send_response(302)
            self.send_header("Location", "/login")
            self.send_header("Content-Length", "0")
            self.end_headers()
        return False

    def login(self) -> None:
        ip = auth.client_ip(self)
        if auth.too_many_failures(ip):
            return self.send_json({"error": "ลองผิดหลายครั้งเกินไป รอ 1 นาทีแล้วลองใหม่"}, 429)
        given = str(self.body().get("password", ""))
        if not auth.check_password(given):
            auth.record_failure(ip)
            time.sleep(0.5)
            return self.send_json({"error": "รหัสไม่ถูกต้อง"}, 401)
        secure = "; Secure" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        self.send_json({"ok": True}, headers={"Set-Cookie": f"ns_auth={auth.token()}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400{secure}"})

    def static(self, path: str):
        rel = "index.html" if path in ("/", "") else ("login.html" if path == "/login" else path.lstrip("/"))
        f = (WEB / rel).resolve()
        if WEB not in f.parents and f != WEB or not f.is_file():
            return self.send_json({"error": "not found"}, 404)
        data = f.read_bytes()
        self.send_response(200)
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/javascript")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---- API
    def api(self, method, path, q):
        if method == "GET" and path == "/api/state":
            return self.send_json({"share": auth.SHARE, "demo": DEMO["active"] and fakelab.running(),
                                   "devices": [store.public(d) for d in store.devices()],
                                   "topology": store.load("topology.json", None),
                                   "activity": store.activity()})
        if method == "DELETE" and path == "/api/topology":
            store.save("topology.json", None)
            store.save("console_cdp.json", {"lab": "", "devices": {}})
            store.log_event("Topology", "cleared from dashboard", "info")
            return self.send_json({"ok": True})
        if method == "GET" and (m := re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)):
            job = JOBS.get(m.group(1))
            if not job:
                return self.send_json({"error": "unknown job"}, 404)
            return self.send_json(job.view(int(q.get("since", ["0"])[0])))

        # devices
        if method == "POST" and path == "/api/devices":
            return self.add_device(self.body())
        if m := re.fullmatch(r"/api/devices/(\d+)", path):
            if method == "DELETE":
                store.delete_device(int(m.group(1)))
                return self.send_json({"ok": True})
        if method == "POST" and (m := re.fullmatch(r"/api/devices/(\d+)/refresh", path)):
            dev = store.get_device(int(m.group(1)))
            if not dev:
                return self.send_json({"error": "device not found"}, 404)
            res = discovery.refresh_device(dev)
            return self.send_json({**res, "topology": store.load("topology.json", None)})
        if method == "POST" and (m := re.fullmatch(r"/api/devices/(\d+)/traffic", path)):
            dev = store.get_device(int(m.group(1)))
            if not dev:
                return self.send_json({"error": "device not found"}, 404)
            with devices.session(dev) as s:
                traffic = devices.collect_traffic(s)
            return self.send_json({"device": dev["name"], "interfaces": traffic, "collected_at": time.time()})

        # initial config
        if method == "POST" and path == "/api/initconfig/preview":
            p = initconfig.normalise(self.body())
            return self.send_json({"commands": initconfig.build_commands(p)})
        if method == "POST" and path == "/api/initconfig/apply":
            b = self.body()
            if not b.get("approved"):
                return self.send_json({"error": "approval required"}, 403)
            share_check_console(b.get("console") or {})
            params = initconfig.normalise(b["params"])  # validate before starting the job
            existing = store.find_device(params["mgmt_ip"])
            if existing and existing.get("name", "").lower() != params["hostname"].lower():
                return self.send_json({
                    "error": f"Management IP {params['mgmt_ip']} ถูกใช้อยู่กับ {existing['name']} แล้ว — ใช้ IP ใหม่สำหรับ {params['hostname']}"
                }, 409)
            job = start_job("Initial config " + b["params"].get("hostname", ""), job_initconfig,
                            b["console"], b["params"])
            return self.send_json({"job": job.id})

        # EVE-NG
        if path.startswith("/api/eve/"):
            share_block("(ไม่มี EVE-NG)")
        if method == "POST" and path == "/api/eve/folder":
            b = self.body()
            c = eve.EveClient(b["host"], b.get("username") or "admin", b.get("password") or "eve", bool(b.get("https")))
            return self.send_json(c.list_folder(b.get("path") or "/"))
        if method == "POST" and path == "/api/eve/nodes":
            b = self.body()
            c = eve.EveClient(b["host"], b.get("username") or "admin", b.get("password") or "eve", bool(b.get("https")))
            return self.send_json({"nodes": c.nodes(b["lab"])})

        # demo mode (built-in simulator)
        if method == "POST" and path == "/api/demo/start":
            return self.send_json({"job": start_job("Demo mode", job_demo, bool(self.body().get("reset"))).id})
        if method == "POST" and path == "/api/demo/blank":
            # blank lab: simulator running at factory state, nothing registered - the user does everything by hand
            start_sim()
            wipe_demo()
            fakelab.reset()
            store.log_event("Demo", "blank lab started", "info")
            return self.send_json({"devices": fakelab.info()})
        if method == "GET" and path == "/api/demo/info":
            return self.send_json({"running": DEMO["active"] and fakelab.running(), "devices": fakelab.info() if DEMO["active"] and fakelab.running() else []})
        if method == "POST" and path == "/api/demo/stop":
            DEMO["active"] = False
            wipe_demo()
            fakelab.reset()
            return self.send_json({"ok": True})

        # discovery
        if method == "POST" and path == "/api/discovery/run":
            b = self.body()
            job = start_job("Auto discovery", job_discovery, bool(b.get("crawl", True)), b.get("creds") or [])
            return self.send_json({"job": job.id})

        # prompt -> plan -> apply
        if method == "POST" and path == "/api/prompt/parse":
            b = self.body()
            return self.send_json(self.parse_prompt(b.get("text", ""), bool(b.get("rules"))))
        if method == "GET" and path == "/api/settings":
            return self.send_json(self.public_settings())
        if method == "POST" and path == "/api/prompt/fix":
            b = self.body()
            st = ai_settings()
            if not st.get("ai_key"):
                raise ValueError("ต้องมี OpenRouter API key ก่อนจึงให้ AI แก้ไขได้")
            if not auth.AI_QUOTA.take(auth.ai_limit()):
                return self.send_json({"error": "โควตา AI ของโหมดแชร์ครบแล้ว (ต่อชั่วโมง)"}, 429)
            try:
                plans, note, summary = ai.fix(b.get("goal", ""), b.get("attempts") or [], store.devices(),
                                              store.load("topology.json", None), st["ai_key"], st.get("ai_model"), b.get("hint", ""))
            except ai.AiError as e:
                return self.send_json({"error": f"AI: {e}"}, 502)
            return self.send_json({"plans": plans, "problems": [note] if note else [], "failed": [], "ai_used": True,
                                   "ai_summary": summary, "source": "ai", "goal": b.get("goal", "")})
        if method == "POST" and path == "/api/settings":
            share_block("(เปลี่ยน API key ไม่ได้)")
            b = self.body()
            st = store.load("settings.json", {})
            if b.get("ai_model"):
                st["ai_model"] = b["ai_model"].strip()
            if b.get("ai_key"):
                st["ai_key"] = b["ai_key"].strip()
            if b.get("clear_key"):
                st.pop("ai_key", None)
            store.save("settings.json", st)
            return self.send_json(self.public_settings())
        if method == "POST" and path == "/api/ai/test":
            share_block()
            st = ai_settings()
            if not st.get("ai_key"):
                raise ValueError("ยังไม่ได้ใส่ OpenRouter API key")
            try:
                return self.send_json({"ok": True, "reply": ai.test(st["ai_key"], st.get("ai_model"))})
            except ai.AiError as e:
                return self.send_json({"error": str(e)}, 502)

        # serial / console helpers
        if method == "GET" and path == "/api/serial/ports":
            share_block("(ไม่มี COM port)")
            return self.send_json({"ports": list_serial_ports()})
        if method == "POST" and path == "/api/console/probe":
            # Request bodies are streams; reading self.body() twice consumes
            # the payload and leaves probe_console with an empty console.
            b = self.body()
            share_check_console(b.get("console") or {})
            return self.send_json(probe_console(b))
        if method == "POST" and path == "/api/discovery/console":
            b = self.body()
            share_check_console(b.get("console") or {})
            return self.send_json(discovery.scan_console_cdp(b.get("console") or {}, b.get("name"), b.get("lab") or ""))
        if method == "POST" and path == "/api/config/apply":
            b = self.body()
            if not b.get("approved"):
                return self.send_json({"error": "approval required: the user must confirm the commands first"}, 403)
            plans = b.get("plans") or []
            if not plans:
                raise ValueError("nothing to apply")
            if b.get("auto_fix") and ai_settings().get("ai_key"):
                job = start_job("Apply + AI auto-fix", job_autofix, b.get("goal", ""), plans, bool(b.get("save", True)),
                                int(b.get("max_rounds") or 3), b.get("hint", ""), b.get("prior_results"))
            else:
                job = start_job("Apply configuration", job_apply_plans, plans, bool(b.get("save", True)))
            return self.send_json({"job": job.id})

        # tests
        if method == "POST" and path == "/api/ping":
            b = self.body()
            job = start_job("Ping", job_ping, int(b["device_id"]), b["target"], int(b.get("count") or 5), b.get("source") or None)
            return self.send_json({"job": job.id})
        if method == "POST" and path == "/api/ping/matrix":
            return self.send_json({"job": start_job("Ping matrix", job_matrix).id})

        return self.send_json({"error": "not found"}, 404)

    def public_settings(self) -> dict:
        st = ai_settings()
        return {"ai_ready": bool(st.get("ai_key")), "ai_model": st.get("ai_model") or ai.DEFAULT_MODEL,
                "has_key": bool(st.get("ai_key")), "key_from_env": bool(os.getenv("OPENROUTER_API_KEY")) and not store.load("settings.json", {}).get("ai_key")}

    def parse_prompt(self, text: str, rules_only: bool = False) -> dict:
        """The prompt goes straight to the AI planner. Without an API key (or if the AI call fails) the built-in
        rule-based parser answers instead and the reason is shown to the user."""
        devs, topo = store.devices(), store.load("topology.json", None)
        st = ai_settings()
        if not devs or rules_only:
            res = prompt.parse(text, devs, topo)
            res["source"] = "rules"
            return res
        if st.get("ai_key") and not auth.AI_QUOTA.take(auth.ai_limit()):
            res = prompt.parse(text, devs, topo)
            res["problems"].insert(0, "โควตา AI ของโหมดแชร์ครบแล้ว (ต่อชั่วโมง) — ใช้ parser ในตัวแทน")
            res["source"] = "rules"
            return res
        if st.get("ai_key"):
            try:
                plans, note, summary = ai.translate([text], devs, topo, st["ai_key"], st.get("ai_model"))
                if plans or note:
                    return {"plans": plans, "problems": [note] if note else [],
                            "failed": [], "ai_used": True, "ai_summary": summary, "source": "ai"}
                reason = "AI ไม่ได้เสนอแผน"
            except ai.AiError as e:
                reason = f"AI ใช้ไม่ได้ ({e})"
            res = prompt.parse(text, devs, topo)
            res["problems"].insert(0, reason + " — ใช้ parser ในตัวแทน")
        else:
            res = prompt.parse(text, devs, topo)
            res["problems"].insert(0, "ยังไม่ได้ตั้ง OpenRouter API key (กดปุ่ม 🤖 มุมกล่อง Prompt) — ตอนนี้ใช้ parser ในตัวซึ่งเข้าใจเฉพาะรูปแบบคำสั่งที่กำหนด")
        res["source"] = "rules"
        return res

    def add_device(self, b: dict):
        """Register an already-configured device (SSH reachable). Verifies the login first."""
        if auth.SHARE and str(b.get("mgmt_ip", "")).strip() not in _sim_ips():
            share_block("(เพิ่มอุปกรณ์จริงไม่ได้)")
        for k in ("name", "mgmt_ip", "username", "password"):
            if not str(b.get(k, "")).strip():
                raise ValueError(f"{k} is required")
        rec = {"name": b["name"].strip(), "kind": b.get("kind") or "router", "mgmt_ip": b["mgmt_ip"].strip(),
               "protocol": b.get("protocol") or "ssh", "port": int(b.get("port") or (23 if b.get("protocol") == "telnet" else 22)),
               "username": b["username"].strip(), "password": b["password"],
               "enable_secret": b.get("enable_secret") or b["password"], "status": "unreachable"}
        try:
            with devices.session(rec) as s:
                info = parsers.parse_version(s.run("show version", 30))
            rec.update({"status": "online", "model": info["model"], "version": info["version"],
                        "kind": info["kind"], "name": info["hostname"] or rec["name"]})
        except (CliError, AuthError) as e:
            return self.send_json({"error": f"Cannot log in to {rec['mgmt_ip']}: {e}"}, 502)
        saved = store.upsert_device(rec)
        store.log_event("Device added", f"{saved['name']} {saved['mgmt_ip']}", "success")
        return self.send_json({"device": store.public(saved)})


class DualStackServer(ThreadingHTTPServer):
    """Listens on IPv6 *and* IPv4 so tunnels/proxies that connect to ::1 or 127.0.0.1 both work."""
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()


def serve(host: str = "127.0.0.1", port: int = 8080) -> None:
    mimetypes.add_type("application/javascript", ".js")
    stale = [d["name"] for d in store.devices() if d.get("mgmt_ip") in _sim_ips()]
    if stale:                                  # the simulator is volatile: forget its devices from the previous run
        wipe_demo()
        print("Removed stale demo devices from the previous run: " + ", ".join(stale))
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not loopback and not auth.PASSWORD:
        raise SystemExit("เปิดให้เครื่องอื่นเข้าถึง (--host " + host + ") ต้องมีรหัสผ่านก่อน: ใช้ --share หรือใส่ NETSCOPE_PASSWORD ใน .env")
    ThreadingHTTPServer.allow_reuse_address = False     # on Windows SO_REUSEADDR would let a 2nd copy silently share the port
    try:
        srv = DualStackServer((host, port), Handler) if ":" in host else ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, 10048):
            raise SystemExit(f"Port {port} is already in use ({e}). Another NetScope may be running - close it or use --port.")
        if ":" in host:                         # no IPv6 on this machine -> plain IPv4
            srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        else:
            raise
    srv.daemon_threads = True
    if auth.SHARE:
        start_sim()                             # shared sessions get a ready (blank) simulated lab
        fakelab.reset()
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0", "::") else host
    print(f"NetScope running at http://{shown}:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("bye")
