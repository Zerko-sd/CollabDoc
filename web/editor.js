/**
 * Block editor over a plain-text buffer (WBS 7.1).
 *
 * The OT engine synchronises characters, and the server parses those
 * characters into blocks line by line, so the buffer stays the single source
 * of truth: `# heading`, `- item`, `**bold**`. What changes is how it is shown.
 * Each line becomes a block element whose markup characters are still in the
 * DOM -- as text, inside hidden `.mk` spans -- so a block's textContent is
 * exactly its line. That identity is what makes the editor tractable: a DOM
 * caret converts to a buffer offset by counting characters, and back again.
 *
 * The browser is never allowed to edit the DOM itself. Every `beforeinput` is
 * cancelled and turned into a text operation; the host applies it, re-renders
 * and asks the editor to put the caret back. The one exception is IME
 * composition, which cannot be cancelled -- it is reconciled from the DOM when
 * composition ends.
 */
import { TextOperation } from "./ot.js";

// --------------------------------------------------------------------------
// parsing
// --------------------------------------------------------------------------

const EMOJI = /^(\p{Extended_Pictographic}(?:\uFE0F|\u200D\p{Extended_Pictographic})*) /u;
const FENCE = /^(\s*```)(\s*[\w+#.-]*\s*)$/;
const IMAGE = /^!\[([^\]\n]*)\]\((\/api\/files\/[0-9a-f-]{36})\)$/;
const TABLE_ROW = /^\s*\|.*\|\s*$/;
const TABLE_RULE = /^\s*\|(\s*:?-{3,}:?\s*\|)+\s*$/;
const TOC_MARK = "[TOC]";
// A due date on a to-do: "📅 2026-10-12".
const DUE = /^📅 ?(\d{4}-\d{2}-\d{2})/u;

/** Today as YYYY-MM-DD in the viewer's own time zone. */
export function todayKey(now = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** How urgent a due date is: overdue, today, soon (within a week) or later. */
export function dueState(iso, today = todayKey()) {
  if (iso < today) return "overdue";
  if (iso === today) return "today";
  const days = (Date.parse(`${iso}T00:00:00Z`) - Date.parse(`${today}T00:00:00Z`)) / 86400000;
  return days <= 7 ? "soon" : "later";
}

/** The headings a table of contents lists: [{level, text, line}]. */
export function headingsOf(lines, contexts = lineContexts(lines)) {
  const found = [];
  lines.forEach((line, index) => {
    const parsed = parseLine(line, contexts[index]);
    if (/^h[1-3]$/.test(parsed.type) && parsed.content.trim()) {
      found.push({ level: Number(parsed.type[1]), text: parsed.content.replace(/(\*\*|__|~~|`|\*)/g, "").trim(), line: index });
    }
  });
  return found;
}

export const isTableRow = (line) => line.trim().length > 1 && TABLE_ROW.test(line);

/**
 * Split one line into its block type, markup prefix and content.
 *
 * Most lines say what they are on their own. Lines inside a code block do
 * not -- `# x` there is a comment, not a heading -- so `ctx` (from
 * `lineContexts`) says when a line sits between two ``` fences.
 */
export function parseLine(line, ctx = null) {
  if (ctx?.code) return { type: "code", prefix: "", content: line, indent: 0, lang: ctx.lang };
  const fence = FENCE.exec(line);
  if (fence) {
    if (ctx?.closing) return { type: "fence", prefix: line, content: "", indent: 0, closing: true };
    return { type: "fence", prefix: fence[1], content: fence[2], indent: 0, lang: fence[2].trim() };
  }
  if (line.trim() === TOC_MARK) return { type: "toc", prefix: line, content: "", indent: 0 };
  const image = IMAGE.exec(line);
  if (image) return { type: "image", prefix: line, content: "", indent: 0, alt: image[1], src: image[2] };
  if (isTableRow(line)) {
    if (TABLE_RULE.test(line)) return { type: "table-rule", prefix: line, content: "", indent: 0 };
    return { type: "table", prefix: "", content: line, indent: 0 };
  }

  const indentText = /^ */.exec(line)[0];
  const rest = line.slice(indentText.length);
  const indent = Math.min(4, Math.floor(indentText.length / 2));
  let match;

  if (/^(-{3,}|\*{3,})\s*$/.test(rest)) {
    return { type: "divider", prefix: line, content: "", indent: 0 };
  }
  const block = (type, marker, extra = {}) => ({
    type,
    prefix: indentText + marker,
    content: rest.slice(marker.length),
    indent,
    ...extra,
  });
  if ((match = /^(#{1,3}) /.exec(rest))) return block(`h${match[1].length}`, match[0]);
  if ((match = /^[-*+] \[([ xX])\] /.exec(rest))) {
    return block("todo", match[0], { checked: match[1] !== " " });
  }
  if ((match = /^[-*+] /.exec(rest))) return block("bullet", match[0]);
  if ((match = /^(\d{1,9})[.)] /.exec(rest))) {
    return block("number", match[0], { n: Number(match[1]) });
  }
  if ((match = /^> /.exec(rest))) {
    const icon = EMOJI.exec(rest.slice(2));
    if (icon) {
      return block("callout", match[0], { icon: icon[1], iconText: icon[0] });
    }
    return block("quote", match[0]);
  }
  return block("paragraph", "");
}

const LIST_TYPES = new Set(["bullet", "number", "todo"]);
// Blocks that are one unit: Backspace removes them whole.
const ATOMIC_TYPES = new Set(["divider", "image", "toc"]);
// Lines whose markup is structure: block conversions and indenting skip them.
const STRUCTURAL_TYPES = new Set(["code", "fence", "table", "table-rule"]);

/**
 * What each line needs to know about its neighbours: whether it is inside a
 * code block (and which fence closes it), and the shape of the table it
 * belongs to. A fence opens a block and the next fence closes it, matching
 * the server's block parser; an unclosed fence runs to the end.
 */
export function lineContexts(lines) {
  const contexts = new Array(lines.length).fill(null);
  let lang = null; // non-null while inside a code block
  for (let i = 0; i < lines.length; i += 1) {
    const fence = FENCE.exec(lines[i]);
    if (lang !== null) {
      if (fence) {
        contexts[i] = { closing: true };
        lang = null;
      } else {
        contexts[i] = { code: true, lang };
      }
    } else if (fence) {
      lang = fence[2].trim();
      contexts[i] = { opening: true };
    } else if (isTableRow(lines[i])) {
      // A run of rows: every row takes the widest row's column count, and
      // the first row is a header when a |---| rule follows it.
      let end = i;
      while (end + 1 < lines.length && isTableRow(lines[end + 1])) end += 1;
      const cols = Math.max(...lines.slice(i, end + 1).map((row) => tableCells(row).length));
      const header = end > i && TABLE_RULE.test(lines[i + 1]);
      for (let k = i; k <= end; k += 1) contexts[k] = { cols, header: header && k === i, last: k === end };
      i = end;
    }
  }
  return contexts;
}

/** A table row's cells as [from, to) ranges of the line, between the pipes. */
export function tableCells(line) {
  const cells = [];
  const first = line.indexOf("|");
  const last = line.lastIndexOf("|");
  let from = first + 1;
  for (let i = from; i <= last; i += 1) {
    if (line[i] === "|" && line[i - 1] !== "\\") {
      cells.push([from, i]);
      from = i + 1;
    }
  }
  return cells;
}

/** The prefix a new line gets when Enter is pressed at the end of this one. */
function continuation(parsed) {
  const pad = " ".repeat(parsed.indent * 2);
  switch (parsed.type) {
    case "bullet":
      return `${pad}- `;
    case "todo":
      return `${pad}- [ ] `;
    case "number":
      return `${pad}${parsed.n + 1}. `;
    case "quote":
      return "> ";
    default:
      return "";
  }
}

/** The prefix that turns a line into `type`, keeping list indentation. */
export function prefixFor(type, parsed) {
  const pad = LIST_TYPES.has(type) ? " ".repeat((parsed?.indent || 0) * 2) : "";
  return {
    paragraph: "",
    h1: "# ",
    h2: "## ",
    h3: "### ",
    bullet: `${pad}- `,
    number: `${pad}${parsed?.type === "number" ? parsed.n : 1}. `,
    todo: `${pad}- [ ] `,
    quote: "> ",
    callout: "> 💡 ",
  }[type];
}

/** Content of a line without its prefix (a callout's icon counts as prefix). */
function bodyOf(parsed) {
  return parsed.type === "callout" ? parsed.content.slice(parsed.iconText.length) : parsed.content;
}

// --------------------------------------------------------------------------
// rendering
// --------------------------------------------------------------------------

const INLINE = [
  { re: /^`([^`]+)`/, tag: "code", open: "`", close: "`", nested: false },
  { re: /^\*\*(.+?)\*\*/, tag: "strong", open: "**", close: "**" },
  { re: /^~~(.+?)~~/, tag: "s", open: "~~", close: "~~" },
  { re: /^__(.+?)__/, tag: "u", open: "__", close: "__" },
  { re: /^\*([^*\s](?:[^*]*[^*\s])?)\*/, tag: "em", open: "*", close: "*" },
];

export const MENTION = /^@\[([^\]\n]+)\]\(user:([0-9a-f-]{36})\)/;

// Keywords shared by the languages people paste most; good enough to make
// code readable without shipping a highlighter.
const KEYWORDS = new Set(
  (
    "as async await break case catch class const continue def default del do elif else enum " +
    "except export extends false finally fn for from func function if impl import in interface " +
    "is lambda let match new nil none not null or pass pub raise return self static struct super " +
    "switch this throw true try type typeof undefined use var void while with yield and package " +
    "private protected public select where insert update delete create table into values"
  ).split(" ")
);
const HASH_COMMENTS = new Set(["python", "py", "sh", "bash", "shell", "ruby", "rb", "yaml", "yml", "toml", "r"]);

/** One line of code as highlighted spans; their text is exactly `text`. */
export function highlightCode(text, lang, parent) {
  const comment = HASH_COMMENTS.has((lang || "").toLowerCase()) ? /^#.*/ : /^\/\/.*/;
  const token = /^("(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|`[^`]*`?)|^(\d[\d_.]*\w*)|^([A-Za-z_$][\w$]*)/;
  let plain = "";
  const flush = () => {
    if (plain) parent.append(plain);
    plain = "";
  };
  const span = (cls, value) => {
    flush();
    const node = document.createElement("span");
    node.className = cls;
    node.textContent = value;
    parent.append(node);
  };
  let i = 0;
  while (i < text.length) {
    const rest = text.slice(i);
    const remark = comment.exec(rest);
    if (remark) {
      span("tok-comment", remark[0]);
      break;
    }
    const match = token.exec(rest);
    if (!match) {
      plain += text[i];
      i += 1;
      continue;
    }
    if (match[1]) span("tok-string", match[1]);
    else if (match[2]) span("tok-number", match[2]);
    else if (KEYWORDS.has(match[3].toLowerCase())) span("tok-keyword", match[3]);
    else plain += match[3];
    i += match[0].length;
  }
  flush();
}

// Images can't be fetched by URL alone -- they need the session's token --
// so the app supplies the loader that turns an /api/files URL into a src.
let imageLoader = (img, src) => {
  img.src = src;
};
export function setImageLoader(loader) {
  imageLoader = loader;
}

// Page links keep the title they were written with in the buffer; the page
// may have been renamed since. The app supplies the current titles, and a
// link shows the live one (the written label stays, hidden, as source).
let pageTitles = new Map();
let pageTitlesVersion = 0;
export function setPageTitles(titles) {
  const next = new Map(titles);
  const same = next.size === pageTitles.size && [...next].every(([id, title]) => pageTitles.get(id) === title);
  if (same) return false;
  pageTitles = next;
  pageTitlesVersion += 1;
  return true;
}

function marker(text, extraClass = "") {
  const span = document.createElement("span");
  span.className = extraClass ? `mk ${extraClass}` : "mk";
  span.textContent = text;
  return span;
}

function safeHref(url) {
  return /^(https?:|mailto:)/i.test(url) ? url : /^[\w.-]+\.[a-z]{2,}(\/|$)/i.test(url) ? `https://${url}` : null;
}

/**
 * Inline markup as DOM, with every markup character kept in a hidden span.
 * Text is only ever inserted as text nodes, so content cannot inject HTML.
 */
export function renderInline(text, parent) {
  let plain = "";
  const flush = () => {
    if (plain) parent.append(plain);
    plain = "";
  };
  let i = 0;
  while (i < text.length) {
    const rest = text.slice(i);
    let handled = false;

    const mention = MENTION.exec(rest);
    if (mention) {
      flush();
      // "@" stays visible as part of the chip; the brackets and id do not.
      const chip = document.createElement("span");
      chip.className = "mention";
      chip.dataset.user = mention[2];
      chip.append("@", marker("["), mention[1], marker(`](user:${mention[2]})`));
      parent.append(chip);
      i += mention[0].length;
      continue;
    }

    const due = DUE.exec(rest);
    if (due) {
      flush();
      // The chip is the date's own text, so the line's text is unchanged.
      const chip = document.createElement("span");
      chip.className = `due ${dueState(due[1])}`;
      chip.dataset.due = due[1];
      chip.textContent = due[0];
      parent.append(chip);
      i += due[0].length;
      continue;
    }

    const link = /^\[([^\]\n]+)\]\(([^)\s]+)\)/.exec(rest);
    if (link && link[2].startsWith("page:")) {
      flush();
      const anchor = document.createElement("a");
      anchor.className = "page-link";
      const pageId = link[2].slice(5);
      anchor.dataset.page = pageId;
      anchor.title = "Open page";
      const current = pageTitles.get(pageId);
      if (current !== undefined && current !== link[1]) {
        // Renamed since the link was written: the old label is source text
        // (hidden), the live title is decoration outside the offset map.
        const live = document.createElement("span");
        live.className = "live-title";
        live.textContent = current || "Untitled";
        anchor.append(marker(link[1], "stale"), nonSource(live));
      } else {
        renderInline(link[1], anchor);
      }
      if (current === undefined && pageTitles.size) {
        anchor.classList.add("missing");
        anchor.title = "This page was deleted, or you do not have access to it";
      }
      parent.append(marker("["), anchor, marker(`](${link[2]})`));
      i += link[0].length;
      continue;
    }
    if (link) {
      flush();
      const anchor = document.createElement("a");
      const href = safeHref(link[2]);
      if (href) anchor.href = href;
      anchor.title = `${link[2]} — Ctrl+click to open`;
      renderInline(link[1], anchor);
      parent.append(marker("["), anchor, marker(`](${link[2]})`));
      i += link[0].length;
      continue;
    }

    for (const rule of INLINE) {
      const match = rule.re.exec(rest);
      if (!match) continue;
      flush();
      const node = document.createElement(rule.tag);
      if (rule.nested === false) node.textContent = match[1];
      else renderInline(match[1], node);
      parent.append(marker(rule.open), node, marker(rule.close));
      i += match[0].length;
      handled = true;
      break;
    }
    if (handled) continue;
    plain += text[i];
    i += 1;
  }
  flush();
}

const CONTENT_TAG = { h1: "h1", h2: "h2", h3: "h3", paragraph: "p" };
const PLACEHOLDER = {
  h1: "Heading 1",
  h2: "Heading 2",
  h3: "Heading 3",
  bullet: "List",
  number: "List",
  todo: "To-do",
  quote: "Empty quote",
  callout: "Type something…",
  paragraph: "Type '/' for commands",
  fence: "Code — type a language",
};

function nonSource(node) {
  node.dataset.nosrc = "";
  node.contentEditable = "false";
  node.setAttribute("contenteditable", "false");
  return node;
}

/**
 * One line as a block element.
 *
 * `info` carries what the line cannot know about itself: its position among
 * the non-blank blocks (the attribution index), its displayed list number,
 * and the author the attribution engine assigned it.
 */
export function renderLine(line, info = {}) {
  const ctx = info.ctx || null;
  const parsed = parseLine(line, ctx);
  const el = document.createElement("div");
  el.className = "line";
  el.dataset.type = parsed.type;
  if (parsed.indent) el.style.setProperty("--indent", parsed.indent);
  if (line.trim()) {
    el.classList.add("block");
    if (info.block !== undefined) el.dataset.index = info.block;
  }

  if (parsed.type === "divider") {
    el.append(marker(line));
    const rule = nonSource(document.createElement("hr"));
    el.append(rule);
    return el;
  }

  if (parsed.type === "toc") {
    el.append(marker(line));
    const nav = nonSource(document.createElement("nav"));
    nav.className = "toc";
    const head = document.createElement("div");
    head.className = "toc-head";
    head.textContent = "Contents";
    nav.append(head);
    const headings = info.toc || [];
    if (!headings.length) {
      const empty = document.createElement("div");
      empty.className = "toc-empty";
      empty.textContent = "Add headings and they will be listed here.";
      nav.append(empty);
    }
    for (const heading of headings) {
      const item = document.createElement("a");
      item.className = `toc-item level-${heading.level}`;
      item.dataset.tocLine = heading.line;
      item.textContent = heading.text;
      nav.append(item);
    }
    el.append(nav);
    return el;
  }

  if (parsed.type === "image") {
    el.append(marker(line));
    const figure = nonSource(document.createElement("figure"));
    const img = document.createElement("img");
    img.alt = parsed.alt;
    img.draggable = false;
    img.loading = "lazy";
    imageLoader(img, parsed.src);
    figure.append(img);
    el.append(figure);
    return el;
  }

  if (parsed.type === "table-rule" || (parsed.type === "fence" && parsed.closing)) {
    // Structure only: kept in the buffer, drawn as the edge of its block.
    if (parsed.closing) el.dataset.closing = "";
    el.append(marker(line), nonSource(document.createElement("span")));
    return el;
  }

  if (parsed.type === "code") {
    const code = document.createElement("code");
    code.className = "c";
    if (line) highlightCode(line, parsed.lang, code);
    else {
      code.append(document.createElement("br"));
      el.classList.add("is-empty");
    }
    el.append(code);
    return el;
  }

  if (parsed.type === "table") {
    const row = document.createElement("div");
    row.className = "c";
    const cells = tableCells(line);
    el.style.setProperty("--cols", ctx?.cols || cells.length);
    if (ctx?.header) el.classList.add("table-head");
    if (ctx?.last) el.classList.add("table-last");
    let at = 0;
    for (const [from, to] of cells) {
      row.append(marker(line.slice(at, from)));
      const cell = document.createElement("span");
      cell.className = "cell";
      renderInline(line.slice(from, to), cell);
      if (!cell.firstChild) cell.append(document.createElement("br"));
      row.append(cell);
      at = to;
    }
    row.append(marker(line.slice(at)));
    el.append(row);
    return el;
  }

  if (parsed.prefix) el.append(marker(parsed.prefix));

  if (parsed.type === "todo") {
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = "todo-box";
    box.checked = parsed.checked;
    box.disabled = !info.interactive;
    box.tabIndex = -1;
    const wrap = nonSource(document.createElement("span"));
    wrap.className = "check";
    wrap.append(box);
    el.append(wrap);
    if (parsed.checked) el.classList.add("done");
  }
  if (parsed.type === "number") el.dataset.num = `${info.number ?? parsed.n}.`;
  if (parsed.type === "callout") el.append(marker(parsed.iconText, "icon"));

  const body = bodyOf(parsed);
  const content = document.createElement(CONTENT_TAG[parsed.type] || "div");
  content.className = "c";
  renderInline(body, content);
  if (!body) {
    content.append(document.createElement("br"));
    el.classList.add("is-empty");
    content.dataset.placeholder = PLACEHOLDER[parsed.type] || "";
  }
  el.append(content);

  const author = info.author;
  if (author) {
    el.dataset.author = author.id;
    if (info.authorStart) el.classList.add("author-start");
    el.style.setProperty("--author-color", author.color);
    el.style.setProperty("--author-tint", `${author.color}14`);
    const who = nonSource(document.createElement("span"));
    who.className = "who";
    const name = document.createElement("span");
    name.className = "who-name";
    name.textContent = author.name;
    const score = document.createElement("span");
    score.className = "who-score";
    score.textContent = ` · ${Math.round(author.confidence * 100)}%`;
    who.append(name, score);
    who.title =
      author.method === "hybrid"
        ? "Stylometric model and keystroke ledger combined"
        : "Keystroke ledger only -- the model needs more writing to train on";
    el.append(who);
  }
  if (info.comments) {
    const badge = nonSource(document.createElement("button"));
    badge.type = "button";
    badge.className = "comment-badge";
    badge.textContent = String(info.comments);
    badge.title = `${info.comments} open comment${info.comments === 1 ? "" : "s"}`;
    el.append(badge);
  }
  return el;
}

/** Display numbers for numbered lists: consecutive items count 1, 2, 3. */
export function listNumbers(lines) {
  const numbers = new Array(lines.length);
  const counters = [];
  for (let i = 0; i < lines.length; i += 1) {
    const parsed = parseLine(lines[i]);
    if (parsed.type === "number") {
      counters.length = parsed.indent + 1;
      const previous = counters[parsed.indent];
      counters[parsed.indent] = previous === undefined ? parsed.n : previous + 1;
      numbers[i] = counters[parsed.indent];
    } else if (!(LIST_TYPES.has(parsed.type) && parsed.indent > 0)) {
      counters.length = 0;
    }
  }
  return numbers;
}

/** A whole buffer as block elements, for read-only surfaces. */
export function renderDocument(text, options = {}) {
  const lines = text.split("\n");
  const numbers = listNumbers(lines);
  const contexts = lineContexts(lines);
  const toc = lines.some((line) => line.trim() === TOC_MARK) ? headingsOf(lines, contexts) : null;
  const nodes = [];
  let block = 0;
  lines.forEach((line, i) => {
    // Blank lines inside code are part of the code.
    if (!line.trim() && options.skipBlank && !contexts[i]?.code) return;
    nodes.push(
      renderLine(line, { block: line.trim() ? block : undefined, number: numbers[i], ctx: contexts[i], toc })
    );
    if (line.trim()) block += 1;
  });
  return nodes;
}

// --------------------------------------------------------------------------
// the editor
// --------------------------------------------------------------------------

// DOM constants by value: the client also runs under jsdom, where Node and
// NodeFilter are not globals.
const TEXT_NODE = 3;
const ELEMENT_NODE = 1;
const SHOW_TEXT = 4;
const FILTER_ACCEPT = 1;
const FILTER_REJECT = 2;

const isHidden = (node) => Boolean(node.parentElement?.closest(".mk"));
const imageFiles = (transfer) =>
  Array.from(transfer?.files || []).filter((file) => /^image\/(png|jpeg|gif|webp)$/.test(file.type));
const MARKERS_BEFORE = /^([*_~`]|\[)*$/;
const MARKERS_AFTER = /^([*_~`]|\]\([^)]*\))*$/;

export const SLASH_ITEMS = [
  { id: "paragraph", label: "Text", hint: "Plain paragraph", icon: "Aa", keys: "text paragraph plain" },
  { id: "h1", label: "Heading 1", hint: "Big section heading", icon: "H1", keys: "heading title h1" },
  { id: "h2", label: "Heading 2", hint: "Medium section heading", icon: "H2", keys: "heading subtitle h2" },
  { id: "h3", label: "Heading 3", hint: "Small section heading", icon: "H3", keys: "heading h3" },
  { id: "bullet", label: "Bulleted list", hint: "A simple list", icon: "•", keys: "bullet list unordered ul" },
  { id: "number", label: "Numbered list", hint: "A list with numbers", icon: "1.", keys: "numbered list ordered ol" },
  { id: "todo", label: "To-do list", hint: "Track tasks", icon: "☑", keys: "todo task checkbox check" },
  { id: "quote", label: "Quote", hint: "Capture a quote", icon: "❝", keys: "quote blockquote citation" },
  { id: "callout", label: "Callout", hint: "Make writing stand out", icon: "💡", keys: "callout note info tip" },
  { id: "divider", label: "Divider", hint: "Separate sections", icon: "—", keys: "divider rule hr line separator" },
  { id: "code", label: "Code", hint: "Capture a code snippet", icon: "</>", keys: "code snippet block pre program" },
  { id: "table", label: "Table", hint: "Rows and columns", icon: "▦", keys: "table grid rows columns spreadsheet" },
  { id: "image", label: "Image", hint: "Upload a picture", icon: "🖼", keys: "image picture photo upload screenshot img" },
  { id: "page", label: "Page", hint: "Add a sub-page here", icon: "📄", keys: "page subpage sub-page child nested link" },
  { id: "mention", label: "Mention", hint: "Notify someone", icon: "@", keys: "mention person people user notify at" },
  { id: "date", label: "Today's date", hint: "Insert the date", icon: "📅", keys: "date today now time" },
  { id: "due", label: "Due date", hint: "Give a to-do a deadline", icon: "⏰", keys: "due deadline date task todo remind" },
  { id: "toc", label: "Table of contents", hint: "A live list of the headings", icon: "☰", keys: "toc table contents outline index headings" },
  { id: "ai-summarize", label: "AI: Summarize page", hint: "Key points as bullets", icon: "✨", keys: "ai summarize summary tldr assistant claude" },
  { id: "ai-continue", label: "AI: Continue writing", hint: "Draft the next paragraph", icon: "✨", keys: "ai continue write draft assistant claude" },
  { id: "ai-improve", label: "AI: Improve writing", hint: "Clearer, tighter prose", icon: "✨", keys: "ai improve rewrite polish grammar assistant claude" },
  { id: "ai-outline", label: "AI: Suggest an outline", hint: "Headings for this page", icon: "✨", keys: "ai outline structure headings assistant claude" },
];

export class BlockEditor {
  /**
   * @param {HTMLElement} root  the contenteditable surface
   * @param {object} host       text(), commit(op), canEdit(), infoFor(lines),
   *                            format(kind), undo(), redo(), save(), onSelection()
   */
  constructor(root, host) {
    this.root = root;
    this.host = host;
    this.lines = [];
    this.contexts = [];
    this.starts = [];
    this.signatures = [];
    this.pending = null; // {from, to, dom} -- the caret we placed, unambiguously
    this.composing = false;
    this.slash = null; // {at} -- offset of the "/" that opened the menu
    this.slashIndex = 0;

    root.addEventListener("beforeinput", (event) => this.onBeforeInput(event));
    root.addEventListener("keydown", (event) => this.onKeyDown(event));
    root.addEventListener("compositionstart", () => (this.composing = true));
    root.addEventListener("compositionend", () => {
      this.composing = false;
      this.reconcile();
    });
    root.addEventListener("input", () => {
      if (!this.composing) this.reconcile();
    });
    root.addEventListener("paste", (event) => this.onPaste(event));
    root.addEventListener("copy", (event) => this.onCopy(event, false));
    root.addEventListener("cut", (event) => this.onCopy(event, true));
    root.addEventListener("dragover", (event) => {
      if (imageFiles(event.dataTransfer).length || event.dataTransfer?.types?.includes("Files")) {
        event.preventDefault();
      }
    });
    root.addEventListener("drop", (event) => this.onDrop(event));
    root.addEventListener("dragstart", (event) => event.preventDefault());
    root.addEventListener("mousedown", (event) => this.onMouseDown(event));
    root.addEventListener("click", (event) => this.onClick(event));
  }

  // -- rendering -----------------------------------------------------------

  render() {
    const text = this.host.text();
    // Re-rendering replaces DOM nodes, which drops the browser's caret. When
    // only decoration changed (attribution, comments), put it back where it
    // was; edits place the caret themselves once the render is done.
    const keep = this.hasFocus() && text === this.renderedText ? this.selection() : null;
    const lines = text.split("\n");
    const contexts = lineContexts(lines);
    const toc = lines.some((line) => line.trim() === TOC_MARK) ? headingsOf(lines, contexts) : null;
    const info = this.host
      .infoFor(lines)
      .map((entry, i) => ({ ...entry, ctx: contexts[i], ...(toc && lines[i].trim() === TOC_MARK ? { toc } : {}) }));
    const today = todayKey();
    // The context is part of the signature: typing a ``` re-renders every
    // line below it, because they all just became code.
    // So is the page-title table for lines that link to pages: a rename
    // elsewhere re-renders exactly the links that show that title.
    const signatures = lines.map((line, i) =>
      JSON.stringify([
        line,
        info[i],
        line.includes("](page:") ? pageTitlesVersion : 0,
        line.includes("📅") ? today : 0, // a due date turns red at midnight
      ])
    );
    this.contexts = contexts;

    // Replace only the lines whose rendering changed: the common prefix and
    // suffix of the old and new signature lists stay in the DOM untouched.
    const old = this.signatures;
    let head = 0;
    while (head < old.length && head < signatures.length && old[head] === signatures[head]) head += 1;
    let tail = 0;
    while (
      tail < old.length - head &&
      tail < signatures.length - head &&
      old[old.length - 1 - tail] === signatures[signatures.length - 1 - tail]
    ) {
      tail += 1;
    }
    const children = this.root.children;
    const removeCount = old.length - head - tail;
    if (children.length !== old.length) {
      this.root.replaceChildren(...lines.map((line, i) => renderLine(line, info[i])));
    } else {
      for (let k = 0; k < removeCount; k += 1) children[head].remove();
      const fresh = lines
        .slice(head, lines.length - tail)
        .map((line, k) => renderLine(line, info[head + k]));
      const before = children[head] || null;
      for (const node of fresh) this.root.insertBefore(node, before);
    }

    this.lines = lines;
    this.signatures = signatures;
    this.starts = [];
    let offset = 0;
    for (const line of lines) {
      this.starts.push(offset);
      offset += line.length + 1;
    }
    this.pending = null;
    this.renderedText = text;
    this.root.classList.toggle("is-blank", text.length === 0);
    if (keep) this.select(keep.from, keep.to);
  }

  // -- offsets <-> DOM -----------------------------------------------------

  sourceNodes(lineEl) {
    return document.createTreeWalker(lineEl, SHOW_TEXT, {
      acceptNode: (node) =>
        node.parentElement?.closest("[data-nosrc]")
          ? FILTER_REJECT
          : FILTER_ACCEPT,
    });
  }

  lineIndexAt(offset) {
    let low = 0;
    let high = this.starts.length - 1;
    while (low < high) {
      const mid = (low + high + 1) >> 1;
      if (this.starts[mid] <= offset) low = mid;
      else high = mid - 1;
    }
    return low;
  }

  /** Buffer offset of a DOM boundary point, or null if it is not in the editor. */
  offsetOf(node, offset) {
    if (!node || !this.root.contains(node)) return null;
    if (node === this.root) {
      if (offset >= this.lines.length) return this.host.text().length;
      return this.starts[offset] ?? 0;
    }
    const lineEl = (node.nodeType === TEXT_NODE ? node.parentElement : node).closest(".line");
    if (!lineEl || lineEl.parentElement !== this.root) return null;
    const index = Array.prototype.indexOf.call(this.root.children, lineEl);
    const base = this.starts[index] ?? 0;

    const skipped = node.parentElement?.closest("[data-nosrc]");
    if (node.nodeType === TEXT_NODE && !skipped) {
      let count = 0;
      const walker = this.sourceNodes(lineEl);
      for (let text = walker.nextNode(); text; text = walker.nextNode()) {
        if (text === node) return base + count + Math.min(offset, text.length);
        count += text.length;
      }
    }
    const range = document.createRange();
    if (skipped || (node.nodeType === ELEMENT_NODE && node.closest("[data-nosrc]"))) {
      range.setStartBefore(skipped || node.closest("[data-nosrc]"));
    } else {
      range.setStart(node, offset);
    }
    let count = 0;
    const walker = this.sourceNodes(lineEl);
    for (let text = walker.nextNode(); text; text = walker.nextNode()) {
      if (range.comparePoint(text, 0) < 0) count += text.length;
      else break;
    }
    return base + count;
  }

  /** The DOM boundary point for a buffer offset, preferring visible text. */
  pointAt(offset) {
    const text = this.host.text();
    const clamped = Math.max(0, Math.min(offset, text.length));
    const index = this.lineIndexAt(clamped);
    const lineEl = this.root.children[index];
    if (!lineEl) return { node: this.root, offset: 0 };
    const relative = clamped - this.starts[index];

    let count = 0;
    const walker = this.sourceNodes(lineEl);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!isHidden(node) && relative <= count + node.length) {
        return { node, offset: Math.max(0, relative - count) };
      }
      count += node.length;
    }
    const content = lineEl.querySelector(".c");
    if (!content) return { node: lineEl, offset: 0 };
    const last = content.lastChild;
    const end = content.childNodes.length - (last?.nodeName === "BR" ? 1 : 0);
    return { node: content, offset: Math.max(0, end) };
  }

  selection() {
    const selection = window.getSelection?.();
    if (!selection || !selection.rangeCount) return null;
    const { anchorNode, anchorOffset, focusNode, focusOffset } = selection;
    if (!this.root.contains(anchorNode) || !this.root.contains(focusNode)) return null;
    const pending = this.pending;
    if (
      pending &&
      pending.anchorNode === anchorNode &&
      pending.anchorOffset === anchorOffset &&
      pending.focusNode === focusNode &&
      pending.focusOffset === focusOffset
    ) {
      return { from: pending.from, to: pending.to };
    }
    const anchor = this.offsetOf(anchorNode, anchorOffset);
    const focus = this.offsetOf(focusNode, focusOffset);
    if (anchor === null || focus === null) return null;
    return { from: Math.min(anchor, focus), to: Math.max(anchor, focus) };
  }

  select(from, to = from) {
    const selection = window.getSelection?.();
    if (!selection) return;
    const start = this.pointAt(from);
    const end = from === to ? start : this.pointAt(to);
    try {
      selection.setBaseAndExtent(start.node, start.offset, end.node, end.offset);
    } catch {
      return;
    }
    this.pending = {
      from,
      to,
      anchorNode: selection.anchorNode,
      anchorOffset: selection.anchorOffset,
      focusNode: selection.focusNode,
      focusOffset: selection.focusOffset,
    };
  }

  hasFocus() {
    return document.activeElement === this.root;
  }

  focus(offset) {
    this.root.focus({ preventScroll: true });
    if (offset !== undefined) this.select(offset);
  }

  /** Screen rectangle of a caret at `offset`, for remote cursors and menus. */
  caretRect(offset) {
    const point = this.pointAt(offset);
    const range = document.createRange();
    try {
      range.setStart(point.node, point.offset);
    } catch {
      return null;
    }
    range.collapse(true);
    const rect = range.getClientRects?.()[0];
    if (rect && (rect.height || rect.width)) return rect;
    const holder = point.node.nodeType === TEXT_NODE ? point.node.parentElement : point.node;
    const box = holder.getBoundingClientRect();
    const style = window.getComputedStyle(holder);
    const height = parseFloat(style.lineHeight) || box.height;
    return { left: box.left, top: box.top, bottom: box.top + height, height, width: 0 };
  }

  lineAt(offset) {
    const index = this.lineIndexAt(offset);
    const line = this.lines[index] ?? "";
    const ctx = this.contexts?.[index] ?? null;
    return { index, start: this.starts[index] ?? 0, line, ctx, parsed: parseLine(line, ctx) };
  }

  // -- editing -------------------------------------------------------------

  /** Replace [from, to) with `insert` and place the caret. */
  replace(from, to, insert, caretFrom = from + insert.length, caretTo = caretFrom) {
    const text = this.host.text();
    const operation = new TextOperation()
      .retain(from)
      .delete(to - from)
      .insert(insert)
      .retain(text.length - to);
    if (operation.isNoop()) {
      this.select(caretFrom, caretTo);
      return;
    }
    this.host.commit(operation);
    this.select(caretFrom, caretTo);
  }

  /** Swap the whole buffer for `next`, as the smallest operation that does it. */
  rewrite(next, caretFrom, caretTo = caretFrom) {
    const operation = TextOperation.fromDiff(this.host.text(), next);
    if (!operation.isNoop()) this.host.commit(operation);
    if (caretFrom !== undefined) this.select(caretFrom, caretTo);
  }

  /** Read the DOM back into text after edits the browser made on its own. */
  reconcile() {
    const lines = [];
    for (const lineEl of this.root.children) {
      let text = "";
      const walker = this.sourceNodes(lineEl);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) text += node.data;
      lines.push(text.replace(/\n/g, ""));
    }
    const next = lines.join("\n");
    const selection = this.selection();
    if (next === this.host.text() || !this.host.canEdit()) {
      this.signatures = [];
      this.render();
      if (selection) this.select(selection.from, selection.to);
      return;
    }
    const operation = TextOperation.fromDiff(this.host.text(), next);
    // The browser changed the DOM behind the renderer's back, so none of
    // the cached line renderings can be trusted.
    this.signatures = [];
    this.host.commit(operation);
    if (selection) this.select(selection.from, selection.to);
  }

  onBeforeInput(event) {
    if (event.inputType === "insertCompositionText" || this.composing) return;
    event.preventDefault();
    if (!this.host.canEdit()) return;
    const selection = this.selection();
    if (!selection) return;
    const type = event.inputType;

    if (type === "insertText" || type === "insertReplacementText") {
      let data = event.data ?? event.dataTransfer?.getData("text/plain") ?? "";
      let range = selection;
      if (type === "insertReplacementText") range = this.targetRange(event) || selection;
      this.insertText(range.from, range.to, data);
    } else if (type === "insertParagraph" || type === "insertLineBreak") {
      this.enter(selection);
    } else if (type === "insertFromPaste" || type === "insertFromDrop" || type === "insertFromYank") {
      const data = event.dataTransfer?.getData("text/plain") ?? event.data ?? "";
      this.insertText(selection.from, selection.to, data);
    } else if (type.startsWith("delete")) {
      this.remove(event, selection);
    } else if (type === "formatBold") {
      this.toggleInline("bold");
    } else if (type === "formatItalic") {
      this.toggleInline("italic");
    } else if (type === "formatUnderline") {
      this.toggleInline("underline");
    } else if (type === "formatStrikeThrough") {
      this.toggleInline("strike");
    } else if (type === "historyUndo") {
      this.host.undo();
    } else if (type === "historyRedo") {
      this.host.redo();
    }
  }

  targetRange(event) {
    const ranges = event.getTargetRanges?.();
    if (!ranges || !ranges.length) return null;
    const range = ranges[0];
    const start = this.offsetOf(range.startContainer, range.startOffset);
    const end = this.offsetOf(range.endContainer, range.endOffset);
    if (start === null || end === null) return null;
    return { from: Math.min(start, end), to: Math.max(start, end) };
  }

  insertText(from, to, data) {
    data = data.replace(/\r\n?/g, "\n");
    if (!data && from === to) return;
    // Typing after bold text continues the bold, but typing after a link
    // does not extend the link.
    ({ from, to } = this.pastClosingMarkup(from, to, { linksOnly: true }));
    const text = this.host.text();

    // Typing "/" (blocks) or "@" (people) at the start of a block or after a
    // space opens a menu -- but not in code, where both are just characters.
    if ((data === "/" || data === "@") && from === to) {
      const { start, parsed } = this.lineAt(from);
      const before = text.slice(start + parsed.prefix.length, from);
      this.replace(from, to, data);
      const literal = parsed.type === "code" || parsed.type === "fence";
      if (!literal && (before === "" || /\s$/.test(before))) {
        this.openSlash(from, data === "@" ? "mention" : "slash");
      }
      return;
    }

    // "[] " at the start of a paragraph is the to-do shortcut.
    if (data === " " && from === to) {
      const { start, parsed } = this.lineAt(from);
      if (parsed.type === "paragraph" && text.slice(start, from) === `${parsed.prefix}[]`) {
        const replacement = `${parsed.prefix}- [ ] `;
        this.replace(start, from, replacement);
        return;
      }
    }
    this.replace(from, to, data);
  }

  /**
   * A caret drawn at the end of a line can sit before hidden closing markup
   * (`**`, `](url)`), since the browser only sees the visible text. For
   * structural edits, the user means the end of the line.
   */
  pastClosingMarkup(from, to, { linksOnly = false } = {}) {
    if (from !== to) return { from, to };
    const { start, line } = this.lineAt(from);
    const rest = line.slice(from - start);
    if (!rest || !MARKERS_AFTER.test(rest)) return { from, to };
    if (linksOnly && !rest.includes("](")) return { from, to };
    const end = start + line.length;
    return { from: end, to: end };
  }

  enter(selection) {
    const structural = this.enterStructural(selection);
    if (structural) return;
    const { from, to } = this.pastClosingMarkup(selection.from, selection.to);
    const { start, line, parsed } = this.lineAt(from);
    const relative = from - start;
    const body = bodyOf(parsed);

    // Enter on an empty list item leaves the list (or steps out one level).
    if (from === to && parsed.prefix && !body && parsed.type !== "divider") {
      if (parsed.indent > 0 && LIST_TYPES.has(parsed.type)) {
        this.replace(start, start + 2, "", from - 2);
      } else {
        this.replace(start, start + parsed.prefix.length, "", start);
      }
      return;
    }

    // At the start of a non-empty block, open a line above and stay put.
    const lead = line.slice(parsed.prefix.length, relative);
    if (from === to && body && relative >= parsed.prefix.length && MARKERS_BEFORE.test(lead)) {
      const above = LIST_TYPES.has(parsed.type) ? continuation({ ...parsed, n: parsed.n - 1 }) : "";
      const inserted = `${above}\n`;
      this.replace(start, start, inserted, from + inserted.length);
      return;
    }

    const next = parsed.type === "divider" ? "" : continuation(parsed);
    this.replace(from, to, `\n${next}`);
  }

  /** Enter inside code, on a fence, in a table or on an image. True if handled. */
  enterStructural({ from, to }) {
    const { index, start, line, ctx, parsed } = this.lineAt(from);
    const end = start + line.length;

    if (parsed.type === "code") {
      // A new line of code keeps the indentation of the one above.
      const indentation = /^[ \t]*/.exec(line.slice(0, from - start))[0];
      this.replace(from, to, `\n${indentation}`);
      return true;
    }
    if (parsed.type === "fence" && !parsed.closing && from === to) {
      const closed = this.contexts.slice(index + 1).some((c) => c?.closing);
      // Typing ``` and Enter opens a block and closes it below the caret.
      this.replace(end, end, closed ? "\n" : "\n\n```", end + 1);
      return true;
    }
    if ((parsed.type === "table" || parsed.type === "table-rule") && from === to) {
      // Enter in a table adds a row below and moves into its first cell.
      const cols = ctx?.cols || Math.max(1, tableCells(line).length);
      const row = `|${"  |".repeat(cols)}`;
      this.replace(end, end, `\n${row}`, end + 2);
      return true;
    }
    if (ATOMIC_TYPES.has(parsed.type) || parsed.type === "fence") {
      this.replace(end, end, "\n", end + 1);
      return true;
    }
    return false;
  }

  /** Tab between table cells; past the last cell, a new row. */
  moveCell(back) {
    const selection = this.selection();
    if (!selection) return;
    const { index, start, line, ctx } = this.lineAt(selection.from);
    const relative = selection.from - start;
    const cells = tableCells(line);
    let current = cells.findIndex(([from, to]) => relative >= from && relative <= to);
    if (current < 0) current = back ? cells.length : -1;
    const target = current + (back ? -1 : 1);

    const selectCell = (lineStart, [from, to], text) => {
      const cell = text.slice(from, to);
      const lead = cell.length - cell.trimStart().length;
      const content = cell.trim();
      const at = lineStart + from + Math.min(lead, Math.max(1, cell.length - 1));
      this.select(at, at + content.length);
    };

    if (target >= 0 && target < cells.length) {
      selectCell(start, cells[target], line);
      return;
    }
    // Off the end of the row: the neighbouring row, skipping a |---| rule.
    let next = index + (back ? -1 : 1);
    if (next >= 0 && next < this.lines.length && parseLine(this.lines[next]).type === "table-rule") {
      next += back ? -1 : 1;
    }
    if (next >= 0 && next < this.lines.length && parseLine(this.lines[next]).type === "table") {
      const rowCells = tableCells(this.lines[next]);
      selectCell(this.starts[next], rowCells[back ? rowCells.length - 1 : 0], this.lines[next]);
      return;
    }
    if (!back && this.host.canEdit()) {
      const end = start + line.length;
      const cols = ctx?.cols || cells.length;
      this.replace(end, end, `\n|${"  |".repeat(cols)}`, end + 2);
    }
  }

  remove(event, { from, to }) {
    const backward = event.inputType.includes("Backward");
    const text = this.host.text();
    if (from !== to) {
      this.replace(from, to, "");
      return;
    }
    const { index, start, line, parsed } = this.lineAt(from);
    const relative = from - start;
    const prefixLength = parsed.type === "callout" ? parsed.prefix.length + parsed.iconText.length : parsed.prefix.length;

    if (parsed.type === "code" || parsed.type === "table") {
      // Code is literal: no hidden markup to step over. A table keeps its
      // pipes -- deleting one would merge two cells.
      const at = backward ? from - 1 : from;
      if (at < 0 || at >= text.length) return;
      if (parsed.type === "table" && (text[at] === "|" || text[at] === "\n")) return;
      this.replace(at, at + 1, "", at);
      return;
    }

    if (backward && (relative <= prefixLength || MARKERS_BEFORE.test(line.slice(prefixLength, relative)))) {
      if (ATOMIC_TYPES.has(parsed.type)) {
        this.replace(start, Math.min(text.length, start + line.length + 1), "", start);
      } else if (parsed.prefix) {
        // Backspace steps a nested item out a level, otherwise turns the
        // block back into text.
        if (LIST_TYPES.has(parsed.type) && parsed.indent > 0) {
          this.replace(start, start + 2, "", Math.max(start, from - 2));
        } else {
          this.replace(start, start + prefixLength, "", start);
        }
      } else if (index > 0) {
        const previousStart = this.starts[index - 1];
        const previous = parseLine(this.lines[index - 1]);
        if (ATOMIC_TYPES.has(previous.type)) {
          this.replace(previousStart, start, "", previousStart);
        } else {
          this.replace(start - 1, start, "", start - 1);
        }
      }
      return;
    }

    if (!backward && MARKERS_AFTER.test(line.slice(relative))) {
      if (index < this.lines.length - 1) {
        const end = start + line.length;
        const nextPrefix = parseLine(this.lines[index + 1]);
        const nextLength =
          nextPrefix.type === "callout"
            ? nextPrefix.prefix.length + nextPrefix.iconText.length
            : ATOMIC_TYPES.has(nextPrefix.type)
              ? this.lines[index + 1].length
              : nextPrefix.prefix.length;
        this.replace(end, end + 1 + nextLength, "", end);
      }
      return;
    }

    const target = this.targetRange(event);
    if (target && target.from !== target.to && !text.slice(target.from, target.to).includes("\n")) {
      this.replace(target.from, target.to, "", target.from);
      return;
    }
    if (backward) {
      if (from === 0) return;
      const width = /[\uDC00-\uDFFF]/.test(text[from - 1]) && from > 1 ? 2 : 1;
      this.replace(from - width, from, "", from - width);
    } else {
      if (from >= text.length) return;
      const width = /[\uD800-\uDBFF]/.test(text[from]) ? 2 : 1;
      this.replace(from, from + width, "", from);
    }
  }

  onPaste(event) {
    event.preventDefault();
    if (!this.host.canEdit()) return;
    const selection = this.selection();
    const images = imageFiles(event.clipboardData);
    if (images.length && this.host.uploadImages) {
      this.host.uploadImages(images);
      return;
    }
    const data = event.clipboardData?.getData("text/plain");
    if (selection && data) this.insertText(selection.from, selection.to, data);
  }

  onDrop(event) {
    event.preventDefault();
    const images = imageFiles(event.dataTransfer);
    if (!images.length || !this.host.canEdit() || !this.host.uploadImages) return;
    // Put the caret where the files were dropped, so they land there.
    const range = document.caretRangeFromPoint?.(event.clientX, event.clientY);
    const offset = range ? this.offsetOf(range.startContainer, range.startOffset) : null;
    this.focus(offset ?? undefined);
    this.host.uploadImages(images);
  }

  /**
   * Put an image on its own line at the caret: in place of an empty line,
   * otherwise below the caret's line. Called once an upload has finished.
   */
  insertImage(url, alt = "") {
    if (!this.host.canEdit()) return;
    const cleanAlt = alt.replace(/[[\]\n]/g, "").slice(0, 120);
    const markup = `![${cleanAlt}](${url})`;
    const text = this.host.text();
    const selection = this.selection() ?? { from: text.length, to: text.length };
    const { start, line } = this.lineAt(selection.from);
    const end = start + line.length;
    if (!line.trim()) {
      this.replace(start, end, `${markup}\n`, start + markup.length + 1);
    } else {
      this.replace(end, end, `\n${markup}\n`, end + markup.length + 2);
    }
  }

  /** Insert inline text (a page link, say) at the caret. */
  insertAtCaret(insert) {
    if (!this.host.canEdit()) return;
    const text = this.host.text();
    const selection = this.selection() ?? { from: text.length, to: text.length };
    this.replace(selection.from, selection.to, insert);
  }

  onCopy(event, cut) {
    const selection = this.selection();
    if (!selection || selection.from === selection.to) return;
    event.preventDefault();
    // The clipboard gets the markup, so a paste elsewhere in the app keeps
    // its formatting, and a paste into a plain editor reads as Markdown.
    event.clipboardData?.setData("text/plain", this.host.text().slice(selection.from, selection.to));
    if (cut && this.host.canEdit()) this.replace(selection.from, selection.to, "", selection.from);
  }

  onMouseDown(event) {
    if (event.target.closest?.(".todo-box, .comment-badge")) event.preventDefault();
  }

  onClick(event) {
    const box = event.target.closest?.(".todo-box");
    if (box) {
      event.preventDefault();
      const lineEl = box.closest(".line");
      const index = Array.prototype.indexOf.call(this.root.children, lineEl);
      this.toggleTodo(index);
      return;
    }
    const badge = event.target.closest?.(".comment-badge");
    if (badge) {
      const lineEl = badge.closest(".line");
      this.host.openComments?.(Array.prototype.indexOf.call(this.root.children, lineEl));
      return;
    }
    const tocItem = event.target.closest?.("[data-toc-line]");
    if (tocItem) {
      event.preventDefault();
      this.host.jumpToLine?.(Number(tocItem.dataset.tocLine));
      return;
    }
    const pageLink = event.target.closest?.(".page-link");
    if (pageLink) {
      event.preventDefault();
      this.host.openPage?.(pageLink.dataset.page);
      return;
    }
    const link = event.target.closest?.("a[href]");
    if (link && (event.ctrlKey || event.metaKey || !this.host.canEdit())) {
      event.preventDefault();
      window.open(link.href, "_blank", "noopener");
    }
  }

  toggleTodo(index) {
    if (!this.host.canEdit() || index < 0) return;
    const line = this.lines[index];
    const parsed = parseLine(line);
    if (parsed.type !== "todo") return;
    const at = this.starts[index] + line.indexOf("[") + 1;
    const selection = this.hasFocus() ? this.selection() : null;
    const text = this.host.text();
    const operation = new TextOperation()
      .retain(at)
      .delete(1)
      .insert(parsed.checked ? " " : "x")
      .retain(text.length - at - 1);
    this.host.commit(operation);
    if (selection) this.select(selection.from, selection.to);
  }

  // -- block-level commands ------------------------------------------------

  /** Line indexes covered by the selection (or the caret's line). */
  selectedLines(selection = this.selection()) {
    if (!selection) return [];
    const first = this.lineIndexAt(selection.from);
    let last = this.lineIndexAt(selection.to);
    // A selection that ends at the very start of a line does not include it.
    if (last > first && selection.to === this.starts[last]) last -= 1;
    const indexes = [];
    for (let i = first; i <= last; i += 1) indexes.push(i);
    return indexes;
  }

  /** Rewrite some lines; the caret follows its own line. */
  mapLines(indexes, transform, selection = this.selection()) {
    if (!indexes.length) return;
    const lines = [...this.lines];
    for (const i of indexes) lines[i] = transform(lines[i], parseLine(lines[i]), i);
    const next = lines.join("\n");
    const operation = TextOperation.fromDiff(this.host.text(), next);
    if (operation.isNoop()) return;
    const from = selection ? operation.transformCursor(selection.from) : undefined;
    const to = selection ? operation.transformCursor(selection.to) : undefined;
    this.host.commit(operation);
    if (selection) this.select(from, to);
  }

  setType(type, indexes = this.selectedLines()) {
    if (!this.host.canEdit() || !indexes.length) return;
    const all = indexes.every((i) => parseLine(this.lines[i]).type === type);
    const target = all ? "paragraph" : type;
    const selection = this.selection();
    this.mapLines(
      indexes,
      (line, parsed) => {
        if (ATOMIC_TYPES.has(parsed.type) || STRUCTURAL_TYPES.has(parsed.type)) return line;
        const body = bodyOf(parsed);
        return prefixFor(target, parsed) + body;
      },
      selection
    );
    // A block converted from nothing leaves the caret at its start; move it past the prefix.
    if (selection && selection.from === selection.to) {
      const { start, parsed, line } = this.lineAt(this.selection()?.from ?? selection.from);
      const bodyStart = start + parsed.prefix.length + (parsed.iconText?.length || 0);
      const current = this.selection()?.from ?? 0;
      if (current < bodyStart) this.select(Math.min(bodyStart, start + line.length));
    }
  }

  indent(outdent) {
    if (!this.host.canEdit()) return;
    this.mapLines(this.selectedLines(), (line, parsed) => {
      if (ATOMIC_TYPES.has(parsed.type) || STRUCTURAL_TYPES.has(parsed.type) || /^h\d$/.test(parsed.type)) return line;
      if (outdent) return line.startsWith("  ") ? line.slice(2) : line.replace(/^ /, "");
      if (!LIST_TYPES.has(parsed.type) && parsed.type !== "paragraph") return line;
      return parsed.indent >= 4 ? line : `  ${line}`;
    });
  }

  moveLines(indexes, targetIndex) {
    if (!this.host.canEdit() || !indexes.length) return;
    const first = indexes[0];
    const last = indexes[indexes.length - 1];
    if (targetIndex >= first && targetIndex <= last + 1) return;
    const lines = [...this.lines];
    const moving = lines.splice(first, indexes.length);
    const at = targetIndex > last ? targetIndex - indexes.length : targetIndex;
    lines.splice(at, 0, ...moving);
    const selection = this.selection();
    const relative = selection ? selection.from - this.starts[first] : null;
    const next = lines.join("\n");
    const operation = TextOperation.fromDiff(this.host.text(), next);
    this.host.commit(operation);
    let start = 0;
    for (let i = 0; i < at; i += 1) start += lines[i].length + 1;
    if (relative !== null && relative >= 0) this.select(start + Math.max(0, relative));
  }

  moveBy(direction) {
    const indexes = this.selectedLines();
    if (!indexes.length) return;
    const target = direction < 0 ? indexes[0] - 1 : indexes[indexes.length - 1] + 2;
    if (target < 0 || target > this.lines.length) return;
    this.moveLines(indexes, target);
  }

  duplicate(indexes = this.selectedLines()) {
    if (!this.host.canEdit() || !indexes.length) return;
    const last = indexes[indexes.length - 1];
    const copy = indexes.map((i) => this.lines[i]).join("\n");
    const end = this.starts[last] + this.lines[last].length;
    this.replace(end, end, `\n${copy}`, end + 1 + copy.length);
  }

  deleteLines(indexes) {
    if (!this.host.canEdit() || !indexes.length) return;
    const lines = this.lines.filter((_, i) => !indexes.includes(i));
    const next = (lines.length ? lines : [""]).join("\n");
    const start = this.starts[Math.min(indexes[0], this.starts.length - 1)];
    this.rewrite(next, this.hasFocus() ? Math.min(start, next.length) : undefined);
  }

  insertBlockAfter(index, type = "paragraph") {
    if (!this.host.canEdit()) return;
    const end = this.starts[index] + this.lines[index].length;
    const prefix = type === "divider" ? "---" : prefixFor(type, parseLine(this.lines[index])) || "";
    this.focus();
    this.replace(end, end, `\n${prefix}`);
  }

  // -- inline formatting ---------------------------------------------------

  static MARKS = { bold: "**", italic: "*", underline: "__", strike: "~~", code: "`" };

  toggleInline(kind) {
    if (!this.host.canEdit()) return;
    const mark = BlockEditor.MARKS[kind];
    let selection = this.selection();
    if (!mark || !selection) return;
    const text = this.host.text();
    let { from, to } = selection;

    if (from === to) {
      // No selection: act on the word around the caret.
      const { start, line } = this.lineAt(from);
      const relative = from - start;
      const left = /[\p{L}\p{N}_'-]*$/u.exec(line.slice(0, relative))[0].length;
      const right = /^[\p{L}\p{N}_'-]*/u.exec(line.slice(relative))[0].length;
      if (!left && !right) return;
      from -= left;
      to += right;
    }

    const before = text.slice(from - mark.length, from);
    const after = text.slice(to, to + mark.length);
    const doubled = mark === "*" && (text[from - 2] === "*" || text[to + 1] === "*");
    if (before === mark && after === mark && !doubled) {
      const operation = new TextOperation()
        .retain(from - mark.length)
        .delete(mark.length)
        .retain(to - from)
        .delete(mark.length)
        .retain(text.length - to - mark.length);
      this.host.commit(operation);
      this.select(from - mark.length, to - mark.length);
      return;
    }

    // Wrap each line's selected content separately; markup cannot span lines,
    // and leading or trailing spaces must stay outside the markers.
    const pieces = text.slice(from, to).split("\n");
    let lineStart = from;
    const wrapped = pieces.map((piece, k) => {
      let prefixLength = 0;
      if (k > 0) {
        const parsed = parseLine(this.lines[this.lineIndexAt(lineStart)] ?? "");
        prefixLength = Math.min(piece.length, parsed.prefix.length);
      }
      lineStart += piece.length + 1;
      const head = piece.slice(0, prefixLength);
      const body = piece.slice(prefixLength);
      const match = /^(\s*)(.*?)(\s*)$/s.exec(body);
      if (!match[2]) return piece;
      return `${head}${match[1]}${mark}${match[2]}${mark}${match[3]}`;
    });
    const insert = wrapped.join("\n");
    const leadingSpace = /^\s*/.exec(pieces[0])[0].length;
    const trailingSpace = /\s*$/.exec(pieces[pieces.length - 1])[0].length;
    this.replace(
      from,
      to,
      insert,
      from + leadingSpace + mark.length,
      from + insert.length - trailingSpace - mark.length
    );
  }

  /** The link around the selection, as buffer offsets, if there is one. */
  linkAt(selection = this.selection()) {
    if (!selection) return null;
    const { start, line } = this.lineAt(selection.from);
    const pattern = /\[([^\]\n]+)\]\(([^)\s]+)\)/g;
    for (let match = pattern.exec(line); match; match = pattern.exec(line)) {
      const from = start + match.index;
      const to = from + match[0].length;
      if (selection.from >= from && selection.to <= to) {
        return { from, to, text: match[1], url: match[2] };
      }
    }
    return null;
  }

  setLink(url, selection = this.selection()) {
    if (!this.host.canEdit() || !selection) return;
    const existing = this.linkAt(selection);
    if (existing) {
      const replacement = url ? `[${existing.text}](${url})` : existing.text;
      this.replace(existing.from, existing.to, replacement, existing.from + replacement.length);
      return;
    }
    if (!url || selection.from === selection.to) return;
    const label = this.host.text().slice(selection.from, selection.to);
    if (label.includes("\n")) return;
    const replacement = `[${label}](${url})`;
    this.replace(selection.from, selection.to, replacement, selection.from + replacement.length);
  }

  // -- keyboard ------------------------------------------------------------

  onKeyDown(event) {
    if (this.slash && this.onSlashKey(event)) return;
    const mod = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();

    if (event.key === "Tab" && this.host.canEdit()) {
      event.preventDefault();
      const selection = this.selection();
      const type = selection ? this.lineAt(selection.from).parsed.type : null;
      if (type === "table" || type === "table-rule") {
        this.moveCell(event.shiftKey);
      } else if (type === "code" && !event.shiftKey && selection.from === selection.to) {
        this.replace(selection.from, selection.to, "  ");
      } else if (type === "code") {
        this.mapLines(this.selectedLines(selection), (line) =>
          event.shiftKey ? line.replace(/^ {1,2}/, "") : `  ${line}`
        );
      } else {
        this.indent(event.shiftKey);
      }
      return;
    }
    if (!mod) return;

    const commands = {
      b: () => this.toggleInline("bold"),
      i: () => this.toggleInline("italic"),
      u: () => this.toggleInline("underline"),
      e: () => this.toggleInline("code"),
      z: () => (event.shiftKey ? this.host.redo() : this.host.undo()),
      y: () => this.host.redo(),
      s: () => (event.shiftKey ? this.toggleInline("strike") : this.host.save()),
      d: () => this.duplicate(),
    };
    if (event.shiftKey && (event.key === "ArrowUp" || event.key === "ArrowDown")) {
      event.preventDefault();
      this.moveBy(event.key === "ArrowUp" ? -1 : 1);
      return;
    }
    // Shift and Alt change event.key, so number shortcuts read the key code.
    const digit = /^Digit(\d)$/.exec(event.code)?.[1];
    if (event.altKey && digit !== undefined && digit <= "3") {
      event.preventDefault();
      this.setType(["paragraph", "h1", "h2", "h3"][Number(digit)]);
      return;
    }
    if (event.shiftKey && digit !== undefined && digit >= "5") {
      const type = { 5: "todo", 6: "bullet", 7: "number", 8: "quote", 9: "callout" }[digit];
      event.preventDefault();
      this.setType(type);
      return;
    }
    if (key === "k") {
      const selection = this.selection();
      if (selection && (selection.from !== selection.to || this.linkAt(selection))) {
        event.preventDefault();
        event.stopPropagation();
        this.host.requestLink?.();
      }
      return;
    }
    const command = commands[key];
    if (command && !(key === "i" && event.shiftKey)) {
      event.preventDefault();
      command();
    }
  }

  // -- slash and mention menus ---------------------------------------------

  /**
   * Open the menu for the trigger character at `at`: "/" lists blocks,
   * "@" lists the people on the page. Both filter on what is typed after it.
   */
  openSlash(at, kind = "slash") {
    this.slash = { at, kind };
    this.slashIndex = 0;
    this.updateSlash();
  }

  closeSlash() {
    this.slash = null;
    this.host.slashMenu?.(null);
  }

  slashQuery() {
    if (!this.slash) return null;
    const text = this.host.text();
    const selection = this.selection();
    const { at, kind } = this.slash;
    const trigger = kind === "mention" ? "@" : "/";
    if (!selection || selection.from !== selection.to || text[at] !== trigger) return null;
    if (selection.from <= at || this.lineIndexAt(selection.from) !== this.lineIndexAt(at)) return null;
    const query = text.slice(at + 1, selection.from);
    const allowed = kind === "mention" ? /^[\p{L}\p{N} .'-]{0,30}$/u : /^[\p{L}\p{N} ]{0,24}$/u;
    if (!allowed.test(query) || /\s{2}/.test(query)) return null;
    return query;
  }

  slashItems(query) {
    const words = query.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const match = (haystack) => words.every((word) => haystack.toLowerCase().includes(word));
    if (this.slash?.kind === "mention") {
      return (this.host.people?.() || [])
        .filter((person) => match(`${person.name} ${person.email}`))
        .slice(0, 8)
        .map((person) => ({
          id: person.id,
          label: person.name,
          hint: person.email,
          icon: initialsOf(person.name),
          color: person.presence_color,
          person,
        }));
    }
    return SLASH_ITEMS.filter((item) => match(`${item.label} ${item.keys}`));
  }

  /** Re-evaluate the menu after the caret or text moved. */
  updateSlash() {
    if (!this.slash) return;
    const query = this.slashQuery();
    if (query === null) {
      this.closeSlash();
      return;
    }
    const items = this.slashItems(query);
    if (!items.length && query.length > 2) {
      this.closeSlash();
      return;
    }
    this.slashIndex = Math.min(this.slashIndex, Math.max(0, items.length - 1));
    this.host.slashMenu?.({
      kind: this.slash.kind,
      items,
      index: this.slashIndex,
      rect: this.caretRect(this.slash.at),
      choose: (item) => this.chooseSlash(item),
    });
  }

  onSlashKey(event) {
    const query = this.slashQuery();
    if (query === null) {
      this.closeSlash();
      return false;
    }
    const items = this.slashItems(query);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      this.slashIndex = (this.slashIndex + step + items.length) % Math.max(1, items.length);
      this.updateSlash();
      return true;
    }
    if ((event.key === "Enter" || event.key === "Tab") && items.length) {
      event.preventDefault();
      this.chooseSlash(items[this.slashIndex]);
      return true;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      this.closeSlash();
      return true;
    }
    return false;
  }

  chooseSlash(item) {
    const query = this.slashQuery() ?? "";
    const at = this.slash?.at;
    const kind = this.slash?.kind;
    this.closeSlash();
    if (at === undefined) return;
    this.focus();

    if (kind === "mention") {
      // "@que" becomes "@[Name](user:id) ", and the person is told.
      const name = item.label.replace(/[[\]()\n]/g, "").trim() || "someone";
      const markup = `@[${name}](user:${item.id}) `;
      this.replace(at, at + 1 + query.length, markup);
      const { line } = this.lineAt(at);
      this.host.onMention?.(item.person, line);
      return;
    }

    // Remove the "/query" first; everything else is relative to that.
    this.replace(at, at + 1 + query.length, "", at);
    const { index, start, line, parsed } = this.lineAt(at);
    const body = bodyOf(parsed).trim();
    const end = start + line.length;
    // Multi-line blocks replace an empty line, or go below one with text.
    const place = (block, caret) => {
      if (!body && !parsed.prefix) this.replace(start, end, block, start + caret);
      else this.replace(end, end, `\n${block}`, end + 1 + caret);
    };

    if (item.id.startsWith("ai-")) {
      this.host.assist?.(item.id.slice(3));
      return;
    }
    if (item.id === "toc") {
      if (!body && !parsed.prefix) this.replace(start, end, `${TOC_MARK}\n`, start + TOC_MARK.length + 1);
      else this.replace(end, end, `\n${TOC_MARK}\n`, end + TOC_MARK.length + 2);
      return;
    }
    if (item.id === "due") {
      const week = new Date(Date.now() + 7 * 86400000);
      const due = `📅 ${todayKey(week)}`;
      this.replace(at, at, due, at + 2, at + due.length);
      return;
    }
    if (item.id === "date") {
      const today = new Date().toLocaleDateString([], { year: "numeric", month: "long", day: "numeric" });
      this.replace(at, at, today);
      return;
    }
    if (item.id === "divider") {
      if (!body && !parsed.prefix) this.replace(start, end, "---\n", start + 4);
      else this.replace(end, end, "\n---\n", end + 5);
      return;
    }
    if (item.id === "code") {
      place("```\n\n```", 4);
      return;
    }
    if (item.id === "table") {
      const skeleton = "| Column 1 | Column 2 | Column 3 |\n|---|---|---|\n|  |  |  |\n|  |  |  |";
      place(skeleton, 2);
      const caret = this.selection()?.from;
      if (caret !== undefined) this.select(caret, caret + "Column 1".length);
      return;
    }
    if (item.id === "image") {
      this.host.pickImage?.();
      return;
    }
    if (item.id === "page") {
      this.host.createSubpage?.();
      return;
    }
    if (item.id === "mention") {
      this.replace(at, at, "@");
      this.openSlash(at, "mention");
      return;
    }
    if (body) {
      // The command was typed after some text: the new block goes below.
      const prefix = prefixFor(item.id, parsed) ?? "";
      this.replace(end, end, `\n${prefix}`);
      return;
    }
    this.setType(item.id, [index]);
    const fresh = this.lineAt(start);
    this.select(start + fresh.line.length);
  }
}

function initialsOf(name) {
  return (name || "?")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("");
}
