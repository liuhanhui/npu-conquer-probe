"use strict";

const fs = require("fs");
const path = require("path");
const dotenv = require("dotenv");

const ROOT = path.resolve(__dirname, "..");
dotenv.config({ path: path.join(ROOT, ".env"), override: true });

function bool(v, fallback = false) {
  if (v == null || v === "") return fallback;
  return ["1", "true", "yes", "on"].includes(String(v).trim().toLowerCase());
}

function slug(name) {
  const s = String(name || "")
    .trim()
    .replace(/[^a-zA-Z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
  return s || "server";
}

function parseHosts() {
  const raw = String(process.env.NPU_HOSTS || "").trim();
  const defaultUser = process.env.NPU_SSH_USER || "root";
  const defaultPass = process.env.NPU_SSH_PASSWORD || "";
  const defaultPort = Number(process.env.NPU_SSH_PORT || 22);
  let hosts = [];

  if (raw.startsWith("[")) {
    try {
      const arr = JSON.parse(raw);
      hosts = arr.map((item) => {
        const name = String(item.name || item.id || item.host || "server");
        return {
          id: String(item.id || slug(name)),
          name,
          host: String(item.host),
          port: Number(item.port || defaultPort),
          username: String(item.username || item.user || defaultUser),
          password: String(item.password || defaultPass),
        };
      });
    } catch {
      hosts = [];
    }
  }

  if (!hosts.length && raw) {
    for (const part of raw.split(",")) {
      const bits = part
        .trim()
        .split("|")
        .map((b) => b.trim());
      if (bits.length < 2) continue;
      const [name, host] = bits;
      hosts.push({
        id: slug(name),
        name,
        host,
        username: bits[2] || defaultUser,
        password: bits[3] || defaultPass,
        port: bits[4] ? Number(bits[4]) : defaultPort,
      });
    }
  }

  if (!hosts.length) {
    const name = process.env.NPU_HOST_NAME || "A2";
    hosts.push({
      id: slug(name),
      name,
      host: process.env.NPU_HOST || "192.168.9.179",
      port: defaultPort,
      username: defaultUser,
      password: defaultPass,
    });
  }

  const seen = new Set();
  const uniq = [];
  for (const h of hosts) {
    const key = `${h.host}:${h.port}`;
    if (seen.has(key)) continue;
    seen.add(key);
    uniq.push(h);
  }
  return uniq;
}

function parseAliases() {
  const aliases = {};
  for (const part of String(process.env.IP_ALIASES || "").split(",")) {
    const p = part.trim();
    if (!p || !p.includes("=")) continue;
    const [ip, name] = p.split("=", 2);
    aliases[ip.trim()] = name.trim();
  }
  return aliases;
}

let cached = null;

function getSettings() {
  if (cached) return cached;
  const hosts = parseHosts();
  const primary = hosts[0];
  cached = {
    root: ROOT,
    hosts,
    host: primary.host,
    port: primary.port,
    username: primary.username,
    password: primary.password,
    refreshSeconds: Number(process.env.REFRESH_SECONDS || 8),
    demoMode: bool(process.env.DEMO_MODE, false),
    listenHost: process.env.LISTEN_HOST || "127.0.0.1",
    listenPort: Number(process.env.LISTEN_PORT || 8787),
    ipAliases: parseAliases(),
    probeLocalPath: path.join(ROOT, "remote_probe.py"),
    probeRemotePath: process.env.PROBE_REMOTE_PATH || "/tmp/npu_who_remote_probe.py",
  };
  return cached;
}

function reloadSettings() {
  cached = null;
  return getSettings();
}

function getHost(serverId) {
  const cfg = getSettings();
  const h = cfg.hosts.find(
    (x) => x.id === serverId || x.name === serverId || x.host === serverId
  );
  if (!h) throw new Error(`unknown server: ${serverId}`);
  return h;
}

function probeExists() {
  return fs.existsSync(getSettings().probeLocalPath);
}

module.exports = {
  ROOT,
  getSettings,
  reloadSettings,
  getHost,
  probeExists,
};
