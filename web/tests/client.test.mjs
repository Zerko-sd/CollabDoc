/**
 * Browser client smoke tests (WBS 9.2).
 *
 * Loads the real index.html and app.js into jsdom with the network stubbed,
 * then drives the client the way a user and a server would: sign in, type,
 * receive a remote edit, receive an attribution update. What is being checked
 * is the wiring the Python suite cannot see -- that a keystroke becomes an
 * operation on the wire, that a remote operation lands in the textarea with
 * the caret intact, and that attribution reaches the rendered document.
 *
 *     node --test web/tests/client.test.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { JSDOM } from "jsdom";

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = resolve(HERE, "..");

/** A WebSocket stand-in that records what the client sent. */
class FakeSocket {
  static last = null;

  constructor(url) {
    this.url = url;
    this.readyState = 1; // OPEN
    this.sent = [];
    FakeSocket.last = this;
    queueMicrotask(() => this.onopen?.());
  }

  send(raw) {
    this.sent.push(JSON.parse(raw));
  }

  close() {
    this.readyState = 3;
  }

  /** Deliver a server frame to the client. */
  deliver(message) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  messagesOfType(type) {
    return this.sent.filter((message) => message.type === type);
  }
}

const USER = {
  id: "user-alice",
  email: "alice@example.com",
  name: "Alice Chen",
  presence_color: "#2563eb",
  avatar_url: null,
};

const DOCUMENT = {
  id: "doc-1",
  title: "Demo document",
  current_version: 0,
  owner_id: USER.id,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  role: "OWNER",
  collaborator_count: 1,
  content: "",
  share_token: "tok",
  share_role: null,
};

/** Routes the handful of endpoints the client touches on start-up. */
function fakeFetch(calls) {
  return async (path, options = {}) => {
    calls.push({ path, method: options.method || "GET" });
    const json = (body, status = 200) =>
      Promise.resolve({
        ok: status < 400,
        status,
        statusText: "",
        json: async () => body,
      });

    if (path === "/api/auth/login" || path === "/api/auth/register") {
      return json({ access_token: "test-token", token_type: "bearer", user: USER });
    }
    if (path === "/api/auth/me") return json(USER);
    if (path === "/api/documents" && (options.method || "GET") === "GET") {
      return json([DOCUMENT]);
    }
    if (path === "/api/documents" && options.method === "POST") return json(DOCUMENT, 201);
    if (path.startsWith("/api/documents/doc-1/versions")) return json([]);
    if (path.startsWith("/api/documents/doc-1/collaborators")) {
      return json([{ user: USER, role: "OWNER" }]);
    }
    if (path.startsWith("/api/documents/doc-1/share")) {
      return json({ share_token: null, share_role: null, url: null });
    }
    if (path === "/api/documents/doc-1") return json(DOCUMENT);
    return json({ detail: "not stubbed: " + path }, 404);
  };
}

async function boot() {
  const html = readFileSync(resolve(WEB, "index.html"), "utf8");
  const dom = new JSDOM(html, {
    url: "http://localhost/",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const calls = [];

  window.fetch = fakeFetch(calls);
  window.WebSocket = FakeSocket;
  window.WebSocket.OPEN = 1;

  // app.js reads these as bare globals. Some of them (navigator) are
  // getter-only on globalThis in current Node, so define rather than assign.
  const expose = (name, value) => {
    if (value === undefined) return;
    Object.defineProperty(globalThis, name, {
      value,
      configurable: true,
      writable: true,
    });
  };
  for (const name of [
    "window", "document", "localStorage", "location", "history",
    "navigator", "URLSearchParams", "Event",
  ]) {
    expose(name, window[name]);
  }
  expose("fetch", window.fetch);
  expose("WebSocket", FakeSocket);

  // A fresh module registry per test, so module-level state does not leak.
  const appUrl = `${pathToFileURL(resolve(WEB, "app.js")).href}?t=${Math.random()}`;
  await import(appUrl);
  return { window, document: window.document, calls };
}

async function signIn(window, document) {
  document.getElementById("auth-email").value = USER.email;
  document.getElementById("auth-password").value = "demo-password-1";
  document.getElementById("auth-form").dispatchEvent(
    new window.Event("submit", { bubbles: true, cancelable: true })
  );
  await settle();
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 30));

function type(window, document, text) {
  const source = document.getElementById("source");
  source.value = text;
  source.selectionStart = source.selectionEnd = text.length;
  source.dispatchEvent(new window.Event("input", { bubbles: true }));
}

test("signing in opens the workspace and lists documents", async () => {
  const { window, document } = await boot();
  assert.equal(document.getElementById("app").hidden, true);

  await signIn(window, document);

  assert.equal(document.getElementById("auth").hidden, true);
  assert.equal(document.getElementById("app").hidden, false);
  assert.equal(document.querySelectorAll("#doc-list li").length, 1);
  assert.match(document.querySelector("#doc-list .name").textContent, /Demo document/);
  assert.equal(document.getElementById("doc-title").value, "Demo document");
});

test("the socket carries the document token and the init frame is applied", async () => {
  const { window, document } = await boot();
  await signIn(window, document);

  assert.match(FakeSocket.last.url, /\/ws\/documents\/doc-1\?token=test-token$/);
  FakeSocket.last.deliver({
    type: "init",
    document_id: "doc-1",
    version: 3,
    content: "# Title\nA line of prose.\n",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    attribution_version: -1,
    blocks: [],
    model: {},
  });
  await settle();

  assert.equal(document.getElementById("source").value, "# Title\nA line of prose.\n");
  // The preview renders one block per non-blank line.
  assert.equal(document.querySelectorAll("#preview .block").length, 2);
  assert.equal(document.querySelector("#preview h1").textContent, "Title");
});

test("typing sends exactly the operation that describes the change", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 1, content: "hello",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  await settle();

  type(window, document, "hello world");
  const operations = FakeSocket.last.messagesOfType("op");
  assert.equal(operations.length, 1);
  assert.deepEqual(operations[0].operation, [5, " world"]);
  assert.equal(operations[0].version, 1);

  // Only one operation may be in flight; the next keystroke is buffered.
  type(window, document, "hello world!");
  assert.equal(FakeSocket.last.messagesOfType("op").length, 1);

  // Acknowledging the first releases the buffered one.
  FakeSocket.last.deliver({ type: "ack", version: 2, server_latency_ms: 1 });
  await settle();
  const after = FakeSocket.last.messagesOfType("op");
  assert.equal(after.length, 2);
  assert.deepEqual(after[1].operation, [11, "!"]);
  assert.equal(after[1].version, 2);
});

test("typing before the room's init is kept and sent once it arrives", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  assert.notEqual(document.getElementById("connection-text").textContent, "live");

  // The page opened with "" and the socket is open, but init is still on its way.
  type(window, document, "# Al");
  type(window, document, "# Alpha\n");
  assert.equal(FakeSocket.last.messagesOfType("op").length, 0, "nothing is sent before init");

  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 1, content: "",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  await settle();

  assert.equal(document.getElementById("connection-text").textContent, "live");
  assert.equal(document.getElementById("source").value, "# Alpha\n");
  const operations = FakeSocket.last.messagesOfType("op");
  assert.equal(operations.length, 1, "the early keystrokes travel as one operation");
  assert.deepEqual(operations[0].operation, ["# Alpha\n"]);
  assert.equal(operations[0].version, 1);
});

test("early typing is rebased when someone else edited before init", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  type(window, document, "mine");

  // Between opening the page and joining the room, Bob wrote a line.
  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 2, content: "Bob\n",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  await settle();

  const source = document.getElementById("source").value;
  assert.ok(source.includes("mine") && source.includes("Bob\n"), source);
  const [operation] = FakeSocket.last.messagesOfType("op");
  assert.equal(operation.version, 2);
});

test("a remote edit lands in the textarea and keeps the caret on its character", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 1, content: "hello world",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  await settle();

  const source = document.getElementById("source");
  source.selectionStart = source.selectionEnd = 11; // caret at the end
  FakeSocket.last.deliver({
    type: "op", version: 2, operation: ["Hi. ", 11], user_id: "user-bob",
  });
  await settle();

  assert.equal(source.value, "Hi. hello world");
  assert.equal(source.selectionStart, 15, "the caret should follow the text it sat after");
});

test("undo reverses a local edit through the same pipeline", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 1, content: "base",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  await settle();

  type(window, document, "base plus more");
  FakeSocket.last.deliver({ type: "ack", version: 2, server_latency_ms: 1 });
  await settle();

  document.getElementById("undo").click();
  await settle();

  assert.equal(document.getElementById("source").value, "base");
  const operations = FakeSocket.last.messagesOfType("op");
  assert.deepEqual(operations[operations.length - 1].operation, [4, -10]);
});

test("attribution tints each block with its author and lists it", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 2, content: "Alice wrote this.\nBob wrote that.\n",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  FakeSocket.last.deliver({
    type: "presence",
    users: [
      { connection_id: "c1", user_id: USER.id, name: "Alice Chen", presence_color: "#2563eb", role: "OWNER", cursor: 0, selection_end: 0 },
      { connection_id: "c2", user_id: "user-bob", name: "Bob Ortiz", presence_color: "#dc2626", role: "EDITOR", cursor: 0, selection_end: 0 },
    ],
  });
  FakeSocket.last.deliver({
    type: "attribution",
    attribution_version: 2,
    model: { trained: true, authors: [USER.id, "user-bob"], samples: 6, training_accuracy: 1.0, reason: "ok" },
    blocks: [
      { index: 0, block_type: "PARAGRAPH", content: "Alice wrote this.", start: 0, end: 17, sort_order: 1024, attributed_user_id: USER.id, confidence_score: 0.93, method: "hybrid", evidence: { agree: true } },
      { index: 1, block_type: "PARAGRAPH", content: "Bob wrote that.", start: 18, end: 33, sort_order: 2048, attributed_user_id: "user-bob", confidence_score: 0.88, method: "hybrid", evidence: { agree: true } },
    ],
  });
  await settle();

  const blocks = document.querySelectorAll("#preview .block");
  assert.equal(blocks[0].dataset.author, USER.id);
  assert.equal(blocks[0].style.getPropertyValue("--author-color"), "#2563eb");
  assert.equal(blocks[1].dataset.author, "user-bob");
  assert.equal(blocks[1].style.getPropertyValue("--author-color"), "#dc2626");
  assert.match(blocks[0].querySelector(".who").textContent, /Alice Chen · 93%/);

  assert.equal(document.querySelectorAll("#block-list li").length, 2);
  assert.match(document.getElementById("model-state").textContent, /2 authors, 6 samples/);

  // Two people in the room, two avatars.
  assert.equal(document.querySelectorAll("#presence .avatar").length, 2);
});

test("a viewer gets a read-only surface", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  // The client takes the role from the document detail; simulate a viewer by
  // replaying init with can_edit false after flipping the stubbed role.
  DOCUMENT.role = "VIEWER";
  document.querySelector("#doc-list li").click();
  await settle();

  assert.equal(document.getElementById("source").readOnly, true);
  assert.equal(document.querySelector('.toolbar button[data-format="bold"]').disabled, true);
  DOCUMENT.role = "OWNER";
});

test("markup is escaped so document text cannot inject HTML", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 1,
    content: "<img src=x onerror=alert(1)> and **bold**",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  await settle();

  const block = document.querySelector("#preview .block");
  assert.equal(block.querySelectorAll("img").length, 0);
  assert.match(block.innerHTML, /&lt;img/);
  assert.equal(block.querySelector("strong").textContent, "bold");
});

test("offsets travel as code points, so an emoji does not desynchronise the server", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  // "💡" is one code point but two UTF-16 units.
  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 1, content: "💡 hi",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  await settle();

  type(window, document, "💡 hi!");
  assert.deepEqual(FakeSocket.last.messagesOfType("op")[0].operation, [4, "!"]);

  // The server counts in code points too: retain 4 means after "💡 hi".
  FakeSocket.last.deliver({ type: "op", version: 2, operation: [4, "?"], user_id: "user-bob" });
  await settle();
  // Both inserted at the same place; the client's own text goes first.
  assert.equal(document.getElementById("source").value, "💡 hi!?");

  // The buffered edit is converted against the server's text once acknowledged.
  type(window, document, "💡 hi!?.");
  FakeSocket.last.deliver({ type: "ack", version: 3, server_latency_ms: 1 });
  await settle();
  const operations = FakeSocket.last.messagesOfType("op");
  assert.deepEqual(operations[operations.length - 1].operation, [6, "."]);
});

test("the page renders blocks with their markup hidden but present", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 1,
    content: "## Plan\n- [x] **ship** it\n> 💡 note\n---\n1. one\n7. two",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  await settle();

  const lines = [...document.querySelectorAll("#preview .line")];
  assert.deepEqual(
    lines.map((line) => line.dataset.type),
    ["h2", "todo", "callout", "divider", "number", "number"]
  );
  // Each block's source text is exactly its line, markup included.
  const source = (line) => {
    const walker = document.createTreeWalker(line, 4);
    let text = "";
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (!node.parentElement.closest("[data-nosrc]")) text += node.data;
    }
    return text;
  };
  assert.equal(source(lines[1]), "- [x] **ship** it");
  assert.equal(lines[0].querySelector("h2").textContent, "Plan");
  assert.equal(lines[1].querySelector(".todo-box").checked, true);
  assert.equal(lines[1].querySelector("strong").textContent, "ship");
  // Consecutive numbered items count from the first, whatever the source says.
  assert.deepEqual(lines.slice(4).map((line) => line.dataset.num), ["1.", "2."]);
});

test("typing into the page becomes an operation", async () => {
  const { window, document } = await boot();
  await signIn(window, document);
  FakeSocket.last.deliver({
    type: "init", document_id: "doc-1", version: 1, content: "# Title\n**bold** end",
    you: { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true },
    blocks: [], model: {},
  });
  await settle();

  const page = document.getElementById("preview");
  const text = page.querySelectorAll(".line")[1].querySelector("strong").firstChild;
  // A caret after "bo" inside the bold word is offset 8 + 2 + 2 in the buffer.
  window.getSelection().setBaseAndExtent(text, 2, text, 2);
  page.dispatchEvent(
    new window.InputEvent("beforeinput", { inputType: "insertText", data: "X", bubbles: true, cancelable: true })
  );
  await settle();

  assert.equal(document.getElementById("source").value, "# Title\n**boXld** end");
  assert.deepEqual(FakeSocket.last.messagesOfType("op")[0].operation, [12, "X", 8]);
  assert.equal(page.querySelector("strong").textContent, "boXld");
});

// --------------------------------------------------------------------------
// M7: code blocks, tables, images, mentions, page tree
// --------------------------------------------------------------------------

const ME = { user_id: USER.id, name: USER.name, presence_color: "#2563eb", role: "OWNER", connection_id: "c1", can_edit: true };
const FILE_ID = "0b6c8a9e-3f1d-4c2a-9e7b-5d4c3b2a1f00";
const BOB = { id: "8f5a1c2e-7d3b-4e6f-9a0b-1c2d3e4f5a6b", email: "bob@example.com", name: "Bob Ray", presence_color: "#16a34a" };

function sourceOf(document, line) {
  const walker = document.createTreeWalker(line, 4);
  let text = "";
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (!node.parentElement.closest("[data-nosrc]")) text += node.data;
  }
  return text;
}

/** Boot, optionally wrap fetch, sign in, and deliver a document. */
async function open(content, wrapFetch = null) {
  const booted = await boot();
  if (wrapFetch) {
    const stub = wrapFetch(globalThis.fetch, booted.calls);
    globalThis.fetch = stub;
    booted.window.fetch = stub;
  }
  await signIn(booted.window, booted.document);
  FakeSocket.last.deliver({ type: "init", document_id: "doc-1", version: 1, content, you: ME, blocks: [], model: {} });
  await settle();
  return { ...booted, page: booted.document.getElementById("preview") };
}

function press(window, page, inputType, data) {
  page.dispatchEvent(new window.InputEvent("beforeinput", { inputType, data, bubbles: true, cancelable: true }));
}

test("code blocks, tables, images and mentions render with their source intact", async () => {
  const content = [
    "```python",
    "# not a heading",
    "def f(): return 'x'",
    "```",
    "| Name | Role |",
    "|---|---|",
    "| **Ada** | Eng |",
    `![chart](/api/files/${FILE_ID})`,
    `Ping @[Bob Ray](user:${BOB.id}) about [Plans](page:doc-2)`,
  ].join("\n");
  const { document, page } = await open(content);
  const lines = [...page.querySelectorAll(".line")];
  assert.deepEqual(
    lines.map((line) => line.dataset.type),
    ["fence", "code", "code", "fence", "table", "table-rule", "table", "image", "paragraph"]
  );
  // Every line's source text is still exactly the buffer line.
  assert.deepEqual(lines.map((line) => sourceOf(document, line)), content.split("\n"));

  assert.ok(lines[2].querySelector(".tok-keyword"), "code is highlighted");
  assert.equal(lines[1].querySelector("h1"), null, "a # inside code is not a heading");
  assert.ok(lines[3].hasAttribute("data-closing"));
  assert.equal(lines[4].style.getPropertyValue("--cols"), "2");
  assert.ok(lines[4].classList.contains("table-head"));
  assert.equal(lines[6].querySelectorAll(".cell").length, 2);
  assert.equal(lines[6].querySelector(".cell strong").textContent, "Ada");
  assert.equal(lines[7].querySelector("img").alt, "chart");
  const chip = lines[8].querySelector(".mention").cloneNode(true);
  chip.querySelectorAll(".mk").forEach((node) => node.remove());
  assert.equal(chip.textContent, "@Bob Ray");
  assert.equal(lines[8].querySelector(".page-link").dataset.page, "doc-2");
});

test("Enter inside code keeps the indentation and stays in the block", async () => {
  const { window, document, page } = await open("```\n  x = 1\n```");
  const code = page.querySelectorAll(".line")[1].querySelector(".c");
  const walker = document.createTreeWalker(code, 4);
  let last = null;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) last = node;
  window.getSelection().setBaseAndExtent(last, last.length, last, last.length);
  press(window, page, "insertParagraph");
  await settle();
  assert.equal(document.getElementById("source").value, "```\n  x = 1\n  \n```");
});

test("typing a fence and Enter opens a closed code block", async () => {
  const { window, document, page } = await open("```js");
  const lang = page.querySelector(".line .c").firstChild;
  window.getSelection().setBaseAndExtent(lang, 2, lang, 2);
  press(window, page, "insertParagraph");
  await settle();
  assert.equal(document.getElementById("source").value, "```js\n\n```");
});

test("Tab moves between table cells and past the last one adds a row", async () => {
  const { window, document, page } = await open("| a | b |");
  const first = page.querySelector(".cell").firstChild;
  window.getSelection().setBaseAndExtent(first, 1, first, 1);
  const tab = () =>
    page.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
  tab();
  await settle();
  assert.equal(window.getSelection().toString(), "b");
  tab();
  await settle();
  assert.equal(document.getElementById("source").value, "| a | b |\n|  |  |");
});

test("Backspace in a table never deletes a cell border", async () => {
  const { window, document, page } = await open("| a | b |");
  const second = page.querySelectorAll(".cell")[1].firstChild;
  window.getSelection().setBaseAndExtent(second, 0, second, 0);
  press(window, page, "deleteContentBackward");
  await settle();
  assert.equal(document.getElementById("source").value, "| a | b |");
});

test("@ opens a people picker that inserts a mention and notifies", async () => {
  const { window, document, page, calls } = await open("Hi ", (original, calls) => async (path, options = {}) => {
    if (path.startsWith("/api/documents/doc-1/collaborators")) {
      return { ok: true, status: 200, json: async () => [{ user: USER, role: "OWNER" }, { user: BOB, role: "EDITOR" }] };
    }
    if (path === "/api/documents/doc-1/mentions") {
      calls.push({ path, method: options.method, body: JSON.parse(options.body) });
      return { ok: true, status: 201, json: async () => null };
    }
    return original(path, options);
  });

  const text = page.querySelector(".line .c").firstChild;
  window.getSelection().setBaseAndExtent(text, 3, text, 3);
  press(window, page, "insertText", "@");
  await settle();
  const caret = page.querySelector(".line .c").lastChild;
  window.getSelection().setBaseAndExtent(caret, caret.length, caret, caret.length);
  press(window, page, "insertText", "b");
  await settle();

  const menu = document.getElementById("slash-menu");
  assert.equal(menu.hidden, false);
  const option = [...menu.querySelectorAll(".slash-item")].find((item) => item.textContent.includes("Bob Ray"));
  assert.ok(option, "Bob is offered");
  option.click();
  await settle();

  assert.equal(document.getElementById("source").value, `Hi @[Bob Ray](user:${BOB.id}) `);
  const mention = calls.find((call) => call.path === "/api/documents/doc-1/mentions");
  assert.equal(mention.body.user_id, BOB.id);
});

test("the sidebar nests sub-pages and the page shows its breadcrumbs", async () => {
  const { window, document } = await boot();
  const original = globalThis.fetch;
  const parent = { ...DOCUMENT, id: "doc-1", title: "Handbook", parent_id: null };
  const child = { ...DOCUMENT, id: "doc-2", title: "Onboarding", parent_id: "doc-1" };
  const stub = async (path, options = {}) => {
    if (path === "/api/documents" && (options.method || "GET") === "GET") {
      return { ok: true, status: 200, json: async () => [parent, child] };
    }
    if (path === "/api/documents/doc-2") {
      return { ok: true, status: 200, json: async () => ({ ...child, content: "" }) };
    }
    if (path.startsWith("/api/documents/doc-2/")) return original(path.replace("doc-2", "doc-1"), options);
    return original(path, options);
  };
  globalThis.fetch = stub;
  window.fetch = stub;
  window.history.replaceState({}, "", "/?doc=doc-2");
  await signIn(window, document);

  const rows = [...document.querySelectorAll("#doc-list li")];
  assert.deepEqual(rows.map((row) => row.querySelector(".name").textContent), ["Handbook", "Onboarding"]);
  assert.equal(rows[1].style.getPropertyValue("--depth"), "1");
  assert.ok(rows[0].querySelector(".tree-caret.open"), "the open page's parent is expanded");
  assert.match(document.getElementById("crumb-parents").textContent, /Handbook/);
});

test("diffLines finds the changed lines and Markdown export keeps blocks together", async () => {
  await boot();
  const { diffLines, toMarkdown } = await import(`${pathToFileURL(resolve(WEB, "extras.js")).href}?t=${Math.random()}`);
  assert.deepEqual(
    diffLines("a\nb\nc", "a\nB\nc\nd").map((change) => `${change.type}:${change.text}`),
    ["same:a", "del:b", "add:B", "same:c", "add:d"]
  );
  const markdown = toMarkdown("T", null, "Intro\n```\nx\ny\n```\n| a |\n| b |\nEnd");
  assert.ok(markdown.includes("```\nx\ny\n```"), markdown);
  assert.ok(markdown.includes("| a |\n| b |"), markdown);
  assert.ok(markdown.includes("Intro\n\n```"), markdown);
});

test("the activity feed shows editing sessions and audit events, newest first", async () => {
  await boot();
  const { activityFeed, formatDuration } = await import(`${pathToFileURL(resolve(WEB, "extras.js")).href}?t=${Math.random()}`);
  assert.equal(formatDuration(42), "42 sec");
  assert.equal(formatDuration(12 * 60), "12 min");
  assert.equal(formatDuration(65 * 60), "1 h 5 min");

  const ann = { user_id: "a", name: "Ann", presence_color: "#f00" };
  const feed = activityFeed(
    [
      { ...ann, started_at: "2026-01-01T10:00:00Z", ended_at: "2026-01-01T10:04:00Z", inserted: 312, deleted: 20 },
      { ...ann, started_at: "2026-01-01T09:00:00Z", ended_at: "2026-01-01T09:00:05Z", inserted: 1, deleted: 0 },
    ],
    [{ action: "invited", detail: "bob@x.io as editor", actor: { id: "a", name: "Ann" }, created_at: "2026-01-01T09:30:00Z" }]
  );
  assert.deepEqual(
    feed.map((item) => item.text),
    ["edited for 4 min · +312 −20", "shared the page with bob@x.io as editor", "made a quick edit · +1"]
  );
});

// --------------------------------------------------------------------------
// M8: renamed page links, due dates, contents, find, re-anchoring, sign-in
// --------------------------------------------------------------------------

const extras = () => import(`${pathToFileURL(resolve(WEB, "extras.js")).href}?t=${Math.random()}`);

test("a page link shows the page's current title but keeps its source text", async () => {
  const content = "See [Old name](page:doc-1) and [Gone](page:00000000-0000-0000-0000-000000000000)";
  const { document, page } = await open(content);
  const [live, missing] = page.querySelectorAll(".page-link");
  assert.equal(live.querySelector(".live-title").textContent, "Demo document");
  assert.equal(missing.classList.contains("missing"), true);
  assert.deepEqual([...page.querySelectorAll(".line")].map((line) => sourceOf(document, line)), [content]);
});

test("due dates render as chips, a table of contents lists the headings", async () => {
  const content = ["[TOC]", "# Plan", "- [ ] Ship it 📅 2000-01-01", "## Details", "- [ ] Later 📅 2999-12-31"].join("\n");
  const { document, page } = await open(content);
  const lines = [...page.querySelectorAll(".line")];
  assert.equal(lines[0].dataset.type, "toc");
  assert.deepEqual(
    [...lines[0].querySelectorAll(".toc-item")].map((item) => [item.textContent, item.dataset.tocLine]),
    [["Plan", "1"], ["Details", "3"]]
  );
  assert.ok(lines[2].querySelector(".due.overdue"));
  assert.ok(lines[4].querySelector(".due.later"));
  assert.deepEqual(lines.map((line) => sourceOf(document, line)), content.split("\n"));
});

test("find counts matches and replace-all rewrites them in one edit", async () => {
  const { findMatches, replaceMatches } = await extras();
  const text = "Cat cat CAT dog";
  assert.equal(findMatches(text, "cat").length, 3);
  assert.deepEqual(findMatches(text, "cat", { caseSensitive: true }), [{ from: 4, to: 7 }]);
  assert.equal(replaceMatches(text, findMatches(text, "cat"), "fox"), "fox fox fox dog");
  assert.deepEqual(findMatches(text, ""), []);
});

test("an edited comment quote is found again; a deleted one is not", async () => {
  const { locateQuote, similarity } = await extras();
  const lines = ["Intro", "The cache is invalidated on every single write.", "Outro"];
  assert.deepEqual(locateQuote(lines, "The cache is invalidated on every single write."), { index: 1, exact: true, score: 1 });
  const moved = locateQuote(lines, "The cache is invalidated on each write.");
  assert.equal(moved.index, 1);
  assert.equal(moved.exact, false);
  assert.equal(locateQuote(lines, "Completely unrelated sentence here").index, -1);
  assert.ok(similarity("abc", "abc") === 1 && similarity("abc", "xyz") < 0.2);
});

test("the graph layout is deterministic and stays inside its box", async () => {
  const { forceLayout } = await extras();
  const nodes = ["a", "b", "c", "d"].map((id) => ({ id }));
  const edges = [{ source: "a", target: "b" }, { source: "b", target: "c" }];
  const first = forceLayout(nodes, edges, { width: 400, height: 300, ticks: 120 });
  const second = forceLayout(nodes, edges, { width: 400, height: 300, ticks: 120 });
  assert.deepEqual([...first], [...second]);
  for (const { x, y } of first.values()) assert.ok(x >= 30 && x <= 370 && y >= 30 && y <= 270);
  const distance = (p, q) => Math.hypot(p.x - q.x, p.y - q.y);
  // Linked pages settle closer together than unlinked ones.
  assert.ok(distance(first.get("a"), first.get("b")) < distance(first.get("a"), first.get("d")) + 40);
});

test("the integrity report draws shares and flags, and its download cannot be injected", async () => {
  await boot();
  const { renderIntegrity, integrityHtml } = await extras();
  const report = {
    document_id: "doc-1", title: "<img src=x onerror=alert(1)>", version: 9,
    generated_at: "2026-10-10T10:00:00Z", characters: 300, keystroke_share: 0.5,
    agreement_rate: 0.5, judged_blocks: 2, model: { trained: true },
    contributors: [
      { user_id: "a", name: "Alice", color: "#2563eb", operations: 5, typed: 150, pasted: 0, deleted: 2,
        surviving: 150, share: 0.5, sessions: 1, active_minutes: 4, first_edit: null, last_edit: null },
      { user_id: "b", name: "<b>Bob</b>", color: "#dc2626", operations: 2, typed: 0, pasted: 150, deleted: 0,
        surviving: 150, share: 0.5, sessions: 1, active_minutes: 0, first_edit: null, last_edit: null },
    ],
    flags: [
      { kind: "paste", severity: "high", user_id: "b", other_user_id: "a", title: "Pasted",
        detail: "150 characters", snippet: "<script>alert(1)</script>", start: 150, end: 300,
        version: 3, timestamp: "2026-10-10T09:00:00Z" },
    ],
  };
  const jumps = [];
  const box = document.createElement("div");
  renderIntegrity(box, report, { onJump: (flag) => jumps.push(flag.start) });
  assert.equal(box.querySelectorAll(".integrity-tile").length, 4);
  assert.equal(box.querySelectorAll(".integrity-table tbody tr").length, 2);
  assert.equal(box.querySelectorAll(".integrity-bar span").length, 2);
  const flag = box.querySelector(".integrity-flag.sev-high");
  assert.match(flag.querySelector(".flag-who").textContent, /Pasted by <b>Bob<\/b> · reads like Alice/);
  flag.querySelector("button").click();
  assert.deepEqual(jumps, [150]);

  const html = integrityHtml(report);
  assert.ok(!html.includes("<script>alert"), "page text is escaped, not executed");
  assert.ok(!html.includes("<img src=x"));
  assert.ok(!html.includes("Show on page"), "the download has no dead buttons");
});

test("dragging a page in the live graph pulls its neighbour along, then it settles", async () => {
  const { GraphSimulation } = await extras();
  const nodes = ["a", "b", "c"].map((id) => ({ id }));
  const edges = [{ source: "a", target: "b" }];
  const positions = new Map([["a", { x: 100, y: 100 }], ["b", { x: 160, y: 100 }], ["c", { x: 300, y: 300 }]]);
  const sim = new GraphSimulation(nodes, edges, { width: 400, height: 400, positions });
  for (let i = 0; i < 400; i += 1) sim.tick();
  assert.ok(sim.settled, "the system cools down");

  const before = { ...sim.points.get("b") };
  sim.pin("a", 380, 380);
  sim.reheat(0.5);
  for (let i = 0; i < 60; i += 1) sim.tick();
  const a = sim.points.get("a");
  const b = sim.points.get("b");
  assert.deepEqual([a.x, a.y], [380, 380], "a pinned page stays under the pointer");
  assert.ok(Math.hypot(b.x - 380, b.y - 380) < Math.hypot(before.x - 380, before.y - 380), "its neighbour follows");
  sim.unpin("a");
  assert.equal(sim.points.get("a").fx, null);
});

test("the local graph keeps pages within two steps, either direction", async () => {
  const { neighbourhood } = await extras();
  const edges = [
    { source: "a", target: "b" },
    { source: "c", target: "b" },
    { source: "c", target: "d" },
    { source: "x", target: "y" },
  ];
  assert.deepEqual([...neighbourhood(edges, "a", 2)].sort(), ["a", "b", "c"]);
  assert.deepEqual([...neighbourhood(edges, "a", 3)].sort(), ["a", "b", "c", "d"]);
  assert.deepEqual([...neighbourhood(edges, "lonely", 2)], ["lonely"]);
});

test("a provider sign-in redirect is read from the fragment", async () => {
  const { hashParams } = await extras();
  assert.deepEqual(hashParams("#oauth_token=abc"), { oauth_token: "abc" });
  assert.deepEqual(hashParams("#mfa_token=x&other=1"), { mfa_token: "x" });
  assert.deepEqual(hashParams(""), {});
});

test("the AI slash item asks the assistant and shows the suggestion", async () => {
  const { window, document, page } = await open("Hello world. This is a page about caches.", (inner, calls) => async (path, options = {}) => {
    if (path === "/api/documents/doc-1/assist") {
      calls.push({ path, method: options.method, body: JSON.parse(options.body) });
      return { ok: true, status: 200, json: async () => ({ text: "- Caches", source: "local", model: null }) };
    }
    return inner(path, options);
  });
  window.document.getElementById("ai-button").click();
  await settle();
  assert.equal(document.getElementById("ai-modal").hidden, false);
  assert.equal(document.querySelector("#ai-result li, #ai-result .line").textContent.replace(/^- /, "").trim(), "Caches");
  assert.match(document.getElementById("ai-source").textContent, /Local summary/);
  assert.ok(page);
});
