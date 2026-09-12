<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  fetchHealth,
  fetchSnapshot,
  memPct,
  postKill,
  postRefresh,
  serversOf,
} from "./api.js";

const snapshot = ref(null);
const health = ref(null);
const refreshing = ref(false);
const killingPid = ref(null);
const banner = ref("");
const selectedServerId = ref(localStorage.getItem("npu_who_server") || "");
const tabFade = ref(false);

let timer = null;

const refreshSeconds = computed(() => health.value?.refresh_seconds || 8);
const servers = computed(() => serversOf(snapshot.value));
const current = computed(() => {
  const list = servers.value;
  if (!list.length) return null;
  let s = list.find((x) => x.id === selectedServerId.value);
  if (!s) {
    selectedServerId.value = list[0].id;
    s = list[0];
  }
  return s;
});

const pageErrors = computed(() => {
  const s = current.value;
  if (!s) return snapshot.value?.errors || [];
  return [...(s.errors || [])];
});

const metaText = computed(() => {
  const s = current.value;
  if (!s) return "无机器";
  return `当前 ${s.name} · ${s.server_ip || "-"}`;
});

const footText = computed(() => {
  const mode = snapshot.value?.mode || "live";
  return `mode=${mode} · auto ${refreshSeconds.value}s · tab=${selectedServerId.value} · vue`;
});

const summaryText = computed(() => {
  const s = current.value;
  if (!s) return "无可用机器";
  const st = s.ok === false ? "连接失败" : "在线";
  return `${s.name} · ${s.server_ip || "-"} · 主机 ${s.hostname || "-"} · 采集 ${s.timestamp || snapshot.value?.timestamp || "-"} · ${st}`;
});

watch(selectedServerId, (id) => {
  if (id) localStorage.setItem("npu_who_server", id);
});

function selectServer(id) {
  if (selectedServerId.value === id) return;
  selectedServerId.value = id;
  tabFade.value = false;
  requestAnimationFrame(() => {
    tabFade.value = true;
  });
}

function applyData(data, h) {
  snapshot.value = data;
  if (h) health.value = h;
  const list = serversOf(data);
  if (!selectedServerId.value && list[0]) selectedServerId.value = list[0].id;
  if (h?.hosts?.length && !selectedServerId.value) {
    selectedServerId.value = h.hosts[0].id;
  }
}

async function loadAll() {
  const [data, h] = await Promise.all([fetchSnapshot(), fetchHealth()]);
  applyData(data, h);
}

async function refreshNow() {
  refreshing.value = true;
  banner.value = "";
  try {
    const data = await postRefresh();
    const h = await fetchHealth();
    applyData(data, h);
    schedule();
  } catch (e) {
    banner.value = `刷新失败: ${e.message}`;
  } finally {
    refreshing.value = false;
  }
}

function schedule() {
  if (timer) clearInterval(timer);
  const sec = Math.max(3, refreshSeconds.value);
  timer = setInterval(async () => {
    try {
      const [data, h] = await Promise.all([fetchSnapshot(), fetchHealth()]);
      applyData(data, h);
    } catch {
      /* ignore transient */
    }
  }, sec * 1000);
}

async function killOccupant(o) {
  const pid = Number(o.tgid || o.pid);
  const serverId = o.server_id || selectedServerId.value || "";
  const model = o.model_short || o.model || "";
  const cmd = (o.cmdline || "").slice(0, 120);
  const tip = [
    `确认结束 ${serverId || "当前机器"} NPU${o.npu_id} 上的进程?`,
    `PID/TGID: ${pid}`,
    model ? `模型: ${model}` : "",
    cmd ? `命令: ${cmd}` : "",
    "",
    "将先发送 SIGTERM，若 2 秒内未退出再 SIGKILL。",
    "此操作不可撤销。",
  ].join("\n");
  if (!window.confirm(tip)) return;

  killingPid.value = pid;
  banner.value = "";
  try {
    let data = await postKill({ pid, force: false, serverId });
    let force = false;
    if (data.alive) {
      const again = window.confirm(`${data.message || "进程仍在"}\n是否立即强制 SIGKILL？`);
      if (again) {
        force = true;
        data = await postKill({ pid, force: true, serverId });
      }
    }
    if (data.snapshot) {
      const h = await fetchHealth();
      applyData(data.snapshot, h);
    } else {
      await refreshNow();
    }
    banner.value = `${data.message || (force ? "已强制结束" : "已结束")} · PID ${pid}`;
  } catch (e) {
    banner.value = `结束失败: ${e.message}`;
  } finally {
    killingPid.value = null;
  }
}

onMounted(async () => {
  try {
    await loadAll();
    schedule();
  } catch (e) {
    banner.value = `服务未就绪: ${e.message}`;
  }
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <div class="bg" />

  <header class="top">
    <div class="brand">
      <div class="mark" aria-hidden="true" />
      <div>
        <h1>NPU Who</h1>
        <p class="sub">按登录 IP 定位谁在占用昇腾 NPU</p>
      </div>
    </div>
    <div class="actions">
      <div class="meta">{{ metaText }}</div>
      <button type="button" class="btn" :disabled="refreshing" @click="refreshNow">
        {{ refreshing ? "刷新中…" : "立即刷新" }}
      </button>
    </div>
  </header>

  <nav class="page-tabs" role="tablist" aria-label="机器分页">
    <button
      v-for="s in servers"
      :key="s.id"
      type="button"
      class="page-tab"
      :class="{ active: s.id === selectedServerId, bad: s.ok === false }"
      role="tab"
      :aria-selected="s.id === selectedServerId"
      @click="selectServer(s.id)"
    >
      <span class="page-tab-name">{{ s.name }}</span>
      <span class="page-tab-meta">
        {{ s.server_ip || "" }}{{ (s.occupants || []).length ? ` · 占用 ${(s.occupants || []).length}` : " · 空闲" }}
      </span>
    </button>
  </nav>

  <main class="tab-page" :class="{ 'tab-fade': tabFade }" role="tabpanel">
    <section v-if="banner || pageErrors.length" class="strip">
      <div v-if="banner">{{ banner }}</div>
      <div v-for="(e, i) in pageErrors" :key="i">{{ e }}</div>
    </section>

    <section class="server-summary">{{ summaryText }}</section>

    <section class="devices">
      <div v-if="!(current?.devices || []).length" class="empty">暂无 NPU 设备信息</div>
      <article
        v-for="(d, i) in current?.devices || []"
        :key="`${d.npu_id}-${i}`"
        class="device"
        :style="{ animationDelay: `${i * 40}ms` }"
      >
        <div class="id">NPU {{ d.npu_id }} · {{ d.health || "-" }}</div>
        <div class="name">{{ d.name || "Ascend" }}</div>
        <div class="bars">
          <div class="bar-row">
            <span>AICore</span>
            <div class="bar"><span :style="{ width: `${Number(d.aicore) || 0}%` }" /></div>
            <span>{{ d.aicore || "0" }}%</span>
          </div>
          <div class="bar-row">
            <span>HBM</span>
            <div class="bar mem"><span :style="{ width: `${memPct(d.hbm_used, d.hbm_total)}%` }" /></div>
            <span>{{ Math.round(memPct(d.hbm_used, d.hbm_total)) }}%</span>
          </div>
        </div>
        <div class="foot">
          <span>{{ d.hbm_used || "-" }}/{{ d.hbm_total || "-" }} MB</span>
          <span>{{ d.temp || "-" }}°C · {{ d.power || "-" }}W</span>
        </div>
      </article>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>登录来源</h2>
        <p>同事都用 root 登录时，用客户端 IP 区分是谁</p>
      </div>
      <div class="session-grid">
        <div v-if="!(current?.sessions || []).length" class="empty">当前没有检测到 SSH 登录会话</div>
        <article
          v-for="(s, i) in current?.sessions || []"
          :key="`${s.remote_ip}-${i}`"
          class="session"
          :class="{ busy: (s.npu_pids || []).length > 0 }"
          :style="{ animationDelay: `${i * 50}ms` }"
        >
          <div class="ip">{{ s.remote_ip }}</div>
          <div v-if="s.alias || s.client_hostname" class="alias">
            {{ s.alias || s.client_hostname }}
            <template v-if="s.alias && s.client_hostname && s.alias !== s.client_hostname">
              · {{ s.client_hostname }}
            </template>
          </div>
          <div class="stats">
            <span v-if="(s.npu_pids || []).length" class="chip hot">占用 NPU ×{{ s.npu_pids.length }}</span>
            <span v-else class="chip">未占 NPU</span>
            <span class="chip">连接 {{ s.connections || 1 }}</span>
            <span class="chip">进程 {{ s.process_count || 0 }}</span>
            <span v-for="t in s.ttys || []" :key="t" class="chip">{{ t }}</span>
          </div>
          <ul class="proc-list">
            <li
              v-for="p in (s.top_processes || []).slice(0, 5)"
              :key="p.pid"
              :title="p.cmdline"
            >
              #{{ p.pid }} {{ p.cmdline || p.name }}
              <template v-if="p.start_time || p.model">
                <br />
                <span class="chip">{{ [p.start_time, p.model].filter(Boolean).join(" · ") }}</span>
              </template>
            </li>
            <li v-if="!(s.top_processes || []).length" class="empty">无明显业务进程</li>
          </ul>
        </article>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>NPU 占用明细</h2>
        <p>仅显示当前机器上的占用</p>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>NPU</th>
              <th>登录 IP</th>
              <th>备注/主机名</th>
              <th>开始时间</th>
              <th>模型</th>
              <th>关键参数</th>
              <th>PID</th>
              <th>显存(MB)</th>
              <th>命令 / 代码</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!(current?.occupants || []).length">
              <td colspan="10" class="empty">没有进程占用 NPU</td>
            </tr>
            <tr v-for="o in current?.occupants || []" :key="`${o.npu_id}-${o.pid}`">
              <td><span class="badge">{{ o.npu_id }}</span></td>
              <td class="ip-cell">{{ o.remote_ip || "-" }}</td>
              <td>{{ o.alias || "-" }}</td>
              <td class="mono nowrap">{{ o.start_time || "-" }}</td>
              <td class="model" :title="o.model || ''">{{ o.model_short || o.model || "-" }}</td>
              <td class="cmd" :title="o.key_params || o.script || ''">{{ o.key_params || o.script || "-" }}</td>
              <td class="mono">{{ o.pid }}</td>
              <td class="mono">{{ o.process_mem_mb || "-" }}</td>
              <td class="cmd" :title="o.cmdline || o.note || ''">{{ o.cmdline || o.note || "" }}</td>
              <td class="actions-cell">
                <button
                  type="button"
                  class="btn btn-kill"
                  :disabled="killingPid === Number(o.tgid || o.pid)"
                  @click="killOccupant(o)"
                >
                  {{ killingPid === Number(o.tgid || o.pid) ? "结束中…" : "结束" }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <footer>
    <span>本地服务 · Vue 前端 · 数据经 SSH 采集自目标机</span>
    <span>{{ footText }}</span>
  </footer>
</template>
