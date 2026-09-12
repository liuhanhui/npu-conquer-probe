"use strict";

const dns = require("dns").promises;
const fs = require("fs");
const { Client } = require("ssh2");
const { getSettings, getHost, probeExists } = require("./config");

const dnsCache = new Map();

function nowStamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtLocal(ts) {
  const n = Number(ts);
  if (!n || n <= 0 || Number.isNaN(n)) return "";
  return nowStampFromDate(new Date(n * 1000));
}

function nowStampFromDate(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function reverseHostname(ip) {
  if (!ip) return "";
  if (dnsCache.has(ip)) return dnsCache.get(ip);
  let name = "";
  try {
    const hosts = await Promise.race([
      dns.reverse(ip),
      new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 1500)),
    ]);
    name = (hosts && hosts[0] ? String(hosts[0]) : "").replace(/\.$/, "");
  } catch {
    name = "";
  }
  dnsCache.set(ip, name);
  return name;
}

function localizeTimes(data) {
  const localTs = fmtLocal(data.timestamp_ts);
  if (localTs) {
    data.timestamp_server = data.timestamp || "";
    data.timestamp = localTs;
  }
  for (const occ of data.occupants || []) {
    const local = fmtLocal(occ.start_ts);
    if (local) {
      occ.start_time_server = occ.start_time || "";
      occ.start_time = local;
    }
  }
  for (const sess of data.sessions || []) {
    const list = sess.login_ts_list || [];
    if (list.length) {
      sess.login_times_server = [...(sess.login_times || [])];
      sess.login_times = list.map(fmtLocal).filter(Boolean);
    }
    for (const proc of sess.top_processes || []) {
      const local = fmtLocal(proc.start_ts);
      if (local) proc.start_time = local;
    }
  }
  return data;
}

function withTimeout(promise, ms, label = "timeout") {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error(label)), ms)),
  ]);
}

function sshConnect(hostCfg) {
  const cfg = getSettings();
  if (cfg.demoMode) return Promise.reject(new Error("演示模式不能连接远程"));
  if (!hostCfg.password) {
    return Promise.reject(new Error(`${hostCfg.name} 未配置密码`));
  }
  return new Promise((resolve, reject) => {
    const client = new Client();
    const timer = setTimeout(() => {
      client.end();
      reject(new Error(`SSH connect timeout: ${hostCfg.host}`));
    }, 15000);
    client
      .on("ready", () => {
        clearTimeout(timer);
        resolve(client);
      })
      .on("error", (err) => {
        clearTimeout(timer);
        reject(err);
      })
      .connect({
        host: hostCfg.host,
        port: Number(hostCfg.port || 22),
        username: hostCfg.username,
        password: hostCfg.password,
        readyTimeout: 12000,
        tryKeyboard: false,
        agent: undefined,
      });
  });
}

function sftpPut(client, localPath, remotePath) {
  return new Promise((resolve, reject) => {
    client.sftp((err, sftp) => {
      if (err) return reject(err);
      sftp.fastPut(localPath, remotePath, (e) => {
        sftp.end();
        if (e) reject(e);
        else resolve();
      });
    });
  });
}

function sshExec(client, command, { timeoutMs = 40000, stdin = null } = {}) {
  return new Promise((resolve, reject) => {
    client.exec(command, (err, stream) => {
      if (err) return reject(err);
      let out = "";
      let errout = "";
      const timer = setTimeout(() => {
        stream.close();
        reject(new Error(`exec timeout: ${command.slice(0, 80)}`));
      }, timeoutMs);
      stream
        .on("close", (code) => {
          clearTimeout(timer);
          resolve({ code: code ?? 0, out, err: errout });
        })
        .on("data", (d) => {
          out += d.toString("utf8");
        });
      stream.stderr.on("data", (d) => {
        errout += d.toString("utf8");
      });
      if (stdin != null) {
        stream.write(stdin);
        stream.end();
      }
    });
  });
}

class Collector {
  constructor() {
    this._cache = null;
    this._lastError = "";
    this._lastOkAt = "";
    this._timer = null;
  }

  demoServer(hostCfg) {
    return {
      id: hostCfg.id,
      name: hostCfg.name,
      server_ip: hostCfg.host,
      timestamp: nowStamp(),
      hostname: `demo-${hostCfg.id}`,
      devices: [
        {
          npu_id: "0",
          name: "Ascend910B",
          health: "OK",
          power: "210",
          temp: "48",
          aicore: "91.0",
          hbm_used: "42000",
          hbm_total: "65536",
        },
      ],
      occupants: [
        {
          npu_id: "0",
          chip_id: "0",
          pid: 22481,
          tgid: 22481,
          process_name: "python",
          process_mem_mb: "18000",
          cmdline: "python serve.py --model /data/models/Qwen2.5-72B --port 8000",
          start_time: "2026-09-12 09:15:22",
          model: "/data/models/Qwen2.5-72B",
          model_short: "Qwen2.5-72B",
          script: "serve.py",
          cwd: "/home/demo",
          key_params: "port=8000",
          remote_ip: "192.168.9.56",
          remote_source: "demo",
          tty: "pts/3",
          note: "",
          alias: "示例同事A",
          server_id: hostCfg.id,
          server_name: hostCfg.name,
        },
      ],
      sessions: [
        {
          remote_ip: "192.168.9.56",
          alias: "示例同事A",
          connections: 1,
          sshd_pids: [22001],
          ttys: ["pts/3"],
          login_times: ["2026-09-12 10:01"],
          process_count: 1,
          top_processes: [{ pid: 22481, name: "python", cmdline: "python serve.py" }],
          npu_pids: [22481],
        },
      ],
      errors: [],
      mode: "demo",
      ok: true,
    };
  }

  async tagServer(data, hostCfg) {
    localizeTimes(data);
    const aliases = getSettings().ipAliases;
    const sid = hostCfg.id;
    const sname = hostCfg.name;
    data.id = sid;
    data.name = sname;
    data.server_ip = hostCfg.host;
    for (const occ of data.occupants || []) {
      const ip = occ.remote_ip || "";
      const host = await reverseHostname(ip);
      occ.client_hostname = host;
      occ.alias = aliases[ip] || host;
      occ.server_id = sid;
      occ.server_name = sname;
    }
    for (const sess of data.sessions || []) {
      const ip = sess.remote_ip || "";
      const host = await reverseHostname(ip);
      sess.client_hostname = host;
      sess.alias = aliases[ip] || host;
      sess.server_id = sid;
      sess.server_name = sname;
    }
    return data;
  }

  async sshCollectOne(hostCfg) {
    if (!probeExists()) throw new Error("缺少 remote_probe.py");
    const cfg = getSettings();
    const client = await sshConnect(hostCfg);
    try {
      await sftpPut(client, cfg.probeLocalPath, cfg.probeRemotePath);
      const { code, out, err } = await sshExec(
        client,
        `python3 ${cfg.probeRemotePath} 2>/tmp/npu_who_probe.err`,
        { timeoutMs: 45000 }
      );
      if (code !== 0 && !String(out).trim()) {
        throw new Error(`远程探针失败(exit=${code}): ${err.trim() || out}`);
      }
      const text = String(out).trim();
      const start = text.indexOf("{");
      const end = text.lastIndexOf("}");
      if (start < 0 || end < 0) {
        throw new Error(`远程未返回 JSON: ${(err || text).slice(0, 500)}`);
      }
      const data = JSON.parse(text.slice(start, end + 1));
      if (err && err.trim()) {
        data.errors = data.errors || [];
        data.errors.push(`stderr: ${err.trim().slice(0, 300)}`);
      }
      data.mode = "live";
      data.ok = true;
      return await this.tagServer(data, hostCfg);
    } finally {
      client.end();
    }
  }

  async collectOneSafe(hostCfg, forceDemo = false) {
    const cfg = getSettings();
    try {
      if (forceDemo || cfg.demoMode) {
        return await this.tagServer(this.demoServer(hostCfg), hostCfg);
      }
      return await this.sshCollectOne(hostCfg);
    } catch (exc) {
      return {
        id: hostCfg.id,
        name: hostCfg.name,
        server_ip: hostCfg.host,
        timestamp: nowStamp(),
        hostname: "",
        devices: [],
        occupants: [],
        sessions: [],
        errors: [String(exc.message || exc)],
        mode: "error",
        ok: false,
      };
    }
  }

  aggregate(servers) {
    const errors = [];
    const occupants = [];
    const sessions = [];
    for (const s of servers) {
      for (const e of s.errors || []) errors.push(`[${s.name || s.id}] ${e}`);
      occupants.push(...(s.occupants || []));
      sessions.push(...(s.sessions || []));
    }
    const okAny = servers.some((s) => s.ok);
    const modes = new Set(servers.map((s) => s.mode));
    let mode = "mixed";
    if (modes.size === 1 && modes.has("live")) mode = "live";
    else if (modes.size === 1 && modes.has("demo")) mode = "demo";
    return {
      timestamp: nowStamp(),
      servers,
      occupants,
      sessions,
      errors,
      mode,
      ok: okAny,
      server_ip: servers[0] ? servers[0].server_ip : "",
      hostname: servers[0] ? servers[0].hostname || "" : "",
      devices: servers[0] ? servers[0].devices || [] : [],
    };
  }

  async refresh(forceDemo = false) {
    const hosts = getSettings().hosts;
    const results = await Promise.all(hosts.map((h) => this.collectOneSafe(h, forceDemo)));
    const byId = Object.fromEntries(results.map((s) => [s.id, s]));
    const ordered = hosts.map((h) => byId[h.id]).filter(Boolean);
    const data = this.aggregate(ordered);

    if (!data.ok && this._cache && this._cache.ok) {
      const cached = { ...this._cache, stale: true };
      cached.errors = [...(data.errors || []), "部分/全部刷新失败，仍显示上次成功数据"];
      return cached;
    }
    this._cache = data;
    this._lastError = (data.errors || []).join("; ");
    if (data.ok) this._lastOkAt = nowStamp();
    return data;
  }

  async get() {
    if (this._cache) return { ...this._cache };
    return this.refresh();
  }

  findOccupant(pid, serverId = "") {
    const matches = [];
    for (const occ of (this._cache && this._cache.occupants) || []) {
      const p = Number(occ.pid || 0);
      const t = Number(occ.tgid || 0);
      if (p !== pid && t !== pid) continue;
      if (serverId && occ.server_id && occ.server_id !== serverId) continue;
      matches.push(occ);
    }
    if (!matches.length) return null;
    if (matches.length > 1 && !serverId) return null;
    return matches[0];
  }

  async killProcess(pid, force = false, serverId = "") {
    if (pid <= 1) throw new Error("非法 PID");
    let occ = this.findOccupant(pid, serverId);
    if (!occ) {
      if (!serverId) {
        let n = 0;
        for (const o of (this._cache && this._cache.occupants) || []) {
          if (Number(o.pid) === pid || Number(o.tgid) === pid) n += 1;
        }
        if (n > 1) throw new Error("多台机器存在相同 PID，请指定机器后再结束");
      }
      throw new Error("只能结束当前列表中的 NPU 占用进程，请先刷新后再试");
    }
    serverId = occ.server_id || serverId;
    const target = Number(occ.tgid || occ.pid || pid);
    const meta = {
      pid: Number(occ.pid || pid),
      tgid: target,
      npu_id: occ.npu_id,
      model: occ.model_short || occ.model || "",
      cmdline: String(occ.cmdline || "").slice(0, 200),
      remote_ip: occ.remote_ip || "",
      server_id: serverId,
      server_name: occ.server_name || "",
    };

    const hostCfg = getHost(serverId);
    const client = await sshConnect(hostCfg);
    try {
      const script = `
set -e
PID=${target}
FORCE=${force ? 1 : 0}
if [ ! -d /proc/$PID ]; then
  echo JSON:{"ok":true,"already_dead":true,"target":$PID}
  exit 0
fi
PGID=$(ps -o pgid= -p "$PID" 2>/dev/null | tr -d ' ' || true)
TGID=$(awk '/^Tgid:/{print $2}' /proc/$PID/status 2>/dev/null || echo "$PID")
KILL_TARGET=\${PGID:-$TGID}
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
`;
      const { code, out, err } = await sshExec(client, "bash -s", {
        timeoutMs: 30000,
        stdin: script,
      });
      const result = {
        ok: code === 0,
        exit_code: code,
        stdout: String(out).trim().slice(-500),
        stderr: String(err).trim().slice(0, 300),
        meta,
      };
      for (const line of String(out).split(/\r?\n/)) {
        if (line.startsWith("JSON:")) {
          try {
            Object.assign(result, JSON.parse(line.slice(5)));
          } catch {
            /* ignore */
          }
        }
      }
      if (result.alive) {
        result.ok = false;
        result.message = "进程仍存活，可再试强制结束";
      } else if (result.already_dead) {
        result.message = "进程已不存在";
      } else {
        result.message = "已发送结束信号";
      }
      return result;
    } finally {
      client.end();
    }
  }

  status() {
    const cfg = getSettings();
    return {
      hosts: cfg.hosts.map((h) => ({
        id: h.id,
        name: h.name,
        host: h.host,
        port: h.port,
      })),
      host: cfg.host,
      port: cfg.port,
      username: cfg.username,
      refresh_seconds: cfg.refreshSeconds,
      demo_mode: cfg.demoMode,
      last_ok_at: this._lastOkAt,
      last_error: this._lastError,
      has_cache: this._cache != null,
      runtime: "node",
    };
  }

  startPolling() {
    const cfg = getSettings();
    const interval = Math.max(3, cfg.refreshSeconds) * 1000;
    const tick = async () => {
      try {
        await this.refresh();
      } catch (e) {
        this._lastError = String(e.message || e);
      }
    };
    tick();
    this._timer = setInterval(tick, interval);
    if (this._timer.unref) this._timer.unref();
  }

  stopPolling() {
    if (this._timer) clearInterval(this._timer);
    this._timer = null;
  }
}

const collector = new Collector();

module.exports = { collector, withTimeout };
