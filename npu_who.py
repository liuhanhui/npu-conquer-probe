#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ascend NPU occupancy helper: who is using which NPU, from which IP.

Run on the Ascend server (Linux):
  python3 npu_who.py
  python3 npu_who.py --watch 5
  python3 npu_who.py --json

Requires: npu-smi in PATH. Prefer root or sudo to see other users' /proc.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple


@dataclass
class NpuDevice:
    npu_id: str
    name: str = ""
    health: str = ""
    aicore: str = ""
    hbm_used: str = ""
    hbm_total: str = ""
    temp: str = ""
    power: str = ""


@dataclass
class Occupant:
    npu_id: str
    chip_id: str = ""
    pid: int = 0
    process_name: str = ""
    process_mem_mb: str = ""
    user: str = ""
    cmdline: str = ""
    remote_ip: str = ""
    remote_source: str = ""  # how IP was resolved
    tty: str = ""
    note: str = ""


@dataclass
class Snapshot:
    timestamp: str
    devices: List[NpuDevice] = field(default_factory=list)
    occupants: List[Occupant] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def run_cmd(argv: List[str], timeout: int = 15) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            universal_newlines=True,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout: {' '.join(argv)}"


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return default


def read_environ(pid: int) -> Dict[str, str]:
    raw = read_text(Path(f"/proc/{pid}/environ"))
    if not raw:
        return {}
    env: Dict[str, str] = {}
    for item in raw.split("\x00"):
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        env[k] = v
    return env


def read_cmdline(pid: int) -> str:
    raw = read_text(Path(f"/proc/{pid}/cmdline"))
    if not raw:
        # zombie / kernel thread fallback
        status = read_text(Path(f"/proc/{pid}/status"))
        for line in status.splitlines():
            if line.startswith("Name:"):
                return f"[{line.split(':', 1)[1].strip()}]"
        return ""
    return " ".join(x for x in raw.split("\x00") if x)


def read_status_field(pid: int, field: str) -> str:
    status = read_text(Path(f"/proc/{pid}/status"))
    prefix = f"{field}:"
    for line in status.splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


def read_uid(pid: int) -> Optional[int]:
    # Uid: real effective saved fs
    uid_line = read_status_field(pid, "Uid")
    if not uid_line:
        return None
    try:
        return int(uid_line.split()[0])
    except (ValueError, IndexError):
        return None


def uid_to_user(uid: Optional[int]) -> str:
    if uid is None:
        return ""
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except Exception:
        return str(uid)


def read_ppid(pid: int) -> Optional[int]:
    ppid = read_status_field(pid, "PPid")
    try:
        return int(ppid)
    except (TypeError, ValueError):
        return None


def read_tty(pid: int) -> str:
    try:
        link = os.readlink(f"/proc/{pid}/fd/0")
    except Exception:
        return ""
    if link.startswith("/dev/"):
        return link[len("/dev/") :]
    return link


def parent_chain(pid: int, limit: int = 32) -> List[int]:
    seen: Set[int] = set()
    chain: List[int] = []
    cur = pid
    for _ in range(limit):
        if cur is None or cur <= 1 or cur in seen:
            break
        seen.add(cur)
        chain.append(cur)
        cur = read_ppid(cur) or 0
    return chain


def parse_ssh_client(env: Dict[str, str]) -> Optional[str]:
    # SSH_CLIENT="ip port localport"
    # SSH_CONNECTION="client_ip client_port server_ip server_port"
    for key in ("SSH_CONNECTION", "SSH_CLIENT"):
        val = env.get(key, "").strip()
        if not val:
            continue
        parts = val.split()
        if parts:
            return parts[0]
    return None


def parse_who_map() -> Dict[str, str]:
    """Map TTY -> remote host from `who`."""
    code, out, _ = run_cmd(["who"])
    if code != 0:
        return {}
    mapping: Dict[str, str] = {}
    # e.g. alice pts/3 2026-03-12 10:01 (10.1.2.3)
    pat = re.compile(r"^(\S+)\s+(\S+)\s+.*?(\(([^)]+)\))?\s*$")
    for line in out.splitlines():
        m = pat.match(line.strip())
        if not m:
            continue
        tty = m.group(2)
        host = (m.group(4) or "").strip()
        if host:
            mapping[tty] = host
    return mapping


def ss_ssh_peers() -> Dict[int, str]:
    """Map sshd PID -> peer IP using `ss -tnp`."""
    code, out, _ = run_cmd(["ss", "-tnp"])
    if code != 0:
        code, out, _ = run_cmd(["ss", "-tn"])
        if code != 0:
            return {}
    peers: Dict[int, str] = {}
    # ESTAB ... 10.0.0.1:22 10.1.2.3:54321 users:(("sshd",pid=1234,fd=3))
    for line in out.splitlines():
        if "sshd" not in line and ":22" not in line:
            continue
        # peer is usually the last address before users:
        addrs = re.findall(r"(\d+\.\d+\.\d+\.\d+|\[?[0-9a-fA-F:]+\]?):\d+", line)
        if len(addrs) < 2:
            continue
        peer = addrs[1].strip("[]")
        for m in re.finditer(r'pid=(\d+)', line):
            peers[int(m.group(1))] = peer
    return peers


def resolve_remote_ip(pid: int) -> Tuple[str, str]:
    """Return (ip, source). Empty ip if unknown."""
    who_map = getattr(resolve_remote_ip, "_who_cache", None)
    if who_map is None:
        who_map = parse_who_map()
        resolve_remote_ip._who_cache = who_map  # type: ignore[attr-defined]

    ss_map = getattr(resolve_remote_ip, "_ss_cache", None)
    if ss_map is None:
        ss_map = ss_ssh_peers()
        resolve_remote_ip._ss_cache = ss_map  # type: ignore[attr-defined]

    chain = parent_chain(pid)
    for cur in chain:
        env = read_environ(cur)
        ip = parse_ssh_client(env)
        if ip:
            return ip, f"SSH_CLIENT via pid {cur}"

    for cur in chain:
        tty = read_tty(cur)
        if tty and tty in who_map:
            return who_map[tty], f"who/{tty}"

    for cur in chain:
        name = read_status_field(cur, "Name")
        if name == "sshd" and cur in ss_map:
            return ss_map[cur], f"ss/sshd:{cur}"
        # login shell child of sshd: check parent
        ppid = read_ppid(cur)
        if ppid and ppid in ss_map:
            return ss_map[ppid], f"ss/parent-sshd:{ppid}"

    # last resort: any sshd ancestor in ss_map
    for cur in chain:
        if cur in ss_map:
            return ss_map[cur], f"ss/ancestor:{cur}"

    return "", "unknown"


def clear_resolve_caches() -> None:
    for attr in ("_who_cache", "_ss_cache"):
        if hasattr(resolve_remote_ip, attr):
            delattr(resolve_remote_ip, attr)


def parse_npu_smi_info(text: str) -> Tuple[List[NpuDevice], List[Occupant]]:
    """Parse `npu-smi info` for device summary and process table."""
    devices: List[NpuDevice] = []
    occupants: List[Occupant] = []

    # Device header rows often look like:
    # | 0     Ascend910B   | OK        | 90.0        | 45           |
    # | 0                  | 0000:xx   | 0000:yy     | 72           | 32768 / 65536 |
    device_re = re.compile(
        r"\|\s*(\d+)\s+([A-Za-z0-9._-]+)\s*\|\s*(\S+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|"
    )
    mem_re = re.compile(
        r"\|\s*(\d+)\s*\|\s*\S+\s*\|\s*\S+\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*/\s*([\d.]+)\s*\|"
    )
    # Process table:
    # | NPU     Chip | Process id | Process name | Process memory(MB) |
    # | 0       0    | 12345      | python       | 1024               |
    proc_re = re.compile(
        r"\|\s*(\d+)\s+(\d+)\s*\|\s*(\d+)\s*\|\s*(.*?)\s*\|\s*([\d.]+)\s*\|"
    )

    seen_dev: Set[str] = set()
    for line in text.splitlines():
        if "No running processes" in line:
            continue
        m = device_re.search(line)
        if m:
            name = m.group(2)
            # Prefer chip/name rows like "Ascend910B", skip pure numeric noise.
            if name[0].isalpha() or "Ascend" in name or "NPU" in name.upper():
                npu_id = m.group(1)
                if npu_id not in seen_dev:
                    devices.append(
                        NpuDevice(
                            npu_id=npu_id,
                            name=name,
                            health=m.group(3),
                            power=m.group(4),
                            temp=m.group(5),
                        )
                    )
                    seen_dev.add(npu_id)
                continue

        m = mem_re.search(line)
        if m:
            npu_id = m.group(1)
            for d in devices:
                if d.npu_id == npu_id and not d.aicore:
                    d.aicore = m.group(2)
                    d.hbm_used = m.group(3)
                    d.hbm_total = m.group(4)
                    break
            continue

        m = proc_re.search(line)
        if m:
            occupants.append(
                Occupant(
                    npu_id=m.group(1),
                    chip_id=m.group(2),
                    pid=int(m.group(3)),
                    process_name=m.group(4).strip(),
                    process_mem_mb=m.group(5),
                )
            )

    return devices, occupants


def parse_npu_smi_usages(text: str) -> Dict[str, str]:
    """Best-effort AICore% from `npu-smi info -t usages`."""
    usages: Dict[str, str] = {}
    # NPU ID / Chip ID / AICore(%) ...
    for line in text.splitlines():
        m = re.search(r"^\s*(\d+)\s+(\d+)\s+([\d.]+)", line)
        if m and m.group(1) not in usages:
            usages[m.group(1)] = m.group(3)
    return usages


def pids_holding_davinci() -> Dict[int, Set[str]]:
    """Fallback: map PID -> set of NPU ids via /dev/davinci*."""
    mapping: Dict[int, Set[str]] = {}
    dev_root = Path("/dev")
    if not dev_root.exists():
        return mapping

    devices = sorted(dev_root.glob("davinci[0-9]*"))
    for dev in devices:
        m = re.search(r"davinci(\d+)$", dev.name)
        if not m:
            continue
        npu_id = m.group(1)
        code, out, _ = run_cmd(["fuser", "-v", str(dev)], timeout=10)
        # fuser prints pids on stdout/stderr mixed
        blob = out
        code2, out2, err2 = run_cmd(["lsof", "-t", str(dev)], timeout=10)
        if code2 == 0 and out2.strip():
            blob += "\n" + out2
        if code != 0 and not blob.strip():
            # try reading fuser stderr via combined earlier; already have out
            pass
        for tok in re.findall(r"\b(\d+)\b", blob):
            pid = int(tok)
            mapping.setdefault(pid, set()).add(npu_id)
        # Also parse fuser -v style from stderr by re-running and capturing both
        if not mapping:
            try:
                p = subprocess.run(
                    ["fuser", str(dev)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=10,
                    universal_newlines=True,
                )
                for tok in re.findall(r"\b(\d+)\b", p.stdout or ""):
                    mapping.setdefault(int(tok), set()).add(npu_id)
            except Exception:
                pass
    return mapping


def enrich_occupant(occ: Occupant) -> Occupant:
    if occ.pid <= 0:
        return occ
    uid = read_uid(occ.pid)
    occ.user = uid_to_user(uid)
    cmd = read_cmdline(occ.pid)
    if cmd:
        occ.cmdline = cmd if len(cmd) <= 160 else cmd[:157] + "..."
    if not occ.process_name and cmd:
        occ.process_name = Path(cmd.split()[0]).name
    occ.tty = read_tty(occ.pid)
    ip, src = resolve_remote_ip(occ.pid)
    occ.remote_ip = ip
    occ.remote_source = src
    if not ip:
        occ.note = "no SSH/IP found (local/service/tmux/nohup?)"
    return occ


def collect_snapshot() -> Snapshot:
    clear_resolve_caches()
    snap = Snapshot(timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    code, out, err = run_cmd(["npu-smi", "info"], timeout=20)
    if code == 127:
        snap.errors.append("npu-smi not found. Install Ascend driver toolkit and ensure PATH.")
        return snap
    if code != 0 and not out.strip():
        snap.errors.append(f"npu-smi info failed: {err.strip() or code}")
        return snap

    devices, occupants = parse_npu_smi_info(out)

    code_u, out_u, _ = run_cmd(["npu-smi", "info", "-t", "usages"], timeout=20)
    if code_u == 0:
        usages = parse_npu_smi_usages(out_u)
        for d in devices:
            if not d.aicore and d.npu_id in usages:
                d.aicore = usages[d.npu_id]

    # Fallback discovery if process table empty
    if not occupants:
        davinci = pids_holding_davinci()
        for pid, npu_ids in sorted(davinci.items()):
            for npu_id in sorted(npu_ids):
                occupants.append(
                    Occupant(
                        npu_id=npu_id,
                        pid=pid,
                        process_name=Path(read_cmdline(pid).split()[0]).name
                        if read_cmdline(pid)
                        else "",
                        note="from /dev/davinci*",
                    )
                )

    # Deduplicate by (npu_id, pid)
    uniq: Dict[Tuple[str, int], Occupant] = {}
    for occ in occupants:
        key = (occ.npu_id, occ.pid)
        if key not in uniq:
            uniq[key] = occ
    occupants = [enrich_occupant(o) for o in uniq.values()]
    occupants.sort(key=lambda o: (int(o.npu_id) if o.npu_id.isdigit() else 0, o.pid))

    snap.devices = devices
    snap.occupants = occupants
    if not devices and not occupants and not snap.errors:
        snap.errors.append("Parsed empty result from npu-smi; driver output format may differ.")
    return snap


def pad(s: str, width: int) -> str:
    s = s or ""
    if len(s) > width:
        return s[: width - 1] + "…"
    return s.ljust(width)


def print_table(snap: Snapshot) -> None:
    print(f"=== NPU Who @ {snap.timestamp} ===")
    if snap.errors:
        for e in snap.errors:
            print(f"[!] {e}")
        if not snap.devices and not snap.occupants:
            return

    if snap.devices:
        print("\n-- Devices --")
        print(
            pad("NPU", 5)
            + pad("Name", 14)
            + pad("Health", 8)
            + pad("AICore%", 9)
            + pad("HBM(MB)", 18)
            + pad("Temp", 6)
            + pad("Power", 8)
        )
        for d in snap.devices:
            hbm = ""
            if d.hbm_used or d.hbm_total:
                hbm = f"{d.hbm_used}/{d.hbm_total}"
            print(
                pad(d.npu_id, 5)
                + pad(d.name, 14)
                + pad(d.health, 8)
                + pad(d.aicore, 9)
                + pad(hbm, 18)
                + pad(d.temp, 6)
                + pad(d.power, 8)
            )

    print("\n-- Occupants --")
    if not snap.occupants:
        print("(no processes found on NPU)")
        return

    print(
        pad("NPU", 5)
        + pad("PID", 8)
        + pad("User", 12)
        + pad("RemoteIP", 16)
        + pad("Proc", 16)
        + pad("MemMB", 8)
        + "Cmdline"
    )
    for o in snap.occupants:
        print(
            pad(o.npu_id, 5)
            + pad(str(o.pid), 8)
            + pad(o.user, 12)
            + pad(o.remote_ip or "-", 16)
            + pad(o.process_name, 16)
            + pad(o.process_mem_mb, 8)
            + (o.cmdline or "")
        )

    # Extra detail for communication
    print("\n-- Contact hints --")
    for o in snap.occupants:
        bits = [f"NPU{o.npu_id}", f"pid={o.pid}", f"user={o.user or '?'}"]
        if o.remote_ip:
            bits.append(f"ip={o.remote_ip}")
            bits.append(f"via={o.remote_source}")
        else:
            bits.append(o.note or "ip=?")
        print("  " + " | ".join(bits))


def print_json(snap: Snapshot) -> None:
    payload = {
        "timestamp": snap.timestamp,
        "devices": [asdict(d) for d in snap.devices],
        "occupants": [asdict(o) for o in snap.occupants],
        "errors": snap.errors,
    }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    print()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Query Ascend NPU usage and map occupying processes to remote login IP."
    )
    parser.add_argument(
        "--watch",
        type=float,
        metavar="SEC",
        help="Refresh every SEC seconds",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    parser.add_argument(
        "--once-demo",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    if args.once_demo:
        # Offline demo for local Windows development without npu-smi
        demo = Snapshot(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            devices=[
                NpuDevice(
                    npu_id="0",
                    name="Ascend910B",
                    health="OK",
                    aicore="86.0",
                    hbm_used="42000",
                    hbm_total="65536",
                    temp="51",
                    power="220",
                )
            ],
            occupants=[
                Occupant(
                    npu_id="0",
                    chip_id="0",
                    pid=12345,
                    process_name="python",
                    process_mem_mb="18000",
                    user="alice",
                    cmdline="python train.py --device npu",
                    remote_ip="10.12.34.56",
                    remote_source="demo",
                )
            ],
        )
        print_table(demo)
        return 0

    def once() -> int:
        snap = collect_snapshot()
        if args.json:
            print_json(snap)
        else:
            print_table(snap)
        return 1 if snap.errors and not snap.occupants and not snap.devices else 0

    if args.watch:
        try:
            while True:
                if not args.json:
                    # clear screen when interactive
                    if sys.stdout.isatty():
                        print("\033[2J\033[H", end="")
                rc = once()
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nstopped.")
            return rc
    return once()


if __name__ == "__main__":
    sys.exit(main())
