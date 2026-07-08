import {chip, esc, fmt, id, raw} from "/ui/util.js";

const SECTIONS = ["runs", "actions", "policy", "audit", "index", "sync", "logs", "setup"];

export default function mount(el, segments, ctx) {
  const state = {section: segments[0] || "", data: null};
  load(el, ctx, state);
  return () => {};
}

async function load(el, ctx, state) {
  if (!state.section) {
    render(el, ctx, state);
    return;
  }
  state.data = await loadSection(ctx, state.section);
  render(el, ctx, state);
}

async function loadSection(ctx, section) {
  if (section === "runs") return ctx.api("/api/ops/runs");
  if (section === "actions") return ctx.api("/api/cos/actions?limit=200");
  if (section === "policy") return ctx.api("/api/cos/policy");
  if (section === "audit") return ctx.api("/api/cos/audit");
  if (section === "index") return ctx.api("/api/status");
  if (section === "sync") return Promise.all([ctx.api("/api/sync/status"), ctx.api("/api/sync/conflicts")]).then(([status, conflicts]) => ({status, conflicts}));
  if (section === "logs") return ctx.api("/api/logs");
  if (section === "setup") return ctx.api("/api/setup");
  return {};
}

function render(el, ctx, state) {
  el.innerHTML = `
    <h1>Ops</h1>
    <div class="ops-layout">
      <aside class="panel filters">${SECTIONS.map(section => `<a class="chip" aria-current="${state.section === section}" href="#/ops/${section}" data-section="${section}">${section}</a>`).join("")}</aside>
      <section class="panel">${state.section ? sectionHtml(state.section, state.data) : overviewHtml()}</section>
    </div>
  `;
  for (const link of el.querySelectorAll("[data-section]")) {
    link.addEventListener("click", async event => {
      event.preventDefault();
      state.section = link.dataset.section;
      history.replaceState(null, "", `#/ops/${state.section}`);
      state.data = await loadSection(ctx, state.section);
      render(el, ctx, state);
    });
  }
  for (const button of el.querySelectorAll("[data-revert]")) {
    button.addEventListener("click", async () => {
      if (!confirm("Revert this action?")) return;
      try {
        const result = await ctx.api(`/api/actions/${encodeURIComponent(button.dataset.revert)}/revert`, {method: "POST"});
        ctx.toast(`Revert ${result.action.status}.`);
        state.data = await loadSection(ctx, state.section);
        render(el, ctx, state);
      } catch (error) {
        ctx.toast(error.message);
      }
    });
  }
}

function overviewHtml() {
  return `<div class="ops-grid">
    ${SECTIONS.map(section => `<a class="panel ops-tile" href="#/ops/${section}"><h2>${esc(section)}</h2><div class="muted">Open ${esc(section)}</div></a>`).join("")}
  </div>`;
}

function sectionHtml(section, data) {
  if (!data) return `<div class="muted">Loading...</div>`;
  if (section === "runs") return runsHtml(data);
  if (section === "actions") return actionsHtml(data);
  if (section === "policy") return policyHtml(data);
  if (section === "audit") return auditHtml(data);
  if (section === "index") return indexHtml(data);
  if (section === "sync") return syncHtml(data);
  if (section === "logs") return logsHtml(data);
  if (section === "setup") return setupHtml(data);
  return raw(data);
}

function runsHtml(data) {
  return `<h2>Runs</h2>
    ${runTable("Automation", data.automation_runs || [])}
    ${runTable("Ingestion", data.ingestion_runs || [])}
    ${raw(data)}`;
}

function runTable(title, rows) {
  return `<h2>${esc(title)}</h2><table class="compact"><thead><tr><th>Run</th><th>Status</th><th>Started</th><th>Finished</th><th>Summary</th></tr></thead>
    <tbody>${rows.map(row => `<tr><td>${id(row.id)}</td><td>${chip(row.status || "", statusState(row.status))}</td><td>${esc(fmt(row.started_at))}</td><td>${esc(fmt(row.finished_at))}</td><td>${esc(JSON.stringify(row.summary || row.error || ""))}</td></tr>`).join("") || emptyRow(5)}</tbody></table>`;
}

function actionsHtml(data) {
  const actions = data.actions || [];
  return `<h2>Actions</h2><table class="compact"><thead><tr><th>Action</th><th>Type</th><th>Status</th><th>Audit</th><th>Targets</th><th></th></tr></thead>
    <tbody>${actions.map(action => `<tr><td>${id(action.id)}</td><td>${esc(action.action_type)}</td><td>${chip(action.status || "", statusState(action.status))}</td><td>${chip(action.audit_status || "")}</td><td>${esc([...(action.target_page_paths || []), ...(action.target_fact_ids || [])].slice(0, 3).join(", "))}</td><td>${["applied", "auto_applied"].includes(action.status) ? `<button data-revert="${esc(action.id)}" type="button">Revert</button>` : ""}</td></tr>`).join("") || emptyRow(6)}</tbody></table>
    ${raw(data)}`;
}

function policyHtml(data) {
  return `<h2>Policy ${esc(data.version)}</h2><table class="compact"><thead><tr><th>Action</th><th>Level</th><th>Predicate</th></tr></thead>
    <tbody>${(data.rules || []).map(rule => `<tr><td>${esc((rule.match_action_types || []).join(", "))}</td><td>${esc(rule.autonomy_level || "")}</td><td>${esc(JSON.stringify(rule.match_predicate || {}))}</td></tr>`).join("") || emptyRow(3)}</tbody></table>
    ${raw(data)}`;
}

function auditHtml(data) {
  return `<h2>Audit</h2><div class="meta-row">${Object.entries(data.counts || {}).map(([key, value]) => chip(`${key} ${value}`, key === "sampled_bad" ? "bad" : "")).join("")}</div>
    <table class="compact"><tbody>${(data.failures || []).map(row => `<tr><td>${id(row.id)}</td><td>${esc(row.action_type)}</td><td>${esc(row.audit_status)}</td></tr>`).join("") || emptyRow(3)}</tbody></table>
    ${raw(data)}`;
}

function indexHtml(data) {
  const index = data.index || {};
  return `<h2>Index & Embeddings</h2>
    <div class="pulse">
      <div class="panel">${chip(`documents ${index.documents ?? 0}`)}</div>
      <div class="panel">${chip(`chunks ${index.chunks ?? 0}`)}</div>
      <div class="panel">${chip(`facts ${data.retrieval_surfaces?.find(row => row.surface === "Fact ledger")?.count ?? 0}`)}</div>
      <div class="panel">${chip(index.embedding_provider || "embeddings")}</div>
    </div>
    ${raw(data)}`;
}

function syncHtml(data) {
  return `<h2>Sync</h2><div class="meta-row">${chip(data.status?.configured ? "configured" : "not configured")}${chip(data.status?.role || "")}${chip(`conflicts ${data.conflicts?.count || 0}`, data.conflicts?.count ? "warn" : "ok")}</div>${raw(data)}`;
}

function logsHtml(data) {
  return `<h2>Logs</h2><table class="compact"><tbody>${(data.logs || []).map(log => `<tr><td>${esc(log.name)}</td><td class="mono">${esc(log.bytes)}</td><td>${esc(log.path)}</td></tr>`).join("") || emptyRow(3)}</tbody></table>${raw(data)}`;
}

function setupHtml(data) {
  return `<h2>Setup</h2><div class="meta-row">${chip(data.role || "")}${chip(data.node_id || "")}${chip(`${(data.planned_writes || []).length} writes`)}</div>${raw(data)}`;
}

function statusState(status) {
  const text = String(status || "").toLowerCase();
  if (["ok", "success", "completed", "applied", "auto_applied"].includes(text)) return "ok";
  if (["failed", "error"].includes(text)) return "bad";
  return "";
}

function emptyRow(cols) {
  return `<tr><td colspan="${cols}" class="muted">None</td></tr>`;
}
