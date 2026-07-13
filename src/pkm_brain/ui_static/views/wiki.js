import {render as renderMarkdown} from "/ui/md.js";
import {chip, esc, factRefMap, fmt, provenanceNode, raw} from "/ui/util.js";

export default function mount(el, segments, ctx) {
  const state = {
    path: segments.join("/") || "",
    pages: [],
    page: null,
    q: "",
    editing: false,
  };
  load(el, ctx, state);
  return () => {};
}

async function load(el, ctx, state) {
  el.innerHTML = `<div class="panel muted">Loading wiki...</div>`;
  const pages = await ctx.api("/api/wiki/pages");
  state.pages = pages.pages || [];
  if (!state.path && state.pages[0]) state.path = state.pages[0].relative_path;
  if (state.path) state.page = await ctx.api(`/api/wiki/page?path=${encodeURIComponent(state.path)}`);
  render(el, ctx, state);
}

function render(el, ctx, state) {
  const page = state.page;
  el.innerHTML = `
    <h1>Wiki</h1>
    <div class="wiki-layout">
      <aside class="panel">
        <input id="wiki-search" type="search" placeholder="Search pages" value="${esc(state.q)}">
        <div class="tree-list">${pageList(state)}</div>
      </aside>
      <section class="panel reading-panel">
        ${page ? pageHtml(page, state) : `<div class="muted">No wiki pages yet.</div>`}
      </section>
      <aside class="panel right-rail">
        ${page ? railHtml(page) : ""}
      </aside>
    </div>
  `;
  el.querySelector("#wiki-search").addEventListener("input", event => {
    state.q = event.target.value;
    render(el, ctx, state);
  });
  for (const link of el.querySelectorAll("[data-page]")) {
    link.addEventListener("click", async event => {
      event.preventDefault();
      state.path = link.dataset.page;
      state.editing = false;
      history.replaceState(null, "", `#/wiki/${encodeURIComponent(state.path)}`);
      state.page = await ctx.api(`/api/wiki/page?path=${encodeURIComponent(state.path)}`);
      render(el, ctx, state);
    });
  }
  const edit = el.querySelector("#wiki-edit");
  if (edit) edit.addEventListener("click", () => {
    state.editing = true;
    render(el, ctx, state);
  });
  const cancel = el.querySelector("#wiki-cancel");
  if (cancel) cancel.addEventListener("click", () => {
    state.editing = false;
    render(el, ctx, state);
  });
  const save = el.querySelector("#wiki-save");
  if (save) save.addEventListener("click", async () => {
    const markdown = el.querySelector("#wiki-editor").value;
    state.page = await ctx.api("/api/wiki/page", {
      method: "POST",
      body: {path: state.path, markdown},
    });
    state.editing = false;
    ctx.toast("Page saved.");
    render(el, ctx, state);
  });
  attachFactPopovers(el, ctx, state);
}

function pageList(state) {
  const q = state.q.toLowerCase();
  return state.pages
    .filter(page => !q || `${page.title} ${page.relative_path}`.toLowerCase().includes(q))
    .map(page => `<a href="#/wiki/${encodeURIComponent(page.relative_path)}" data-page="${esc(page.relative_path)}" aria-current="${page.relative_path === state.path}"><span>${esc(page.title)}</span><span class="muted">${esc(page.relative_path)}</span></a>`)
    .join("");
}

function pageHtml(page, state) {
  if (state.editing) {
    return `<div class="toolbar">
      <button class="primary" id="wiki-save" type="button">Save</button>
      <button id="wiki-cancel" type="button">Cancel</button>
    </div>
    <textarea id="wiki-editor">${esc(page.markdown)}</textarea>`;
  }
  const article = renderMarkdown(page.body || "", {factRefs: factRefMap(page.facts || [])});
  const wrapper = document.createElement("div");
  wrapper.append(article);
  return `<div class="page-meta">
      <h2>${esc(page.frontmatter?.title || page.relative_path)}</h2>
      <div class="meta-row">
        ${chip(page.frontmatter?.page_type || "")}
        ${chip(page.frontmatter?.status || "")}
        ${chip(`${(page.facts || []).length} facts`)}
        ${chip(`${(page.source_ids || []).length} sources`)}
        ${chip(fmt(page.frontmatter?.updated_at))}
      </div>
      ${page.generated ? `<div class="muted projection-banner">projection - edits happen via facts</div>` : `<button id="wiki-edit" type="button">Edit</button>`}
    </div>
    <article class="reading">${wrapper.innerHTML}</article>
    ${raw({frontmatter: page.frontmatter, source_ids: page.source_ids})}`;
}

function railHtml(page) {
  const contract = page.contract;
  return `<h2>Contract</h2>
    ${contract ? `<p>${esc(contract.retrieval_purpose || contract.page_scope || "")}</p>
      <h2>Belongs</h2><p>${esc(contract.belongs_here || contract.include || "")}</p>
      <h2>Doesn't</h2><p>${esc(contract.does_not_belong || contract.exclude || "")}</p>` : `<p class="muted">No contract yet.</p>`}
    <h2>Related</h2>
    ${(page.related || []).map(path => `<div><a href="#/wiki/${encodeURIComponent(path)}">${esc(path)}</a></div>`).join("") || `<div class="muted">None</div>`}
    <h2>Snapshots</h2>
    ${(page.snapshots || []).map(snapshot => `<details><summary>${esc(fmt(snapshot.created_at))} ${esc(snapshot.reason || "")}</summary><pre>${esc(snapshot.after_preview || "")}</pre></details>`).join("") || `<div class="muted">None</div>`}`;
}

function attachFactPopovers(el, ctx, state) {
  const facts = new Map((state.page?.facts || []).map(fact => [fact.id, fact]));
  for (const li of el.querySelectorAll(".fact-ref")) {
    li.addEventListener("click", () => openFact(li, ctx, facts.get(li.dataset.factId)));
    li.addEventListener("keydown", event => {
      if (event.key === "Enter") openFact(li, ctx, facts.get(li.dataset.factId));
    });
  }
}

function openFact(anchor, ctx, fact) {
  if (!fact) return;
  const actions = `<div class="decision-bar">
    <button id="confirm-fact" type="button">Confirm</button>
    <button id="flag-fact" type="button">Flag</button>
  </div>`;
  const node = provenanceNode(fact, actions);
  ctx.popover(anchor, node);
  node.querySelector("#confirm-fact").addEventListener("click", async () => {
    await ctx.api(`/api/wiki/facts/${encodeURIComponent(fact.id)}/confirm`, {method: "POST"});
    ctx.toast("Fact confirmed.");
  });
  node.querySelector("#flag-fact").addEventListener("click", async () => {
    const reason = prompt("Flag reason", "Review this fact");
    if (!reason) return;
    await ctx.api(`/api/wiki/facts/${encodeURIComponent(fact.id)}/flag`, {
      method: "POST",
      body: {reason},
    });
    ctx.toast("Flagged for review.");
  });
}
