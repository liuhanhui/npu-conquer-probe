from __future__ import annotations

import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import paramiko

from .config import get_host, get_settings

ROOT = Path(__file__).resolve().parent.parent
PROBE_LOCAL = ROOT / "remote_probe.py"

_dns_cache: Dict[str, str] = {}
_dns_lock = threading.Lock()


def reverse_hostname(ip: str) -> str:
    if not ip:
        return ""
    with _dns_lock:
        if ip in _dns_cache:
            return _dns_cache[ip]
    name = ""
    try:
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(1.5)
        try:
            host, _aliases, _ips = socket.gethostbyaddr(ip)
            name = (host or "").rstrip(".")
        finally:
            socket.setdefaulttimeout(old)
    except Exception:
        name = ""
    with _dns_lock:
        _dns_cache[ip] = name
    return name


def _fmt_local(ts: float) -> str:
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _localize_times(data: Dict[str, Any]) -> Dict[str, Any]:
    ts = data.get("timestamp_ts")
    local_ts = _fmt_local(ts) if ts else ""
    if local_ts:
        data["timestamp_server"] = data.get("timestamp", "")
        data["timestamp"] = local_ts

    for occ in data.get("occupants") or []:
        local = _fmt_local(occ.get("start_ts"))
        if local:
            occ["start_time_server"] = occ.get("start_time", "")
            occ["start_time"] = local

    for sess in data.get("sessions") or []:
        login_ts_list = sess.get("login_ts_list") or []
        if login_ts_list:
            sess["login_times_server"] = list(sess.get("login_times") or [])
            sess["login_times"] = [_fmt_local(t) for t in login_ts_list if _fmt_local(t)]
        for proc in sess.get("top_processes") or []:
            local = _fmt_local(proc.get("start_ts"))
            if local:
                proc["start_time"] = local
    return data


class Collector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Optional[Dict[str, Any]] = None
        self._last_error: str = ""
        self._last_ok_at: str = ""

    def _demo_server(self, host_cfg: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": host_cfg["id"],
            "name": host_cfg["name"],
            "server_ip": host_cfg["host"],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "hostname": "demo-" + host_cfg["id"],
            "devices": [
                {
                    "npu_id": "0",
                    "name": "Ascend910B",
                    "health": "OK",
                    "power": "210",
                    "temp": "48",
                    "aicore": "91.0",
                    "hbm_used": "42000",
                    "hbm_total": "65536",
                }
            ],
            "occupants": [
                {
                    "npu_id": "0",
                    "chip_id": "0",
                    "pid": 22481,
                    "tgid": 22481,
                    "process_name": "python",
                    "process_mem_mb": "18000",
                    "cmdline": "python serve.py --model /data/models/Qwen2.5-72B --port 8000",
                    "start_time": "2026-09-12 09:15:22",
                    "model": "/data/models/Qwen2.5-72B",
                    "model_short": "Qwen2.5-72B",
                    "script": "serve.py",
                    "cwd": "/home/demo",
                    "key_params": "port=8000",
                    "remote_ip": "192.168.9.56",
                    "remote_source": "demo",
                    "tty": "pts/3",
                    "note": "",
                    "alias": "示例同事A",
                    "server_id": host_cfg["id"],
                    "server_name": host_cfg["name"],
                }
            ],
            "sessions": [
                {
                    "remote_ip": "192.168.9.56",
                    "alias": "示例同事A",
                    "connections": 1,
                    "sshd_pids": [22001],
                    "ttys": ["pts/3"],
                    "login_times": ["2026-09-12 10:01"],
                    "process_count": 1,
                    "top_processes": [
                        {"pid": 22481, "name": "python", "cmdline": "python serve.py"}
                    ],
                    "npu_pids": [22481],
                }
            ],
            "errors": [],
            "mode": "demo",
            "ok": True,
        }

    def _tag_server(self, data: Dict[str, Any], host_cfg: Dict[str, Any]) -> Dict[str, Any]:
        data = _localize_times(data)
        aliases = get_settings()["ip_aliases"]
        sid, sname, sip = host_cfg["id"], host_cfg["name"], host_cfg["host"]
        data["id"] = sid
        data["name"] = sname
        data["server_ip"] = sip
        for occ in data.get("occupants") or []:
            ip = occ.get("remote_ip") or ""
            host = reverse_hostname(ip)
            occ["client_hostname"] = host
            occ["alias"] = aliases.get(ip, "") or host
            occ["server_id"] = sid
            occ["server_name"] = sname
        for sess in data.get("sessions") or []:
            ip = sess.get("remote_ip") or ""
            host = reverse_hostname(ip)
            sess["client_hostname"] = host
            sess["alias"] = aliases.get(ip, "") or host
            sess["server_id"] = sid
            sess["server_name"] = sname
        return data

    def _ssh_connect(self, host_cfg: Dict[str, Any]):
        if get_settings()["demo_mode"]:
            raise RuntimeError("演示模式不能连接远程")
        if not host_cfg.get("password"):
            raise RuntimeError("%s 未配置密码" % host_cfg.get("name"))
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host_cfg["host"],
            port=int(host_cfg.get("port") or 22),
            username=host_cfg["username"],
            password=host_cfg["password"],
            timeout=12,
            allow_agent=False,
            look_for_keys=False,
        )
        return client

    def _ssh_collect_one(self, host_cfg: Dict[str, Any]) -> Dict[str, Any]:
        if not PROBE_LOCAL.exists():
            raise RuntimeError("缺少 remote_probe.py")
        cfg = get_settings()
        client = self._ssh_connect(host_cfg)
        try:
            sftp = client.open_sftp()
            remote_path = cfg["probe_remote_path"]
            sftp.put(str(PROBE_LOCAL), remote_path)
            sftp.close()

            cmd = "python3 %s 2>/tmp/npu_who_probe.err" % remote_path
            stdin, stdout, stderr = client.exec_command(cmd, timeout=40)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            if code != 0 and not out.strip():
                raise RuntimeError("远程探针失败(exit=%s): %s" % (code, err.strip() or out))
            text = out.strip()
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < 0:
                raise RuntimeError("远程未返回 JSON: %s" % (err or text[:500]))
            data = json.loads(text[start : end + 1])
            if err.strip():
                data.setdefault("errors", []).append("stderr: " + err.strip()[:300])
            data["mode"] = "live"
            data["ok"] = True
            return self._tag_server(data, host_cfg)
        finally:
            client.close()

    def _collect_one_safe(self, host_cfg: Dict[str, Any], force_demo: bool = False) -> Dict[str, Any]:
        cfg = get_settings()
        try:
            if force_demo or cfg["demo_mode"]:
                return self._tag_server(self._demo_server(host_cfg), host_cfg)
            return self._ssh_collect_one(host_cfg)
        except Exception as exc:
            return {
                "id": host_cfg["id"],
                "name": host_cfg["name"],
                "server_ip": host_cfg["host"],
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "hostname": "",
                "devices": [],
                "occupants": [],
                "sessions": [],
                "errors": [str(exc)],
                "mode": "error",
                "ok": False,
            }

    def _aggregate(self, servers: List[Dict[str, Any]]) -> Dict[str, Any]:
        errors: List[str] = []
        occupants: List[Dict[str, Any]] = []
        sessions: List[Dict[str, Any]] = []
        for s in servers:
            for e in s.get("errors") or []:
                errors.append("[%s] %s" % (s.get("name") or s.get("id"), e))
            for occ in s.get("occupants") or []:
                occupants.append(occ)
            for sess in s.get("sessions") or []:
                sessions.append(sess)
        ok_any = any(s.get("ok") for s in servers)
        modes = {s.get("mode") for s in servers}
        mode = "live" if modes == {"live"} else ("demo" if "demo" in modes and len(modes) == 1 else "mixed")
        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "servers": servers,
            "occupants": occupants,
            "sessions": sessions,
            "errors": errors,
            "mode": mode,
            "ok": ok_any,
            # backward-compatible: first server detail for old UI pieces
            "server_ip": servers[0]["server_ip"] if servers else "",
            "hostname": servers[0].get("hostname", "") if servers else "",
            "devices": servers[0].get("devices", []) if servers else [],
        }

    def refresh(self, force_demo: bool = False) -> Dict[str, Any]:
        hosts = get_settings()["hosts"]
        servers: List[Dict[str, Any]] = []
        # parallel collect
        with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
            futs = {pool.submit(self._collect_one_safe, h, force_demo): h for h in hosts}
            for fut in as_completed(futs):
                servers.append(fut.result())
        # keep stable order as config
        by_id = {s["id"]: s for s in servers}
        ordered = [by_id[h["id"]] for h in hosts if h["id"] in by_id]

        data = self._aggregate(ordered)
        with self._lock:
            if not data["ok"] and self._cache and self._cache.get("ok"):
                cached = dict(self._cache)
                cached["stale"] = True
                cached["errors"] = list(data.get("errors") or []) + ["部分/全部刷新失败，仍显示上次成功数据"]
                # merge fresh errors onto servers where possible
                return cached
            self._cache = data
            self._last_error = "; ".join(data.get("errors") or [])
            self._last_ok_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if data["ok"] else self._last_ok_at
            return data

    def get(self) -> Dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return dict(self._cache)
        return self.refresh()

    def _find_occupant(self, pid: int, server_id: str = "") -> Optional[Dict[str, Any]]:
        matches = []
        for occ in (self._cache or {}).get("occupants") or []:
            try:
                if int(occ.get("pid") or 0) != pid and int(occ.get("tgid") or 0) != pid:
                    continue
            except (TypeError, ValueError):
                continue
            if server_id and occ.get("server_id") and occ.get("server_id") != server_id:
                continue
            matches.append(occ)
        if not matches:
            return None
        if len(matches) > 1 and not server_id:
            return None  # ambiguous
        return matches[0]

    def kill_process(self, pid: int, force: bool = False, server_id: str = "") -> Dict[str, Any]:
        if pid <= 1:
            raise RuntimeError("非法 PID")
        with self._lock:
            occ = self._find_occupant(pid, server_id)
            if not occ:
                if not server_id:
                    # check ambiguity
                    n = 0
                    for o in (self._cache or {}).get("occupants") or []:
                        try:
                            if int(o.get("pid") or 0) == pid or int(o.get("tgid") or 0) == pid:
                                n += 1
                        except (TypeError, ValueError):
                            pass
                    if n > 1:
                        raise RuntimeError("多台机器存在相同 PID，请指定机器后再结束")
                raise RuntimeError("只能结束当前列表中的 NPU 占用进程，请先刷新后再试")
            server_id = occ.get("server_id") or server_id
            target = int(occ.get("tgid") or occ.get("pid") or pid)
            meta = {
                "pid": int(occ.get("pid") or pid),
                "tgid": target,
                "npu_id": occ.get("npu_id"),
                "model": occ.get("model_short") or occ.get("model") or "",
                "cmdline": (occ.get("cmdline") or "")[:200],
                "remote_ip": occ.get("remote_ip") or "",
                "server_id": server_id,
                "server_name": occ.get("server_name") or "",
            }

        host_cfg = get_host(server_id)
        client = self._ssh_connect(host_cfg)
        try:
            remote = r"""
set -e
PID=%(target)d
FORCE=%(force)d
if [ ! -d /proc/$PID ]; then
  echo JSON:{"ok":true,"already_dead":true,"target":$PID}
  exit 0
fi
PGID=$(ps -o pgid= -p "$PID" 2>/dev/null | tr -d ' ' || true)
TGID=$(awk '/^Tgid:/{print $2}' /proc/$PID/status 2>/dev/null || echo "$PID")
KILL_TARGET=${PGID:-$TGID}
if [ -z "$KILL_TARGET" ] || [ "$KILL_TARGET" = "0" ] || [ "$KILL_TARGET" = "1" ]; then
  KILL_TARGET=$TGID
fi
if [ "$FORCE" = "1" ]; then
  kill -KILL -"$KILL_TARGET" 2>/dev/null || kill -KILL "$TGID" 2>/dev/null || true
else
  kill -TERM -"$KILL_TARGET" 2>/dev/null || kill -TERM "$TGID" 2>/dev/null || true
  sleep 2
  if [ -d /proc/$TGID ] || [ -d /proc/$PID ]; then
    kill -KILL -"$KILL_TARGET" 2>/dev/null || kill -KILL "$TGID" 2>/dev/null || true
    sleep 1
  fi
fi
ALIVE=0
if [ -d /proc/$TGID ] || [ -d /proc/$PID ]; then ALIVE=1; fi
echo JSON:{"ok":true,"already_dead":false,"target":$TGID,"pgid":"$KILL_TARGET","alive":$ALIVE,"forced":$FORCE}
""" % {
                "target": target,
                "force": 1 if force else 0,
            }
            stdin, stdout, stderr = client.exec_command("bash -s", timeout=30)
            stdin.write(remote)
            stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            result: Dict[str, Any] = {
                "ok": code == 0,
                "exit_code": code,
                "stdout": out.strip()[-500:],
                "stderr": err.strip()[:300],
            }
            for line in out.splitlines():
                if line.startswith("JSON:"):
                    try:
                        result.update(json.loads(line[5:]))
                    except Exception:
                        pass
            result["meta"] = meta
            if result.get("alive"):
                result["ok"] = False
                result["message"] = "进程仍存活，可再试强制结束"
            elif result.get("already_dead"):
                result["message"] = "进程已不存在"
            else:
                result["message"] = "已发送结束信号"
            return result
        finally:
            client.close()

    def status(self) -> Dict[str, Any]:
        cfg = get_settings()
        return {
            "hosts": [
                {"id": h["id"], "name": h["name"], "host": h["host"], "port": h["port"]}
                for h in cfg["hosts"]
            ],
            "host": cfg["host"],
            "port": cfg["port"],
            "username": cfg["username"],
            "refresh_seconds": cfg["refresh_seconds"],
            "demo_mode": cfg["demo_mode"],
            "last_ok_at": self._last_ok_at,
            "last_error": self._last_error,
            "has_cache": self._cache is not None,
        }


collector = Collector()


def background_poller(stop_event: threading.Event) -> None:
    cfg = get_settings()
    interval = max(3, cfg["refresh_seconds"])
    collector.refresh()
    while not stop_event.wait(interval):
        collector.refresh()
