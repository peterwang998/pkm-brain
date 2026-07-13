/**
 * Brain UI v2 — minimal Markdown renderer. Escape-by-default, no raw HTML.
 */
export function render(markdown, opts = {}) {
  const root = document.createElement("div");
  root.className = "reading-doc";
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  let paragraph = [];
  let list = null;
  let nested = null;

  const flushParagraph = () => {
    if (!paragraph.length) return;
    const p = document.createElement("p");
    p.innerHTML = inline(paragraph.join(" "));
    root.append(p);
    paragraph = [];
  };
  const closeNested = () => {
    if (nested) {
      nested = null;
    }
  };
  const closeList = () => {
    closeNested();
    list = null;
  };
  const ensureList = () => {
    if (!list) {
      flushParagraph();
      list = document.createElement("ul");
      root.append(list);
    }
    return list;
  };
  const ensureNested = parentLi => {
    if (!nested) {
      nested = document.createElement("ul");
      parentLi.append(nested);
    }
    return nested;
  };

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      flushParagraph();
      closeList();
      continue;
    }
    if (/^---+$/.test(trimmed)) {
      flushParagraph();
      closeList();
      root.append(document.createElement("hr"));
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(trimmed);
    if (heading) {
      flushParagraph();
      closeList();
      const el = document.createElement(`h${heading[1].length}`);
      el.innerHTML = inline(heading[2]);
      root.append(el);
      continue;
    }
    if (trimmed.startsWith("> ")) {
      flushParagraph();
      closeList();
      const quote = document.createElement("blockquote");
      quote.className = "evidence";
      quote.innerHTML = inline(trimmed.slice(2));
      root.append(quote);
      continue;
    }
    const nestedBullet = /^ {2,}-\s+(.+)$/.exec(line);
    if (nestedBullet && list?.lastElementChild) {
      const ul = ensureNested(list.lastElementChild);
      ul.append(listItem(nestedBullet[1], opts.factRefs));
      continue;
    }
    const bullet = /^-\s+(.+)$/.exec(trimmed);
    if (bullet) {
      closeNested();
      ensureList().append(listItem(bullet[1], opts.factRefs));
      continue;
    }
    closeList();
    paragraph.push(trimmed);
  }
  flushParagraph();
  return root;
}

function listItem(text, factRefs) {
  const li = document.createElement("li");
  li.innerHTML = inline(text);
  const fact = factForBullet(text, factRefs);
  if (fact) {
    li.classList.add("fact-ref");
    li.tabIndex = 0;
    li.dataset.factId = fact.id || "";
  }
  return li;
}

function factForBullet(text, factRefs) {
  if (!factRefs) return null;
  const normalized = normalize(text);
  if (factRefs instanceof Map) {
    return factRefs.get(normalized) || factRefs.get(text) || null;
  }
  return factRefs[normalized] || factRefs[text] || null;
}

function inline(text) {
  const code = [];
  let escaped = escapeHtml(text).replace(/`([^`]+)`/g, (_match, value) => {
    const token = `\u0000${code.length}\u0000`;
    code.push(`<code>${value}</code>`);
    return token;
  });
  escaped = escaped.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (match, label, url) => {
    const decoded = url.replace(/&amp;/g, "&");
    if (!/^https?:\/\//i.test(decoded)) return match;
    return `<a href="${escapeAttr(decoded)}" rel="noreferrer" target="_blank">${label}</a>`;
  });
  escaped = escaped
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  return escaped.replace(/\u0000(\d+)\u0000/g, (_match, index) => code[Number(index)]);
}

export function normalize(text) {
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

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    char => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[char]),
  );
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/`/g, "&#96;");
}
