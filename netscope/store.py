"""Tiny JSON-file persistence (inventory, last topology, activity log). Thread-safe, atomic writes."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("NETSCOPE_DATA", Path(__file__).resolve().parents[1] / "data"))
_LOCK = threading.RLock()


def _path(name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / name


def load(name: str, default):
    with _LOCK:
        try:
            return json.loads(_path(name).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default


def save(name: str, value) -> None:
    with _LOCK:
        p = _path(name)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)


# ---- inventory ---------------------------------------------------------------
def devices() -> list[dict]:
    return load("inventory.json", [])


def get_device(dev_id: int) -> dict | None:
    return next((d for d in devices() if d["id"] == int(dev_id)), None)


def find_device(name_or_ip: str) -> dict | None:
    key = (name_or_ip or "").lower()
    return next((d for d in devices() if d["name"].lower() == key or d.get("mgmt_ip") == name_or_ip), None)


def upsert_device(dev: dict) -> dict:
    with _LOCK:
        items = devices()
        if dev.get("id"):
            for i, d in enumerate(items):
                if d["id"] == dev["id"]:
                    items[i] = {**d, **dev}
                    save("inventory.json", items)
                    return items[i]
        existing = next((d for d in items if d.get("mgmt_ip") and d.get("mgmt_ip") == dev.get("mgmt_ip")), None)
        if existing:
            existing.update({k: v for k, v in dev.items() if v not in (None, "")})
            save("inventory.json", items)
            return existing
        dev["id"] = max([d["id"] for d in items], default=0) + 1
        items.append(dev)
        save("inventory.json", items)
        return dev


def delete_device(dev_id: int) -> None:
    with _LOCK:
        save("inventory.json", [d for d in devices() if d["id"] != int(dev_id)])


def public(dev: dict) -> dict:
    """Device record without secrets, for the browser."""
    hidden = {"password", "enable_secret"}
    out = {k: v for k, v in dev.items() if k not in hidden}
    out["has_password"] = bool(dev.get("password"))
    return out


# ---- activity log ------------------------------------------------------------
def log_event(title: str, detail: str = "", level: str = "info") -> None:
    with _LOCK:
        items = load("activity.json", [])
        items.append({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "title": title, "detail": detail, "level": level})
        save("activity.json", items[-300:])


def activity(limit: int = 60) -> list[dict]:
    return list(reversed(load("activity.json", [])[-limit:]))
