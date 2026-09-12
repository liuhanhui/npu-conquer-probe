const metaEl = document.getElementById("meta");
const alertsEl = document.getElementById("alerts");
const serverTabsEl = document.getElementById("serverTabs");
const serverSummaryEl = document.getElementById("serverSummary");
const devicesEl = document.getElementById("devices");
const sessionsEl = document.getElementById("sessions");
const occupantsEl = document.getElementById("occupants");
const footModeEl = document.getElementById("footMode");
const btnRefresh = document.getElementById("btnRefresh");
const tabPageEl = document.getElementById("tabPage");

let refreshSeconds = 8;
let timer = null;
let latestData = null;
let selectedServerId = localStorage.getItem("npu_who_server") || "";

function pct(used, total) {
  const u = Number(used), t = Number(total);
  if (!t || Number.isNaN(u) || Number.isNaN(t)) return 0;
  return Math.max(0, Math.min(100, (u / t) * 100));
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function serversOf(data) {
  if (data?.servers?.length) return data.servers;
  if (data && (data.devices || data.occupants)) {
    return [
      {
        id: "default",
        name: data.server_ip || "Server",
        server_ip: data.server_ip,
        hostname: data.hostname,
        timestamp: data.timestamp,
        devices: data.devices || [],
        occupants: data.occupants || [],
        sessions: data.sessions || [],
        errors: data.errors || [],
        ok: true,
        mode: data.mode,
      },
    ];
  }
  return [];
}

function currentServer(data) {
  const list = serversOf(data);
  if (!list.length) return null;
  let s = list.find((x) => x.id === selectedServerId);
  if (!s) {
    selectedServerId = list[0].id;
    localStorage.setItem("npu_who_server", selectedServerId);
    s = list[0];
  }
  return s;
}

function renderAlerts(errors) {
  if (!errors || !errors.length) {
    alertsEl.hidden = true;
    alertsEl.innerHTML = "";
    return;
  }
  alertsEl.hidden = false;
  alertsEl.innerHTML = errors.map((e) => `<div>${esc(e)}</div>`).join("");
}

function renderTabs(data) {
  const list = serversOf(data);
  serverTabsEl.innerHTML = list
    .map((s) => {
      const selected = s.id === selectedServerId;
      const bad = s.ok === false ? "bad" : "";
      const nOcc = (s.occupants || []).length;
      return `<button type="button" class="page-tab ${selected ? "active" : ""} ${bad}"
        role="tab" aria-selected="${selected}" data-id="${esc(s.id)}" id="tab-${esc(s.id)}">
        <span class="page-tab-name">${esc(s.name)}</span>
        <span class="page-tab-meta">${esc(s.server_ip || "")}${nOcc ? ` · 占用 ${nOcc}` : " · 空闲"}</span>
      </button>`;
    })
    .join("");

  serverTabsEl.querySelectorAll(".page-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (selectedServerId === btn.dataset.id) return;
      selectedServerId = btn.dataset.id;
      localStorage.setItem("npu_who_server", selectedServerId);
      tabPageEl.classList.remove("tab-fade");
      void tabPageEl.offsetWidth;
      tabPageEl.classList.add("tab-fade");
      render(latestData, null);
    });
  });
}

function renderDevices(devices) {
  if (!devices || !devices.length) {
    devicesEl.innerHTML = `<div class="empty">暂无 NPU 设备信息</div>`;
    return;
  }
  devicesEl.innerHTML = devices
    .map((d, i) => {
      const core = Number(d.aicore) || 0;
      const mem = pct(d.hbm_used, d.hbm_total);
      return `
      <article class="device" style="animation-delay:${i * 40}ms">
        <div class="id">NPU ${esc(d.npu_id)} · ${esc(d.health || "-")}</div>
        <div class="name">${esc(d.name || "Ascend")}</div>
        <div class="bars">
          <div class="bar-row"><span>AICore</span><div class="bar"><span style="width:${core}%"></span></div><span>${esc(d.aicore || "0")}%</span></div>
          <div class="bar-row"><span>HBM</span><div class="bar mem"><span style="width:${mem}%"></span></div><span>${Math.round(mem)}%</span></div>
        </div>
        <div class="foot"><span>${esc(d.hbm_used || "-")}/${esc(d.hbm_total || "-")} MB</span><span>${esc(d.temp || "-")}°C · ${esc(d.power || "-")}W</span></div>
      </article>`;
    })
    .join("");
}

function renderSessions(sessions) {
  if (!sessions || !sessions.length) {
    sessionsEl.innerHTML = `<div class="empty">当前没有检测到 SSH 登录会话</div>`;
    return;
  }
  sessionsEl.innerHTML = sessions
    .map((s, i) => {
      const busy = (s.npu_pids || []).length > 0;
      const procs = (s.top_processes || [])
        .slice(0, 5)
        .map((p) => {
          const extra = [p.start_time, p.model].filter(Boolean).join(" · ");
          return `<li title="${esc(p.cmdline)}">#${esc(p.pid)} ${esc(p.cmdline || p.name)}${extra ? `<br><span class="chip">${esc(extra)}</span>` : ""}</li>`;
        })
        .join("");
      return `
      <article class="session ${busy ? "busy" : ""}" style="animation-delay:${i * 50}ms">
        <div class="ip">${esc(s.remote_ip)}</div>
        ${s.alias ? `<div class="alias">${esc(s.alias)}${s.client_hostname && s.alias !== s.client_hostname ? ` · ${esc(s.client_hostname)}` : ""}</div>` : s.client_hostname ? `<div class="alias">${esc(s.client_hostname)}</div>` : ""}
        <div class="stats">
          ${busy ? `<span class="chip hot">占用 NPU ×${s.npu_pids.length}</span>` : `<span class="chip">未占 NPU</span>`}
          <span class="chip">连接 ${esc(s.connections || 1)}</span>
          <span class="chip">进程 ${esc(s.process_count || 0)}</span>
          ${(s.ttys || []).map((t) => `<span class="chip">${esc(t)}</span>`).join("")}
        </div>
        <ul class="proc-list">${procs || `<li class="empty">无明显业务进程</li>`}</ul>
      </article>`;
    })
    .join("");
}

function renderOccupants(occupants) {
  if (!occupants || !occupants.length) {
    occupantsEl.innerHTML = `<tr><td colspan="10" class="empty">没有进程占用 NPU</td></tr>`;
    return;
  }
  occupantsEl.innerHTML = occupants
    .map((o) => {
      const model = o.model_short || o.model || "-";
      const params = o.key_params || o.script || "-";
      const cmd = o.cmdline || o.note || "";
      const killPid = o.tgid || o.pid;
      const sid = o.server_id || selectedServerId || "";
      return `<tr data-pid="${esc(o.pid)}">
        <td><span class="badge">${esc(o.npu_id)}</span></td>
        <td class="ip-cell">${esc(o.remote_ip || "-")}</td>
        <td>${esc(o.alias || "-")}</td>
        <td class="mono nowrap">${esc(o.start_time || "-")}</td>
        <td class="model" title="${esc(o.model || "")}">${esc(model)}</td>
        <td class="cmd" title="${esc(params)}">${esc(params)}</td>
        <td class="mono">${esc(o.pid)}</td>
        <td class="mono">${esc(o.process_mem_mb || "-")}</td>
        <td class="cmd" title="${esc(cmd)}">${esc(cmd)}</td>
        <td class="actions-cell">
          <button type="button" class="btn btn-kill" data-pid="${esc(killPid)}" data-server="${esc(sid)}" data-npu="${esc(o.npu_id)}" data-model="${esc(model)}" data-cmd="${esc(cmd.slice(0, 120))}">结束</button>
        </td>
      </tr>`;
    })
    .join("");

  occupantsEl.querySelectorAll(".btn-kill").forEach((btn) => {
    btn.addEventListener("click", () => onKillClick(btn));
  });
}

async function onKillClick(btn) {
  const pid = Number(btn.dataset.pid);
  const serverId = btn.dataset.server || selectedServerId || "";
  const npu = btn.dataset.npu || "?";
  const model = btn.dataset.model || "";
  const cmd = btn.dataset.cmd || "";
  const tip = [
    `确认结束 ${serverId || "当前机器"} NPU${npu} 上的进程?`,
    `PID/TGID: ${pid}`,
    model ? `模型: ${model}` : "",
    cmd ? `命令: ${cmd}` : "",
    "",
    "将先发送 SIGTERM，若 2 秒内未退出再 SIGKILL。",
    "此操作不可撤销。",
  ].join("\n");

  if (!window.confirm(tip)) return;

  let force = false;
  btn.disabled = true;
  btn.textContent = "结束中…";
  try {
    let res = await fetch("/api/kill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pid, force: false, server_id: serverId }),
    });
    let data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    if (data.alive) {
      const again = window.confirm((data.message || "进程仍在") + "\n是否立即强制 SIGKILL？");
      if (again) {
        force = true;
        res = await fetch("/api/kill", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pid, force: true, server_id: serverId }),
        });
        data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || res.statusText);
      }
    }
    const health = await loadHealth();
    if (data.snapshot) render(data.snapshot, health);
    else await refreshNow();
    alertsEl.hidden = false;
    alertsEl.innerHTML = `<div>${esc(data.message || (force ? "已强制结束" : "已结束"))} · PID ${esc(pid)}</div>`;
  } catch (e) {
    alertsEl.hidden = false;
    alertsEl.innerHTML = `<div>结束失败: ${esc(e.message)}</div>`;
    btn.disabled = false;
    btn.textContent = "结束";
  }
}

function render(data, health) {
  latestData = data;
  const list = serversOf(data);
  if (!selectedServerId && list[0]) selectedServerId = list[0].id;

  renderTabs(data);
  const s = currentServer(data);

  metaEl.innerHTML = s
    ? `当前 <strong style="color:var(--ink)">${esc(s.name)}</strong> · ${esc(s.server_ip || "-")}`
    : "无机器";
  footModeEl.textContent = `mode=${data.mode || "live"} · auto ${refreshSeconds}s · tab=${selectedServerId}`;

  if (!s) {
    renderAlerts(data.errors || []);
    serverSummaryEl.textContent = "无可用机器";
    devicesEl.innerHTML = "";
    sessionsEl.innerHTML = "";
    occupantsEl.innerHTML = "";
    return;
  }

  // Only this tab's errors — never mix other machines into the page
  const pageErrors = [...(s.errors || [])];
  renderAlerts(pageErrors);

  serverSummaryEl.textContent = `${s.name} · ${s.server_ip || "-"} · 主机 ${s.hostname || "-"} · 采集 ${s.timestamp || data.timestamp || "-"} · ${s.ok === false ? "连接失败" : "在线"}`;
  renderDevices(s.devices || []);
  renderSessions(s.sessions || []);
  renderOccupants(s.occupants || []);
}

async function loadHealth() {
  const res = await fetch("/api/health");
  return res.json();
}

async function loadSnapshot() {
  const res = await fetch("/api/snapshot");
  if (!res.ok) throw new Error("snapshot failed");
  return res.json();
}

async function refreshNow() {
  btnRefresh.disabled = true;
  try {
    const res = await fetch("/api/refresh", { method: "POST" });
    const data = await res.json();
    const health = await loadHealth();
    refreshSeconds = health.refresh_seconds || refreshSeconds;
    render(data, health);
    schedule();
  } catch (e) {
    alertsEl.hidden = false;
    alertsEl.textContent = "刷新失败: " + e.message;
  } finally {
    btnRefresh.disabled = false;
  }
}

function schedule() {
  if (timer) clearInterval(timer);
  timer = setInterval(async () => {
    try {
      const [data, health] = await Promise.all([loadSnapshot(), loadHealth()]);
      refreshSeconds = health.refresh_seconds || refreshSeconds;
      render(data, health);
    } catch (_) {}
  }, Math.max(3, refreshSeconds) * 1000);
}

btnRefresh.addEventListener("click", refreshNow);

(async function init() {
  try {
    const [data, health] = await Promise.all([loadSnapshot(), loadHealth()]);
    refreshSeconds = health.refresh_seconds || 8;
    if (health.hosts?.length && !selectedServerId) {
      selectedServerId = health.hosts[0].id;
    }
    render(data, health);
    schedule();
  } catch (e) {
    metaEl.textContent = "服务未就绪: " + e.message;
  }
})();
