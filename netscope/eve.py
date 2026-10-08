"""Minimal EVE-NG (Community/Pro) REST client: list labs and nodes with their telnet console ports."""
from __future__ import annotations

import http.cookiejar
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request


class EveError(Exception):
    pass


class EveClient:
    def __init__(self, host: str, username: str = "admin", password: str = "eve", https: bool = False):
        self.host = host.strip()
        self.base = f"{'https' if https else 'http'}://{self.host}"
        ctx = ssl._create_unverified_context() if https else None      # EVE uses a self-signed certificate
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            *( [urllib.request.HTTPSHandler(context=ctx)] if ctx else []))
        self.username, self.password = username, password

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("message", e.reason)
            except Exception:
                msg = e.reason
            raise EveError(f"EVE-NG: {msg} (HTTP {e.code})") from e
        except (urllib.error.URLError, OSError) as e:
            raise EveError(f"Cannot reach EVE-NG at {self.host}: {e}") from e

    def login(self) -> None:
        self._call("POST", "/api/auth/login", {"username": self.username, "password": self.password, "html5": "-1"})

    def list_folder(self, path: str = "/") -> dict:
        self.login()
        quoted = urllib.parse.quote(path if path.startswith("/") else "/" + path)
        return self._call("GET", f"/api/folders{quoted}").get("data", {})

    def nodes(self, lab_path: str) -> list[dict]:
        """lab_path like 'Lab1.unl' or 'Folder/Lab1.unl'. Returns [{name, id, console_host, console_port, status}]."""
        self.login()
        lab = urllib.parse.quote(lab_path.strip("/"))
        data = self._call("GET", f"/api/labs/{lab}/nodes").get("data", {}) or {}
        out = []
        for n in data.values():
            m = re.match(r"telnet://([^:]+):(\d+)", n.get("url", "") or "")
            host = m.group(1) if m else self.host
            if host in ("0.0.0.0", ""):
                host = self.host
            out.append({"id": n.get("id"), "name": n.get("name"), "template": n.get("template"),
                        "running": n.get("status") == 2, "console_host": host,
                        "console_port": int(m.group(2)) if m else None})
        return sorted(out, key=lambda x: (x["id"] or 0))

    def start_all(self, lab_path: str) -> None:
        self.login()
        self._call("GET", f"/api/labs/{urllib.parse.quote(lab_path.strip('/'))}/nodes/start")
