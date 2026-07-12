/**
 * Brain UI v2 — shell: router, keyboard, palette, toasts, popovers.
 */
import {api, ApiError} from "/ui/api.js";

const VIEWS = ["today", "queue", "wiki", "entities", "ask", "ops"];
const viewEl = document.getElementById("view");
const paletteEl = document.getElementById("palette");
const tokenDialog = document.getElementById("token-dialog");
const toastRoot = document.getElementById("toast-root");
const popoverRoot = document.getElementById("popover-root");
let currentUnmount = null;
let chord = "";
let lastUndo = null;
let paletteCache = null;
let latestQueueSummary = null;

const ctx = {
  api,
  toast,
  popover,
  navigate: hash => {
    location.hash = hash;
  },
  setLastUndo: handle => {
    lastUndo = handle;
  },
  acceptQueueSummary,
};

window.addEventListener("hashchange", route);
window.addEventListener("brain:auth-required", openTokenDialog);
document.getElementById("palette-open").addEventListener("click", openPalette);
document.getElementById("token-open").addEventListener("click", openTokenDialog);
document.addEventListener("keydown", globalKeydown);
document.addEventListener("click", clickToCopy);

if (!location.hash) location.hash = "#/today";
route();

async function route() {
  closePopover();
  const {view, segments} = parseHash(location.hash);
  if (currentUnmount) currentUnmount();
  currentUnmount = null;
  setActiveRail(view);
  viewEl.innerHTML = `<div class="panel muted">Loading...</div>`;
  try {
    const mod = await import(`/ui/views/${view}.js`);
    currentUnmount = mod.default(viewEl, segments, ctx) || null;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) openTokenDialog();
    viewEl.innerHTML = `<div class="panel bad-text">${escapeHtml(error.message || error)}</div>`;
  }
  refreshQueueBadge();
}

function parseHash(hash) {
  const raw = decodeURIComponent(String(hash || "").replace(/^#\/?/, ""));
  const [viewName = "today", ...segments] = raw.split("/").filter(Boolean);
  const view = VIEWS.includes(viewName) ? viewName : "today";
  if (view !== viewName) location.replace("#/today");
  return {view, segments};
}

function setActiveRail(view) {
  for (const link of document.querySelectorAll("#rail a[data-view]")) {
    link.setAttribute("aria-current", link.dataset.view === view ? "page" : "false");
  }
}

async function refreshQueueBadge() {
  try {
    const data = await api("/api/digest");
    acceptQueueSummary(data.queue_summary || {
      as_of: data.generated_at || "",
      active_total: Number(data.queue_counts?.total || 0),
      actionable_total: Number(data.queue_counts?.total || 0),
      blocked_total: 0,
      deferred_total: 0,
      by_kind: data.queue_counts?.by_kind || {},
      raw: data.queue_counts?.raw || {},
    });
  } catch (_error) {
    if (!latestQueueSummary) document.getElementById("queue-count").hidden = true;
  }
}

function acceptQueueSummary(summary) {
  if (!summary?.as_of) return false;
  if (latestQueueSummary && latestQueueSummary.as_of > summary.as_of) return false;
  latestQueueSummary = summary;
  const count = Number(summary.actionable_total || 0);
  const badge = document.getElementById("queue-count");
  badge.hidden = count === 0;
  badge.textContent = count > 999 ? "999+" : String(count);
  badge.title = summary.blocked_total
    ? `${count} actionable; ${summary.blocked_total} blocked`
    : `${count} actionable`;
  return true;
}

function globalKeydown(event) {
  if (isTyping(event.target)) return;
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openPalette();
    return;
  }
  if (event.key === "?") {
    event.preventDefault();
    openHelp();
    return;
  }
  if (event.key.toLowerCase() === "u" && lastUndo) {
    event.preventDefault();
    undoLast();
    return;
  }
  if (chord === "g") {
    const target = {t: "today", q: "queue", w: "wiki", e: "entities", a: "ask", o: "ops"}[
      event.key.toLowerCase()
    ];
    chord = "";
    if (target) {
      event.preventDefault();
      location.hash = `#/${target}`;
    }
    return;
  }
  if (event.key.toLowerCase() === "g") {
    chord = "g";
    window.setTimeout(() => {
      chord = "";
    }, 1200);
  }
}

function isTyping(target) {
  const tag = target?.tagName?.toLowerCase();
  return tag === "input" || tag === "textarea" || target?.isContentEditable;
}

export function toast(text, opts = {}) {
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `<span>${escapeHtml(text)}</span>`;
  if (opts.undo) {
    lastUndo = opts.undo;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Undo";
    button.addEventListener("click", undoLast);
    el.append(button);
  }
  toastRoot.append(el);
  window.setTimeout(() => el.remove(), 6000);
}

async function undoLast() {
  if (!lastUndo) return;
  const handle = lastUndo;
  lastUndo = null;
  try {
    const result = await api("/api/queue/undo", {method: "POST", body: {undo_handle: handle}});
    acceptQueueSummary(result.queue_summary);
    toast("Undone.");
    route();
  } catch (error) {
    toast(error.message || "Undo failed.");
  }
}

export function popover(anchorEl, contentEl) {
  closePopover();
  const wrap = document.createElement("div");
  wrap.className = "popover";
  wrap.append(contentEl);
  popoverRoot.append(wrap);
  const rect = anchorEl.getBoundingClientRect();
  const top = Math.min(window.innerHeight - 24, rect.bottom + window.scrollY + 8);
  const left = Math.min(window.innerWidth - 360, Math.max(12, rect.left + window.scrollX));
  wrap.style.top = `${top}px`;
  wrap.style.left = `${left}px`;
  const close = event => {
    if (event.key === "Escape") closePopover();
  };
  document.addEventListener("keydown", close, {once: true});
  return () => {
    document.removeEventListener("keydown", close);
    wrap.remove();
  };
}

function closePopover() {
  popoverRoot.replaceChildren();
}

async function openPalette() {
  paletteEl.hidden = false;
  paletteEl.innerHTML = `<div class="panel"><input id="palette-q" autofocus placeholder="Jump or run..." autocomplete="off"><div id="palette-results"></div></div>`;
  const input = paletteEl.querySelector("input");
  const results = paletteEl.querySelector("#palette-results");
  const commands = await paletteCommands();
  const render = () => {
    const q = input.value.toLowerCase();
    const matches = commands
      .filter(item => !q || `${item.label} ${item.hint || ""}`.toLowerCase().includes(q))
      .slice(0, 20);
    results.innerHTML = matches
      .map(
        (item, index) =>
          `<button class="palette-row" data-index="${index}" type="button"><span>${escapeHtml(item.label)}</span><span class="muted">${escapeHtml(item.hint || "")}</span></button>`,
      )
      .join("");
    for (const button of results.querySelectorAll("button")) {
      button.addEventListener("click", () => runCommand(matches[Number(button.dataset.index)]));
    }
  };
  input.addEventListener("input", render);
  input.addEventListener("keydown", event => {
    if (event.key === "Escape") closePalette();
    if (event.key === "Enter") {
      const first = results.querySelector("button");
      if (first) first.click();
    }
  });
  paletteEl.addEventListener("click", event => {
    if (event.target === paletteEl) closePalette();
  }, {once: true});
  render();
  input.focus();
}

async function paletteCommands() {
  if (paletteCache) return paletteCache;
  const nav = VIEWS.map(view => ({label: view[0].toUpperCase() + view.slice(1), hint: "navigate", hash: `#/${view}`}));
  const commands = [...nav];
  try {
    const [pages, entities] = await Promise.all([
      api("/api/wiki/pages"),
      api("/api/entities"),
    ]);
    for (const page of pages.pages || []) {
      commands.push({
        label: page.title || page.relative_path,
        hint: page.relative_path,
        hash: `#/wiki/${encodeURIComponent(page.relative_path)}`,
      });
    }
    for (const entity of entities.entities || []) {
      commands.push({
        label: entity.name,
        hint: `entity · ${entity.entity_type || ""}`,
        hash: `#/entities/${entity.id}`,
      });
    }
  } catch (_error) {
    // Auth dialog will handle 401; palette still works for navigation.
  }
  paletteCache = commands;
  return commands;
}

function runCommand(command) {
  if (!command) return;
  closePalette();
  if (command.hash) location.hash = command.hash;
}

function closePalette() {
  paletteEl.hidden = true;
  paletteEl.replaceChildren();
}

function openHelp() {
  paletteEl.hidden = false;
  paletteEl.innerHTML = `<div class="panel">
    <h2>Keys</h2>
    <table class="compact"><tbody>
      <tr><td><kbd>g t</kbd></td><td>Today</td></tr>
      <tr><td><kbd>g q</kbd></td><td>Queue</td></tr>
      <tr><td><kbd>g w</kbd></td><td>Wiki</td></tr>
      <tr><td><kbd>g e</kbd></td><td>Entities</td></tr>
      <tr><td><kbd>g a</kbd></td><td>Ask</td></tr>
      <tr><td><kbd>g o</kbd></td><td>Ops</td></tr>
      <tr><td><kbd>u</kbd></td><td>Undo last queue decision</td></tr>
    </tbody></table>
    <div class="decision-bar"><button type="button" id="help-close">Close</button></div>
  </div>`;
  paletteEl.querySelector("#help-close").addEventListener("click", closePalette);
}

function openTokenDialog() {
  tokenDialog.hidden = false;
  tokenDialog.innerHTML = `<div class="panel">
    <h2>API Token</h2>
    <input id="token-value" type="password" autocomplete="off" value="${escapeAttr(localStorage.getItem("brain_token") || "")}">
    <div class="decision-bar">
      <button class="primary" id="token-save" type="button">Save</button>
      <button id="token-clear" type="button">Clear</button>
      <button id="token-cancel" type="button">Close</button>
    </div>
  </div>`;
  const input = tokenDialog.querySelector("input");
  tokenDialog.querySelector("#token-save").addEventListener("click", () => {
    localStorage.setItem("brain_token", input.value.trim());
    closeTokenDialog();
    route();
  });
  tokenDialog.querySelector("#token-clear").addEventListener("click", () => {
    localStorage.removeItem("brain_token");
    input.value = "";
  });
  tokenDialog.querySelector("#token-cancel").addEventListener("click", closeTokenDialog);
  input.focus();
}

function closeTokenDialog() {
  tokenDialog.hidden = true;
  tokenDialog.replaceChildren();
}

function clickToCopy(event) {
  const id = event.target.closest?.(".id");
  if (!id) return;
  const value = id.dataset.id || id.textContent;
  navigator.clipboard?.writeText(value);
  toast("Copied.");
}

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]),
  );
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}
