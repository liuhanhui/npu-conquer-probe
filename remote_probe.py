#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runs ON the Ascend server. Emits JSON: SSH login IPs + NPU occupants.

Intended to be uploaded and executed by the local web service via SSH.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


def run(argv, timeout=20):
    try:
        p = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            universal_newlines=True,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", "not found: " + argv[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return default


def read_environ(pid: int) -> Dict[str, str]:
    raw = read_text("/proc/%d/environ" % pid)
    env = {}
    for item in raw.split("\x00"):
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v
    return env


def read_cmdline(pid: int) -> str:
    raw = read_text("/proc/%d/cmdline")
    if not raw:
        return ""
    return " ".join(x for x in raw.split("\x00") if x)


def status_field(pid: int, field: str) -> str:
    for line in read_text("/proc/%d/status" % pid).splitlines():
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def ppid_of(pid: int) -> int:
    try:
        return int(status_field(pid, "PPid") or "0")
    except ValueError:
        return 0


def parent_chain(pid: int, limit: int = 40) -> List[int]:
    seen = set()
    chain = []
    cur = pid
    for _ in range(limit):
        if cur <= 1 or cur in seen:
            break
        seen.add(cur)
        chain.append(cur)
        cur = ppid_of(cur)
    return chain


def tty_of(pid: int) -> str:
    try:
        link = os.readlink("/proc/%d/fd/0" % pid)
    except Exception:
        return ""
    if link in ("/dev/null", "/dev/zero") or link.endswith("/null"):
        return ""
    if link.startswith("/dev/pts/") or link.startswith("/dev/tty"):
        return link[len("/dev/") :]
    # ignore other fd targets (pipes, sockets, files)
    return ""


def parse_ssh_ip(env: Dict[str, str]) -> str:
    for key in ("SSH_CONNECTION", "SSH_CLIENT"):
        val = (env.get(key) or "").strip()
        if val:
            return val.split()[0]
    return ""


def who_tty_map() -> Dict[str, Dict[str, str]]:
    """tty -> {ip, user, login_time}"""
    mapping: Dict[str, Dict[str, str]] = {}
    for argv in (["who"], ["w", "-h"]):
        code, out, _ = run(argv)
        if code != 0:
            continue
        for line in out.splitlines():
            host_m = re.search(r"\(([^)]+)\)", line)
            host = host_m.group(1).strip() if host_m else ""
            tty_m = re.search(r"\b(pts/\d+|tty\d+)\b", line)
            if not tty_m:
                continue
            tty = tty_m.group(1)
            parts = line.split()
            user = parts[0] if parts else ""
            when = " ".join(parts[2:4]) if len(parts) >= 4 else (parts[2] if len(parts) > 2 else "")
            slot = mapping.setdefault(tty, {"user": user, "login_time": when, "ip": ""})
            if host and host not in ("-", ":0", ":0.0") and not slot.get("ip"):
                slot["ip"] = host
            if user and not slot.get("user"):
                slot["user"] = user
    return mapping


def ss_ssh_peers() -> List[Dict]:
    """Active SSH connections with peer IP and sshd pid."""
    code, out, _ = run(["ss", "-tnp"])
    if code != 0:
        code, out, _ = run(["ss", "-tn"])
    sessions = []
    seen = set()
    for line in out.splitlines():
        if "ESTAB" not in line.upper() and "ESTABLISHED" not in line.upper():
            # ss uses ESTAB
            if not line.strip().startswith("ESTAB"):
                continue
        if ":22" not in line and "sshd" not in line:
            continue
        addrs = re.findall(r"(\d+\.\d+\.\d+\.\d+|\[?[0-9a-fA-F:]+\]?):(\d+)", line)
        if len(addrs) < 2:
            continue
        local_ip, local_port = addrs[0][0].strip("[]"), addrs[0][1]
        peer_ip, peer_port = addrs[1][0].strip("[]"), addrs[1][1]
        if local_port != "22" and peer_port == "22":
            # direction flipped
            peer_ip, local_ip = local_ip, peer_ip
            peer_port, local_port = local_port, peer_port
        if local_port != "22":
            continue
        pids = [int(x) for x in re.findall(r"pid=(\d+)", line)]
        key = (peer_ip, peer_port, tuple(pids))
        if key in seen:
            continue
        seen.add(key)
        sessions.append(
            {
                "remote_ip": peer_ip,
                "remote_port": peer_port,
                "sshd_pids": pids,
                "local_ip": local_ip,
            }
        )
    return sessions


def build_child_index() -> Dict[int, List[int]]:
    """ppid -> [children]"""
    idx: Dict[int, List[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        parent = ppid_of(pid)
        idx.setdefault(parent, []).append(pid)
    return idx


def descendants(root: int, idx: Dict[int, List[int]], limit: int = 5000) -> List[int]:
    out = []
    stack = [root]
    seen = set()
    while stack and len(out) < limit:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        kids = idx.get(cur, [])
        out.extend(kids)
        stack.extend(kids)
    return out


def tgid_of(pid: int) -> int:
    try:
        return int(status_field(pid, "Tgid") or pid)
    except ValueError:
        return pid


def boot_time_epoch() -> float:
    for line in read_text("/proc/stat").splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                break
    return 0.0


def process_start_epoch(pid: int) -> float:
    """Unix epoch (UTC-based absolute) of process start, or 0."""
    try:
        raw = read_text("/proc/%d/stat" % pid)
        rparen = raw.rfind(")")
        fields = raw[rparen + 2 :].split()
        start_ticks = int(fields[19])  # field 22
        clk = os.sysconf("SC_CLK_TCK")
        return boot_time_epoch() + (start_ticks / float(clk))
    except Exception:
        return 0.0


def process_start_time(pid: int) -> str:
    """Process start time as server-local 'YYYY-mm-dd HH:MM:SS' (legacy)."""
    ts = process_start_epoch(pid)
    if ts > 0:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    code, out, _ = run(["ps", "-o", "lstart=", "-p", str(pid)])
    if code == 0 and out.strip():
        return out.strip()
    return ""


def wall_to_epoch(wall: str) -> float:
    """Parse server-local wall clock (from who/w) to unix epoch."""
    if not wall:
        return 0.0
    wall = wall.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%b %d %H:%M", "%a %b %d %H:%M:%S %Y"):
        try:
            dt = datetime.strptime(wall[:28].strip(), fmt)
            # who sometimes omits year
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return time.mktime(dt.timetuple())
        except Exception:
            continue
    return 0.0


def process_cwd(pid: int) -> str:
    try:
        return os.readlink("/proc/%d/cwd" % pid)
    except Exception:
        return ""


def best_cmdline(pid: int, child_idx: Optional[Dict[int, List[int]]] = None) -> str:
    """Prefer real argv; walk parents / tgid because NPU threads often have empty cmdline."""
    candidates = []
    tgid = tgid_of(pid)
    for cur in [pid, tgid] + parent_chain(pid)[:12]:
        cmd = read_cmdline(cur)
        if cmd:
            candidates.append(cmd)

    if not candidates:
        for cur in [pid, tgid] + parent_chain(pid)[:6]:
            code, out, _ = run(["ps", "-o", "args=", "-p", str(cur)], timeout=5)
            if code == 0 and out.strip() and not out.strip().lower().startswith("args"):
                candidates.append(out.strip())

    idx = child_idx if child_idx is not None else build_child_index()
    for child in descendants(tgid, idx, limit=80)[:80]:
        cmd = read_cmdline(child)
        if cmd and any(k in cmd.lower() for k in ("python", "vllm", "torch", "mindspore", ".py")):
            candidates.append(cmd)

    if not candidates:
        return ""

    def score(c: str) -> tuple:
        cl = c.lower()
        s = 0
        if ".py" in cl:
            s += 5
        if "vllm" in cl or "torch" in cl:
            s += 4
        if "--model" in cl or "model=" in cl:
            s += 6
        if len(c) > 40:
            s += 2
        return (s, len(c))

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def _argv_tokens(cmdline: str) -> List[str]:
    # rough split; good enough for --flag value extraction
    return cmdline.split() if cmdline else []


def _flag_value(tokens: List[str], flags: List[str]) -> str:
    for i, tok in enumerate(tokens):
        for fl in flags:
            if tok == fl and i + 1 < len(tokens):
                return tokens[i + 1]
            if tok.startswith(fl + "="):
                return tok.split("=", 1)[1]
    return ""


def parse_launch_info(cmdline: str, env: Dict[str, str], cwd: str) -> Dict[str, str]:
    """Extract model name / script / key serving args."""
    tokens = _argv_tokens(cmdline)
    model = (
        _flag_value(tokens, ["--model", "--model-path", "--model_name_or_path",
                             "--pretrained_model_name_or_path", "--served-model-name",
                             "--served_model_name"])
        or env.get("MODEL_NAME")
        or env.get("VLLM_MODEL")
        or env.get("MODEL")
        or env.get("LLM_MODEL")
        or ""
    )
    # vllm serve <model_path> ...
    if not model:
        for i, tok in enumerate(tokens):
            if tok in ("serve", "run") and i + 1 < len(tokens):
                nxt = tokens[i + 1]
                if not nxt.startswith("-"):
                    model = nxt
                    break
    # bare path that looks like a model dir/file among args
    if not model:
        for tok in tokens:
            if tok.startswith("-"):
                continue
            low = tok.lower()
            if any(x in low for x in ("/qwen", "/llama", "/chatglm", "/deepseek", "/yi-",
                                      "/baichuan", "/mistral", "w8a8", "instruct", "-7b", "-14b",
                                      "-32b", "-72b", "-27b", "/data/")):
                model = tok
                break

    # -m as last resort (avoid matching unrelated short flags earlier)
    if not model:
        model = _flag_value(tokens, ["-m"])

    script = ""
    for tok in tokens:
        if tok.endswith(".py") or tok.endswith(".sh"):
            script = tok
            break
    if not script:
        for tok in tokens:
            base = tok.rsplit("/", 1)[-1]
            if base in ("vllm", "torchrun", "accelerate", "deepspeed") or tok.endswith("/vllm"):
                script = tok
                break
    if not script and len(tokens) >= 2 and "python" in tokens[0].lower():
        script = tokens[1]

    tp = _flag_value(tokens, ["--tensor-parallel-size", "--tensor_parallel_size", "-tp", "--tp"])
    pp = _flag_value(tokens, ["--pipeline-parallel-size", "--pipeline_parallel_size", "-pp"])
    dp = _flag_value(tokens, ["--data-parallel-size", "--data_parallel_size", "-dp", "--dp"])
    port = _flag_value(tokens, ["--port"])
    dtype = _flag_value(tokens, ["--dtype", "--torch-dtype"])
    max_len = _flag_value(tokens, ["--max-model-len", "--max_model_len", "--max-seq-len"])
    gpu_util = _flag_value(tokens, ["--gpu-memory-utilization", "--gpu_memory_utilization"])
    quant = _flag_value(tokens, ["--quantization", "--quantize"])

    model_short = model
    if model and ("/" in model or "\\" in model):
        parts = [p for p in re.split(r"[\\/]", model.rstrip("/\\")) if p]
        model_short = parts[-1] if parts else model

    key_bits = []
    if script:
        key_bits.append("script=" + script)
    if cwd:
        key_bits.append("cwd=" + cwd)
    if tp:
        key_bits.append("tp=" + tp)
    if pp:
        key_bits.append("pp=" + pp)
    if dp:
        key_bits.append("dp=" + dp)
    if port:
        key_bits.append("port=" + port)
    if dtype:
        key_bits.append("dtype=" + dtype)
    if max_len:
        key_bits.append("max_len=" + max_len)
    if gpu_util:
        key_bits.append("gpu_util=" + gpu_util)
    if quant:
        key_bits.append("quant=" + quant)

    interesting = []
    skip = set()
    for i, tok in enumerate(tokens):
        if tok in skip:
            continue
        low = tok.lower()
        if any(k in low for k in ("lora", "trust-remote", "enable-", "disable-",
                                   "block-size", "swap-space", "enforce-eager",
                                   "ascend", "device", "config", "mtp", "speculative")):
            if tok.startswith("--") and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                interesting.append("%s=%s" % (tok.lstrip("-"), tokens[i + 1]))
                skip.add(tokens[i + 1])
            elif tok.startswith("--"):
                interesting.append(tok)
    key_bits.extend(interesting[:6])

    return {
        "model": model,
        "model_short": model_short,
        "script": script,
        "cwd": cwd,
        "key_params": "  ".join(key_bits),
    }


def enrich_tty_ip_from_env(who_map: Dict[str, Dict[str, str]]) -> None:
    """Fill tty->ip using SSH_CLIENT from any live process environ."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        ip = parse_ssh_ip(read_environ(pid))
        if not ip:
            continue
        tty = tty_of(pid)
        if not tty:
            continue
        slot = who_map.setdefault(tty, {"user": "", "login_time": "", "ip": ""})
        if not slot.get("ip"):
            slot["ip"] = ip


def parse_usages(text: str) -> Dict[str, dict]:
    """Best-effort parse of npu-smi info -t usages / info -t common."""
    out = {}
    for line in text.splitlines():
        # NPU ID  Chip  AICore(%)  ...
        m = re.search(r"^\s*(\d+)\s+(\d+)\s+([\d.]+)", line)
        if m:
            npu_id = m.group(1)
            out.setdefault(npu_id, {})
            out[npu_id]["aicore"] = m.group(3)
    return out


def list_npu_ids() -> List[str]:
    code, out, _ = run(["npu-smi", "info", "-l"])
    ids = []
    if code == 0:
        for m in re.finditer(r"NPU\s*ID\s*[:=]\s*(\d+)|^\s*(\d+)\s*$", out, re.I | re.M):
            n = m.group(1) or m.group(2)
            if n and n not in ids:
                ids.append(n)
        for m in re.finditer(r"Total Count\s*[:=]\s*(\d+)", out, re.I):
            total = int(m.group(1))
            if total and not ids:
                ids = [str(i) for i in range(total)]
    return ids


def pids_on_tty(tty: str) -> List[int]:
    found = []
    if not tty:
        return found
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if tty_of(pid) == tty:
            found.append(pid)
    return found


def resolve_ip_for_pid(pid: int, who_map, ss_pid_to_ip: Dict[int, str]) -> Tuple[str, str]:
    chain = parent_chain(pid)
    tgid = tgid_of(pid)
    if tgid != pid:
        chain = [tgid] + [x for x in chain if x != tgid]

    for cur in chain:
        ip = parse_ssh_ip(read_environ(cur))
        if ip:
            return ip, "SSH_CLIENT"
    for cur in chain:
        tty = tty_of(cur)
        if tty and tty in who_map and who_map[tty].get("ip"):
            return who_map[tty]["ip"], "who/" + tty
    for cur in (pid, tgid):
        tty = tty_of(cur)
        if tty and tty in who_map and who_map[tty].get("ip"):
            return who_map[tty]["ip"], "tty/" + tty

    tty = tty_of(pid) or tty_of(tgid)
    if tty:
        for peer in pids_on_tty(tty):
            for cur in parent_chain(peer):
                ip = parse_ssh_ip(read_environ(cur))
                if ip:
                    return ip, "pts-peer/" + tty
                if cur in ss_pid_to_ip:
                    return ss_pid_to_ip[cur], "pts-ss/" + tty
                parent = ppid_of(cur)
                if parent in ss_pid_to_ip:
                    return ss_pid_to_ip[parent], "pts-ss-parent/" + tty

    for cur in chain:
        if cur in ss_pid_to_ip:
            return ss_pid_to_ip[cur], "ss"
        parent = ppid_of(cur)
        if parent in ss_pid_to_ip:
            return ss_pid_to_ip[parent], "ss/parent"
    return "", "unknown"


def parse_npu_smi(text: str):
    devices = []
    occupants = []
    device_re = re.compile(
        r"\|\s*(\d+)\s+([A-Za-z0-9._-]+)\s*\|\s*(\S+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|"
    )
    mem_re = re.compile(
        r"\|\s*(\d+)\s*\|\s*\S+\s*\|\s*\S+\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*/\s*([\d.]+)\s*\|"
    )
    proc_re = re.compile(
        r"\|\s*(\d+)\s+(\d+)\s*\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*([\d.]+)\s*\|"
    )
    seen = set()
    for line in text.splitlines():
        if "No running processes" in line:
            continue
        m = device_re.search(line)
        if m and (m.group(2)[0].isalpha() or "Ascend" in m.group(2)):
            npu_id = m.group(1)
            if npu_id not in seen:
                devices.append(
                    {
                        "npu_id": npu_id,
                        "name": m.group(2),
                        "health": m.group(3),
                        "power": m.group(4),
                        "temp": m.group(5),
                        "aicore": "",
                        "hbm_used": "",
                        "hbm_total": "",
                    }
                )
                seen.add(npu_id)
            continue
        m = mem_re.search(line)
        if m:
            for d in devices:
                if d["npu_id"] == m.group(1) and not d["aicore"]:
                    d["aicore"] = m.group(2)
                    d["hbm_used"] = m.group(3)
                    d["hbm_total"] = m.group(4)
            continue
        m = proc_re.search(line)
        if m:
            occupants.append(
                {
                    "npu_id": m.group(1),
                    "chip_id": m.group(2),
                    "pid": int(m.group(3)),
                    "process_name": m.group(4).strip(),
                    "process_mem_mb": m.group(5),
                }
            )
    return devices, occupants


def davinci_pids():
    mapping = {}
    for dev in sorted(Path("/dev").glob("davinci[0-9]*")):
        m = re.search(r"davinci(\d+)$", dev.name)
        if not m:
            continue
        npu_id = m.group(1)
        code, out, err = run(["fuser", str(dev)])
        blob = (out or "") + "\n" + (err or "")
        code2, out2, _ = run(["lsof", "-t", str(dev)])
        if code2 == 0:
            blob += "\n" + out2
        for tok in re.findall(r"\b(\d+)\b", blob):
            mapping.setdefault(int(tok), set()).add(npu_id)
    return mapping


def collect():
    errors = []
    who_map = who_tty_map()
    enrich_tty_ip_from_env(who_map)
    raw_sessions = ss_ssh_peers()
    ss_pid_to_ip = {}
    for s in raw_sessions:
        for pid in s.get("sshd_pids") or []:
            ss_pid_to_ip[pid] = s["remote_ip"]

    child_idx = build_child_index()

    # Build login sessions keyed by remote IP
    sessions_by_ip: Dict[str, dict] = {}

    for s in raw_sessions:
        ip = s["remote_ip"]
        entry = sessions_by_ip.setdefault(
            ip,
            {
                "remote_ip": ip,
                "connections": 0,
                "sshd_pids": [],
                "ttys": [],
                "login_times": [],
                "login_ts_list": [],
                "process_count": 0,
                "top_processes": [],
                "npu_pids": [],
            },
        )
        entry["connections"] += 1
        entry["sshd_pids"].extend(s.get("sshd_pids") or [])

    for tty, info in who_map.items():
        ip = info.get("ip") or ""
        if not ip:
            continue
        entry = sessions_by_ip.setdefault(
            ip,
            {
                "remote_ip": ip,
                "connections": 0,
                "sshd_pids": [],
                "ttys": [],
                "login_times": [],
                "login_ts_list": [],
                "process_count": 0,
                "top_processes": [],
                "npu_pids": [],
            },
        )
        if tty not in entry["ttys"]:
            entry["ttys"].append(tty)
        if info.get("login_time") and info["login_time"] not in entry["login_times"]:
            entry["login_times"].append(info["login_time"])
            ts = wall_to_epoch(info["login_time"])
            if ts:
                entry.setdefault("login_ts_list", []).append(ts)

    # Collect notable processes under each sshd
    for ip, entry in sessions_by_ip.items():
        procs = []
        seen_pids = set()
        for spid in entry["sshd_pids"]:
            for pid in descendants(spid, child_idx):
                if pid in seen_pids:
                    continue
                seen_pids.add(pid)
                name = status_field(pid, "Name")
                if name in ("sshd", "bash", "sh", "zsh", "sudo", "su"):
                    continue
                cmd = read_cmdline(pid) or read_cmdline(tgid_of(pid))
                if not cmd:
                    continue
                procs.append(
                    {
                        "pid": pid,
                        "name": name,
                        "cmdline": cmd[:180],
                    }
                )
        entry["process_count"] = len(procs)

        def rank(p):
            c = (p["cmdline"] + p["name"]).lower()
            score = 0
            for kw in ("python", "torch", "train", "vllm", "mindspore", "npu", "cuda"):
                if kw in c:
                    score -= 10
            return (score, p["pid"])

        procs.sort(key=rank)
        entry["top_processes"] = procs[:12]

    # NPU
    code, out, err = run(["npu-smi", "info"], timeout=25)
    devices, occupants = [], []
    if code == 127:
        errors.append("npu-smi not found on server")
    elif code != 0 and not out.strip():
        errors.append("npu-smi info failed: %s" % (err.strip() or code))
    else:
        devices, occupants = parse_npu_smi(out)

    code_u, out_u, _ = run(["npu-smi", "info", "-t", "usages"], timeout=20)
    usages = parse_usages(out_u) if code_u == 0 else {}
    if not devices:
        for npu_id in list_npu_ids() or sorted(usages.keys()) or ["0", "1"]:
            u = usages.get(npu_id, {})
            devices.append(
                {
                    "npu_id": npu_id,
                    "name": "Ascend",
                    "health": "",
                    "power": "",
                    "temp": "",
                    "aicore": u.get("aicore", ""),
                    "hbm_used": "",
                    "hbm_total": "",
                }
            )
    else:
        for d in devices:
            if not d.get("aicore") and d["npu_id"] in usages:
                d["aicore"] = usages[d["npu_id"]].get("aicore", "")

    if not occupants:
        for pid, npu_ids in davinci_pids().items():
            for npu_id in sorted(npu_ids):
                occupants.append(
                    {
                        "npu_id": npu_id,
                        "chip_id": "",
                        "pid": pid,
                        "process_name": status_field(pid, "Name"),
                        "process_mem_mb": "",
                        "note": "from /dev/davinci*",
                    }
                )

    enriched = []
    uniq = {}
    # child index already built; rebuild once more for best_cmdline child scan cache
    for occ in occupants:
        key = (occ["npu_id"], occ["pid"])
        if key in uniq:
            continue
        uniq[key] = True
        pid = occ["pid"]
        tgid = tgid_of(pid)
        cmd = best_cmdline(pid, child_idx)
        env = read_environ(tgid) or read_environ(pid)
        # merge parent env for MODEL_* if missing
        if not any(k in env for k in ("MODEL_NAME", "VLLM_MODEL", "MODEL", "LLM_MODEL")):
            for cur in parent_chain(tgid)[:8]:
                penv = read_environ(cur)
                for k in ("MODEL_NAME", "VLLM_MODEL", "MODEL", "LLM_MODEL"):
                    if k in penv and k not in env:
                        env[k] = penv[k]
        cwd = process_cwd(tgid) or process_cwd(pid)
        launch = parse_launch_info(cmd, env, cwd)
        started_ts = process_start_epoch(tgid) or process_start_epoch(pid)
        started = (
            datetime.fromtimestamp(started_ts).strftime("%Y-%m-%d %H:%M:%S")
            if started_ts
            else (process_start_time(tgid) or process_start_time(pid))
        )
        ip, src = resolve_ip_for_pid(pid, who_map, ss_pid_to_ip)
        tty = tty_of(pid) or tty_of(tgid)
        item = {
            "npu_id": occ["npu_id"],
            "chip_id": occ.get("chip_id", ""),
            "pid": pid,
            "tgid": tgid,
            "process_name": occ.get("process_name") or status_field(pid, "Name"),
            "process_mem_mb": occ.get("process_mem_mb", ""),
            "cmdline": (cmd[:400] if cmd else ""),
            "start_time": started,
            "start_ts": started_ts,
            "model": launch.get("model", ""),
            "model_short": launch.get("model_short", ""),
            "script": launch.get("script", ""),
            "cwd": launch.get("cwd", ""),
            "key_params": launch.get("key_params", ""),
            "remote_ip": ip,
            "remote_source": src,
            "tty": tty,
            "note": occ.get("note", "") if ip else (occ.get("note") or "no SSH/IP found"),
        }
        enriched.append(item)
        if ip:
            entry = sessions_by_ip.setdefault(
                ip,
                {
                    "remote_ip": ip,
                    "connections": 0,
                    "sshd_pids": [],
                    "ttys": [],
                    "login_times": [],
                    "login_ts_list": [],
                    "process_count": 0,
                    "top_processes": [],
                    "npu_pids": [],
                },
            )
            if pid not in entry["npu_pids"]:
                entry["npu_pids"].append(pid)
            if tty and tty not in entry["ttys"]:
                entry["ttys"].append(tty)
            already = {p["pid"] for p in entry["top_processes"]}
            if pid not in already:
                summary = item["model_short"] or item["script"] or item["process_name"]
                entry["top_processes"].insert(
                    0,
                    {
                        "pid": pid,
                        "name": item["process_name"],
                        "cmdline": item["cmdline"]
                        or ("[NPU%s] %s" % (item["npu_id"], summary)),
                        "start_time": started,
                        "start_ts": started_ts,
                        "model": item["model_short"],
                    },
                )
                entry["process_count"] = max(entry["process_count"], len(entry["top_processes"]))

    enriched.sort(key=lambda x: (int(x["npu_id"]) if str(x["npu_id"]).isdigit() else 0, x["pid"]))
    sessions = sorted(sessions_by_ip.values(), key=lambda s: (-len(s["npu_pids"]), s["remote_ip"]))

    return {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timestamp_ts": time.time(),
        "hostname": read_text("/etc/hostname").strip() or os.uname()[1],
        "devices": devices,
        "occupants": enriched,
        "sessions": sessions,
        "errors": errors,
    }


if __name__ == "__main__":
    try:
        data = collect()
    except Exception as exc:
        data = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "hostname": "",
            "devices": [],
            "occupants": [],
            "sessions": [],
            "errors": ["probe crashed: %s" % exc],
        }
    json.dump(data, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
