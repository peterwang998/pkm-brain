import {chip, compact, esc, provenanceNode, raw} from "/ui/util.js";

export default function mount(el, _segments, ctx) {
  const state = {
    result: null,
    history: loadHistory(),
  };
  render(el, ctx, state);
  return () => {};
}

function render(el, ctx, state) {
  el.innerHTML = `
    <h1>Ask</h1>
    <section class="panel ask-form">
      <input id="ask-task" autofocus placeholder="Retrieve context for...">
      <select id="ask-mode">
        <option>default</option>
        <option>compact</option>
        <option>broad</option>
        <option>inspect</option>
      </select>
      <label>${'<input id="ask-debug" type="checkbox">'} debug</label>
      <button class="primary" id="ask-run" type="button">Run</button>
    </section>
    <div class="ask-layout">
      <section class="panel">${state.result ? resultHtml(state.result) : `<div class="muted">Run a retrieval to inspect the packet.</div>`}</section>
      <aside class="panel">
        <h2>History</h2>
        ${(state.history || []).map(item => `<button class="history-row" data-task="${esc(item.task)}" data-mode="${esc(item.mode)}" type="button">${esc(compact(item.task, 80))}<span class="muted">${esc(item.mode)}</span></button>`).join("") || `<div class="muted">No recent asks.</div>`}
      </aside>
    </div>`;
  const task = el.querySelector("#ask-task");
  const mode = el.querySelector("#ask-mode");
  const debug = el.querySelector("#ask-debug");
  const run = async () => {
    if (!task.value.trim()) return;
    state.result = await ctx.api("/api/retrieve", {
      method: "POST",
      body: {task: task.value.trim(), mode: mode.value, debug: debug.checked},
    });
    pushHistory(state, {task: task.value.trim(), mode: mode.value});
    render(el, ctx, state);
  };
  task.addEventListener("keydown", event => {
    if (event.key === "Enter") run();
  });
  el.querySelector("#ask-run").addEventListener("click", run);
  for (const button of el.querySelectorAll(".history-row")) {
    button.addEventListener("click", () => {
      task.value = button.dataset.task;
      mode.value = button.dataset.mode || "default";
    });
  }
  attachProvenance(el, ctx, state.result);
}

function resultHtml(result) {
  const state = result.retrieval_verdict === "found" ? "ok" : result.retrieval_verdict === "no_strong_match" ? "bad" : "warn";
  return `<div class="verdict">${chip(`${result.retrieval_verdict} · ${result.retrieval_confidence}`, state)} <span class="muted">${esc((result.retrieval_reasons || []).join(" · "))}</span></div>
    ${section("Facts", result.relevant_facts || [], factRow)}
    ${section("Wiki Pages", result.relevant_wiki_pages || [], pageRow)}
    ${section("Chunks", result.supporting_chunks || result.results || [], chunkRow)}
    ${section("Memories", [...(result.active_memories || []), ...(result.candidate_memories || [])], memoryRow)}
    ${result.retrieval_debug || result.debug ? raw(result.retrieval_debug || result.debug) : ""}`;
}

function section(title, rows, rowFn) {
  return `<h2>${esc(title)}</h2>
    <table class="compact"><tbody>${rows.map(rowFn).join("") || `<tr><td class="muted">None</td></tr>`}</tbody></table>`;
}

function factRow(fact) {
  return `<tr class="prov-row" data-kind="fact" data-id="${esc(fact.id)}"><td class="mono">${esc(fact.retrieval_score ?? fact.score ?? "")}</td><td>${esc(fact.statement || "")}<div class="muted">${esc((fact.selection_reasons || fact.fact_relevance_reasons || []).join(" · "))}</div></td></tr>`;
}

function pageRow(page) {
  return `<tr><td class="mono">${esc(page.score ?? "")}</td><td><a href="#/wiki/${encodeURIComponent(page.relative_path)}">${esc(page.title || page.relative_path)}</a><div class="muted">${esc((page.selection_reasons || []).join(" · "))}</div></td></tr>`;
}

function chunkRow(chunk) {
  return `<tr class="prov-row" data-kind="chunk" data-id="${esc(chunk.chunk_id || chunk.id)}"><td class="mono">${esc(chunk.score ?? chunk.rerank_score ?? "")}</td><td>${esc(compact(chunk.text || chunk.snippet || "", 220))}<div class="muted">${esc((chunk.selection_reasons || chunk.reasons || []).join(" · "))}</div></td></tr>`;
}

function memoryRow(memory) {
  return `<tr><td class="mono">${esc(memory.memory_relevance_score ?? "")}</td><td>${esc(memory.content || "")}<div class="muted">${esc(memory.memory_type || "")} · ${esc(memory.scope || "")}</div></td></tr>`;
}

function attachProvenance(el, ctx, result) {
  if (!result) return;
  const facts = new Map((result.relevant_facts || []).map(fact => [fact.id, fact]));
  for (const row of el.querySelectorAll(".prov-row[data-kind='fact']")) {
    row.addEventListener("click", () => {
      const fact = facts.get(row.dataset.id);
      if (fact) ctx.popover(row, provenanceNode(fact));
    });
  }
}

function loadHistory() {
  try {
    return JSON.parse(localStorage.getItem("brain_ask_history") || "[]");
  } catch (_error) {
    return [];
  }
}

function pushHistory(state, item) {
  state.history = [item, ...state.history.filter(existing => existing.task !== item.task)].slice(0, 10);
  localStorage.setItem("brain_ask_history", JSON.stringify(state.history));
}
