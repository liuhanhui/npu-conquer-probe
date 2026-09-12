export async function fetchHealth() {
  const res = await fetch("/api/health");
  if (!res.ok) throw new Error("health failed");
  return res.json();
}

export async function fetchSnapshot() {
  const res = await fetch("/api/snapshot");
  if (!res.ok) throw new Error("snapshot failed");
  return res.json();
}

export async function postRefresh() {
  const res = await fetch("/api/refresh", { method: "POST" });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || res.statusText);
  }
  return res.json();
}

export async function postKill({ pid, force = false, serverId = "" }) {
  const res = await fetch("/api/kill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pid, force, server_id: serverId }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

export function serversOf(data) {
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

export function memPct(used, total) {
  const u = Number(used);
  const t = Number(total);
  if (!t || Number.isNaN(u) || Number.isNaN(t)) return 0;
  return Math.max(0, Math.min(100, (u / t) * 100));
}
