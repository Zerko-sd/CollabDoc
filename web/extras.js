/**
 * Workspace extras: page templates, the icon picker's emoji set, export, time
 * formatting and the activity chart. Nothing here touches application state;
 * the client passes in whatever each piece needs.
 */
import { lineContexts, parseLine, renderDocument } from "./editor.js";

// --------------------------------------------------------------------------
// templates
// --------------------------------------------------------------------------

const today = () =>
  new Date().toLocaleDateString([], { year: "numeric", month: "long", day: "numeric" });

export const TEMPLATES = [
  {
    id: "blank",
    icon: "📄",
    title: "Untitled",
    description: "Start from an empty page.",
    content: () => "",
  },
  {
    id: "meeting",
    icon: "🗓️",
    title: "Meeting notes",
    description: "Agenda, notes, decisions and follow-ups.",
    content: () =>
      [
        `> 📅 ${today()} · Attendees: `,
        "## Agenda",
        "1. ",
        "## Notes",
        "",
        "## Decisions",
        "- ",
        "## Action items",
        "- [ ] ",
      ].join("\n"),
  },
  {
    id: "project",
    icon: "🚀",
    title: "Project plan",
    description: "Goals, scope, milestones and risks.",
    content: () =>
      [
        "> 🎯 One sentence on what this project changes, and for whom.",
        "## Goals",
        "- ",
        "## Out of scope",
        "- ",
        "## Milestones",
        "- [ ] **Kick-off** — ",
        "- [ ] **First release** — ",
        "- [ ] **Retrospective** — ",
        "## Risks",
        "- **Risk:** … — **Mitigation:** …",
        "## Open questions",
        "- ",
      ].join("\n"),
  },
  {
    id: "weekly",
    icon: "📈",
    title: "Weekly update",
    description: "What shipped, what's next, what's blocked.",
    content: () =>
      [
        `> 🗞️ Week of ${today()}`,
        "## Shipped",
        "- ",
        "## In progress",
        "- ",
        "## Next week",
        "- [ ] ",
        "## Blockers",
        "- ",
      ].join("\n"),
  },
  {
    id: "research",
    icon: "🔬",
    title: "Research brief",
    description: "Question, method, findings and next steps.",
    content: () =>
      [
        "## Question",
        "What are we trying to learn, and why now?",
        "## Method",
        "",
        "## Findings",
        "> 💡 Lead with the single most important finding.",
        "1. ",
        "## Next steps",
        "- [ ] ",
      ].join("\n"),
  },
  {
    id: "todo",
    icon: "✅",
    title: "To-do list",
    description: "A simple checklist.",
    content: () => ["## Today", "- [ ] ", "## Later", "- [ ] "].join("\n"),
  },
];

// --------------------------------------------------------------------------
// icons
// --------------------------------------------------------------------------

export const EMOJIS = [
  "📄", "📝", "📒", "📓", "📚", "🗂️", "📁", "📌", "📍", "🔖",
  "🚀", "🎯", "💡", "🧠", "🔬", "🧪", "📈", "📊", "📉", "🗓️",
  "✅", "☑️", "⚡", "🔥", "⭐", "🌟", "✨", "🎉", "🏆", "🎨",
  "🛠️", "⚙️", "🧩", "🔒", "🔑", "🧭", "🗺️", "🌍", "🏠", "🏢",
  "💼", "💰", "🧾", "📦", "🛒", "📣", "💬", "📮", "✉️", "📞",
  "👋", "🤝", "👥", "🙌", "❤️", "🌱", "🌸", "☕", "🍕", "🎵",
  "🐛", "🧯", "🚧", "🧱", "💻", "🖥️", "📱", "🤖", "🛰️", "🧬",
];

export const randomIcon = () => EMOJIS[Math.floor(Math.random() * 30)];

// --------------------------------------------------------------------------
// time
// --------------------------------------------------------------------------

/** The server stores UTC but SQLite drops the zone, so a bare timestamp is UTC. */
export function parseTime(timestamp) {
  if (timestamp instanceof Date) return timestamp;
  const hasZone = /(Z|[+-]\d\d:?\d\d)$/.test(timestamp);
  return new Date(hasZone ? timestamp : `${timestamp}Z`);
}

export function formatWhen(timestamp) {
  const date = parseTime(timestamp);
  const seconds = (Date.now() - date.getTime()) / 1000;
  if (seconds < 45) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  if (seconds < 7 * 86400) return `${Math.round(seconds / 86400)}d ago`;
  return date.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** "45 sec", "12 min", "1 h 5 min" -- a span of active editing. */
export function formatDuration(seconds) {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} sec`;
  const minutes = Math.round(total / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

// --------------------------------------------------------------------------
// activity feed
// --------------------------------------------------------------------------

const AUDIT_PHRASES = {
  created: (d) => (d ? `created the page “${d}”` : "created the page"),
  renamed: (d) => `renamed the page (${d})`,
  moved: (d) => `moved the page ${d}`,
  trashed: (d) => (d ? `moved the page to the trash ${d}` : "moved the page to the trash"),
  restored: () => "restored the page from the trash",
  reverted: (d) => `restored an earlier version (${d})`,
  invited: (d) => `shared the page with ${d}`,
  role_changed: (d) => `changed access: ${d}`,
  removed: (d) => `removed ${d}`,
  link_sharing: (d) => `set link sharing: ${d}`,
  joined_by_link: (d) => `joined through the link as ${d}`,
  published: () => "published the page to the web",
  unpublished: () => "stopped publishing the page",
};

/** One sentence (without the actor) for an audit-log entry. */
export function auditPhrase(action, detail) {
  const phrase = AUDIT_PHRASES[action];
  return phrase ? phrase(detail || "") : `${action.replace(/_/g, " ")}${detail ? ` (${detail})` : ""}`;
}

/**
 * Editing sessions and audit-log entries, merged newest first.
 *
 * Each item is {at, user, color, text, kind}: a session reads "edited for
 * 4 min · +312 −20", an audit entry reads like "shared the page with …".
 */
export function activityFeed(sessions, audit = [], limit = 25) {
  const items = [
    ...sessions.map((session) => {
      const seconds = (parseTime(session.ended_at) - parseTime(session.started_at)) / 1000;
      const change = [
        session.inserted ? `+${session.inserted.toLocaleString()}` : "",
        session.deleted ? `−${session.deleted.toLocaleString()}` : "",
      ].filter(Boolean).join(" ");
      return {
        kind: "edit",
        at: parseTime(session.ended_at),
        user: { id: session.user_id, name: session.name, presence_color: session.presence_color },
        text: seconds < 60 ? `made a quick edit${change ? ` · ${change}` : ""}`
          : `edited for ${formatDuration(seconds)}${change ? ` · ${change}` : ""}`,
      };
    }),
    ...audit.map((entry) => ({
      kind: "audit",
      at: parseTime(entry.created_at),
      user: entry.actor || { name: "Someone", presence_color: null },
      text: auditPhrase(entry.action, entry.detail),
    })),
  ];
  return items.sort((a, b) => b.at - a.at).slice(0, limit);
}

// --------------------------------------------------------------------------
// page statistics
// --------------------------------------------------------------------------

/** The prose of a buffer with markup stripped, for counting and export. */
export function plainText(text) {
  return text
    .split("\n")
    .map((line) => {
      const parsed = parseLine(line);
      if (parsed.type === "divider") return "";
      const body = parsed.type === "callout" ? parsed.content.slice(parsed.iconText.length) : parsed.content;
      return body
        .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
        .replace(/(\*\*|__|~~|`|\*)/g, "");
    })
    .join("\n");
}

export function pageStats(text) {
  const plain = plainText(text);
  const words = (plain.match(/[\p{L}\p{N}][\p{L}\p{N}'’-]*/gu) || []).length;
  const lines = text.split("\n");
  const todos = lines.map(parseLine).filter((p) => p.type === "todo");
  return {
    words,
    characters: plain.replace(/\n/g, "").length,
    minutes: Math.max(1, Math.round(words / 230)),
    blocks: lines.filter((line) => line.trim()).length,
    todos: todos.length,
    done: todos.filter((p) => p.checked).length,
  };
}

/** Headings in order, with the offset each starts at. */
export function outline(text) {
  const items = [];
  let offset = 0;
  for (const line of text.split("\n")) {
    const parsed = parseLine(line);
    if (/^h[1-3]$/.test(parsed.type) && parsed.content.trim()) {
      items.push({
        level: Number(parsed.type[1]),
        text: plainText(line).trim(),
        offset: offset + line.length,
      });
    }
    offset += line.length + 1;
  }
  return items;
}

// --------------------------------------------------------------------------
// export
// --------------------------------------------------------------------------

export function download(filename, type, body) {
  const blob = new Blob([body], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export const fileName = (title, extension) =>
  `${(title || "Untitled").replace(/[\\/:*?"<>|]+/g, "").trim() || "Untitled"}.${extension}`;

export function toMarkdown(title, icon, text) {
  // Each block becomes its own Markdown paragraph -- except the lines of a
  // code block or a table, which have to stay together to mean anything.
  const lines = text.split("\n");
  const contexts = lineContexts(lines);
  const joined = lines
    .map((line, i) => {
      if (i === lines.length - 1) return line;
      const here = contexts[i];
      const next = contexts[i + 1];
      const together = here?.code || here?.opening || next?.code || next?.closing || (here?.cols && next?.cols);
      return line + (together ? "\n" : "\n\n");
    })
    .join("");
  return `# ${icon ? `${icon} ` : ""}${title}\n\n${joined.replace(/\n{3,}/g, "\n\n")}\n`;
}

/**
 * Line diff between two versions of a page, as [{type, text}] with type
 * "same", "add" or "del" -- the longest common subsequence, which is what
 * makes moved paragraphs read as one deletion and one insertion.
 */
export function diffLines(before, after) {
  const a = before.split("\n");
  const b = after.split("\n");
  // Trim the shared head and tail first: most versions differ in a few lines.
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) {
    tail += 1;
  }
  const midA = a.slice(head, a.length - tail);
  const midB = b.slice(head, b.length - tail);
  const out = a.slice(0, head).map((text) => ({ type: "same", text }));

  if (midA.length * midB.length > 4_000_000) {
    // Too large for the table: show it as a replacement rather than hang.
    out.push(...midA.map((text) => ({ type: "del", text })), ...midB.map((text) => ({ type: "add", text })));
  } else {
    const rows = midA.length + 1;
    const cols = midB.length + 1;
    const table = new Uint32Array(rows * cols);
    for (let i = midA.length - 1; i >= 0; i -= 1) {
      for (let j = midB.length - 1; j >= 0; j -= 1) {
        table[i * cols + j] =
          midA[i] === midB[j]
            ? table[(i + 1) * cols + j + 1] + 1
            : Math.max(table[(i + 1) * cols + j], table[i * cols + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < midA.length || j < midB.length) {
      if (i < midA.length && j < midB.length && midA[i] === midB[j]) {
        out.push({ type: "same", text: midA[i] });
        i += 1;
        j += 1;
      } else if (i < midA.length && (j === midB.length || table[(i + 1) * cols + j] >= table[i * cols + j + 1])) {
        // On a tie the old line goes first, so a rewrite reads "-old +new".
        out.push({ type: "del", text: midA[i] });
        i += 1;
      } else {
        out.push({ type: "add", text: midB[j] });
        j += 1;
      }
    }
  }
  out.push(...a.slice(a.length - tail).map((text) => ({ type: "same", text })));
  return out;
}

const EXPORT_CSS = `
  body { font: 16px/1.6 ui-sans-serif, -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
         color: #37352f; max-width: 720px; margin: 64px auto; padding: 0 24px; }
  h1.title { font-size: 40px; margin: 0 0 24px; }
  .line { margin: 2px 0; padding-left: calc(var(--indent, 0) * 24px); }
  .line h1, .line h2, .line h3 { margin: 24px 0 4px; }
  .line[data-type="bullet"] .c::before { content: "•"; margin-right: 8px; }
  .line[data-type="number"] .c::before { content: attr(data-num); margin-right: 8px; }
  .line[data-type="quote"] { border-left: 3px solid currentColor; padding-left: 14px; }
  .line[data-type="callout"] { background: #f1f1ef; border-radius: 6px; padding: 12px 16px; }
  .line.done .c { text-decoration: line-through; color: #9b9a97; }
  .check { margin-right: 8px; }
  code { background: rgba(135,131,120,.15); color: #eb5757; border-radius: 4px; padding: .2em .4em; font-size: 85%; }
  hr { border: 0; border-top: 1px solid #e9e9e7; }
  .line[data-type="fence"], .line[data-type="code"] { background: #f7f6f3; margin: 0; padding: 0 16px; }
  .line[data-type="fence"] { color: #9b9a97; font-size: 12px; padding-top: 8px; border-radius: 6px 6px 0 0; }
  .line[data-type="fence"]:has(+ :not([data-type="code"])) { border-radius: 0 0 6px 6px; padding: 0 0 8px; }
  .line[data-type="code"] code { display: block; background: none; color: inherit; padding: 0; white-space: pre-wrap;
         font: 14px/1.6 ui-monospace, SFMono-Regular, Consolas, monospace; }
  .tok-keyword { color: #d73a49; } .tok-string { color: #22863a; } .tok-number { color: #005cc5; } .tok-comment { color: #9b9a97; }
  .line[data-type="table"] .c { display: grid; grid-template-columns: repeat(var(--cols), minmax(0, 1fr)); }
  .line[data-type="table"] .cell { border: 1px solid #e9e9e7; padding: 6px 10px; margin: -1px 0 0 -1px; }
  .line.table-head .cell { background: #f7f6f3; font-weight: 600; }
  .line[data-type="table-rule"] { display: none; }
  figure { margin: 12px 0; } figure img { max-width: 100%; border-radius: 4px; }
  .mention, .page-link { color: #2383e2; font-weight: 500; text-decoration: none; }
`;

/**
 * The page as a standalone HTML file. `images` maps each /api/files URL to
 * a data: URL, since the exported file has no session to fetch them with.
 */
export function toHtml(title, icon, text, images = new Map()) {
  const holder = document.createElement("div");
  holder.append(...renderDocument(text, { skipBlank: true }));
  holder.querySelectorAll(".line[data-type='image']").forEach((line) => {
    const img = line.querySelector("img");
    const source = parseLine(line.textContent).src;
    if (img && images.has(source)) img.setAttribute("src", images.get(source));
    else line.remove();
  });
  holder.querySelectorAll(".mk:not(.icon)").forEach((node) => node.remove());
  holder.querySelectorAll("[contenteditable]").forEach((node) => node.removeAttribute("contenteditable"));
  holder.querySelectorAll(".todo-box").forEach((box) => {
    box.setAttribute("disabled", "");
    if (box.checked) box.setAttribute("checked", "");
  });
  holder.querySelectorAll(".line[data-type='number']").forEach((line) => {
    line.querySelector(".c")?.setAttribute("data-num", line.dataset.num);
  });
  const heading = document.createElement("h1");
  heading.className = "title";
  heading.textContent = `${icon ? `${icon} ` : ""}${title}`;
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${heading.textContent.replace(/</g, "&lt;")}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>${EXPORT_CSS}</style></head>
<body>${heading.outerHTML}${holder.innerHTML}</body></html>`;
}

// --------------------------------------------------------------------------
// activity chart
// --------------------------------------------------------------------------

const SVG = "http://www.w3.org/2000/svg";

function svg(tag, attributes = {}) {
  const node = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  return node;
}

function bucketLabel(seconds) {
  if (seconds < 3600) return `${seconds / 60} min`;
  if (seconds < 86400) return seconds === 3600 ? "hour" : `${seconds / 3600} h`;
  return seconds === 86400 ? "day" : "week";
}

function tickTime(date, seconds) {
  return seconds < 86400
    ? date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

/**
 * Stacked columns of characters changed per time bucket, one colour per author.
 * (Characters, not operations: the log has a row per keystroke, which would
 * make a one-letter fix look like real work.)
 *
 * Authors keep the presence colour they have everywhere else in the app, so a
 * person is the same colour in the chart, the carets and the tints. The legend
 * and the per-author table carry identity for readers who cannot rely on hue.
 */
export function renderActivityChart(container, tooltip, activity) {
  container.replaceChildren();
  const buckets = activity.buckets;
  if (!buckets.length) {
    const empty = document.createElement("p");
    empty.className = "panel-hint";
    empty.textContent = "No edits yet.";
    container.append(empty);
    return;
  }
  const authors = activity.authors;
  const width = Math.max(220, container.clientWidth || 288);
  const height = 132;
  const pad = { top: 8, right: 4, bottom: 22, left: 30 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const totals = buckets.map((bucket) => Object.values(bucket.counts).reduce((a, b) => a + b, 0));
  const rawMax = Math.max(...totals, 1);
  const magnitude = 10 ** Math.floor(Math.log10(rawMax));
  const max = Math.ceil(rawMax / magnitude) * magnitude;
  const slot = plotWidth / buckets.length;
  const barWidth = Math.max(2, Math.min(24, slot * 0.72));
  const y = (value) => pad.top + plotHeight - (value / max) * plotHeight;

  const chart = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width: "100%",
    height,
    role: "img",
    "aria-label": `Characters changed per ${bucketLabel(activity.bucket_seconds)}, by author`,
    class: "activity-chart",
  });

  for (const value of [0, max / 2, max]) {
    chart.append(
      svg("line", {
        x1: pad.left,
        x2: width - pad.right,
        y1: y(value),
        y2: y(value),
        class: value === 0 ? "axis" : "grid",
      })
    );
    const label = svg("text", { x: pad.left - 6, y: y(value) + 3.5, class: "tick", "text-anchor": "end" });
    label.textContent = Math.round(value).toLocaleString();
    chart.append(label);
  }

  buckets.forEach((bucket, index) => {
    const x = pad.left + index * slot + (slot - barWidth) / 2;
    let base = 0;
    const segments = authors.filter((author) => bucket.counts[author.user_id]);
    segments.forEach((author, k) => {
      const value = bucket.counts[author.user_id];
      const top = y(base + value);
      const bottom = y(base);
      // A 2px surface gap separates stacked segments.
      const segmentHeight = Math.max(1, bottom - top - (k > 0 ? 2 : 0));
      const isTop = k === segments.length - 1;
      const radius = isTop ? Math.min(4, barWidth / 2, segmentHeight) : 0;
      const segmentBottom = bottom - (k > 0 ? 2 : 0);
      const segmentTop = segmentBottom - segmentHeight;
      const path = svg("path", {
        d: [
          `M${x},${segmentBottom}`,
          `V${segmentTop + radius}`,
          `Q${x},${segmentTop} ${x + radius},${segmentTop}`,
          `H${x + barWidth - radius}`,
          `Q${x + barWidth},${segmentTop} ${x + barWidth},${segmentTop + radius}`,
          `V${segmentBottom}Z`,
        ].join(" "),
        fill: author.presence_color,
        class: "segment",
      });
      chart.append(path);
      base += value;
    });

    // Hit target: the whole slot, taller and wider than the mark.
    const hit = svg("rect", {
      x: pad.left + index * slot,
      y: pad.top,
      width: slot,
      height: plotHeight,
      class: "hit",
    });
    const start = new Date(bucket.start);
    hit.addEventListener("pointerenter", () => {
      hit.classList.add("on");
      tooltip.replaceChildren();
      const head = document.createElement("div");
      head.className = "tip-head";
      head.textContent = `${start.toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })} · ${totals[index].toLocaleString()} character${totals[index] === 1 ? "" : "s"} changed`;
      tooltip.append(head);
      for (const author of segments.slice().reverse()) {
        const row = document.createElement("div");
        row.className = "tip-row";
        const key = document.createElement("i");
        key.style.background = author.presence_color;
        const name = document.createElement("span");
        name.textContent = author.name;
        const count = document.createElement("b");
        count.textContent = bucket.counts[author.user_id].toLocaleString();
        row.append(key, name, count);
        tooltip.append(row);
      }
      tooltip.hidden = false;
      const box = container.getBoundingClientRect();
      const left = ((pad.left + (index + 0.5) * slot) / width) * box.width;
      tooltip.style.left = `${Math.min(Math.max(left, 70), box.width - 70)}px`;
      tooltip.style.top = `${Math.max(0, (y(totals[index]) / height) * box.height - 8)}px`;
    });
    hit.addEventListener("pointerleave", () => {
      hit.classList.remove("on");
      tooltip.hidden = true;
    });
    chart.append(hit);
  });

  const first = new Date(buckets[0].start);
  const last = new Date(buckets[buckets.length - 1].start);
  for (const [date, anchor, x] of [
    [first, "start", pad.left],
    [last, "end", width - pad.right],
  ]) {
    const label = svg("text", { x, y: height - 6, class: "tick", "text-anchor": anchor });
    label.textContent = tickTime(date, activity.bucket_seconds);
    chart.append(label);
  }
  container.append(chart);
}

export { bucketLabel };

// --------------------------------------------------------------------------
// comment re-anchoring
// --------------------------------------------------------------------------

function trigrams(text) {
  const padded = `  ${text.toLowerCase().replace(/\s+/g, " ")} `;
  const grams = new Map();
  for (let i = 0; i < padded.length - 2; i += 1) {
    const gram = padded.slice(i, i + 3);
    grams.set(gram, (grams.get(gram) || 0) + 1);
  }
  return grams;
}

/** Dice similarity of two strings' character trigrams, 0..1. */
export function similarity(a, b) {
  const x = trigrams(a);
  const y = trigrams(b);
  let shared = 0;
  let total = 0;
  for (const [gram, count] of x) {
    shared += Math.min(count, y.get(gram) || 0);
    total += count;
  }
  for (const count of y.values()) total += count;
  return total ? (2 * shared) / total : 0;
}

/**
 * The line a comment's quote now lives on.
 *
 * An exact match wins. Otherwise the quote was edited: every window of the
 * quote's length on every line is scored by trigram similarity and the best
 * one at or above `threshold` is taken, ties going to the line nearest the
 * thread's last known position. -1 when nothing is close enough.
 */
export function locateQuote(lines, quote, hint = -1, threshold = 0.6) {
  const exact = lines.findIndex((line) => line.includes(quote));
  if (exact >= 0) return { index: exact, exact: true, score: 1 };
  let best = { index: -1, exact: false, score: 0 };
  const size = quote.length;
  lines.forEach((line, index) => {
    if (!line.trim()) return;
    const step = Math.max(1, Math.floor(size / 4));
    for (let start = 0; start === 0 || start + size <= line.length + step; start += step) {
      const window = line.slice(start, start + Math.max(size, 1) + Math.floor(size / 4));
      const score = similarity(quote, window);
      const closer = best.index < 0 || hint < 0 || Math.abs(index - hint) < Math.abs(best.index - hint);
      if (score > best.score + 1e-9 || (Math.abs(score - best.score) < 1e-9 && closer)) {
        best = { index, exact: false, score };
      }
      if (start + size >= line.length) break;
    }
  });
  return best.score >= threshold ? best : { index: -1, exact: false, score: best.score };
}

// --------------------------------------------------------------------------
// find and replace
// --------------------------------------------------------------------------

/** Every non-overlapping occurrence of `query` in `text`, as {from, to}. */
export function findMatches(text, query, { caseSensitive = false } = {}) {
  if (!query) return [];
  const haystack = caseSensitive ? text : text.toLowerCase();
  const needle = caseSensitive ? query : query.toLowerCase();
  const found = [];
  let at = haystack.indexOf(needle);
  while (at >= 0) {
    found.push({ from: at, to: at + needle.length });
    at = haystack.indexOf(needle, at + needle.length);
  }
  return found;
}

/** `text` with every match replaced -- what Replace all commits as one edit. */
export function replaceMatches(text, matches, replacement) {
  let out = "";
  let at = 0;
  for (const { from, to } of matches) {
    out += text.slice(at, from) + replacement;
    at = to;
  }
  return out + text.slice(at);
}

// --------------------------------------------------------------------------
// the page graph
// --------------------------------------------------------------------------

/**
 * Lay out a graph with a small force simulation: every pair of nodes repels
 * (inverse square), every edge is a spring, and a weak pull keeps the whole
 * thing centred. Deterministic -- nodes start on a circle in input order --
 * so the same graph always settles into the same picture.
 *
 * Returns a Map of node id -> {x, y} inside a width x height box.
 */
export function forceLayout(nodes, edges, { width = 800, height = 600, ticks = 300 } = {}) {
  const positions = new Map();
  const n = nodes.length;
  nodes.forEach((node, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, n);
    const radius = Math.min(width, height) * 0.35;
    positions.set(node.id, { x: width / 2 + radius * Math.cos(angle), y: height / 2 + radius * Math.sin(angle), vx: 0, vy: 0 });
  });
  const ideal = Math.max(60, Math.sqrt((width * height) / Math.max(1, n)) * 0.6);
  const links = edges.filter((edge) => positions.has(edge.source) && positions.has(edge.target));
  for (let tick = 0; tick < ticks; tick += 1) {
    const cooling = 1 - tick / ticks;
    const list = [...positions.values()];
    for (let a = 0; a < list.length; a += 1) {
      for (let b = a + 1; b < list.length; b += 1) {
        const p = list[a];
        const q = list[b];
        let dx = p.x - q.x;
        let dy = p.y - q.y;
        const distance = Math.max(1, Math.hypot(dx, dy));
        const force = (ideal * ideal) / distance / distance;
        dx /= distance;
        dy /= distance;
        p.vx += dx * force;
        p.vy += dy * force;
        q.vx -= dx * force;
        q.vy -= dy * force;
      }
    }
    for (const edge of links) {
      const p = positions.get(edge.source);
      const q = positions.get(edge.target);
      const dx = q.x - p.x;
      const dy = q.y - p.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const force = (distance - ideal) * 0.02;
      p.vx += (dx / distance) * force;
      p.vy += (dy / distance) * force;
      q.vx -= (dx / distance) * force;
      q.vy -= (dy / distance) * force;
    }
    for (const p of positions.values()) {
      p.vx += (width / 2 - p.x) * 0.005;
      p.vy += (height / 2 - p.y) * 0.005;
      const speed = Math.hypot(p.vx, p.vy);
      const limit = 12 * cooling + 0.5;
      if (speed > limit) {
        p.vx = (p.vx / speed) * limit;
        p.vy = (p.vy / speed) * limit;
      }
      p.x = Math.min(width - 30, Math.max(30, p.x + p.vx));
      p.y = Math.min(height - 30, Math.max(30, p.y + p.vy));
      p.vx *= 0.6;
      p.vy *= 0.6;
    }
  }
  return new Map([...positions].map(([id, p]) => [id, { x: p.x, y: p.y }]));
}

/**
 * The same forces as ``forceLayout``, run a frame at a time so the graph can
 * move: dragging a page pins it under the pointer and reheats the system, and
 * its neighbours follow on their springs until everything settles again.
 */
export class GraphSimulation {
  constructor(nodes, edges, { width = 800, height = 600, positions = new Map() } = {}) {
    this.width = width;
    this.height = height;
    this.points = new Map();
    const n = nodes.length;
    nodes.forEach((node, i) => {
      const known = positions.get(node.id);
      // A page new to this view starts beside one it is linked to, if any.
      const anchor = known || this.#neighbourOf(node.id, edges, positions);
      const angle = (2 * Math.PI * i) / Math.max(1, n);
      this.points.set(node.id, {
        x: anchor ? anchor.x + (known ? 0 : 20 * Math.cos(angle)) : width / 2 + 40 * Math.cos(angle),
        y: anchor ? anchor.y + (known ? 0 : 20 * Math.sin(angle)) : height / 2 + 40 * Math.sin(angle),
        vx: 0,
        vy: 0,
        fx: null,
        fy: null,
      });
    });
    this.links = edges.filter((edge) => this.points.has(edge.source) && this.points.has(edge.target));
    this.ideal = Math.max(60, Math.sqrt((width * height) / Math.max(1, n)) * 0.6);
    this.alpha = 1;
  }

  #neighbourOf(id, edges, positions) {
    for (const edge of edges) {
      const other = edge.source === id ? edge.target : edge.target === id ? edge.source : null;
      if (other && positions.has(other)) return positions.get(other);
    }
    return null;
  }

  get settled() {
    return this.alpha < 0.005;
  }

  reheat(alpha = 0.3) {
    this.alpha = Math.max(this.alpha, alpha);
  }

  pin(id, x, y) {
    const p = this.points.get(id);
    if (!p) return;
    p.fx = x;
    p.fy = y;
  }

  unpin(id) {
    const p = this.points.get(id);
    if (p) p.fx = p.fy = null;
  }

  tick() {
    const list = [...this.points.values()];
    const ideal = this.ideal;
    for (let a = 0; a < list.length; a += 1) {
      for (let b = a + 1; b < list.length; b += 1) {
        const p = list[a];
        const q = list[b];
        let dx = p.x - q.x;
        let dy = p.y - q.y;
        if (!dx && !dy) dx = 0.5; // two pages on one spot still push apart
        const distance = Math.max(1, Math.hypot(dx, dy));
        const force = (ideal * ideal) / distance / distance;
        dx /= distance;
        dy /= distance;
        p.vx += dx * force;
        p.vy += dy * force;
        q.vx -= dx * force;
        q.vy -= dy * force;
      }
    }
    for (const edge of this.links) {
      const p = this.points.get(edge.source);
      const q = this.points.get(edge.target);
      const dx = q.x - p.x;
      const dy = q.y - p.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const force = (distance - ideal) * 0.02;
      p.vx += (dx / distance) * force;
      p.vy += (dy / distance) * force;
      q.vx -= (dx / distance) * force;
      q.vy -= (dy / distance) * force;
    }
    const limit = 12 * this.alpha + 0.2;
    for (const p of list) {
      if (p.fx !== null) {
        p.x = p.fx;
        p.y = p.fy;
        p.vx = p.vy = 0;
        continue;
      }
      p.vx += (this.width / 2 - p.x) * 0.005;
      p.vy += (this.height / 2 - p.y) * 0.005;
      const speed = Math.hypot(p.vx, p.vy);
      if (speed > limit) {
        p.vx = (p.vx / speed) * limit;
        p.vy = (p.vy / speed) * limit;
      }
      p.x += p.vx;
      p.y += p.vy;
      p.vx *= 0.6;
      p.vy *= 0.6;
    }
    this.alpha *= 0.985;
  }
}

/** Pages within ``depth`` steps of ``start``, following edges both ways. */
export function neighbourhood(edges, start, depth = 1) {
  const around = new Map();
  for (const edge of edges) {
    if (!around.has(edge.source)) around.set(edge.source, new Set());
    if (!around.has(edge.target)) around.set(edge.target, new Set());
    around.get(edge.source).add(edge.target);
    around.get(edge.target).add(edge.source);
  }
  const seen = new Set([start]);
  let frontier = [start];
  for (let step = 0; step < depth; step += 1) {
    const next = [];
    for (const id of frontier) {
      for (const other of around.get(id) || []) {
        if (!seen.has(other)) {
          seen.add(other);
          next.push(other);
        }
      }
    }
    frontier = next;
  }
  return seen;
}

// --------------------------------------------------------------------------
// sign-in redirects
// --------------------------------------------------------------------------

/** Parameters a sign-in redirect left in the URL fragment (#oauth_token=...). */
export function hashParams(hash) {
  const params = new URLSearchParams((hash || "").replace(/^#/, ""));
  const wanted = ["oauth_token", "oauth_error", "mfa_token"];
  const found = {};
  for (const key of wanted) if (params.has(key)) found[key] = params.get(key);
  return found;
}

// --------------------------------------------------------------------------
// integrity report
// --------------------------------------------------------------------------

const percent = (fraction) => `${Math.round((fraction || 0) * 100)}%`;
const SEVERITY_LABEL = { high: "High", medium: "Review", low: "Note" };

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function minutesLabel(minutes) {
  if (!minutes || minutes < 1) return "< 1 min";
  if (minutes < 60) return `${Math.round(minutes)} min`;
  return `${Math.floor(minutes / 60)} h ${Math.round(minutes % 60)} min`;
}

/**
 * Draw an integrity report (GET /api/documents/:id/integrity) into
 * ``container``. ``onJump(flag)`` is called when a flag's "Show on page" is
 * pressed; without it the buttons are left out, as in the downloaded copy.
 */
export function renderIntegrity(container, report, { onJump = null } = {}) {
  const people = new Map(report.contributors.map((person) => [person.user_id, person]));
  const nameOf = (id) => people.get(id)?.name || "another collaborator";
  const high = report.flags.filter((flag) => flag.severity === "high").length;

  const tiles = node("div", "integrity-tiles");
  const tile = (label, value, hint, tone = "") => {
    const box = node("div", `integrity-tile ${tone}`.trim());
    box.append(node("div", "value", value), node("div", "label", label), node("div", "hint", hint));
    tiles.append(box);
  };
  tile("Characters on the page", report.characters.toLocaleString(), `version ${report.version}`);
  tile(
    "Typed, not pasted",
    percent(report.keystroke_share),
    "of all text ever inserted",
    report.keystroke_share < 0.6 ? "warn" : ""
  );
  tile(
    "Style matches the typist",
    report.judged_blocks ? percent(report.agreement_rate) : "—",
    report.judged_blocks
      ? `${report.judged_blocks} block${report.judged_blocks === 1 ? "" : "s"}, leave-one-out`
      : "needs two authors with enough writing"
  );
  tile(
    "Flags",
    String(report.flags.length),
    report.flags.length ? `${high} high` : "nothing to review",
    high ? "bad" : report.flags.length ? "warn" : "good"
  );

  const shares = node("section", "integrity-section");
  shares.append(node("h4", "", "Who wrote the final text"));
  const bar = node("div", "share-bar integrity-bar");
  for (const person of report.contributors) {
    if (!person.share) continue;
    const slice = node("span");
    slice.style.width = percent(person.share);
    slice.style.background = person.color;
    slice.title = `${person.name}: ${percent(person.share)}`;
    bar.append(slice);
  }
  shares.append(bar);

  const table = node("table", "integrity-table");
  const head = table.createTHead().insertRow();
  for (const label of ["Person", "Final text", "Typed", "Pasted", "Deleted", "Time active", "Sessions"]) {
    head.append(node("th", "", label));
  }
  const body = table.createTBody();
  for (const person of report.contributors) {
    const row = body.insertRow();
    const who = row.insertCell();
    const dot = node("i", "dot");
    dot.style.background = person.color;
    who.append(dot, document.createTextNode(person.name));
    row.insertCell().textContent = percent(person.share);
    row.insertCell().textContent = person.typed.toLocaleString();
    const pasted = row.insertCell();
    pasted.textContent = person.pasted.toLocaleString();
    if (person.pasted) pasted.className = "warn";
    row.insertCell().textContent = person.deleted.toLocaleString();
    row.insertCell().textContent = minutesLabel(person.active_minutes);
    row.insertCell().textContent = String(person.sessions);
  }
  shares.append(table);

  const flags = node("section", "integrity-section");
  flags.append(node("h4", "", "What to look at"));
  if (!report.flags.length) {
    flags.append(
      node(
        "p",
        "integrity-clear",
        "Nothing to flag: the text was typed here, and each passage reads like the person who typed it."
      )
    );
  }
  const list = node("ul", "integrity-flags");
  for (const flag of report.flags) {
    const item = node("li", `integrity-flag sev-${flag.severity}`);
    const top = node("div", "flag-top");
    top.append(node("span", "flag-badge", SEVERITY_LABEL[flag.severity] || flag.severity));
    top.append(node("strong", "", flag.title));
    item.append(top);
    item.append(
      node(
        "div",
        "flag-who",
        flag.kind === "style_mismatch"
          ? `Typed by ${nameOf(flag.user_id)} · reads like ${nameOf(flag.other_user_id)}`
          : `Pasted by ${nameOf(flag.user_id)}${flag.other_user_id ? ` · reads like ${nameOf(flag.other_user_id)}` : ""}${
              flag.timestamp ? ` · ${formatWhen(parseTime(flag.timestamp))}` : ""
            }`
      )
    );
    item.append(node("blockquote", "flag-quote", flag.snippet));
    item.append(node("p", "flag-detail", flag.detail));
    if (onJump && flag.start !== null && flag.start !== undefined) {
      const jump = node("button", "small", "Show on page");
      jump.type = "button";
      jump.addEventListener("click", () => onJump(flag));
      item.append(jump);
    }
    list.append(item);
  }
  flags.append(list);

  const method = node("section", "integrity-section integrity-method");
  method.append(node("h4", "", "How this was worked out"));
  method.append(
    node(
      "p",
      "",
      "Shares come from the keystroke ledger, which records who typed every character still on the page. " +
        "Typing and pasting come from the operation log: an insert of 120 characters or more in one " +
        "operation was not typed. Style is checked leave-one-out -- each block is judged by a " +
        "stylometric model retrained without it, so a copied passage cannot vouch for itself."
    ),
    node(
      "p",
      "",
      "These are signals for a reviewer, not verdicts. People quote sources, paste their own notes and " +
        "change register; read a flagged passage before drawing a conclusion."
    )
  );

  container.replaceChildren(tiles, shares, flags, method);
}

const INTEGRITY_CSS = `
body{font:14px/1.5 -apple-system,"Segoe UI",Helvetica,Arial,sans-serif;color:#37352f;max-width:860px;margin:40px auto;padding:0 24px}
h1{font-size:26px;margin:0 0 4px}h4{font-size:15px;margin:28px 0 10px}.sub{color:#787774;margin:0 0 24px}
.integrity-tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.integrity-tile{border:1px solid #e9e9e7;border-radius:8px;padding:10px 12px}
.integrity-tile .value{font-size:22px;font-weight:600}.integrity-tile .label{font-size:12px}.integrity-tile .hint{font-size:11px;color:#9b9a97}
.integrity-tile.good .value{color:#0f7b6c}.integrity-tile.warn .value{color:#c27c0e}.integrity-tile.bad .value{color:#e03e3e}
.share-bar{display:flex;gap:2px;height:10px;border-radius:5px;overflow:hidden;margin:6px 0 12px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #e9e9e7}
th{font-weight:500;color:#787774}td.warn{color:#c27c0e;font-weight:600}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px}
ul{list-style:none;padding:0}.integrity-flag{border:1px solid #e9e9e7;border-left-width:4px;border-radius:6px;padding:10px 14px;margin:0 0 10px;break-inside:avoid}
.sev-high{border-left-color:#e03e3e}.sev-medium{border-left-color:#d9a40e}.sev-low{border-left-color:#9b9a97}
.flag-badge{font-size:11px;font-weight:600;text-transform:uppercase;margin-right:8px;color:#787774}
.flag-who{font-size:12.5px;color:#787774}.flag-quote{margin:8px 0;padding-left:10px;border-left:3px solid #e9e9e7;font-style:italic}
.flag-detail{font-size:12.5px;margin:0}.integrity-method p{font-size:12.5px;color:#787774}
@media print{body{margin:0}}`;

/** The report as a standalone, printable HTML file. */
export function integrityHtml(report) {
  const holder = document.createElement("div");
  renderIntegrity(holder, report);
  const heading = node("h1", "", `Integrity report: ${report.title}`);
  const sub = node(
    "p",
    "sub",
    `Generated ${new Date(report.generated_at).toLocaleString()} · version ${report.version}`
  );
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>${heading.textContent.replace(/</g, "&lt;")}</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>${INTEGRITY_CSS}</style></head>
<body>${heading.outerHTML}${sub.outerHTML}${holder.innerHTML}</body></html>`;
}
