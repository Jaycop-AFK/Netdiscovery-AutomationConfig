"""Optional helper: start an ngrok tunnel to the local server and report its public URL."""
from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import threading
import time
import urllib.request

API = "http://127.0.0.1:4040/api/tunnels"


def pick_https_url(payload: dict) -> str | None:
    """ngrok's /api/tunnels JSON -> the https public URL (falls back to the first tunnel)."""
    tunnels = payload.get("tunnels") or []
    for t in tunnels:
        if str(t.get("public_url", "")).startswith("https://"):
            return t["public_url"]
    return tunnels[0].get("public_url") if tunnels else None


def start_ngrok(port: int, code: str | None, say=print, wait: float = 25.0) -> subprocess.Popen | None:
    exe = shutil.which("ngrok")
    if not exe:
        say("! ไม่พบคำสั่ง ngrok — ติดตั้งจาก https://ngrok.com/download แล้วรัน: ngrok config add-authtoken <token ของคุณ>")
        return None
    proc = subprocess.Popen([exe, "http", str(port), "--log=stdout"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    atexit.register(lambda: proc.poll() is None and proc.terminate())
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1)
        if proc.poll() is not None:
            say("! ngrok ปิดตัวทันที — ยังไม่ได้ตั้ง authtoken? รัน: ngrok config add-authtoken <token>  "
                "หรือมี ngrok ตัวอื่นเปิดอยู่แล้ว (ปิดก่อน แล้วลองใหม่)")
            return None
        try:
            with urllib.request.urlopen(API, timeout=2) as r:
                url = pick_https_url(json.loads(r.read().decode()))
        except (OSError, ValueError):
            continue
        if url:
            say("=" * 64)
            say(f" PUBLIC LINK : {url}")
            if code:
                say(f" ACCESS CODE : {code}")
            say(" ส่งลิงก์ + รหัสให้เพื่อน (ngrok แบบฟรีจะมีหน้าเตือนก่อน กด Visit Site) ปิดแชร์: กด Ctrl+C")
            say("=" * 64)
            return proc
    say("! รอ ngrok นานเกินไป — เปิดดู http://127.0.0.1:4040 เพื่อดูสถานะ")
    return proc


def start_in_background(port: int, code: str | None) -> None:
    threading.Thread(target=lambda: (time.sleep(1.5), start_ngrok(port, code)), daemon=True).start()
