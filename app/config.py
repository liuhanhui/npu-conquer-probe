from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)


def _bool(v: str, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip()).strip("-").lower()
    return s or "server"


def _parse_hosts() -> List[Dict[str, Any]]:
    """
    Preferred: NPU_HOSTS JSON array
      [{"id":"a2","name":"A2","host":"192.168.9.179","user":"root","password":"...","port":22}]
    Fallback: single NPU_HOST / NPU_SSH_*  (named by NPU_HOST_NAME, default A2)
    Also accept lines:
      NPU_HOSTS=A2|192.168.9.179|root|Huawei@123,A1|192.168.9.178|root|pass
    """
    raw = (os.getenv("NPU_HOSTS") or "").strip()
    default_user = os.getenv("NPU_SSH_USER", "root")
    default_pass = os.getenv("NPU_SSH_PASSWORD", "")
    default_port = int(os.getenv("NPU_SSH_PORT", "22"))
    hosts: List[Dict[str, Any]] = []

    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            for item in arr:
                name = str(item.get("name") or item.get("id") or item.get("host") or "server")
                hid = str(item.get("id") or _slug(name))
                hosts.append(
                    {
                        "id": hid,
                        "name": name,
                        "host": str(item["host"]),
                        "port": int(item.get("port") or default_port),
                        "username": str(item.get("username") or item.get("user") or default_user),
                        "password": str(item.get("password") or default_pass),
                    }
                )
        except Exception:
            hosts = []

    if not hosts and raw:
        # A2|ip|user|pass,A1|ip|user|pass
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            bits = [b.strip() for b in part.split("|")]
            if len(bits) < 2:
                continue
            name, host = bits[0], bits[1]
            user = bits[2] if len(bits) > 2 and bits[2] else default_user
            password = bits[3] if len(bits) > 3 and bits[3] else default_pass
            port = int(bits[4]) if len(bits) > 4 and bits[4] else default_port
            hosts.append(
                {
                    "id": _slug(name),
                    "name": name,
                    "host": host,
                    "port": port,
                    "username": user,
                    "password": password,
                }
            )

    if not hosts:
        host = os.getenv("NPU_HOST", "192.168.9.179")
        name = os.getenv("NPU_HOST_NAME", "A2")
        hosts.append(
            {
                "id": _slug(name),
                "name": name,
                "host": host,
                "port": default_port,
                "username": default_user,
                "password": default_pass,
            }
        )

    # de-dupe by host:port, keep first name
    seen = set()
    uniq: List[Dict[str, Any]] = []
    for h in hosts:
        key = "%s:%s" % (h["host"], h["port"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(h)
    return uniq


@lru_cache(maxsize=1)
def get_settings():
    aliases_raw = os.getenv("IP_ALIASES", "")
    aliases: Dict[str, str] = {}
    for part in aliases_raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        ip, name = part.split("=", 1)
        aliases[ip.strip()] = name.strip()

    hosts = _parse_hosts()
    primary = hosts[0]
    return {
        "hosts": hosts,
        "host": primary["host"],
        "port": primary["port"],
        "username": primary["username"],
        "password": primary["password"],
        "refresh_seconds": int(os.getenv("REFRESH_SECONDS", "8")),
        "demo_mode": _bool(os.getenv("DEMO_MODE", ""), default=False),
        "listen_host": os.getenv("LISTEN_HOST", "127.0.0.1"),
        "listen_port": int(os.getenv("LISTEN_PORT", "8787")),
        "ip_aliases": aliases,
        "probe_remote_path": os.getenv("PROBE_REMOTE_PATH", "/tmp/npu_who_remote_probe.py"),
    }


def get_host(server_id: str) -> Dict[str, Any]:
    cfg = get_settings()
    for h in cfg["hosts"]:
        if h["id"] == server_id or h["name"] == server_id or h["host"] == server_id:
            return h
    raise KeyError("unknown server: %s" % server_id)
