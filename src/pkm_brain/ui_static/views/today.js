import {chip, compact, esc, fmt, raw} from "/ui/util.js";

export default function mount(el, _segments, ctx) {
  let cancelled = false;
  render(el, ctx, () => cancelled);
  return () => {
    cancelled = true;
  };
}

async function render(el, ctx, isCancelled) {
  const since = localStorage.getItem("brain_last_visit") || "";
  const data = await ctx.api(`/api/digest${since ? `?since=${encodeURIComponent(since)}` : ""}`);
  if (isCancelled()) return;
  const total = Number(data.queue_counts?.total || 0);
  el.innerHTML = `
    <h1>Today</h1>
    <div class="pulse">
      ${(data.pulse || []).map(pulseChip).join("")}
    </div>
    <section class="panel">
      <h2>Since Your Last Visit</h2>
      ${digestHtml(data)}
    </section>
    <section class="panel needs-strip">
      <div>
        <h2>Needs You</h2>
        <div class="meta-row">${queueCounts(data.queue_counts?.by_kind || {})}</div>
      </div>
      <button class="primary" type="button" id="start-review">Start Review</button>
    </section>
    ${raw(data)}
  `;
  el.querySelector("#start-review").addEventListener("click", () => {
    location.hash = "#/queue";
  });
  localStorage.setItem("brain_last_visit", new Date().toISOString());
  if (total === 0 && !(data.facts_by_page || []).length) {
    ctx.toast(`Nothing needs you. Last run ${fmt(data.latest_run?.finished_at || data.latest_run?.started_at) || "unknown"}.`);
  }
}

function pulseChip(item) {
  return `<a class="panel pulse-chip" href="${esc(item.href || "#/ops")}">
    <div class="muted">${esc(item.label)}</div>
    <div>${chip(item.value || "unknown", item.state || "")}</div>
    <div class="mono muted">${esc(fmt(item.timestamp))}</div>
  </a>`;
}

function digestHtml(data) {
  const facts = data.facts_by_page || [];
  const reverts = data.reverts || [];
  const demotions = data.demotions || [];
  const evals = data.eval_transitions || [];
  if (!facts.length && !reverts.length && !demotions.length && !evals.length) {
    return `<p class="muted">Nothing happened since yesterday. Nightly ran clean at ${esc(fmt(data.latest_run?.finished_at || data.latest_run?.started_at) || "unknown")}.</p>`;
  }
  return `<div class="digest-list">
    ${facts.slice(0, 8).map(row => `<div>• ${esc(row.count)} facts added → <a href="#/wiki/${encodeURIComponent(row.page_hint)}">${esc(row.page_hint)}</a></div>`).join("")}
    ${facts.length > 8 ? `<div class="muted">• ${facts.length - 8} more pages changed</div>` : ""}
    ${reverts.map(action => `<div>• action reverted → <a href="#/ops/actions">${esc(action.action_type)} ${esc(action.id?.slice(0, 10))}</a></div>`).join("")}
    ${demotions.map(action => `<div>• audit finding → <a href="#/queue/audit/${esc(action.id)}">${esc(compact(action.action_type, 60))}</a></div>`).join("")}
    ${evals.map(run => `<div>• eval ${esc(run.status)} → ${esc(run.job_name)}</div>`).join("")}
  </div>`;
}

function queueCounts(counts) {
  const entries = Object.entries(counts);
  if (!entries.length) return chip("empty", "ok");
  return entries.map(([kind, count]) => chip(`${kind} ${count}`, count ? "warn" : "ok")).join("");
}
