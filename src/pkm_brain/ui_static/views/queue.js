import {chip, compact, dateOnly, esc, fmt, id, raw, sourceList} from "/ui/util.js";

export default function mount(el, segments, ctx) {
  const state = {
    kind: segments[0] || "all",
    reviewState: "actionable",
    requestedId: segments[1] || "",
    items: [],
    counts: {},
    index: 0,
    selected: new Set(),
    alternativeSelections: new Map(),
    tally: loadTally(),
    total: 0,
    summary: null,
    nextCursor: null,
    limit: 50,
  };
  const keydown = event => onKey(event, el, ctx, state);
  window.addEventListener("keydown", keydown);
  load(el, ctx, state).catch(error => renderLoadError(el, error));
  return () => window.removeEventListener("keydown", keydown);
}

async function load(el, ctx, state) {
  el.innerHTML = `<div class="panel muted">Loading queue...</div>`;
  const data = await ctx.api(`/api/queue?kind=${encodeURIComponent(state.kind)}&state=${encodeURIComponent(state.reviewState)}&limit=${state.limit}&cursor=0`);
  state.items = data.items || [];
  state.counts = data.counts || {};
  state.summary = data.queue_summary || null;
  ctx.acceptQueueSummary(state.summary);
  state.total = Number(data.total || 0);
  state.nextCursor = data.next_cursor;
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
        <div class="queue-state-control">${reviewStateHtml(state)}</div>
        <div class="filters">${filtersHtml(state)}</div>
        <div class="progress">${progressText(state)}</div>
        ${batchBar(state)}
        <div class="queue-list">
          ${state.items.map((item, index) => itemRow(item, index, state)).join("") || `<div class="muted">${esc(emptyQueueText(state))}</div>`}
          ${state.nextCursor !== null && state.nextCursor !== undefined ? `<button id="load-more" type="button">Load More</button>` : ""}
        </div>
      </section>
      <section class="panel queue-detail">
        ${current ? detailHtml(current, state) : `<div class="muted">${esc(emptyQueueText(state))}</div>`}
      </section>
    </div>
  `;
  for (const button of el.querySelectorAll("[data-kind]")) {
    button.addEventListener("click", () => {
      state.kind = button.dataset.kind || "all";
      state.index = 0;
      state.selected.clear();
      state.requestedId = "";
      updateHash(state);
      load(el, ctx, state).catch(error => renderLoadError(el, error));
    });
  }
  for (const button of el.querySelectorAll("[data-review-state]")) {
    button.addEventListener("click", () => {
      state.reviewState = button.dataset.reviewState;
      state.kind = "all";
      state.index = 0;
      state.selected.clear();
      state.requestedId = "";
      updateHash(state);
      load(el, ctx, state).catch(error => renderLoadError(el, error));
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
      const item = state.items.find(candidate => candidate.id === checkbox.dataset.select);
      toggleSelected(state, item);
      render(el, ctx, state);
    });
  }
  for (const button of el.querySelectorAll("[data-decision]")) {
    button.addEventListener("click", () => doDecision(el, ctx, state, button.dataset.decision, payloadFromButton(button)));
  }
  for (const button of el.querySelectorAll("[data-alternative-id]")) {
    button.addEventListener("click", () => {
      toggleAlternative(state, current, button.dataset.alternativeId);
      render(el, ctx, state);
    });
  }
  const selectAllAlternatives = el.querySelector("#select-all-alternatives");
  if (selectAllAlternatives) selectAllAlternatives.addEventListener("click", () => {
    selectAllAlternativeFacts(state, current);
    render(el, ctx, state);
  });
  const clearAlternatives = el.querySelector("#clear-alternatives");
  if (clearAlternatives) clearAlternatives.addEventListener("click", () => {
    state.alternativeSelections.set(current.id, new Set());
    render(el, ctx, state);
  });
  const applyAlternatives = el.querySelector("#apply-alternatives");
  if (applyAlternatives) applyAlternatives.addEventListener("click", () => {
    submitAlternativeSelection(el, ctx, state, current);
  });
  const rejectSelected = el.querySelector("#reject-selected");
  if (rejectSelected) rejectSelected.addEventListener("click", () => batchDecision(el, ctx, state, "reject"));
  const routeSelected = el.querySelector("#route-selected");
  if (routeSelected) routeSelected.addEventListener("click", () => {
    const pageHint = prompt("Route selected to page");
    if (pageHint) batchDecision(el, ctx, state, "route", {page_hint: pageHint});
  });
  const loadMoreButton = el.querySelector("#load-more");
  if (loadMoreButton) loadMoreButton.addEventListener("click", () => loadMore(el, ctx, state));
  if (current?.approvable === false) {
    for (const control of el.querySelectorAll(".queue-detail [data-decision]")) {
      control.disabled = true;
      control.title = current.blocking_reason || "This review card is incomplete.";
    }
  }
}

function filtersHtml(state) {
  const counts = state.counts.by_kind || {};
  const entries = [["all", state.counts.total || 0], ...Object.entries(counts)];
  return entries.map(([kind, count]) => `<button type="button" data-kind="${esc(kind)}" aria-pressed="${state.kind === kind || (kind === "all" && state.kind === "all")}">${esc(kindLabel(kind))} ${esc(count)}</button>`).join("");
}

function reviewStateHtml(state) {
  const actionable = Number(state.summary?.actionable_total || 0);
  const blocked = Number(state.summary?.blocked_total || 0);
  const deferred = Number(state.summary?.deferred_total || 0);
  return `<button type="button" data-review-state="actionable" aria-pressed="${state.reviewState === "actionable"}">Review ${actionable}</button>
    <button type="button" data-review-state="blocked" aria-pressed="${state.reviewState === "blocked"}">Needs Repair ${blocked}</button>
    <button type="button" data-review-state="deferred" aria-pressed="${state.reviewState === "deferred"}">Deferred ${deferred}</button>`;
}

function batchBar(state) {
  if (!state.selected.size) return "";
  return `<div class="batch-bar">${chip(`${state.selected.size} selected`, "warn")}<button id="reject-selected" type="button">Reject Selected</button><button id="route-selected" type="button">Route Selected...</button></div>`;
}

function itemRow(item, index, state) {
  const checked = state.selected.has(item.id) ? "checked" : "";
  const batchDisabled = item.approvable === false || item.comparison_mode === "alternatives";
  const disabledReason = item.blocking_reason || (item.comparison_mode === "alternatives" ? "This comparison requires an individual choice" : "Incomplete review card");
  const disabled = batchDisabled ? `disabled title="${esc(disabledReason)}"` : "";
  const candidateSummary = compact(item.candidate?.statement || item.alternatives?.[0]?.statement || "", 92);
  return `<div class="queue-item" data-index="${index}" aria-selected="${index === state.index}">
    <input type="checkbox" data-select="${esc(item.id)}" ${checked} ${disabled} aria-label="Select">
    <div>
      <div>${esc(item.title || item.id)}</div>
      ${candidateSummary ? `<div class="muted queue-summary">${esc(candidateSummary)}</div>` : ""}
      ${item.approvable === false ? `<div class="bad-text">blocked · ${esc(item.blocking_reason || "incomplete card")}</div>` : ""}
      <div class="kind">${esc(kindLabel(item.group))} · ${esc(kindLabel(item.kind))} · ${esc(fmt(item.created_at))}</div>
    </div>
  </div>`;
}

function detailHtml(item, state) {
  const position = `${state.index + 1} of ${state.items.length} loaded · ${state.total} total`;
  return `<div class="detail-head">
    <div>
      <div class="muted">${esc(kindLabel(item.group))} · ${id(item.id)}</div>
      <h2>${esc(item.title || item.id)}</h2>
    </div>
    <div class="progress">${esc(position)} · resolved ${state.tally.resolved} · skipped ${state.tally.skipped}</div>
  </div>
  ${orientationHtml(item)}
  ${item.approvable === false ? `<div class="review-blocked"><strong>Decision controls disabled</strong><div>${esc(item.blocking_reason || "This card is missing required review evidence.")}</div></div>` : ""}
  ${cardHtml(item, state)}
  ${raw(item)}`;
}

function cardHtml(item, state) {
  if (item.kind === "policy_escalation") return policyCard(item);
  if (item.kind === "unrouted_inbox_batch") return inboxBatchCard(item);
  if (item.kind === "document_extraction_anomaly") return anomalyCard(item);
  if (item.group === "conflicts") return conflictCard(item, state);
  if (item.group === "unrouted") return unroutedCard(item);
  if (item.group === "memories") return memoryCard(item);
  if (item.group === "audit") return auditCard(item);
  if (item.group === "topology") return actionCard(item);
  return genericCard(item);
}

function policyCard(item) {
  const action = item.action || {};
  const counterparts = item.counterparts || [];
  const splitPreview = item.topology?.split_preview;
  const approvalDisabled = splitPreview && splitPreview.approvable === false;
  return `<div class="meta-row">
      ${chip(kindLabel(action.action_type || "policy review"))}
      ${chip(action.risk_tier || item.risk_tier || "")}
      ${chip(item.orientation?.relation ? `relation ${item.orientation.relation}` : "")}
    </div>
    <blockquote class="evidence">${esc(item.summary || "This action requires human policy review.")}</blockquote>
    ${topologyTargetHtml(item.topology)}
    ${splitPreviewHtml(splitPreview)}
    ${item.candidate ? factPanel("Candidate", item.candidate) : (!item.topology ? `<div class="muted">The linked action has no fact candidate.</div>` : "")}
    ${counterparts.length ? `<h3>Existing Context</h3><div class="card-pair">${counterparts.map((fact, index) => factPanel(`Existing ${index + 1}`, fact)).join("")}</div>` : ""}
    <div class="decision-bar">
      <button data-decision="approve" type="button" ${approvalDisabled ? "disabled title=\"Split preview is incomplete\"" : ""}><kbd>1</kbd>approve</button>
      <button data-decision="reject" type="button"><kbd>2</kbd>reject</button>
      <button data-decision="skip" type="button"><kbd>3</kbd>skip</button>
    </div>`;
}

function inboxBatchCard(item) {
  const context = item.question?.context || {};
  const count = context.source_question_ids?.length || 0;
  return `<section class="orientation-panel">
      <div class="muted">Inbox batch</div>
      <strong>${esc(item.page_hint || context.page_hint || "Unrouted Inbox")}</strong>
      <div class="meta-row">${chip(`${count} facts`)}${chip(context.section || "Inbox")}</div>
    </section>
    <p>${esc(item.summary || item.question?.question || "")}</p>
    <div class="decision-bar">
      <button data-decision="reviewed" type="button"><kbd>1</kbd>mark reviewed</button>
      <button data-decision="dismiss" type="button"><kbd>2</kbd>dismiss</button>
      <button data-decision="skip" type="button"><kbd>3</kbd>later</button>
    </div>`;
}

function anomalyCard(item) {
  const anomaly = item.anomaly || {};
  const rate = anomaly.block_rate === null || anomaly.block_rate === undefined
    ? ""
    : `${Math.round(Number(anomaly.block_rate) * 100)}% blocked`;
  return `<section class="orientation-panel">
      <div class="muted">Source Document</div>
      <strong>${esc(anomaly.document_title || item.title || "Extraction quality alert")}</strong>
      <div class="meta-row">${chip(rate, "warn")}${chip(`${anomaly.blocked_count || 0} blocked`)}${chip(`${anomaly.reviewed_count || 0} reviewed`)}</div>
    </section>
    <blockquote class="evidence">${esc(item.summary || "")}</blockquote>
    <div class="decision-bar">
      <button data-decision="acknowledge" type="button"><kbd>1</kbd>confirm quality issue</button>
      <button data-decision="dismiss" type="button"><kbd>2</kbd>false positive</button>
      <button data-decision="skip" type="button"><kbd>3</kbd>later</button>
    </div>`;
}

function conflictCard(item, state) {
  if (item.comparison_mode === "alternatives") return alternativeConflictCard(item, state);
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

function alternativeConflictCard(item, state) {
  const alternatives = item.alternatives || [];
  const shortcutCount = Math.min(alternatives.length, 7);
  const selectAllKey = String(shortcutCount + 1);
  const unsureKey = String(shortcutCount + 2);
  const selected = alternativeSelection(state, item);
  return `<div class="alternative-list">
      ${alternatives.map((fact, index) => {
        const factId = fact.id || fact.fact_id || "";
        const shortcut = index < shortcutCount ? `<kbd>${index + 1}</kbd>` : "";
        const isSelected = selected.has(factId);
        return `<div class="alternative-choice">
          ${factPanel(`Historical Fact ${index + 1}`, fact)}
          <button data-alternative-id="${esc(factId)}" type="button" aria-pressed="${isSelected}" ${factId ? "" : "disabled"}>${shortcut}${isSelected ? "kept" : "keep this fact"}</button>
        </div>`;
      }).join("")}
    </div>
    <div class="decision-bar">
      <button id="apply-alternatives" type="button" ${selected.size ? "" : "disabled"}><kbd>enter</kbd>keep selected</button>
      <button id="select-all-alternatives" type="button"><kbd>${selectAllKey}</kbd>select all</button>
      <button id="clear-alternatives" type="button" ${selected.size ? "" : "disabled"}>clear</button>
      <button data-decision="unsure" type="button"><kbd>${unsureKey}</kbd>unsure</button>
    </div>`;
}

function unroutedCard(item) {
  const routes = item.route_candidates || [];
  const keys = unroutedKeys(item);
  return `${factPanel("Fact", item.candidate || {})}
    <h2>Route Candidates</h2>
    <div class="route-list">
      ${routes.map((route, index) => `<button type="button" data-decision="route" data-page-hint="${esc(route.page_hint)}"><kbd>${index + 1}</kbd>${esc(route.title || route.page_hint)} <span class="muted">${route.document_coherence_count ? `${esc(route.document_coherence_count)} routed facts from this source - ` : ""}${esc(route.page_hint)}</span></button>`).join("") || `<div class="muted">No confident route candidates.</div>`}
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
  const audit = item.audit || {};
  const isFact = audit.action_type === "fact_upsert";
  const rejectsCurrentFact = audit.revert_mode === "reject_current_fact";
  const affectedFacts = audit.affected_facts || [];
  const affectedCount = audit.affected_fact_count || 0;
  const affectedPages = audit.affected_page_count;
  const affectedContracts = audit.affected_contract_count;
  return `<h2>Auditor Finding</h2>
    <div class="meta-row">${chip(audit.status || action.audit_status || "", "bad")}${chip(audit.model || "")}${chip(dateOnly(audit.audited_at || ""))}${chip(audit.action_status || action.status || "")}</div>
    <blockquote class="evidence">${esc(audit.rationale || item.summary || "Audit finding unavailable.")}</blockquote>
    ${item.candidate ? factPanel("Applied Fact", item.candidate) : ""}
    ${item.topology ? `<h2>Applied Change</h2>${topologyTargetHtml(item.topology)}<div class="meta-row">${chip(`${affectedCount} affected ${affectedCount === 1 ? "fact" : "facts"}`)}${affectedPages == null ? "" : chip(`${affectedPages} affected ${affectedPages === 1 ? "page" : "pages"}`)}${affectedContracts == null ? "" : chip(`${affectedContracts} affected ${affectedContracts === 1 ? "contract" : "contracts"}`)}</div>${!item.candidate && affectedFacts.length ? `<h3>Representative Affected Facts</h3>${affectedFacts.map((fact, index) => factPanel(`Affected Fact ${index + 1}`, fact)).join("")}` : ""}` : ""}
    ${audit.revertible === false ? `<p class="callout warn">${esc(audit.reviewability_reason === "audited_fact_still_active_after_related_drift" ? "Related state changed after apply. The audited fact is still active, but direct revert is no longer safe." : "This action has no safe direct revert.")}</p>` : ""}
    ${rejectsCurrentFact ? `<p class="callout">Related state changed after apply. Reject will target the current active fact and remain undoable.</p>` : ""}
    <div class="decision-bar">
      <button data-decision="revert" type="button" ${audit.revertible === false ? "disabled title=\"No reversible applied change is available\"" : ""}><kbd>1</kbd>${rejectsCurrentFact ? "reject applied fact" : (isFact ? "revert applied fact" : "revert applied action")}</button>
      <button data-decision="mark_ok" type="button"><kbd>2</kbd>${isFact ? "keep applied fact" : "keep applied action"}</button>
      <button data-decision="skip" type="button"><kbd>3</kbd>later</button>
    </div>`;
}

function actionCard(item) {
  const action = item.action || {};
  const splitPreview = item.topology?.split_preview;
  const approvalDisabled = splitPreview && splitPreview.approvable === false;
  return `<h2>${esc(kindLabel(action.action_type || item.kind))}</h2>
    ${topologyTargetHtml(item.topology)}
    <div class="meta-row">${chip(action.status || "")}${chip(action.risk_tier || "")}${chip(action.proposed_by || "")}</div>
    <blockquote class="evidence">${esc(item.summary || "")}</blockquote>
    ${splitPreviewHtml(splitPreview)}
    <div class="decision-bar">
      <button data-decision="approve" type="button" ${approvalDisabled ? "disabled title=\"Split preview is incomplete\"" : ""}><kbd>1</kbd>approve</button>
      <button data-decision="reject" type="button"><kbd>2</kbd>reject</button>
      <button data-decision="skip" type="button"><kbd>3</kbd>skip</button>
    </div>`;
}

function splitPreviewHtml(preview) {
  if (!preview) return "";
  const children = preview.children || [];
  return `<section class="split-preview">
    <div class="detail-head">
      <div><div class="muted">Page split preview</div><strong>${esc(preview.source_page_hint || "")}</strong></div>
      <div class="meta-row">${chip(`${preview.movable_fact_count || 0} facts move`)}${chip(`${preview.resulting_page_count || 0} resulting pages`)}</div>
    </div>
    ${children.map(child => `<div class="split-child">
      <div><strong>${esc(child.section || "Section")}</strong><div class="kind">${esc(child.page_hint || "")}</div></div>
      ${chip(`${child.fact_count || 0} facts`)}
      <ul>${(child.representative_facts || []).map(fact => `<li>${esc(fact.statement || "")}</li>`).join("")}</ul>
    </div>`).join("") || `<div class="bad-text">No movable section facts remain. Approval is disabled.</div>`}
  </section>`;
}

function topologyTargetHtml(topology) {
  if (!topology) return "";
  const labels = topology.entity_labels || [];
  const ids = topology.entity_ids || [];
  const pages = topology.page_hints || [];
  const statuses = topology.entity_statuses || {};
  const pageStatuses = topology.page_statuses || {};
  const title = topology.target_label || labels.join(", ") || pages[0] || "";
  if (!title && !ids.length && !pages.length) return "";
  return `<section class="orientation-panel">
    <div class="muted">Target</div>
    <strong>${esc(title || "Topology target")}</strong>
    <div class="meta-row">
      ${ids.length ? chip(`ids ${ids.join(", ")}`) : ""}
      ${Object.keys(statuses).length ? chip(Object.entries(statuses).map(([entityId, status]) => `${entityId.slice(0, 10)} ${status}`).join(", ")) : ""}
      ${Object.keys(pageStatuses).length ? chip(Object.entries(pageStatuses).map(([page, status]) => `${page} ${status}`).join(", ")) : ""}
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
  fact = fact || {};
  const quote = fact.evidence_quote || fact.quote || "";
  const sourceDate = fact.source_date || fact.observed_at || "";
  return `<div class="fact-card">
    <div class="muted">${esc(label)} ${fact.id || fact.fact_id ? id(fact.id || fact.fact_id) : ""}</div>
    <p>${esc(fact.statement || "")}</p>
    ${quote ? `<blockquote class="evidence">${esc(quote)}</blockquote>` : `<div class="muted">Evidence quote unavailable.</div>`}
    <div class="meta-row">${chip(sourceDate ? `source ${dateOnly(sourceDate)}` : "source date unavailable", sourceDate ? "" : "warn")}${chip(`conf ${fact.truth_confidence ?? fact.confidence ?? ""}`)}${chip(fact.page_hint || "")}</div>
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
  if (button.dataset.factId) payload.selected_fact_id = button.dataset.factId;
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
  if (item.approvable === false) {
    ctx.toast(item.blocking_reason || "This review card is incomplete.");
    return;
  }
  const actualDecision = payload.decision || decision;
  if (actualDecision === "skip") {
    state.tally.skipped += 1;
    saveTally(state.tally);
    advance(state);
    render(el, ctx, state);
    return;
  }
  const previousAlternativeSelection = item.comparison_mode === "alternatives"
    ? new Set(alternativeSelection(state, item))
    : null;
  const removed = state.items.splice(state.index, 1)[0];
  state.alternativeSelections.delete(item.id);
  render(el, ctx, state);
  try {
    const result = await ctx.api(`/api/queue/${encodeURIComponent(item.id)}/decision`, {
      method: "POST",
      body: {decision: actualDecision, ...payload},
    });
    state.summary = result.queue_summary || state.summary;
    ctx.acceptQueueSummary(result.queue_summary);
    state.tally.resolved += 1;
    state.total = Math.max(0, state.total - 1);
    if (state.nextCursor !== null && state.nextCursor !== undefined) {
      state.nextCursor = Math.max(0, state.nextCursor - 1);
    }
    decrementCounts(state, item);
    saveTally(state.tally);
    ctx.setLastUndo(result.undo_handle);
    ctx.toast(`${compact(item.title, 64)} resolved.`, {undo: result.undo_handle});
    advance(state);
    render(el, ctx, state);
  } catch (error) {
    state.items.splice(state.index, 0, removed);
    if (previousAlternativeSelection) {
      state.alternativeSelections.set(item.id, previousAlternativeSelection);
    }
    ctx.toast(error.message || "Decision failed.");
    render(el, ctx, state);
  }
}

async function loadMore(el, ctx, state) {
  if (state.nextCursor === null || state.nextCursor === undefined) return;
  const button = el.querySelector("#load-more");
  if (button) button.disabled = true;
  try {
    const data = await ctx.api(`/api/queue?kind=${encodeURIComponent(state.kind)}&state=${encodeURIComponent(state.reviewState)}&limit=${state.limit}&cursor=${state.nextCursor}`);
    const known = new Set(state.items.map(item => item.id));
    state.items.push(...(data.items || []).filter(item => !known.has(item.id)));
    state.counts = data.counts || state.counts;
    state.summary = data.queue_summary || state.summary;
    ctx.acceptQueueSummary(data.queue_summary);
    state.total = Number(data.total || state.total);
    state.nextCursor = data.next_cursor;
    render(el, ctx, state);
  } catch (error) {
    ctx.toast(error.message || "Could not load more queue items.");
    if (button) button.disabled = false;
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
    if (item) toggleSelected(state, item);
    render(el, ctx, state);
  } else {
    const item = state.items[state.index];
    if (item?.comparison_mode === "alternatives") {
      const alternatives = item.alternatives || [];
      const shortcutCount = Math.min(alternatives.length, 7);
      const selectedFact = Number(key) <= shortcutCount ? alternatives[Number(key) - 1] : null;
      const selectedFactId = selectedFact?.id || selectedFact?.fact_id;
      if (selectedFactId) {
        event.preventDefault();
        toggleAlternative(state, item, selectedFactId);
        render(el, ctx, state);
      } else if (key === String(shortcutCount + 1)) {
        event.preventDefault();
        selectAllAlternativeFacts(state, item);
        render(el, ctx, state);
      } else if (key === String(shortcutCount + 2)) {
        event.preventDefault();
        doDecision(el, ctx, state, "unsure");
      } else if (event.key === "Enter") {
        event.preventDefault();
        submitAlternativeSelection(el, ctx, state, item);
      }
      return;
    }
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
  if (item.approvable === false) return "";
  if (item.comparison_mode === "alternatives") return "";
  if (item.kind === "document_extraction_anomaly") {
    return {1: "acknowledge", 2: "dismiss", 3: "skip"}[key] || "";
  }
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
  if (item.topology?.split_preview?.approvable === false && key === "1") return "";
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

function toggleSelected(state, item) {
  if (!item || item.approvable === false || item.comparison_mode === "alternatives") return;
  if (state.selected.has(item.id)) state.selected.delete(item.id);
  else state.selected.add(item.id);
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

function alternativeSelection(state, item) {
  if (!item) return new Set();
  if (!state.alternativeSelections.has(item.id)) {
    state.alternativeSelections.set(item.id, new Set());
  }
  return state.alternativeSelections.get(item.id);
}

function toggleAlternative(state, item, factId) {
  if (!item || !factId) return;
  const selected = alternativeSelection(state, item);
  if (selected.has(factId)) selected.delete(factId);
  else selected.add(factId);
}

function selectAllAlternativeFacts(state, item) {
  if (!item) return;
  state.alternativeSelections.set(
    item.id,
    new Set((item.alternatives || []).map(fact => fact.id || fact.fact_id).filter(Boolean)),
  );
}

function submitAlternativeSelection(el, ctx, state, item) {
  const selected = alternativeSelection(state, item);
  const selectedFactIds = (item?.alternatives || [])
    .map(fact => fact.id || fact.fact_id)
    .filter(factId => factId && selected.has(factId));
  if (!selectedFactIds.length) return;
  doDecision(el, ctx, state, "select_facts", {selected_fact_ids: selectedFactIds});
}

function progressText(state) {
  const blocked = Number(state.summary?.blocked_total || 0);
  const deferred = Number(state.summary?.deferred_total || 0);
  const suffix = state.reviewState === "blocked"
    ? "needs repair"
    : state.reviewState === "deferred" ? "deferred" : "to review";
  if (!state.items.length) return `0 loaded · ${state.total} ${suffix}`;
  return `${state.index + 1} of ${state.items.length} loaded · ${state.total} ${suffix} · ${blocked} needs repair · ${deferred} deferred · resolved ${state.tally.resolved || 0} · skipped ${state.tally.skipped || 0}`;
}

function emptyQueueText(state) {
  if (state.reviewState === "blocked") return "No review cards currently need repair.";
  if (state.reviewState === "deferred") return "No review work is waiting behind the active queue.";
  return "Nothing needs you. Nightly runs will add items here.";
}

function kindLabel(kind) {
  const labels = {
    all: "All",
    conflicts: "Conflicts",
    unrouted: "Inbox",
    unrouted_inbox_batch: "Inbox batch",
    topology: "Topology",
    memories: "Memories",
    audit: "Audit",
    anomalies: "Anomalies",
    policy_escalation: "Policy",
    policy: "Policy",
    proposed_action: "Topology proposal",
    fact_conflict_review: "Fact conflict",
    document_extraction_anomaly: "Extraction anomaly",
    page_split: "Page split",
    page_merge: "Page merge",
    entity_merge: "Entity merge",
  };
  if (labels[kind]) return labels[kind];
  return String(kind || "").replaceAll("_", " ").replace(/\b\w/g, letter => letter.toUpperCase());
}

function decrementCounts(state, item) {
  state.counts.total = Math.max(0, Number(state.counts.total || 0) - 1);
  const byKind = state.counts.by_kind || {};
  for (const key of new Set([state.kind, item.group, item.kind])) {
    if (key && byKind[key] !== undefined) byKind[key] = Math.max(0, Number(byKind[key]) - 1);
  }
}

function renderLoadError(el, error) {
  el.innerHTML = `<div class="panel"><h1>Queue unavailable</h1><p class="bad-text">${esc(error.message || "Could not load the review queue.")}</p></div>`;
}
