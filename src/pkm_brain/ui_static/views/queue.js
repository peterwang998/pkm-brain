import {chip, compact, esc, fmt, id, raw, sourceList} from "/ui/util.js";

export default function mount(el, segments, ctx) {
  const state = {
    kind: segments[0] || "all",
    requestedId: segments[1] || "",
    items: [],
    counts: {},
    index: 0,
    selected: new Set(),
    tally: loadTally(),
  };
  const keydown = event => onKey(event, el, ctx, state);
  window.addEventListener("keydown", keydown);
  load(el, ctx, state);
  return () => window.removeEventListener("keydown", keydown);
}

async function load(el, ctx, state) {
  el.innerHTML = `<div class="panel muted">Loading queue...</div>`;
  const data = await ctx.api(`/api/queue?kind=${encodeURIComponent(state.kind)}`);
  state.items = data.items || [];
  state.counts = data.counts || {};
  if (state.requestedId) {
    const found = state.items.findIndex(item => item.id === state.requestedId);
    if (found >= 0) state.index = found;
  }
  render(el, ctx, state);
}

function render(el, ctx, state) {
  const current = state.items[state.index] || null;
  el.innerHTML = `
    <h1>Queue</h1>
    <div class="queue-layout">
      <section class="panel">
        <div class="filters">${filtersHtml(state)}</div>
        <div class="progress">${progressText(state)}</div>
        ${batchBar(state)}
        <div class="queue-list">
          ${state.items.map((item, index) => itemRow(item, index, state)).join("") || `<div class="muted">Nothing needs you. Nightly runs will add items here.</div>`}
        </div>
      </section>
      <section class="panel queue-detail">
        ${current ? detailHtml(current, state) : `<div class="muted">Nothing needs you. Nightly runs will add items here.</div>`}
      </section>
    </div>
  `;
  for (const button of el.querySelectorAll("[data-kind]")) {
    button.addEventListener("click", () => {
      location.hash = button.dataset.kind === "all" ? "#/queue" : `#/queue/${button.dataset.kind}`;
    });
  }
  for (const row of el.querySelectorAll(".queue-item")) {
    row.addEventListener("click", () => {
      state.index = Number(row.dataset.index);
      updateHash(state);
      render(el, ctx, state);
    });
  }
  for (const checkbox of el.querySelectorAll("[data-select]")) {
    checkbox.addEventListener("click", event => {
      event.stopPropagation();
      toggleSelected(state, checkbox.dataset.select);
      render(el, ctx, state);
    });
  }
  for (const button of el.querySelectorAll("[data-decision]")) {
    button.addEventListener("click", () => doDecision(el, ctx, state, button.dataset.decision, payloadFromButton(button)));
  }
  const rejectSelected = el.querySelector("#reject-selected");
  if (rejectSelected) rejectSelected.addEventListener("click", () => batchDecision(el, ctx, state, "reject"));
  const routeSelected = el.querySelector("#route-selected");
  if (routeSelected) routeSelected.addEventListener("click", () => {
    const pageHint = prompt("Route selected to page");
    if (pageHint) batchDecision(el, ctx, state, "route", {page_hint: pageHint});
  });
}

function filtersHtml(state) {
  const counts = state.counts.by_kind || {};
  const entries = [["all", state.counts.total || 0], ...Object.entries(counts)];
  return entries.map(([kind, count]) => `<button type="button" data-kind="${esc(kind)}" aria-pressed="${state.kind === kind || (kind === "all" && state.kind === "all")}">${esc(kind)} ${esc(count)}</button>`).join("");
}

function batchBar(state) {
  if (!state.selected.size) return "";
  return `<div class="batch-bar">${chip(`${state.selected.size} selected`, "warn")}<button id="reject-selected" type="button">Reject Selected</button><button id="route-selected" type="button">Route Selected...</button></div>`;
}

function itemRow(item, index, state) {
  const checked = state.selected.has(item.id) ? "checked" : "";
  return `<div class="queue-item" data-index="${index}" aria-selected="${index === state.index}">
    <input type="checkbox" data-select="${esc(item.id)}" ${checked} aria-label="Select">
    <div>
      <div>${esc(item.title || item.id)}</div>
      <div class="kind">${esc(item.group)} · ${esc(item.kind)} · ${esc(fmt(item.created_at))}</div>
    </div>
  </div>`;
}

function detailHtml(item, state) {
  const position = `${state.index + 1} of ${state.items.length}`;
  return `<div class="detail-head">
    <div>
      <div class="muted">${esc(item.group)} · ${id(item.id)}</div>
      <h2>${esc(item.title || item.id)}</h2>
    </div>
    <div class="progress">${esc(position)} · resolved ${state.tally.resolved} · skipped ${state.tally.skipped}</div>
  </div>
  ${orientationHtml(item)}
  ${cardHtml(item)}
  ${raw(item)}`;
}

function cardHtml(item) {
  if (item.group === "conflicts") return conflictCard(item);
  if (item.group === "unrouted") return unroutedCard(item);
  if (item.group === "memories") return memoryCard(item);
  if (item.group === "audit") return auditCard(item);
  if (item.group === "topology") return actionCard(item);
  return genericCard(item);
}

function conflictCard(item) {
  const candidate = item.candidate || {};
  const existingFacts = item.counterparts?.length ? item.counterparts : [{}];
  return `<div class="card-pair">
    ${factPanel("Candidate", candidate)}
    ${existingFacts.map((fact, index) => factPanel(existingFacts.length > 1 ? `Existing ${index + 1}` : "Existing", fact)).join("")}
  </div>
  <div class="decision-bar">
    <button data-decision="keep_existing" type="button"><kbd>1</kbd>keep existing</button>
    <button data-decision="candidate_wins" type="button"><kbd>2</kbd>candidate wins</button>
    <button data-decision="both_true" type="button"><kbd>3</kbd>both true</button>
    <button data-decision="supports_existing" type="button"><kbd>4</kbd>supports existing</button>
    <button data-decision="temporal_update" type="button" title="Candidate is the current state; existing fact becomes historical."><kbd>5</kbd>candidate current</button>
    <button data-decision="unsure" type="button"><kbd>6</kbd>unsure</button>
  </div>`;
}

function unroutedCard(item) {
  const routes = item.route_candidates || [];
  const keys = unroutedKeys(item);
  return `${factPanel("Fact", item.candidate || {})}
    <h2>Route Candidates</h2>
    <div class="route-list">
      ${routes.map((route, index) => `<button type="button" data-decision="route" data-page-hint="${esc(route.page_hint)}"><kbd>${index + 1}</kbd>${esc(route.title || route.page_hint)} <span class="muted">${esc(route.page_hint)}</span></button>`).join("") || `<div class="muted">No confident route candidates.</div>`}
    </div>
    <div class="decision-bar">
      <button data-decision="new_page" type="button"><kbd>${keys.newPage}</kbd>new page...</button>
      <button data-decision="reject" type="button"><kbd>${keys.reject}</kbd>reject</button>
      <button data-decision="skip" type="button"><kbd>${keys.skip}</kbd>skip</button>
    </div>`;
}

function memoryCard(item) {
  const memory = item.memory || {};
  return `<p>${esc(memory.content)}</p>
    <div class="meta-row">${chip(memory.memory_type || "")}${chip(memory.scope || "")}${chip(memory.confidence ?? "")}</div>
    ${sourceList(memory.source_ids || [], memory.source_documents || [])}
    <div class="decision-bar">
      <button data-decision="approve" type="button"><kbd>1</kbd>approve</button>
      <button data-decision="reject" type="button"><kbd>2</kbd>reject</button>
      <button data-decision="archive" type="button"><kbd>3</kbd>archive</button>
      <button data-decision="skip" type="button"><kbd>4</kbd>skip</button>
    </div>`;
}

function auditCard(item) {
  const action = item.action || {};
  return `<h2>${esc(action.action_type)}</h2>
    <div class="meta-row">${chip(action.status || "")}${chip(action.audit_status || "", "bad")}${chip(action.risk_tier || "")}</div>
    <blockquote class="evidence">${esc(item.summary || "Audit marked this action as sampled_bad.")}</blockquote>
    <div class="decision-bar">
      <button data-decision="revert" type="button"><kbd>1</kbd>revert</button>
      <button data-decision="mark_ok" type="button"><kbd>2</kbd>mark ok</button>
      <button data-decision="skip" type="button"><kbd>3</kbd>skip</button>
    </div>`;
}

function actionCard(item) {
  const action = item.action || {};
  return `<h2>${esc(action.action_type)}</h2>
    ${topologyTargetHtml(item.topology)}
    <div class="meta-row">${chip(action.status || "")}${chip(action.risk_tier || "")}${chip(action.proposed_by || "")}</div>
    <blockquote class="evidence">${esc(item.summary || "")}</blockquote>
    <div class="decision-bar">
      <button data-decision="approve" type="button"><kbd>1</kbd>approve</button>
      <button data-decision="reject" type="button"><kbd>2</kbd>reject</button>
      <button data-decision="skip" type="button"><kbd>3</kbd>skip</button>
    </div>`;
}

function topologyTargetHtml(topology) {
  if (!topology) return "";
  const labels = topology.entity_labels || [];
  const ids = topology.entity_ids || [];
  const pages = topology.page_hints || [];
  const title = topology.target_label || labels.join(", ");
  if (!title && !ids.length && !pages.length) return "";
  return `<section class="orientation-panel">
    <div class="muted">Target</div>
    <strong>${esc(title || "Topology target")}</strong>
    <div class="meta-row">
      ${ids.length ? chip(`ids ${ids.join(", ")}`) : ""}
      ${pages.length ? chip(`pages ${pages.join(", ")}`) : ""}
    </div>
  </section>`;
}

function genericCard(item) {
  return `<p>${esc(item.summary || item.title || "")}</p>
    <div class="decision-bar">
      <button data-decision="approve" type="button"><kbd>1</kbd>approve</button>
      <button data-decision="reject" type="button"><kbd>2</kbd>reject</button>
      <button data-decision="skip" type="button"><kbd>3</kbd>skip</button>
    </div>`;
}

function factPanel(label, fact) {
  return `<div class="fact-card">
    <div class="muted">${esc(label)} ${fact.id || fact.fact_id ? id(fact.id || fact.fact_id) : ""}</div>
    <p>${esc(fact.statement || "")}</p>
    <blockquote class="evidence">${esc(fact.evidence_quote || fact.quote || "")}</blockquote>
    <div class="meta-row">${chip(`conf ${fact.truth_confidence ?? fact.confidence ?? ""}`)}${chip(fact.page_hint || "")}</div>
    ${sourceList(fact.source_ids || [], fact.source_documents || [])}
  </div>`;
}

function orientationHtml(item) {
  const orientation = item.orientation || {};
  if (!orientation.title && !orientation.page_hint && !orientation.entity_label) return "";
  const relation = orientation.relation ? `relation ${orientation.relation}` : "";
  const candidateTime = orientation.candidate_observed_at ? `candidate ${fmt(orientation.candidate_observed_at)}` : "";
  const existingTime = orientation.existing_observed_at ? `existing ${fmt(orientation.existing_observed_at)}` : "";
  return `<div class="orientation-card">
    <div>
      <div class="muted">Mapped to</div>
      <strong>${esc(orientation.entity_label || item.title || item.id)}</strong>
      <div class="kind">${esc([orientation.page_hint, orientation.section_hint].filter(Boolean).join(" · "))}</div>
    </div>
    <div class="meta-row">
      ${chip(relation)}
      ${chip(orientation.temporal_scope || "")}
      ${chip(candidateTime)}
      ${chip(existingTime)}
      ${chip(orientation.currentness || "")}
    </div>
    ${orientation.relation_rationale ? `<blockquote class="evidence">${esc(orientation.relation_rationale)}</blockquote>` : ""}
  </div>`;
}

function payloadFromButton(button) {
  const payload = {};
  if (button.dataset.pageHint) payload.page_hint = button.dataset.pageHint;
  if (button.dataset.decision === "new_page") {
    const pageHint = prompt("New page path (for example concepts/topic.md)");
    if (!pageHint) payload.cancelled = true;
    payload.page_hint = pageHint;
    payload.decision = "route";
  }
  return payload;
}

async function doDecision(el, ctx, state, decision, payload = {}) {
  if (payload.cancelled) return;
  const item = state.items[state.index];
  if (!item) return;
  const actualDecision = payload.decision || decision;
  if (actualDecision === "skip") {
    state.tally.skipped += 1;
    saveTally(state.tally);
    advance(state);
    render(el, ctx, state);
    return;
  }
  const removed = state.items.splice(state.index, 1)[0];
  render(el, ctx, state);
  try {
    const result = await ctx.api(`/api/queue/${encodeURIComponent(item.id)}/decision`, {
      method: "POST",
      body: {decision: actualDecision, ...payload},
    });
    state.tally.resolved += 1;
    saveTally(state.tally);
    ctx.setLastUndo(result.undo_handle);
    ctx.toast(`${compact(item.title, 64)} resolved.`, {undo: result.undo_handle});
    advance(state);
    render(el, ctx, state);
  } catch (error) {
    state.items.splice(state.index, 0, removed);
    ctx.toast(error.message || "Decision failed.");
    render(el, ctx, state);
  }
}

async function batchDecision(el, ctx, state, decision, payload = {}) {
  const ids = [...state.selected];
  for (const itemId of ids) {
    const index = state.items.findIndex(item => item.id === itemId);
    if (index < 0) continue;
    state.index = index;
    await doDecision(el, ctx, state, decision, payload);
  }
  state.selected.clear();
}

function onKey(event, el, ctx, state) {
  if (isTyping(event.target)) return;
  const key = event.key.toLowerCase();
  if (key === "j" || key === "arrowdown") {
    event.preventDefault();
    state.index = Math.min(state.items.length - 1, state.index + 1);
    updateHash(state);
    render(el, ctx, state);
  } else if (key === "k" || key === "arrowup") {
    event.preventDefault();
    state.index = Math.max(0, state.index - 1);
    updateHash(state);
    render(el, ctx, state);
  } else if (key === "x") {
    event.preventDefault();
    const item = state.items[state.index];
    if (item) toggleSelected(state, item.id);
    render(el, ctx, state);
  } else {
    const item = state.items[state.index];
    if (item?.group === "unrouted") {
      const route = item.route_candidates?.[Number(key) - 1];
      if (route) {
        event.preventDefault();
        doDecision(el, ctx, state, "route", {page_hint: route.page_hint});
        return;
      }
      const keys = unroutedKeys(item);
      if (key === keys.newPage) {
        event.preventDefault();
        const pageHint = prompt("New page path (for example concepts/topic.md)");
        if (pageHint) doDecision(el, ctx, state, "route", {page_hint: pageHint});
        return;
      }
    }
    const decision = keyDecision(item, key);
    if (decision) {
      event.preventDefault();
      doDecision(el, ctx, state, decision);
    }
  }
}

function unroutedKeys(item) {
  const routeCount = item?.route_candidates?.length || 0;
  return {
    newPage: String(routeCount + 1),
    reject: String(routeCount + 2),
    skip: String(routeCount + 3),
  };
}

function keyDecision(item, key) {
  if (!item) return "";
  if (item.group === "conflicts") {
    return {
      1: "keep_existing",
      2: "candidate_wins",
      3: "both_true",
      4: "supports_existing",
      5: "temporal_update",
      6: "unsure",
    }[key] || "";
  }
  if (item.group === "unrouted") {
    const keys = unroutedKeys(item);
    return {
      [keys.reject]: "reject",
      [keys.skip]: "skip",
    }[key] || "";
  }
  if (item.group === "memories") return {1: "approve", 2: "reject", 3: "archive", 4: "skip"}[key] || "";
  if (item.group === "audit") return {1: "revert", 2: "mark_ok", 3: "skip"}[key] || "";
  return {1: "approve", 2: "reject", 3: "skip"}[key] || "";
}

function isTyping(target) {
  const tag = target?.tagName?.toLowerCase();
  return tag === "input" || tag === "textarea" || target?.isContentEditable;
}

function advance(state) {
  if (state.index >= state.items.length) state.index = Math.max(0, state.items.length - 1);
  updateHash(state);
}

function updateHash(state) {
  const item = state.items[state.index];
  const kind = state.kind === "all" ? "" : `/${state.kind}`;
  const suffix = item ? `${kind}/${item.id}` : kind;
  history.replaceState(null, "", `#/queue${suffix}`);
}

function toggleSelected(state, idValue) {
  if (state.selected.has(idValue)) state.selected.delete(idValue);
  else state.selected.add(idValue);
}

function loadTally() {
  try {
    const parsed = JSON.parse(localStorage.getItem("brain_queue_tally") || "{}");
    return {resolved: Number(parsed.resolved || 0), skipped: Number(parsed.skipped || 0)};
  } catch (_error) {
    return {resolved: 0, skipped: 0};
  }
}

function saveTally(tally) {
  localStorage.setItem("brain_queue_tally", JSON.stringify(tally));
}

function progressText(state) {
  if (!state.items.length) return "0 of 0";
  return `${state.index + 1} of ${state.items.length} · resolved ${state.tally.resolved || 0} · skipped ${state.tally.skipped || 0}`;
}
