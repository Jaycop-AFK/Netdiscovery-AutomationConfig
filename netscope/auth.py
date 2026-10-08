"""Access control for when the app is reachable by other people (dev tunnel / port forwarding / LAN).

* NETSCOPE_PASSWORD set (or --share) -> every request needs the access code (login page + HttpOnly cookie).
* No password: only requests that look local are served. A request is "remote" if the Host header is not
  localhost/127.0.0.1/[::1], the client address is not loopback, or a proxy/tunnel header is present.
* SHARE mode: simulator only (see server.share_block) + AI quota.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque

SECRET = secrets.token_bytes(32)          # new every start -> restarting the app logs everybody out
SHARE = os.getenv("NETSCOPE_SHARE") == "1"
PASSWORD = os.getenv("NETSCOPE_PASSWORD") or ""
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip", "x-original-host",
                  "x-ms-tunnel", "x-tunnel-id", "x-original-url")
_fails: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


def configure_share() -> str:
    """Called by run.py --share: make sure an access code exists and return it."""
    global SHARE, PASSWORD
    SHARE = True
    os.environ["NETSCOPE_SHARE"] = "1"
    if not PASSWORD:
        PASSWORD = secrets.token_urlsafe(6).replace("_", "x").replace("-", "y")[:8]
        os.environ["NETSCOPE_PASSWORD"] = PASSWORD
    return PASSWORD


def token() -> str:
    return hmac.new(SECRET, PASSWORD.encode(), hashlib.sha256).hexdigest()


def valid_cookie(cookie_header: str) -> bool:
    if not PASSWORD:
        return False
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "ns_auth" and hmac.compare_digest(v, token()):
            return True
    return False


def check_password(given: str) -> bool:
    return bool(PASSWORD) and hmac.compare_digest(given.encode(), PASSWORD.encode())


def client_ip(handler) -> str:
    fwd = handler.headers.get("X-Forwarded-For")
    return (fwd.split(",")[0].strip() if fwd else handler.client_address[0]) or "?"


def _hostname(h: str) -> str:
    h = (h or "").strip().lower()
    if h.startswith("["):
        return h.split("]")[0] + "]"
    return h.rsplit(":", 1)[0] if ":" in h else h


def is_remote(handler) -> bool:
    if _hostname(handler.headers.get("Host")) not in _LOCAL_HOSTS:
        return True
    if handler.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        return True
    return any(h in handler.headers for h in _PROXY_HEADERS)


def too_many_failures(ip: str) -> bool:
    now = time.time()
    with _lock:
        q = _fails[ip]
        while q and now - q[0] > 60:
            q.popleft()
        return len(q) >= 8


def record_failure(ip: str) -> None:
    with _lock:
        _fails[ip].append(time.time())


class Quota:
    """Sliding-window call limiter (used to protect the owner's OpenRouter key while sharing)."""

    def __init__(self):
        self.calls: deque = deque()
        self.lock = threading.Lock()

    def take(self, limit: int, window: float = 3600) -> bool:
        if limit <= 0:
            return True
        now = time.time()
        with self.lock:
            while self.calls and now - self.calls[0] > window:
                self.calls.popleft()
            if len(self.calls) >= limit:
                return False
            self.calls.append(now)
            return True


AI_QUOTA = Quota()


def ai_limit() -> int:
    return int(os.getenv("NETSCOPE_AI_LIMIT", "40" if SHARE else "0"))
