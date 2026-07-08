export function esc(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]),
  );
}

export function attr(value) {
  return esc(value).replace(/`/g, "&#96;");
}

export function chip(text, state = "") {
  return `<span class="chip ${state}">${esc(text)}</span>`;
}

export function id(value) {
  const text = String(value || "");
  return `<span class="id" data-id="${attr(text)}">${esc(text.slice(0, 10))}</span>`;
}

export function raw(data) {
  return `<details class="raw"><summary>raw</summary><pre>${esc(JSON.stringify(data, null, 2))}</pre></details>`;
}

export function fmt(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

export function compact(value, limit = 140) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1).trim()}...`;
}

export function empty(text) {
  return `<div class="panel muted">${esc(text)}</div>`;
}

export function sourceList(sourceIds = [], docs = []) {
  const docItems = docs.map(doc => `<li><strong>${esc(doc.source_id || doc.id)}</strong> ${esc(doc.title || "")}<br><span class="muted">${esc(doc.source_type || "")} ${esc(doc.source_path || "")}</span></li>`);
  const known = new Set(docs.map(doc => doc.source_id || `document:${doc.id}`));
  const unresolved = sourceIds
    .filter(sourceId => !known.has(sourceId))
    .map(sourceId => `<li><strong>${esc(sourceId)}</strong></li>`);
  if (!docItems.length && !unresolved.length) return `<div class="muted">No source evidence.</div>`;
  return `<ul class="source-list">${docItems.join("")}${unresolved.join("")}</ul>`;
}

export function provenanceNode(fact, actions = "") {
  const node = document.createElement("div");
  node.innerHTML = `
    <div class="mono">${esc(fact.id || fact.fact_id || "")}</div>
    <blockquote class="evidence">${esc(fact.evidence_quote || fact.quote || fact.statement || "")}</blockquote>
    <div class="meta-row">
      ${chip(`truth ${fact.truth_confidence ?? fact.confidence ?? ""}`)}
      ${chip(`extract ${fact.extraction_confidence ?? ""}`)}
      ${chip(fact.extraction_method || "unknown")}
    </div>
    ${sourceList(fact.source_ids || [], fact.source_documents || [])}
    ${actions}`;
  return node;
}

export function factRefMap(facts = []) {
  const out = {};
  for (const fact of facts) {
    const key = normalizeBullet(fact.statement || "");
    out[key] = fact;
  }
  return out;
}

export function normalizeBullet(text) {
  return String(text || "")
    .replace(/\[[^\]]+\]\([^)]+\)/g, "")
    .replace(/\[[^\]]+\]/g, "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*/g, "")
    .replace(/\*/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}
