import {chip, esc, fmt, id, provenanceNode, raw} from "/ui/util.js";

export default function mount(el, segments, ctx) {
  const state = {
    selectedId: segments[0] || "",
    q: "",
    type: "",
    inactive: false,
    index: null,
    detail: null,
  };
  load(el, ctx, state);
  return () => {};
}

async function load(el, ctx, state) {
  await loadIndex(ctx, state);
  if (!state.selectedId && state.index.entities?.[0]) state.selectedId = state.index.entities[0].id;
  if (state.selectedId) state.detail = await ctx.api(`/api/entities/${encodeURIComponent(state.selectedId)}`);
  render(el, ctx, state);
}

async function loadIndex(ctx, state) {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.type) params.set("type", state.type);
  if (state.inactive) params.set("inactive", "1");
  state.index = await ctx.api(`/api/entities?${params.toString()}`);
}

function render(el, ctx, state) {
  const detail = state.detail;
  el.innerHTML = `
    <h1>Entities</h1>
    <div class="entities-layout">
      <section class="panel">
        <div class="toolbar">
          <input id="entity-search" type="search" placeholder="Search entities" value="${esc(state.q)}">
          <label><input id="entity-inactive" type="checkbox" ${state.inactive ? "checked" : ""}> inactive</label>
        </div>
        <div class="filters">${typeFilters(state)}</div>
        <table class="compact"><thead><tr><th>Name</th><th>Type</th><th>Facts</th><th>Aliases</th><th>Last</th></tr></thead>
          <tbody>${(state.index?.entities || []).map(entity => row(entity, state)).join("") || `<tr><td colspan="5" class="muted">No entities of this type yet.</td></tr>`}</tbody>
        </table>
      </section>
      <section class="panel entity-detail">
        ${detail ? detailHtml(detail) : `<div class="muted">Select an entity.</div>`}
      </section>
    </div>
  `;
  el.querySelector("#entity-search").addEventListener("input", async event => {
    state.q = event.target.value;
    await loadIndex(ctx, state);
    render(el, ctx, state);
  });
  el.querySelector("#entity-inactive").addEventListener("change", async event => {
    state.inactive = event.target.checked;
    await loadIndex(ctx, state);
    render(el, ctx, state);
  });
  for (const button of el.querySelectorAll("[data-type]")) {
    button.addEventListener("click", async () => {
      state.type = button.dataset.type;
      await loadIndex(ctx, state);
      render(el, ctx, state);
    });
  }
  for (const link of el.querySelectorAll("[data-entity]")) {
    link.addEventListener("click", async event => {
      event.preventDefault();
      state.selectedId = link.dataset.entity;
      history.replaceState(null, "", `#/entities/${state.selectedId}`);
      state.detail = await ctx.api(`/api/entities/${encodeURIComponent(state.selectedId)}`);
      render(el, ctx, state);
    });
  }
  for (const button of el.querySelectorAll("[data-merge]")) {
    button.addEventListener("click", async () => {
      const candidate = JSON.parse(button.dataset.merge);
      const result = await ctx.api("/api/entities/merge", {method: "POST", body: {candidate}});
      ctx.toast(`Merge action ${result.action.status}.`);
      state.detail = await ctx.api(`/api/entities/${encodeURIComponent(state.selectedId)}`);
      render(el, ctx, state);
    });
  }
  attachFactPopovers(el, ctx, detail);
}

function typeFilters(state) {
  const entries = [["", state.index?.count || 0], ...(state.index?.types || []).map(row => [row.entity_type, row.count])];
  return entries.map(([type, count]) => `<button data-type="${esc(type)}" aria-pressed="${state.type === type}" type="button">${esc(type || "all")} ${esc(count)}</button>`).join("");
}

function row(entity, state) {
  return `<tr class="${entity.id === state.selectedId ? "selected-row" : ""}">
    <td><a href="#/entities/${esc(entity.id)}" data-entity="${esc(entity.id)}">${esc(entity.name)}</a><div class="muted">${id(entity.id)}</div></td>
    <td>${esc(entity.entity_type || "")}</td>
    <td>${esc(entity.fact_count)}</td>
    <td>${esc(entity.alias_count)}</td>
    <td>${esc(fmt(entity.last_observed_at))}</td>
  </tr>`;
}

function detailHtml(detail) {
  const entity = detail.entity;
  return `<div class="entity-head">
    <h2>${esc(entity.name)}</h2>
    <div class="meta-row">${chip(entity.entity_type || "")}${chip(entity.status || "")}${id(entity.id)}</div>
    <div class="alias-row">${(entity.aliases || []).map(alias => chip(alias)).join("")}</div>
  </div>
  <h2>Facts</h2>
  ${(detail.facts_by_page || []).map(group => `<section class="fact-group">
    <h3><a href="#/wiki/${encodeURIComponent(group.page_hint)}">${esc(group.page_hint)}</a></h3>
    ${group.facts.map(fact => `<div class="fact-line fact-ref" tabindex="0" data-fact-id="${esc(fact.id)}">${esc(fact.statement)}</div>`).join("")}
  </section>`).join("") || `<div class="muted">No active facts.</div>`}
  <h2>Co-Mentions</h2>
  <div class="meta-row">${(detail.co_mentions || []).map(item => `<a class="chip" href="#/entities/${esc(item.id)}" data-entity="${esc(item.id)}">${esc(item.name)} ${esc(item.count)}</a>`).join("") || chip("none")}</div>
  <h2>Merge Candidates</h2>
  ${(detail.merge_candidates || []).map(candidate => mergeCandidate(candidate)).join("") || `<div class="muted">No merge candidates.</div>`}
  ${raw(detail)}`;
}

function mergeCandidate(candidate) {
  const names = candidate.entity_names || {};
  const label = (candidate.entity_ids || []).map(entityId => names[entityId] || entityId).join(" + ");
  return `<div class="fact-card">
    <div>${esc(label)}</div>
    <div class="meta-row">${chip(candidate.merge_signal || "")}${chip(candidate.reason || "")}${chip(candidate.score ?? "")}</div>
    <button type="button" data-merge="${esc(JSON.stringify(candidate))}">Propose Merge</button>
  </div>`;
}

function attachFactPopovers(el, ctx, detail) {
  const facts = new Map();
  for (const group of detail?.facts_by_page || []) {
    for (const fact of group.facts || []) facts.set(fact.id, fact);
  }
  for (const node of el.querySelectorAll(".fact-ref")) {
    node.addEventListener("click", () => {
      const fact = facts.get(node.dataset.factId);
      if (fact) ctx.popover(node, provenanceNode(fact));
    });
  }
}
