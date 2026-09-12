"use strict";

const fs = require("fs");
const path = require("path");
const express = require("express");
const { getSettings } = require("./config");
const { collector } = require("./collector");

const app = express();
app.use(express.json({ limit: "1mb" }));

const WEB_DIST = path.join(__dirname, "..", "web", "dist");
const LEGACY_STATIC = path.join(__dirname, "..", "static");

app.get("/api/health", (_req, res) => {
  res.json({ ok: true, ...collector.status() });
});

app.get("/api/snapshot", async (_req, res) => {
  try {
    res.json(await collector.get());
  } catch (e) {
    res.status(500).json({ detail: String(e.message || e) });
  }
});

app.post("/api/refresh", async (_req, res) => {
  try {
    res.json(await collector.refresh());
  } catch (e) {
    res.status(500).json({ detail: String(e.message || e) });
  }
});

app.post("/api/kill", async (req, res) => {
  const pid = Number(req.body && req.body.pid);
  const force = Boolean(req.body && req.body.force);
  const serverId = String((req.body && req.body.server_id) || "");
  if (!Number.isFinite(pid) || pid <= 1) {
    return res.status(400).json({ detail: "非法 PID" });
  }
  try {
    const result = await collector.killProcess(pid, force, serverId);
    try {
      result.snapshot = await collector.refresh();
    } catch (e) {
      result.refresh_error = String(e.message || e);
    }
    res.json(result);
  } catch (e) {
    res.status(400).json({ detail: String(e.message || e) });
  }
});

const useVue = fs.existsSync(path.join(WEB_DIST, "index.html"));
if (useVue) {
  app.use(express.static(WEB_DIST, { maxAge: 0 }));
  app.get(/^\/(?!api(?:\/|$)).*/, (_req, res) => {
    res.sendFile(path.join(WEB_DIST, "index.html"));
  });
} else {
  console.warn("[warn] web/dist missing — serving legacy static/. Run: npm run build:web");
  app.get("/", (_req, res) => {
    res.sendFile(path.join(LEGACY_STATIC, "index.html"));
  });
  app.use("/static", express.static(LEGACY_STATIC, { maxAge: 0 }));
}

function main() {
  const cfg = getSettings();
  collector.startPolling();
  app.listen(cfg.listenPort, cfg.listenHost, () => {
    console.log(`NPU Who (Node${useVue ? "+Vue" : ""}) http://${cfg.listenHost}:${cfg.listenPort}`);
    console.log(
      "hosts:",
      cfg.hosts.map((h) => `${h.name}@${h.host}`).join(", ")
    );
  });
}

if (require.main === module) {
  main();
}

module.exports = { app, main };
