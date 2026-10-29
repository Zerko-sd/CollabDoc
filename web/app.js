/**
 * Collaborative Document Server -- browser client (WBS 7.1-7.3).
 *
 * Responsibilities, in the order they matter:
 *   1. keep the editing surfaces, the OT client, and the server in agreement;
 *   2. show who is in the room and where their carets are;
 *   3. render the page with each block tinted by its attributed author;
 *   4. everything around the page: comments, activity, search, sharing.
 *
 * The buffer is plain text carrying lightweight markup (`#` headings, `-`
 * bullets, `**bold**`). Character operations stay simple and provably
 * convergent; the block editor in editor.js turns that text into a formatted,
 * directly editable page, and the Markdown view edits the same text raw.
 */
import { OTClient, TextOperation } from "./ot.js";
import {
  BlockEditor,
  SLASH_ITEMS,
  listNumbers,
  parseLine,
  renderDocument,
  renderLine,
  setImageLoader,
  setPageTitles,
  dueState,
} from "./editor.js";
import {
  EMOJIS,
  TEMPLATES,
  activityFeed,
  bucketLabel,
  formatDuration,
  locateQuote,
  diffLines,
  download,
  findMatches,
  forceLayout,
  GraphSimulation,
  neighbourhood,
  hashParams,
  integrityHtml,
  renderIntegrity,
  replaceMatches,
  fileName,
  formatWhen,
  outline,
  pageStats,
  parseTime,
  randomIcon,
  renderActivityChart,
  toHtml,
  toMarkdown,
} from "./extras.js";
import {
  fromServerOffset,
  fromServerOperation,
  toServerOffset,
  toServerOperation,
} from "./units.js";

const ZWSP = String.fromCharCode(0x200b);

// --------------------------------------------------------------------------
// state
// --------------------------------------------------------------------------

const state = {
  token: localStorage.getItem("collabdoc.token") || null,
  user: null,
  documents: [],
  doc: null, // {id, title, role, canEdit, icon}
  socket: null,
  client: null, // OTClient
  // Edits typed after a page opens but before the room's `init` arrives:
  // {base, operation}, composed against the text the page opened with.
  early: null,
  versionNumber: null, // user-facing version (one per editing burst)
  content: "", // the text the OT client believes is current
  serverText: "", // the server's document at client.version (see units.js)
  blocks: [],
  model: {},
  people: [], // presence entries
  collaborators: new Map(), // user_id -> user, for authors who are not online
  carets: new Map(), // connection_id -> {position, name, color}
  comments: [],
  commentLines: new Map(), // line index -> open thread count
  undoStack: [],
  redoStack: [],
  lastEditKind: null, // "insert" | "delete", for coalescing undo entries
  lastEditAt: 0,
  lastEdited: null,
  applyingRemote: false,
  pendingDelete: null,
  surface: "editor", // which surface had focus last: "editor" | "source"
  favorites: [], // page ids, in sidebar order (kept on the server)
  notifications: [], // newest first
  userSocket: null, // /ws/me: notifications addressed to this user
  version: null, // {content, version, entry} of the version being previewed
};

const el = (id) => document.getElementById(id);
const ui = {
  auth: el("auth"),
  authForm: el("auth-form"),
  authName: el("auth-name"),
  nameField: el("name-field"),
  authEmail: el("auth-email"),
  authPassword: el("auth-password"),
  authError: el("auth-error"),
  authSubmit: el("auth-submit"),
  authToggle: el("auth-toggle"),
  authToggleText: el("auth-toggle-text"),
  authHeading: el("auth-heading"),
  app: el("app"),
  workspace: document.querySelector(".workspace"),
  docList: el("doc-list"),
  favoriteList: el("favorite-list"),
  favoritesSection: el("favorites-section"),
  docTitle: el("doc-title"),
  crumbTitle: el("crumb-title"),
  crumbIcon: el("crumb-icon"),
  roleChip: el("role-chip"),
  pageIcon: el("page-icon"),
  propRole: el("prop-role"),
  propVersion: el("prop-version"),
  propEdited: el("prop-edited"),
  page: el("page"),
  emptyState: el("empty-state"),
  scroller: el("scroller"),
  saveState: el("save-state"),
  presence: el("presence"),
  connection: el("connection"),
  connectionText: el("connection-text"),
  account: el("account"),
  accountMenu: el("account-menu"),
  moreMenu: el("more-menu"),
  favorite: el("favorite"),
  source: el("source"),
  highlight: el("highlight"),
  mirror: el("mirror"),
  cursorLayer: el("cursor-layer"),
  pageCursors: el("page-cursors"),
  editorWrap: el("editor-wrap"),
  preview: el("preview"),
  modelState: el("model-state"),
  blockList: el("block-list"),
  attributionSummary: el("attribution-summary"),
  versionList: el("version-list"),
  peopleList: el("people-list"),
  toast: el("toast"),
  undo: el("undo"),
  redo: el("redo"),
  showAttribution: el("show-attribution"),
  linkRole: el("link-role"),
  linkUrl: el("link-url"),
  shareNote: el("share-note"),
  versionModal: el("version-modal"),
  versionContent: el("version-content"),
  versionTitle: el("version-modal-title"),
  versionSub: el("version-modal-sub"),
  versionRestore: el("version-restore"),
  slashMenu: el("slash-menu"),
  bubble: el("bubble"),
  bubbleActions: el("bubble-actions"),
  linkForm: el("link-form"),
  linkInput: el("link-input"),
  blockHandle: el("block-handle"),
  blockMenu: el("block-menu"),
  dropLine: el("drop-line"),
  emojiPicker: el("emoji-picker"),
  palette: el("palette"),
  paletteInput: el("palette-input"),
  paletteResults: el("palette-results"),
  templates: el("templates"),
  shortcuts: el("shortcuts"),
  commentForm: el("comment-form"),
  commentBody: el("comment-body"),
  commentAnchor: el("comment-anchor"),
  commentHint: el("comment-hint"),
  commentCount: el("comment-count"),
  threadList: el("thread-list"),
  showResolved: el("show-resolved"),
  crumbParents: el("crumb-parents"),
  inbox: el("open-inbox"),
  inboxCount: el("inbox-count"),
  inboxMenu: el("inbox-menu"),
  inboxList: el("inbox-list"),
  trash: el("trash"),
  trashList: el("trash-list"),
  imageInput: el("image-input"),
  importInput: el("import-input"),
  publishToggle: el("publish-toggle"),
  publishUrl: el("publish-url"),
  publishNote: el("publish-note"),
  openPublish: el("open-publish"),
  versionCompare: el("version-compare"),
};

/** Per-viewer preferences; storage can be missing or blocked, so never rely on it. */
const prefs = {
  get(key, fallback) {
    try {
      return localStorage.getItem(`collabdoc.${key}`) ?? fallback;
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`collabdoc.${key}`, value);
    } catch {
      /* a preference that does not persist is still applied for this visit */
    }
  },
};

let registerMode = false;
let toastTimer = null;
const frame = (callback) => (window.requestAnimationFrame || setTimeout)(callback);

// --------------------------------------------------------------------------
// api
// --------------------------------------------------------------------------

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    ...options,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (response.status === 401) {
    signOut();
    throw new Error("Your session expired. Sign in again.");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : detail;
    } catch {
      /* the body was not JSON; the status text will do */
    }
    throw new Error(detail || "Something went wrong");
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, isError = false) {
  ui.toast.textContent = message;
  ui.toast.classList.toggle("error", isError);
  ui.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (ui.toast.hidden = true), 3200);
}

// --------------------------------------------------------------------------
// authentication
// --------------------------------------------------------------------------

ui.authToggle.addEventListener("click", () => {
  registerMode = !registerMode;
  ui.nameField.hidden = !registerMode;
  ui.authName.required = registerMode;
  ui.authSubmit.textContent = registerMode ? "Create account" : "Continue";
  ui.authHeading.textContent = registerMode ? "Create your account" : "Welcome back";
  ui.authPassword.autocomplete = registerMode ? "new-password" : "current-password";
  ui.authToggleText.textContent = registerMode ? "Already registered?" : "New here?";
  ui.authToggle.textContent = registerMode ? "Log in instead" : "Create an account";
  ui.authError.hidden = true;
  (registerMode ? ui.authName : ui.authEmail).focus();
});

ui.authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  ui.authError.hidden = true;
  const body = {
    email: ui.authEmail.value.trim(),
    password: ui.authPassword.value,
  };
  if (registerMode) body.name = ui.authName.value.trim();
  try {
    let result;
    if (pendingMfa) {
      result = await api("/api/auth/2fa/verify", {
        method: "POST",
        body: { mfa_token: pendingMfa, code: el("auth-code").value.trim() },
      });
    } else {
      result = await api(registerMode ? "/api/auth/register" : "/api/auth/login", {
        method: "POST",
        body,
      });
    }
    if (result.mfa_required) {
      showMfaStep(result.mfa_token);
      return;
    }
    endMfaStep();
    state.token = result.access_token;
    localStorage.setItem("collabdoc.token", state.token);
    state.user = result.user;
    await enterApp();
  } catch (error) {
    ui.authError.textContent = error.message;
    ui.authError.hidden = false;
  }
});

function signOut() {
  localStorage.removeItem("collabdoc.token");
  state.token = null;
  state.user = null;
  state.doc = null;
  closeSocket();
  closeUserSocket();
  state.notifications = [];
  state.favorites = [];
  closeMenus();
  ui.app.hidden = true;
  ui.auth.hidden = false;
}

// --------------------------------------------------------------------------
// menus and popovers
// --------------------------------------------------------------------------

const POPOVERS = () => [ui.accountMenu, ui.moreMenu, ui.blockMenu, ui.emojiPicker, ui.inboxMenu];

function closeMenus(except) {
  for (const menu of POPOVERS()) if (menu !== except) menu.hidden = true;
}

/** Show a fixed-position popover next to a rectangle, kept inside the viewport. */
function placePopover(popover, rect, { align = "left", gap = 6 } = {}) {
  popover.hidden = false;
  popover.style.left = "0px";
  popover.style.top = "0px";
  const box = popover.getBoundingClientRect();
  const width = window.innerWidth;
  const height = window.innerHeight;
  let left = align === "right" ? rect.right - box.width : rect.left;
  left = Math.max(8, Math.min(left, width - box.width - 8));
  let top = rect.bottom + gap;
  if (top + box.height > height - 8 && rect.top - gap - box.height > 8) {
    top = rect.top - gap - box.height;
  }
  popover.style.left = `${left}px`;
  popover.style.top = `${Math.max(8, Math.min(top, height - box.height - 8))}px`;
}

function togglePopover(popover, anchor, options) {
  if (!popover.hidden) {
    popover.hidden = true;
    return false;
  }
  closeMenus(popover);
  placePopover(popover, anchor.getBoundingClientRect(), options);
  return true;
}

ui.account.addEventListener("click", (event) => {
  event.stopPropagation();
  closeMenus(ui.accountMenu);
  ui.accountMenu.hidden = !ui.accountMenu.hidden;
});

document.addEventListener("mousedown", (event) => {
  for (const menu of POPOVERS()) {
    if (!menu.hidden && !menu.contains(event.target) && !event.target.closest?.("[data-popover-anchor]")) {
      menu.hidden = true;
    }
  }
  if (!ui.slashMenu.hidden && !ui.slashMenu.contains(event.target)) editor.closeSlash();
  if (!ui.bubble.hidden && !ui.bubble.contains(event.target) && !ui.preview.contains(event.target)) {
    hideBubble();
  }
});

el("sign-out").addEventListener("click", signOut);

// -- theme -----------------------------------------------------------------

function applyTheme(choice) {
  if (choice === "light" || choice === "dark") document.documentElement.dataset.theme = choice;
  else delete document.documentElement.dataset.theme;
  prefs.set("theme", choice);
  document
    .querySelectorAll("#theme-choice button")
    .forEach((button) => button.classList.toggle("active", button.dataset.themeValue === choice));
}

function effectiveTheme() {
  const chosen = document.documentElement.dataset.theme;
  if (chosen) return chosen;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

el("theme-choice").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-theme-value]");
  if (button) applyTheme(button.dataset.themeValue);
});
applyTheme(prefs.get("theme", "system"));

// --------------------------------------------------------------------------
// workspace
// --------------------------------------------------------------------------

async function enterApp() {
  ui.auth.hidden = true;
  ui.app.hidden = false;
  for (const id of ["account-avatar", "menu-avatar"]) {
    paintAvatar(el(id), state.user);
  }
  el("account-name").textContent = state.user.name;
  el("menu-name").textContent = state.user.name;
  el("menu-email").textContent = state.user.email;
  ui.account.title = `${state.user.name} (${state.user.email})`;

  await claimShareLink();
  await loadDocuments();
  loadFavorites();
  loadNotifications();
  connectUserSocket();

  const wanted = new URLSearchParams(location.search).get("doc");
  const first = state.documents[0];
  if (wanted) await openDocument(wanted);
  else if (first) await openDocument(first.id);
  else showPage(false);
}

function showPage(visible) {
  ui.page.hidden = !visible;
  ui.emptyState.hidden = visible;
  ui.workspace.classList.toggle("no-doc", !visible);
  if (!visible) {
    ui.crumbTitle.textContent = "No page";
    ui.crumbIcon.textContent = "📄";
    ui.roleChip.hidden = true;
    ui.saveState.textContent = "";
    document.title = "CollabDoc";
    hideBubble();
    ui.blockHandle.hidden = true;
    stopFollowing?.();
    loadHome();
  }
}

/** A ?join=<token> link grants access before the document list is loaded. */
async function claimShareLink() {
  const token = new URLSearchParams(location.search).get("join");
  if (!token) return;
  try {
    const grant = await api(`/api/documents/join/${token}`, { method: "POST" });
    toast(`You joined as ${grant.role.toLowerCase()}`);
  } catch (error) {
    toast(error.message, true);
  }
  history.replaceState({}, "", location.pathname);
}

async function loadDocuments() {
  state.documents = await api("/api/documents");
  renderDocumentList();
}

// -- favorites -------------------------------------------------------------

/**
 * Favourites live on the server so they follow the user between devices.
 * Older builds kept them in this browser; the first load moves those over.
 */
async function loadFavorites() {
  try {
    state.favorites = await api("/api/me/favorites");
  } catch {
    state.favorites = [];
  }
  const legacyKey = `favorites.${state.user?.id}`;
  let legacy = [];
  try {
    legacy = JSON.parse(prefs.get(legacyKey, "[]"));
  } catch {
    legacy = [];
  }
  if (legacy.length) {
    const merged = [...new Set([...state.favorites, ...legacy])];
    try {
      state.favorites = await api("/api/me/favorites", { method: "PUT", body: { document_ids: merged } });
      prefs.set(legacyKey, "[]");
    } catch {
      /* keep the local copy and try again next visit */
    }
  }
  renderDocumentList();
}

function favorites() {
  return new Set(state.favorites);
}

async function toggleFavorite(documentId) {
  const was = state.favorites.includes(documentId);
  const next = was ? state.favorites.filter((id) => id !== documentId) : [...state.favorites, documentId];
  state.favorites = next;
  renderDocumentList();
  try {
    state.favorites = await api("/api/me/favorites", { method: "PUT", body: { document_ids: next } });
    renderDocumentList();
    toast(was ? "Removed from favorites" : "Added to favorites");
  } catch (error) {
    toast(error.message, true);
  }
}

ui.favorite.addEventListener("click", () => state.doc && toggleFavorite(state.doc.id));

// -- page tree -------------------------------------------------------------

const TRASH_ICON =
  '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" /></svg>';
const PLUS_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3.5v9M3.5 8h9" /></svg>';
const CARET_ICON = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="m6 4 4 4-4 4" /></svg>';

function expandedPages() {
  try {
    return new Set(JSON.parse(prefs.get("expanded", "[]")));
  } catch {
    return new Set();
  }
}

function setExpanded(documentId, open) {
  const set = expandedPages();
  if (open) set.add(documentId);
  else set.delete(documentId);
  prefs.set("expanded", JSON.stringify([...set]));
}

/** Children of each page, from the flat list the server sends. */
function pageTree() {
  const known = new Set(state.documents.map((doc) => doc.id));
  const children = new Map();
  const roots = [];
  for (const doc of state.documents) {
    if (doc.parent_id && known.has(doc.parent_id)) {
      if (!children.has(doc.parent_id)) children.set(doc.parent_id, []);
      children.get(doc.parent_id).push(doc);
    } else {
      roots.push(doc);
    }
  }
  // Sub-pages keep the order they were made in; top-level pages stay newest first.
  for (const list of children.values()) list.sort((a, b) => parseTime(a.created_at) - parseTime(b.created_at));
  return { roots, children };
}

/** A page's ancestors, outermost first. */
function ancestorsOf(documentId) {
  const byId = new Map(state.documents.map((doc) => [doc.id, doc]));
  const chain = [];
  let current = byId.get(documentId);
  const seen = new Set();
  while (current?.parent_id && byId.has(current.parent_id) && !seen.has(current.parent_id)) {
    seen.add(current.parent_id);
    current = byId.get(current.parent_id);
    chain.unshift(current);
  }
  return chain;
}

let draggedPage = null;

function pageItem(doc, { removable, depth = 0, kids = [], open = false }) {
  const item = document.createElement("li");
  item.className = doc.id === state.doc?.id ? "active" : "";
  item.style.setProperty("--depth", depth);
  item.dataset.id = doc.id;
  item.title = `${doc.title}\n${roleLabel(doc.role)} · v${versionOf(doc)} · ${
    doc.collaborator_count
  } ${doc.collaborator_count === 1 ? "person" : "people"}`;

  if (removable) {
    const caret = document.createElement("button");
    caret.type = "button";
    caret.className = `tree-caret${open ? " open" : ""}${kids.length ? "" : " leaf"}`;
    caret.title = kids.length ? (open ? "Collapse" : "Expand") : "No sub-pages";
    caret.innerHTML = CARET_ICON;
    caret.addEventListener("click", (event) => {
      event.stopPropagation();
      if (!kids.length) return;
      setExpanded(doc.id, !open);
      renderDocumentList();
    });
    item.append(caret);
  }

  const icon = document.createElement("span");
  icon.className = "doc-icon";
  icon.textContent = doc.icon || "📄";

  const name = document.createElement("span");
  name.className = "name";
  name.textContent = doc.title || "Untitled";

  item.append(icon, name);
  item.addEventListener("click", () => openDocument(doc.id));

  if (doc.role !== "OWNER") {
    const shared = document.createElement("span");
    shared.className = "shared-mark";
    shared.textContent = doc.role === "VIEWER" ? "view" : "shared";
    item.append(shared);
  }

  if (removable && doc.role !== "VIEWER") {
    const add = document.createElement("button");
    add.type = "button";
    add.className = "row-action";
    add.title = "Add a sub-page inside";
    add.innerHTML = PLUS_ICON;
    add.addEventListener("click", (event) => {
      event.stopPropagation();
      setExpanded(doc.id, true);
      createDocument({ parentId: doc.id });
    });
    item.append(add);
  }

  if (removable && doc.role === "OWNER") {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "row-action";
    remove.title = "Move to trash";
    remove.innerHTML = TRASH_ICON;
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      confirmDelete(doc, remove);
    });
    item.append(remove);
  }

  if (removable && doc.role !== "VIEWER") {
    // Drag a page onto another to nest it there.
    item.draggable = true;
    item.addEventListener("dragstart", (event) => {
      draggedPage = doc.id;
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", doc.title);
    });
    item.addEventListener("dragend", () => {
      draggedPage = null;
      document.querySelectorAll(".doc-list .drop-into").forEach((node) => node.classList.remove("drop-into"));
    });
  }
  if (removable) {
    item.addEventListener("dragover", (event) => {
      if (!draggedPage || draggedPage === doc.id || doc.role === "VIEWER") return;
      event.preventDefault();
      item.classList.add("drop-into");
    });
    item.addEventListener("dragleave", () => item.classList.remove("drop-into"));
    item.addEventListener("drop", (event) => {
      event.preventDefault();
      item.classList.remove("drop-into");
      if (draggedPage && draggedPage !== doc.id) movePage(draggedPage, doc.id);
    });
  }
  return item;
}

async function movePage(documentId, parentId) {
  try {
    await api(`/api/documents/${documentId}`, { method: "PATCH", body: { parent_id: parentId } });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  if (parentId) setExpanded(parentId, true);
  await loadDocuments();
  renderCrumbs();
  const parent = state.documents.find((doc) => doc.id === parentId);
  toast(parent ? `Moved into ${parent.title || "Untitled"}` : "Moved to the top level");
}

function renderDocumentList() {
  renderDocMeta();
  renderCrumbs();
  // Page links on the open page show the linked pages' current titles.
  if (setPageTitles(state.documents.map((doc) => [doc.id, doc.title])) && state.doc) editor.render();
  const favored = favorites();
  const favoriteDocs = state.favorites
    .map((id) => state.documents.find((doc) => doc.id === id))
    .filter(Boolean);
  ui.favoritesSection.hidden = favoriteDocs.length === 0;
  ui.favoriteList.replaceChildren(...favoriteDocs.map((doc) => pageItem(doc, { removable: false })));
  const isFavorite = Boolean(state.doc && favored.has(state.doc.id));
  ui.favorite.classList.toggle("on", isFavorite);
  ui.favorite.title = isFavorite ? "Remove from favorites" : "Add to favorites";

  if (!state.documents.length) {
    const empty = document.createElement("li");
    empty.className = "empty-row";
    empty.textContent = "No pages yet";
    ui.docList.replaceChildren(empty);
    return;
  }
  const { roots, children } = pageTree();
  const expanded = expandedPages();
  // The open page is always visible: its ancestors count as expanded.
  if (state.doc) for (const ancestor of ancestorsOf(state.doc.id)) expanded.add(ancestor.id);
  const rows = [];
  const walk = (doc, depth) => {
    const kids = children.get(doc.id) || [];
    const open = expanded.has(doc.id);
    rows.push(pageItem(doc, { removable: true, depth, kids, open }));
    if (open && depth < 12) for (const kid of kids) walk(kid, depth + 1);
  };
  for (const doc of roots) walk(doc, 0);
  ui.docList.replaceChildren(...rows);
}

/** Parent / child / page above the title, each step clickable. */
function renderCrumbs() {
  if (!ui.crumbParents) return;
  const chain = state.doc ? ancestorsOf(state.doc.id) : [];
  ui.crumbParents.replaceChildren(
    ...chain.flatMap((doc) => {
      const step = document.createElement("button");
      step.type = "button";
      step.className = "crumb-step";
      step.textContent = `${doc.icon || "📄"} ${doc.title || "Untitled"}`;
      step.addEventListener("click", () => openDocument(doc.id));
      const slash = document.createElement("span");
      slash.className = "crumb-sep";
      slash.textContent = "/";
      return [step, slash];
    })
  );
}

async function confirmDelete(doc, control) {
  if (state.pendingDelete !== doc.id) {
    state.pendingDelete = doc.id;
    const original = control.innerHTML;
    control.classList.add("confirm");
    control.textContent = control.classList.contains("menu-item") ? "Click again to move to trash" : "Trash?";
    setTimeout(() => {
      if (state.pendingDelete === doc.id) {
        state.pendingDelete = null;
        control.classList.remove("confirm");
        control.innerHTML = original;
      }
    }, 3000);
    return;
  }
  state.pendingDelete = null;
  try {
    await api(`/api/documents/${doc.id}`, { method: "DELETE" });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  closeMenus();
  await loadDocuments();
  // Sub-pages go to the trash with their parent, so the open page may be gone too.
  if (state.doc && !state.documents.some((listed) => listed.id === state.doc.id)) {
    closeSocket();
    state.doc = null;
    setEditorText("");
    history.replaceState({}, "", location.pathname);
    const next = state.documents[0];
    if (next) await openDocument(next.id);
    else showPage(false);
  }
  toast("Moved to trash — restore it from Trash in the sidebar");
}

// -- trash -----------------------------------------------------------------

async function openTrash() {
  openModal(ui.trash);
  let pages;
  try {
    pages = await api("/api/documents/trash");
  } catch (error) {
    toast(error.message, true);
    return;
  }
  if (!pages.length) {
    const empty = document.createElement("li");
    empty.className = "panel-hint";
    empty.textContent = "The trash is empty.";
    ui.trashList.replaceChildren(empty);
    return;
  }
  ui.trashList.replaceChildren(
    ...pages.map((doc) => {
      const item = document.createElement("li");
      const icon = document.createElement("span");
      icon.className = "doc-icon";
      icon.textContent = doc.icon || "📄";
      const label = document.createElement("span");
      label.className = "grow";
      const name = document.createElement("b");
      name.textContent = doc.title || "Untitled";
      const when = document.createElement("span");
      when.textContent = `Deleted ${formatWhen(doc.deleted_at)}`;
      label.append(name, when);

      const restore = document.createElement("button");
      restore.type = "button";
      restore.className = "small";
      restore.textContent = "Restore";
      restore.addEventListener("click", async () => {
        try {
          await api(`/api/documents/${doc.id}/restore`, { method: "POST" });
        } catch (error) {
          toast(error.message, true);
          return;
        }
        item.remove();
        await loadDocuments();
        toast(`Restored ${doc.title || "Untitled"}`);
        if (!ui.trashList.children.length) openTrash();
      });

      const destroy = document.createElement("button");
      destroy.type = "button";
      destroy.className = "small danger";
      destroy.textContent = "Delete forever";
      destroy.addEventListener("click", async () => {
        if (destroy.dataset.armed !== "yes") {
          destroy.dataset.armed = "yes";
          destroy.textContent = "Click again — this cannot be undone";
          return;
        }
        try {
          await api(`/api/documents/${doc.id}?permanent=true`, { method: "DELETE" });
        } catch (error) {
          toast(error.message, true);
          return;
        }
        item.remove();
        if (!ui.trashList.children.length) openTrash();
      });
      item.append(icon, label, restore, destroy);
      return item;
    })
  );
}

el("open-trash").addEventListener("click", openTrash);

/**
 * The version people see. `client.version` is the OT revision and moves on
 * every keystroke; `version_number` moves once per editing burst.
 */
function versionOf(doc) {
  return doc.id === state.doc?.id && state.versionNumber != null
    ? state.versionNumber
    : doc.version_number ?? 0;
}

function roleLabel(role) {
  return { OWNER: "Owner", EDITOR: "Can edit", VIEWER: "Can view" }[role] || role;
}

function renderDocMeta() {
  if (!state.doc) return;
  const listed = state.documents.find((doc) => doc.id === state.doc.id);
  const version = listed ? versionOf(listed) : state.versionNumber ?? 0;
  const people = listed?.collaborator_count ?? state.collaborators.size;
  ui.propVersion.textContent = `v${version}`;
  const extra = document.createElement("span");
  extra.className = "muted";
  extra.textContent = `· ${people} ${people === 1 ? "person" : "people"} with access`;
  ui.propVersion.append(" ", extra);
  renderEdited();
}

function renderEdited() {
  if (!state.doc) return;
  const listed = state.documents.find((doc) => doc.id === state.doc.id);
  const when = state.lastEdited || (listed && parseTime(listed.updated_at));
  ui.propEdited.textContent = when ? formatWhen(when) : "—";
  if (when) ui.propEdited.title = when.toLocaleString();
}
// Node (the jsdom test harness) must not be kept alive by this refresh.
setInterval(renderEdited, 30_000)?.unref?.();

async function createDocument({ title = "Untitled", content = "", icon = null, parentId = null } = {}) {
  let created;
  try {
    created = await api("/api/documents", {
      method: "POST",
      body: { title, content, icon, parent_id: parentId },
    });
  } catch (error) {
    toast(error.message, true);
    return null;
  }
  await loadDocuments();
  await openDocument(created.id);
  return created;
}

async function createBlank() {
  const focused = document.activeElement;
  const created = await createDocument();
  // Start on the title -- unless the user already clicked into the page
  // while it was being created, in which case their typing belongs there.
  if (created && document.activeElement === focused) {
    ui.docTitle.focus();
    ui.docTitle.select();
  }
}

el("new-doc").addEventListener("click", createBlank);
el("new-doc-inline").addEventListener("click", createBlank);
el("empty-new").addEventListener("click", createBlank);

function toggleSidebar() {
  ui.workspace.classList.toggle("no-sidebar");
  frame(renderCarets);
}

el("toggle-sidebar").addEventListener("click", toggleSidebar);
el("collapse-sidebar").addEventListener("click", toggleSidebar);

const inspectorToggle = el("toggle-inspector");

function setInspector(open) {
  ui.workspace.classList.toggle("no-inspector", !open);
  inspectorToggle.classList.toggle("active", open);
  prefs.set("inspector", open ? "open" : "closed");
  frame(renderCarets);
}

/** Open the side panel on a tab; a second press of the same control closes it. */
function focusPanel(tab, { toggle = true } = {}) {
  const open = !ui.workspace.classList.contains("no-inspector");
  const current = document.querySelector("#tabs button.active")?.dataset.tab;
  if (toggle && open && current === tab) {
    setInspector(false);
    return;
  }
  setInspector(true);
  if (current !== tab) document.querySelector(`#tabs button[data-tab="${tab}"]`)?.click();
}

inspectorToggle.addEventListener("click", () =>
  setInspector(ui.workspace.classList.contains("no-inspector"))
);
el("open-share").addEventListener("click", () => focusPanel("share"));
el("open-history").addEventListener("click", () => focusPanel("history"));
el("open-comments").addEventListener("click", () => focusPanel("comments"));
if (prefs.get("inspector", "open") === "closed") setInspector(false);

// -- view mode: page / markdown / split -----------------------------------

function currentView() {
  return ui.page.dataset.view;
}

function setView(view) {
  if (view === "write" || view === "read") view = "page"; // older saved values
  if (!["page", "markdown", "split"].includes(view)) view = "page";
  ui.page.dataset.view = view;
  document
    .querySelectorAll("#views button")
    .forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  prefs.set("view", view);
  if (view === "markdown") state.surface = "source";
  if (view === "page") state.surface = "editor";
  hideBubble();
  editor.closeSlash();
  fitSource();
  frame(renderCarets);
}

el("views").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (button) setView(button.dataset.view);
});

// --------------------------------------------------------------------------
// opening a document
// --------------------------------------------------------------------------

async function openDocument(documentId) {
  closeSocket();
  let detail;
  try {
    detail = await api(`/api/documents/${documentId}`);
  } catch (error) {
    toast(error.message, true);
    return;
  }
  state.doc = {
    id: detail.id,
    title: detail.title,
    role: detail.role,
    icon: detail.icon || null,
    canEdit: detail.role !== "VIEWER",
  };
  state.undoStack = [];
  state.redoStack = [];
  state.lastEditKind = null;
  state.lastEdited = null;
  state.blocks = [];
  state.model = {};
  state.comments = [];
  state.collaborators = new Map();
  showPage(true);
  ui.docTitle.value = detail.title;
  renderTitle();
  renderIcon();
  state.doc.locked = Boolean(detail.locked);
  applyRole(detail.role);
  ui.saveState.textContent = "";
  history.replaceState({}, "", `${location.pathname}?doc=${detail.id}`);
  ui.scroller.scrollTop = 0;
  closeMenus();
  hideBubble();
  if (window.matchMedia?.("(max-width: 720px)").matches) {
    ui.workspace.classList.add("no-sidebar");
  }

  setEditorText(detail.content || "");
  state.early = state.doc.canEdit ? { base: state.content, operation: null } : null;
  renderDocumentList();
  renderThreads();
  stopFollowing();
  closeFind();
  loadBacklinks();
  connectSocket(detail.id);
  loadVersions();
  loadPeople();
  loadShareLink();
  loadPublish();
  loadComments();
  if (activeTab() === "activity") loadActivity();
}

/**
 * Make the surfaces match the caller's role. Runs when a page opens, and
 * again when the room reports a different role -- a page was locked or
 * unlocked while it was open.
 */
function applyRole(role) {
  state.doc.role = role;
  state.doc.canEdit = role !== "VIEWER";
  const canEdit = state.doc.canEdit;
  ui.docTitle.readOnly = !canEdit;
  ui.source.readOnly = !canEdit;
  ui.preview.setAttribute("contenteditable", canEdit ? "true" : "false");
  ui.pageIcon.disabled = !canEdit;
  document.querySelectorAll(".toolbar button").forEach((button) => {
    if (button.hasAttribute("data-always")) return;
    button.disabled = !canEdit;
  });
  ui.page.classList.toggle("read-only", !canEdit);
  ui.propRole.textContent = roleLabel(role);
  ui.propRole.className = `tag ${role.toLowerCase()}`;
  const locked = Boolean(state.doc.locked);
  el("lock-chip").hidden = !locked;
  ui.roleChip.hidden = canEdit || locked;
  ui.roleChip.textContent = "View only";
  el("lock-label").textContent = locked ? "Unlock page" : "Lock page";
  document.querySelectorAll("[data-owner-only]").forEach((node) => {
    node.hidden = role !== "OWNER";
  });
  document.querySelectorAll("[data-editor-only]").forEach((node) => {
    node.hidden = !canEdit;
  });
}

// --------------------------------------------------------------------------
// the socket
// --------------------------------------------------------------------------

function closeSocket() {
  if (state.socket) {
    state.socket.onclose = null;
    state.socket.close();
    state.socket = null;
  }
  state.client = null;
  state.early = null;
  state.versionNumber = null;
  state.carets.clear();
  renderCarets();
  setConnection("offline", "");
}

function setConnection(text, className) {
  ui.connectionText.textContent = text;
  ui.connection.className = `connection ${className}`;
}

function connectSocket(documentId) {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const url = `${protocol}://${location.host}/ws/documents/${documentId}?token=${encodeURIComponent(
    state.token
  )}`;
  const socket = new WebSocket(url);
  state.socket = socket;
  setConnection("connecting", "");

  // "live" waits for `init`: an open socket is not yet a synchronised room.
  socket.onopen = () => setConnection("joining", "");

  socket.onmessage = (event) => handleMessage(JSON.parse(event.data));

  socket.onclose = (event) => {
    if (state.socket !== socket) return;
    setConnection("disconnected", "down");
    if (event.code === 4401) {
      signOut();
      return;
    }
    if (event.code === 4403) {
      toast("You no longer have access to this document", true);
      return;
    }
    // Reconnecting re-runs `init`, which resynchronises from the server's
    // authoritative text rather than trying to replay what was missed.
    setTimeout(() => {
      if (state.doc?.id === documentId && state.socket === socket) {
        connectSocket(documentId);
      }
    }, 1500);
  };
}

function send(message) {
  if (state.socket?.readyState === WebSocket.OPEN) {
    state.socket.send(JSON.stringify(message));
  }
}

function handleMessage(message) {
  switch (message.type) {
    case "init": {
      // An operation the client sends always applies to the server document
      // at the version it names, which is exactly `serverText` at that moment.
      state.client = new OTClient(message.version, (operation, version) =>
        send({
          type: "op",
          version,
          operation: toServerOperation(operation, state.serverText).toJSON(),
        })
      );
      state.versionNumber = message.version_number;
      state.serverText = message.content;
      setConnection("live", "live");
      if (message.you?.role && state.doc && message.you.role !== state.doc.role) {
        // Locked or unlocked while open: the room closed and we are back
        // with a different role.
        api(`/api/documents/${state.doc.id}`)
          .then((detail) => {
            state.doc.locked = Boolean(detail.locked);
            applyRole(detail.role);
            toast(detail.locked ? "The owner locked this page" : "This page can be edited again");
          })
          .catch(() => {});
      }
      const early = state.early;
      state.early = null;
      const role = message.you?.role || state.doc?.role;
      if (early?.operation && role && role !== "VIEWER") {
        // Typed before the room answered: rebase onto whatever the server
        // holds now, then send it like any other local edit.
        let operation = early.operation;
        if (message.content !== early.base) {
          [operation] = TextOperation.transform(
            operation,
            TextOperation.fromDiff(early.base, message.content)
          );
        }
        setEditorText(operation.apply(message.content));
        state.client.applyClient(operation);
        markDirty();
      } else {
        setEditorText(message.content);
        markSaved();
      }
      applyAttribution(message);
      renderDocMeta();
      break;
    }
    case "ack":
      if (!state.client) break;
      // The server now has our outstanding operation; fold it in before the
      // buffered one is released, since that one is based on the result.
      if (state.client.outstanding) {
        state.serverText = state.client.outstanding.apply(state.serverText);
      }
      state.client.serverAck();
      state.versionNumber = message.version_number ?? state.versionNumber;
      markSaved();
      renderDocMeta();
      break;
    case "op": {
      if (!state.client) break;
      const remote = fromServerOperation(TextOperation.fromJSON(message.operation), state.serverText);
      state.serverText = remote.apply(state.serverText);
      const operation = state.client.applyServer(remote);
      state.versionNumber = message.version_number ?? state.versionNumber;
      state.lastEdited = new Date();
      applyRemoteOperation(operation);
      renderDocMeta();
      break;
    }
    case "presence":
      state.people = message.users;
      renderPresence();
      break;
    case "cursor":
      state.carets.set(message.connection_id, {
        position: fromServerOffset(state.content, message.position),
        name: message.name,
        color: message.presence_color,
      });
      renderCarets();
      if (state.following && message.user_id === state.following.user_id) followCaret(message.connection_id);
      break;
    case "attribution":
      applyAttribution(message);
      renderDocumentList(); // the version label moves with the document
      if (activeTab() === "activity") scheduleActivity();
      break;
    case "comments":
      loadComments();
      break;
    case "saved":
      state.versionNumber = message.version_number ?? state.versionNumber;
      toast(`Saved as version ${state.versionNumber}`);
      loadVersions();
      loadDocuments();
      break;
    case "error":
      if (message.code === "resync_required") {
        toast("Reconnecting to resynchronise…", true);
        connectSocket(state.doc.id);
      } else {
        toast(message.message, true);
      }
      break;
    default:
      break;
  }
}

// --------------------------------------------------------------------------
// the editing surfaces
// --------------------------------------------------------------------------

const editor = new BlockEditor(ui.preview, {
  text: () => state.content,
  canEdit: () => canType(),
  commit: (operation) => commitLocal(operation),
  infoFor: (lines) => lineInfo(lines),
  undo: () => undo(),
  redo: () => redo(),
  save: () => saveVersion(),
  slashMenu: (menu) => renderSlashMenu(menu),
  requestLink: () => openLinkForm(),
  openComments: (index) => showThreadsForLine(index),
  people: () => [...state.collaborators.values()],
  onMention: (person, line) => notifyMention(person, line),
  uploadImages: (files) => uploadImages(files),
  pickImage: () => ui.imageInput.click(),
  createSubpage: () => createLinkedSubpage(),
  openPage: (documentId) => openLinkedPage(documentId),
  assist: (action) => openAssistant(action),
  jumpToLine: (index) => revealLine(index),
});

// -- images ------------------------------------------------------------------

// Image bytes need the session's token, which an <img src> cannot send; each
// file is fetched once and shown from an object URL.
const imageCache = new Map(); // /api/files URL -> Promise<object URL>

function loadImage(url) {
  if (!imageCache.has(url)) {
    const pending = fetch(url, { headers: { Authorization: `Bearer ${state.token}` } })
      .then((response) => {
        if (!response.ok) throw new Error(`Image unavailable (${response.status})`);
        return response.blob();
      })
      .then((blob) => URL.createObjectURL(blob));
    pending.catch(() => imageCache.delete(url)); // let a later render retry
    imageCache.set(url, pending);
  }
  return imageCache.get(url);
}

setImageLoader((img, url) => {
  img.closest?.("figure")?.classList.add("loading");
  loadImage(url)
    .then((src) => {
      img.src = src;
      img.closest("figure")?.classList.remove("loading");
    })
    .catch(() => {
      const figure = img.closest("figure");
      figure?.classList.remove("loading");
      figure?.classList.add("broken");
    });
});

async function uploadImages(files) {
  if (!state.doc?.canEdit) return;
  for (const file of files) {
    if (file.size > 10 * 1024 * 1024) {
      toast(`${file.name} is larger than 10 MB`, true);
      continue;
    }
    toast(`Uploading ${file.name || "image"}…`);
    let uploaded;
    try {
      const response = await fetch(
        `/api/documents/${state.doc.id}/files?filename=${encodeURIComponent(file.name || "image")}`,
        { method: "POST", headers: { Authorization: `Bearer ${state.token}`, "Content-Type": file.type }, body: file }
      );
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "Upload failed");
      }
      uploaded = await response.json();
    } catch (error) {
      toast(error.message, true);
      continue;
    }
    // Seed the cache with the local file, so the image appears at once.
    imageCache.set(uploaded.url, Promise.resolve(URL.createObjectURL(file)));
    const alt = (file.name || "image").replace(/\.[a-z0-9]+$/i, "");
    editor.insertImage(uploaded.url, alt);
  }
}

/** Every image on the page as a data: URL, for a self-contained export. */
async function inlineImages() {
  const urls = new Set(
    state.content
      .split("\n")
      .map((line) => parseLine(line))
      .filter((parsed) => parsed.type === "image")
      .map((parsed) => parsed.src)
  );
  const images = new Map();
  for (const url of urls) {
    try {
      const blob = await (await fetch(await loadImage(url))).blob();
      images.set(
        url,
        await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = reject;
          reader.readAsDataURL(blob);
        })
      );
    } catch {
      /* an image that cannot be read is left out of the export */
    }
  }
  return images;
}

ui.imageInput.addEventListener("change", () => {
  const files = [...ui.imageInput.files];
  ui.imageInput.value = "";
  if (files.length) uploadImages(files);
});

// -- mentions and page links -------------------------------------------------

async function notifyMention(person, line) {
  if (!state.doc || !person || person.id === state.user?.id) return;
  try {
    await api(`/api/documents/${state.doc.id}/mentions`, {
      method: "POST",
      body: { user_id: person.id, snippet: line.slice(0, 280) },
    });
    toast(`${person.name} will be notified`);
  } catch (error) {
    toast(error.message, true);
  }
}

/** The /page command: a sub-page, linked from where the caret is. */
async function createLinkedSubpage() {
  if (!state.doc?.canEdit) return;
  const parentId = state.doc.id;
  let created;
  try {
    created = await api("/api/documents", {
      method: "POST",
      body: { title: "Untitled", content: "", parent_id: parentId },
    });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  editor.insertAtCaret(`[${created.title}](page:${created.id})`);
  setExpanded(parentId, true);
  await loadDocuments();
  await openDocument(created.id);
  ui.docTitle.focus();
  ui.docTitle.select();
}

function openLinkedPage(documentId) {
  if (state.documents.some((doc) => doc.id === documentId)) openDocument(documentId);
  else toast("That page was deleted, or you don't have access to it", true);
}

/** What each rendered line needs to know beyond its own text. */
function lineInfo(lines) {
  const numbers = listNumbers(lines);
  const interactive = canType();
  let block = 0;
  let previousAuthor = null;
  return lines.map((line, i) => {
    const info = { number: numbers[i], interactive };
    if (line.trim()) {
      info.block = block;
      const author = authorOf(block);
      if (author) {
        info.author = author;
        // The margin names each run of an author's blocks once.
        if (author.id !== previousAuthor) info.authorStart = true;
      }
      previousAuthor = author?.id ?? null;
      block += 1;
    }
    const comments = state.commentLines.get(i);
    if (comments) info.comments = comments;
    return info;
  });
}

function setEditorText(text) {
  state.applyingRemote = true;
  state.content = text;
  ui.source.value = text;
  state.applyingRemote = false;
  fitSource();
  renderPreview();
}

/**
 * Grow the textarea to its content so the page, not the textarea, scrolls.
 *
 * With no inner scroll offset, a character's position in the mirror is its
 * position on screen, which keeps remote carets and authorship tints aligned.
 */
function fitSource() {
  const scroll = ui.scroller.scrollTop;
  ui.source.style.height = "auto";
  ui.source.style.height = `${ui.source.scrollHeight}px`;
  ui.scroller.scrollTop = scroll;
}

/** Put the textarea back in step with the buffer, keeping its selection. */
function syncSource(operation) {
  if (ui.source.value === state.content) return;
  const start = ui.source.selectionStart;
  const end = ui.source.selectionEnd;
  state.applyingRemote = true;
  ui.source.value = state.content;
  if (operation) {
    ui.source.selectionStart = operation.transformCursor(start);
    ui.source.selectionEnd = operation.transformCursor(end);
  }
  state.applyingRemote = false;
  fitSource();
}

/** Apply a rebased remote operation, keeping each local caret on its character. */
function applyRemoteOperation(operation) {
  const editorSelection = editor.hasFocus() ? editor.selection() : null;
  state.content = operation.apply(state.content);
  syncSource(operation);

  // Undo entries describe a document that no longer exists, so they have to be
  // rebased over the remote change as well.
  state.undoStack = transformStack(state.undoStack, operation);
  state.redoStack = transformStack(state.redoStack, operation);
  state.lastEditKind = null;

  for (const [id, caret] of state.carets) {
    state.carets.set(id, { ...caret, position: operation.transformCursor(caret.position) });
  }
  renderPreview();
  if (editorSelection) {
    editor.select(
      operation.transformCursor(editorSelection.from),
      operation.transformCursor(editorSelection.to)
    );
  }
  renderCarets();
}

function transformStack(stack, operation) {
  const rebased = new Array(stack.length);
  let incoming = operation;
  for (let i = stack.length - 1; i >= 0; i -= 1) {
    const [entry, next] = TextOperation.transform(stack[i], incoming);
    rebased[i] = entry;
    incoming = next;
  }
  return rebased;
}

/**
 * Consecutive typing belongs in one undo entry.
 *
 * Every keystroke produces its own operation, so a stack of raw inverses would
 * make Ctrl+Z walk back one character at a time. Real editors group a burst of
 * typing, and break the group at a pause, at a newline, or when the user
 * switches between inserting and deleting.
 */
const UNDO_COALESCE_MS = 800;

function editKind(operation) {
  let inserted = 0;
  let deleted = 0;
  for (const op of operation.ops) {
    if (typeof op === "string") inserted += op.length;
    else if (op < 0) deleted -= op;
  }
  if (inserted && !deleted) return "insert";
  if (deleted && !inserted) return "delete";
  return "replace";
}

function pushUndo(inverse, operation) {
  const kind = editKind(operation);
  const now = Date.now();
  const startsNewGroup =
    kind === "replace" ||
    kind !== state.lastEditKind ||
    now - state.lastEditAt > UNDO_COALESCE_MS ||
    operation.ops.some((op) => typeof op === "string" && op.includes("\n"));

  if (!startsNewGroup && state.undoStack.length) {
    // The newer inverse runs first, then the one already on the stack.
    const previous = state.undoStack[state.undoStack.length - 1];
    state.undoStack[state.undoStack.length - 1] = TextOperation.compose(inverse, previous);
  } else {
    state.undoStack.push(inverse);
    if (state.undoStack.length > 200) state.undoStack.shift();
  }
  state.lastEditKind = kind === "replace" ? null : kind;
  state.lastEditAt = now;
}

/** A local edit from any surface: record it, send it, redraw. */
/** Whether local edits can be taken now: live, or buffered until `init`. */
function canType() {
  return Boolean(state.doc?.canEdit && (state.client || state.early));
}

/** Hand a local operation to the OT client, or hold it until the room is ready. */
function submit(operation) {
  if (state.client) {
    state.client.applyClient(operation);
  } else if (state.early) {
    state.early.operation = state.early.operation
      ? TextOperation.compose(state.early.operation, operation)
      : operation;
  }
}

function commitLocal(operation) {
  if (!canType() || operation.isNoop()) return;
  pushUndo(operation.invert(state.content), operation);
  state.redoStack = [];
  state.content = operation.apply(state.content);
  submit(operation);
  state.lastEdited = new Date();
  syncSource(operation);
  for (const [id, caret] of state.carets) {
    state.carets.set(id, { ...caret, position: operation.transformCursor(caret.position) });
  }
  markDirty();
  renderPreview();
  renderCarets();
  updateUndoButtons();
  sendCursor();
}

ui.source.addEventListener("input", () => {
  if (state.applyingRemote || !canType()) return;
  const next = ui.source.value;
  const operation = TextOperation.fromDiff(state.content, next);
  if (operation.isNoop()) return;

  pushUndo(operation.invert(state.content), operation);
  state.redoStack = [];

  state.content = next;
  submit(operation);
  state.lastEdited = new Date();
  markDirty();
  fitSource();
  renderPreview();
  updateUndoButtons();
  sendCursor();
});

ui.source.addEventListener("focus", () => (state.surface = "source"));
ui.preview.addEventListener("focus", () => (state.surface = "editor"));

["click", "keyup", "select"].forEach((event) =>
  ui.source.addEventListener(event, sendCursor)
);

let cursorTimer = null;
function sendCursor() {
  clearTimeout(cursorTimer);
  cursorTimer = setTimeout(() => {
    if (!state.client) return;
    let from = ui.source.selectionStart;
    let to = ui.source.selectionEnd;
    if (state.surface === "editor") {
      const selection = editor.selection();
      if (!selection) return;
      from = selection.from;
      to = selection.to;
    }
    send({
      type: "cursor",
      version: state.client.version,
      position: toServerOffset(state.content, from),
      selection_end: toServerOffset(state.content, to),
    });
  }, 90);
}

/** Where an operation's change ends -- where the caret goes after undo. */
function changeEnd(operation) {
  let position = 0;
  let end = null;
  for (const op of operation.ops) {
    if (typeof op === "string") {
      position += op.length;
      end = position;
    } else if (op > 0) {
      if (end !== null) break;
      position += op;
    } else if (end === null) {
      end = position;
    }
  }
  return end ?? position;
}

/** Apply an operation the local user caused through a control rather than typing. */
function applyLocal(operation) {
  if (!canType()) return;
  const inEditor = state.surface === "editor" && currentView() !== "markdown";
  const selection = ui.source.selectionStart;
  state.content = operation.apply(state.content);
  state.applyingRemote = true;
  ui.source.value = state.content;
  ui.source.selectionStart = ui.source.selectionEnd = operation.transformCursor(selection);
  state.applyingRemote = false;
  submit(operation);
  state.lastEdited = new Date();
  markDirty();
  fitSource();
  renderPreview();
  if (inEditor && editor.hasFocus()) editor.select(changeEnd(operation));
  renderCarets();
}

// -- undo / redo (SRS 3.3) -------------------------------------------------

function undo() {
  state.lastEditKind = null;
  const operation = state.undoStack.pop();
  if (!operation) return;
  state.redoStack.push(operation.invert(state.content));
  applyLocal(operation);
  updateUndoButtons();
}

function redo() {
  state.lastEditKind = null;
  const operation = state.redoStack.pop();
  if (!operation) return;
  state.undoStack.push(operation.invert(state.content));
  applyLocal(operation);
  updateUndoButtons();
}

function updateUndoButtons() {
  ui.undo.disabled = !state.doc?.canEdit || state.undoStack.length === 0;
  ui.redo.disabled = !state.doc?.canEdit || state.redoStack.length === 0;
}

ui.undo.addEventListener("click", undo);
ui.redo.addEventListener("click", redo);

// -- formatting (SRS 2.2 rich text) ---------------------------------------

const WRAPPERS = { bold: "**", italic: "*", underline: "__", strike: "~~", code: "`" };
const BLOCK_TYPES = new Set(["h1", "h2", "h3", "bullet", "number", "todo", "quote", "callout", "paragraph"]);
const PREFIXES = {
  h1: "# ",
  h2: "## ",
  bullet: "- ",
  number: "1. ",
  quote: "> ",
  todo: "- [ ] ",
  callout: "> 💡 ",
};
// Longest first, so "- [ ] " is recognised before the "- " it starts with.
const KNOWN_PREFIXES = [...Object.values(PREFIXES), "- [x] "].sort((a, b) => b.length - a.length);

/** Formatting on the raw Markdown surface: markers go straight into the text. */
function formatSource(kind) {
  const start = ui.source.selectionStart;
  const end = ui.source.selectionEnd;
  const text = state.content;

  if (WRAPPERS[kind] || kind === "link") {
    const open = kind === "link" ? "[" : WRAPPERS[kind];
    const close = kind === "link" ? "](https://)" : WRAPPERS[kind];
    const operation = new TextOperation()
      .retain(start)
      .insert(open)
      .retain(end - start)
      .insert(close)
      .retain(text.length - end);
    applyLocal(operation);
    ui.source.focus();
    ui.source.selectionStart = start + open.length;
    ui.source.selectionEnd = end + open.length;
    return;
  }

  const prefix = PREFIXES[kind];
  if (!prefix) return;
  const lineStart = text.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
  const existing = KNOWN_PREFIXES.find((candidate) => text.startsWith(candidate, lineStart));
  const operation = new TextOperation().retain(lineStart);
  if (existing) operation.delete(existing.length);
  if (existing !== prefix) operation.insert(prefix);
  operation.retain(text.length - lineStart - (existing ? existing.length : 0));
  applyLocal(operation);
  ui.source.focus();
}

function applyFormat(kind) {
  if (!canType()) return;
  const onSource = state.surface === "source" || currentView() === "markdown";
  if (onSource) {
    formatSource(kind);
    return;
  }
  if (!editor.hasFocus()) editor.focus(editor.selection()?.from ?? state.content.length);
  if (kind === "link") openLinkForm();
  else if (WRAPPERS[kind]) editor.toggleInline(kind);
  else if (BLOCK_TYPES.has(kind)) editor.setType(kind);
}

document.querySelectorAll("[data-format]").forEach((button) => {
  // Keep focus (and the selection) in the editing surface.
  button.addEventListener("mousedown", (event) => event.preventDefault());
  button.addEventListener("click", () => applyFormat(button.dataset.format));
});
ui.undo.addEventListener("mousedown", (event) => event.preventDefault());
ui.redo.addEventListener("mousedown", (event) => event.preventDefault());

ui.source.addEventListener("keydown", (event) => {
  if (!(event.ctrlKey || event.metaKey)) return;
  const key = event.key.toLowerCase();
  if (key === "b" || key === "i" || key === "u") {
    event.preventDefault();
    applyFormat({ b: "bold", i: "italic", u: "underline" }[key]);
  } else if (key === "z") {
    event.preventDefault();
    event.shiftKey ? redo() : undo();
  } else if (key === "s") {
    event.preventDefault();
    saveVersion();
  }
});

// -- saving (SRS 3.3) ------------------------------------------------------

function markDirty() {
  ui.saveState.textContent = "Saving…";
  ui.saveState.classList.add("dirty");
}

function markSaved() {
  if (state.client && state.client.state !== "synchronised") return;
  ui.saveState.textContent = state.doc?.canEdit === false ? "" : "Saved";
  ui.saveState.classList.remove("dirty");
}

function saveVersion() {
  if (!state.doc?.canEdit) return;
  send({ type: "save", label: `Manual save ${new Date().toLocaleTimeString()}` });
}

el("save-now").addEventListener("click", saveVersion);

function renderTitle() {
  const title = ui.docTitle.value.trim() || "Untitled";
  ui.crumbTitle.textContent = title;
  document.title = `${state.doc?.icon ? `${state.doc.icon} ` : ""}${title} · CollabDoc`;
}

ui.docTitle.addEventListener("input", renderTitle);

ui.docTitle.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === "ArrowDown") {
    event.preventDefault();
    ui.docTitle.blur(); // commits the rename through the change event
    if (!state.doc?.canEdit) return;
    if (currentView() === "markdown") {
      ui.source.focus();
      ui.source.selectionStart = ui.source.selectionEnd = 0;
    } else {
      editor.focus(0);
    }
  }
});

ui.docTitle.addEventListener("change", async () => {
  if (!state.doc?.canEdit) return;
  try {
    await api(`/api/documents/${state.doc.id}`, {
      method: "PATCH",
      body: { title: ui.docTitle.value.trim() || "Untitled" },
    });
    await loadDocuments();
  } catch (error) {
    toast(error.message, true);
  }
});

// -- page icon -------------------------------------------------------------

function renderIcon() {
  const icon = state.doc?.icon || "📄";
  ui.pageIcon.textContent = icon;
  ui.crumbIcon.textContent = icon;
  ui.pageIcon.classList.toggle("unset", !state.doc?.icon);
  renderTitle();
}

async function setIcon(icon) {
  if (!state.doc?.canEdit) return;
  ui.emojiPicker.hidden = true;
  try {
    await api(`/api/documents/${state.doc.id}`, { method: "PATCH", body: { icon: icon || "" } });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  state.doc.icon = icon || null;
  renderIcon();
  await loadDocuments();
}

el("emoji-grid").replaceChildren(
  ...EMOJIS.map((emoji) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = emoji;
    button.addEventListener("click", () => setIcon(emoji));
    return button;
  })
);
ui.pageIcon.dataset.popoverAnchor = "";
ui.pageIcon.addEventListener("click", () => {
  if (state.doc?.canEdit) togglePopover(ui.emojiPicker, ui.pageIcon);
});
el("emoji-random").addEventListener("click", () => setIcon(randomIcon()));
el("emoji-remove").addEventListener("click", () => setIcon(""));

// --------------------------------------------------------------------------
// rendering
// --------------------------------------------------------------------------

function initials(name) {
  return (name || "?")
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() || "")
    .join("");
}

function paintAvatar(node, person) {
  node.textContent = initials(person?.name);
  node.style.background = person?.presence_color || "#9b9a97";
  return node;
}

function makeAvatar(person) {
  const node = document.createElement("span");
  node.className = "avatar";
  return paintAvatar(node, person);
}

function findPerson(userId) {
  return (
    state.people.find((p) => p.user_id === userId) ||
    state.collaborators.get(userId) ||
    (userId === state.user?.id ? state.user : null)
  );
}

function authorOf(index) {
  const block = state.blocks[index];
  if (!block || !block.attributed_user_id) return null;
  const person = findPerson(block.attributed_user_id);
  return {
    id: block.attributed_user_id,
    name: person?.name || "Someone",
    color: person?.presence_color || "#9b9a97",
    confidence: block.confidence_score,
    method: block.method,
  };
}

let sideTimer = null;

function renderPreview() {
  locateThreads();
  editor.render();
  renderHighlights();
  // Matches move with every edit; the find bar follows them.
  if (!el("find-bar").hidden) runFind(true);
  // Stats and the outline only matter when their panel is showing.
  clearTimeout(sideTimer);
  sideTimer = setTimeout(() => {
    if (activeTab() === "activity") renderPageStats();
  }, 250);
}

/**
 * Tint the lines of the Markdown surface by author.
 *
 * The layer sits under the transparent textarea and lays out the same text in
 * the same box and font, with the glyphs themselves transparent, so only the
 * backgrounds show. A name label marks where one author's run begins.
 */
function renderHighlights() {
  if (currentView() === "page" && ui.highlight.childElementCount) {
    // Hidden surface: refresh lazily when it is shown.
    ui.highlight.dataset.stale = "1";
    return;
  }
  delete ui.highlight.dataset.stale;
  const lines = [];
  let blockIndex = 0;
  let previousAuthor = null;
  for (const line of state.content.split("\n")) {
    const node = document.createElement("div");
    node.className = "hl-line";
    // A blank line still occupies a line box in the textarea.
    node.textContent = line || ZWSP;
    if (line.trim()) {
      const author = authorOf(blockIndex);
      if (author) {
        node.dataset.author = author.id;
        node.style.setProperty("--author-color", author.color);
        node.style.setProperty("--author-tint", `${author.color}14`);
        if (author.id !== previousAuthor) {
          const who = document.createElement("span");
          who.className = "hl-who";
          const dot = document.createElement("i");
          who.append(dot, author.name);
          node.append(who);
        }
      }
      previousAuthor = author?.id ?? null;
      blockIndex += 1;
    }
    lines.push(node);
  }
  ui.highlight.replaceChildren(...lines);
}

function renderPresence() {
  const seen = new Set();
  const avatars = [];
  for (const person of state.people) {
    if (seen.has(person.user_id)) continue;
    seen.add(person.user_id);
    const avatar = makeAvatar(person);
    avatar.title = `${person.name} (${roleLabel(person.role).toLowerCase()})`;
    if (person.user_id !== state.user?.id) {
      avatar.classList.add("followable");
      avatar.title += " · click to follow";
      avatar.addEventListener("click", () => startFollowing(person));
    }
    if (state.following?.user_id === person.user_id) avatar.classList.add("following");
    avatars.push(avatar);
  }
  ui.presence.replaceChildren(...avatars);

  // Carets belong to connections that are still in the room.
  const live = new Set(state.people.map((p) => p.connection_id));
  for (const id of [...state.carets.keys()]) {
    if (!live.has(id)) state.carets.delete(id);
  }
  renderCarets();
  // Names in the attribution may have been waiting on this list.
  if (state.blocks.length) {
    renderPreview();
    renderAttributionPanel();
  }
}

function caretNode(caret, top, left, height) {
  const node = document.createElement("div");
  node.className = "remote-caret";
  node.style.background = caret.color;
  node.style.top = `${top}px`;
  node.style.left = `${left}px`;
  if (height) node.style.height = `${height}px`;
  node.dataset.name = caret.name;
  return node;
}

/**
 * Position remote carets on whichever surfaces are showing.
 *
 * On the Markdown surface, a hidden mirror of the textarea turns a character
 * offset into pixels. On the page, the block editor maps the offset to a DOM
 * point and the browser measures it.
 */
function renderCarets() {
  const sourceCarets = [];
  const pageCarets = [];
  const view = currentView();
  if (state.carets.size && view !== "page" && ui.source.clientWidth) {
    ui.mirror.style.width = `${ui.source.clientWidth}px`;
    for (const [, caret] of state.carets) {
      const position = Math.min(caret.position, state.content.length);
      ui.mirror.textContent = state.content.slice(0, position);
      const marker = document.createElement("span");
      marker.textContent = ZWSP;
      ui.mirror.append(marker);
      sourceCarets.push(caretNode(caret, marker.offsetTop, marker.offsetLeft));
    }
    ui.mirror.textContent = "";
  }
  if (state.carets.size && view !== "markdown" && ui.editorWrap.clientWidth) {
    const box = ui.editorWrap.getBoundingClientRect();
    for (const [, caret] of state.carets) {
      const rect = editor.caretRect(Math.min(caret.position, state.content.length));
      if (!rect) continue;
      pageCarets.push(caretNode(caret, rect.top - box.top, rect.left - box.left, rect.height));
    }
  }
  ui.cursorLayer.replaceChildren(...sourceCarets);
  ui.pageCursors.replaceChildren(...pageCarets);
}

function applyAttribution(message) {
  state.blocks = message.blocks || [];
  state.model = message.model || {};
  renderPreview();
  renderAttributionPanel();
}

function stat(label, value, small = false) {
  const card = document.createElement("div");
  card.className = "stat";
  const name = document.createElement("div");
  name.className = "label";
  name.textContent = label;
  const number = document.createElement("div");
  number.className = small ? "value small" : "value";
  number.textContent = value;
  card.append(name, number);
  return card;
}

function renderAttributionPanel() {
  const trained = state.model.trained;
  ui.modelState.textContent = trained
    ? `Stylometric model: ${state.model.authors.length} authors, ${state.model.samples} samples`
    : "Keystroke ledger only";

  const blocks = state.blocks;
  const meanConfidence = blocks.length
    ? `${Math.round((blocks.reduce((sum, b) => sum + b.confidence_score, 0) / blocks.length) * 100)}%`
    : "—";

  const stats = document.createElement("div");
  stats.className = "stats";
  stats.append(
    stat("Blocks", blocks.length),
    stat("Mean confidence", meanConfidence),
    stat("Model", trained ? "Trained" : "Learning", true),
    stat(
      trained ? "Training accuracy" : "Samples",
      trained ? `${Math.round(state.model.training_accuracy * 100)}%` : state.model.samples ?? 0,
      !trained
    )
  );

  // Share of the page by characters, so one long paragraph outweighs a heading.
  const totals = new Map();
  let written = 0;
  for (const [index, block] of blocks.entries()) {
    const author = authorOf(index);
    if (!author) continue;
    const size = (block.content || "").length;
    const entry = totals.get(author.id) || { author, size: 0 };
    entry.size += size;
    totals.set(author.id, entry);
    written += size;
  }
  const ranked = [...totals.values()].sort((a, b) => b.size - a.size);

  const nodes = [stats];
  if (ranked.length) {
    const heading = document.createElement("h3");
    heading.textContent = "Contributors";
    const bar = document.createElement("div");
    bar.className = "share-bar";
    const list = document.createElement("ul");
    list.className = "contributors";
    for (const { author, size } of ranked) {
      const share = written ? size / written : 0;
      const segment = document.createElement("span");
      segment.style.width = `${share * 100}%`;
      segment.style.background = author.color;
      segment.title = `${author.name} ${Math.round(share * 100)}%`;
      bar.append(segment);

      const row = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = author.name;
      const pct = document.createElement("span");
      pct.className = "pct";
      pct.textContent = `${Math.round(share * 100)}%`;
      row.append(makeAvatar({ name: author.name, presence_color: author.color }), name, pct);
      list.append(row);
    }
    nodes.push(heading, bar, list);
  }
  if (!trained && state.model.reason) {
    const note = document.createElement("p");
    note.className = "model-note";
    note.textContent = state.model.reason;
    nodes.push(note);
  }
  ui.attributionSummary.replaceChildren(...nodes);

  ui.blockList.replaceChildren(
    ...blocks.map((block, index) => {
      const author = authorOf(index);
      const item = document.createElement("li");
      if (author) item.style.setProperty("--author-color", author.color);

      const text = document.createElement("div");
      text.className = "text";
      text.textContent = block.content || "(empty)";

      const foot = document.createElement("div");
      foot.className = "foot";
      const who = document.createElement("span");
      who.textContent = author ? author.name : "Unattributed";
      const score = document.createElement("span");
      score.className = "meter";
      const track = document.createElement("span");
      track.className = "confidence-track";
      const bar = document.createElement("span");
      bar.className = "confidence";
      bar.style.width = `${Math.round(block.confidence_score * 100)}%`;
      track.append(bar);
      score.append(track, `${Math.round(block.confidence_score * 100)}%`);
      score.title = `${block.block_type} · ${block.method}${
        block.evidence?.agree ? " · style and keystrokes agree" : ""
      }`;
      foot.append(who, score);

      item.append(text, foot);
      item.addEventListener("click", () =>
        revealLine(lineOfBlock(index), fromServerOffset(state.content, block.end))
      );
      return item;
    })
  );
}

function lineOfBlock(index) {
  let seen = -1;
  const lines = state.content.split("\n");
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim()) seen += 1;
    if (seen === index) return i;
  }
  return -1;
}

function lineStart(index) {
  const lines = state.content.split("\n");
  let offset = 0;
  for (let i = 0; i < index && i < lines.length; i += 1) offset += lines[i].length + 1;
  return offset;
}

/** Scroll to a line and flash it, in whichever surface is showing. */
function revealLine(index, caret) {
  if (index < 0) return;
  const target =
    currentView() === "markdown"
      ? ui.highlight.querySelectorAll(".hl-line")[index]
      : ui.preview.children[index];
  if (!target) return;
  target.scrollIntoView({ block: "center", behavior: "smooth" });
  target.classList.remove("flash");
  void target.offsetWidth; // restart the animation
  target.classList.add("flash");
  if (state.doc?.canEdit && currentView() !== "markdown") {
    const lines = state.content.split("\n");
    const end = lineStart(index) + (lines[index]?.length ?? 0);
    editor.focus(Math.min(caret ?? end, end));
  }
}

ui.showAttribution.addEventListener("change", () => {
  ui.app.classList.toggle("attribution-on", ui.showAttribution.checked);
  prefs.set("attribution", ui.showAttribution.checked ? "on" : "off");
});
ui.showAttribution.checked = prefs.get("attribution", "on") !== "off";
ui.app.classList.toggle("attribution-on", ui.showAttribution.checked);

// --------------------------------------------------------------------------
// floating editor UI: slash menu, selection bubble, block handle
// --------------------------------------------------------------------------

function renderSlashMenu(menu) {
  if (!menu) {
    ui.slashMenu.hidden = true;
    return;
  }
  const nodes = menu.items.map((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = index === menu.index ? "slash-item active" : "slash-item";
    button.setAttribute("role", "option");
    const icon = document.createElement("span");
    icon.className = "slash-icon";
    icon.textContent = item.icon;
    if (item.person) {
      icon.classList.add("person");
      icon.style.background = item.color || "var(--ink-3)";
    }
    const text = document.createElement("span");
    text.className = "slash-text";
    const label = document.createElement("b");
    label.textContent = item.label;
    const hint = document.createElement("span");
    hint.textContent = item.hint;
    text.append(label, hint);
    button.append(icon, text);
    button.addEventListener("mousedown", (event) => event.preventDefault());
    button.addEventListener("click", () => menu.choose(item));
    return button;
  });
  if (!nodes.length) {
    const empty = document.createElement("div");
    empty.className = "menu-label";
    empty.textContent = menu.kind === "mention" ? "Nobody here by that name" : "No matching blocks";
    nodes.push(empty);
  } else {
    const label = document.createElement("div");
    label.className = "menu-label";
    label.textContent = menu.kind === "mention" ? "Mention someone with access" : "Blocks";
    nodes.unshift(label);
  }
  ui.slashMenu.replaceChildren(...nodes);
  if (menu.rect) placePopover(ui.slashMenu, menu.rect, { gap: 4 });
  ui.slashMenu.querySelector(".active")?.scrollIntoView?.({ block: "nearest" });
}

let linkSelection = null;

function hideBubble() {
  ui.bubble.hidden = true;
  ui.linkForm.hidden = true;
  ui.bubbleActions.hidden = false;
  linkSelection = null;
}

function updateBubble() {
  if (!ui.linkForm.hidden) return; // the link field has focus; keep the bubble
  const selection = editor.hasFocus() ? editor.selection() : null;
  const native = window.getSelection?.();
  if (!selection || selection.from === selection.to || !native?.rangeCount || currentView() === "markdown") {
    ui.bubble.hidden = true;
    return;
  }
  const canEdit = Boolean(state.doc?.canEdit);
  ui.bubbleActions.querySelectorAll("[data-inline]").forEach((button) => {
    button.hidden = !canEdit && !["comment", "ai"].includes(button.dataset.inline);
  });
  ui.bubbleActions.querySelector(".sep").hidden = !canEdit;
  const rect = native.getRangeAt(0).getBoundingClientRect();
  ui.bubble.hidden = false;
  const box = ui.bubble.getBoundingClientRect();
  const left = Math.max(8, Math.min(rect.left + rect.width / 2 - box.width / 2, window.innerWidth - box.width - 8));
  const top = rect.top - box.height - 8 > 8 ? rect.top - box.height - 8 : rect.bottom + 8;
  ui.bubble.style.left = `${left}px`;
  ui.bubble.style.top = `${top}px`;
}

function openLinkForm() {
  const selection = editor.selection();
  if (!selection) return;
  const existing = editor.linkAt(selection);
  if (!existing && selection.from === selection.to) {
    toast("Select some text to link");
    return;
  }
  updateBubble();
  linkSelection = selection;
  ui.bubble.hidden = false;
  ui.bubbleActions.hidden = true;
  ui.linkForm.hidden = false;
  el("link-remove").hidden = !existing;
  ui.linkInput.value = existing?.url || "";
  ui.linkInput.focus();
  ui.linkInput.select();
}

ui.linkForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const url = ui.linkInput.value.trim();
  const selection = linkSelection;
  hideBubble();
  if (!selection) return;
  editor.focus();
  editor.setLink(url, selection);
});

el("link-remove").addEventListener("click", () => {
  const selection = linkSelection;
  hideBubble();
  if (!selection) return;
  editor.focus();
  editor.setLink("", selection);
});

ui.linkInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    const selection = linkSelection;
    hideBubble();
    if (selection) editor.focus();
  }
});

ui.bubbleActions.addEventListener("mousedown", (event) => event.preventDefault());
ui.bubbleActions.addEventListener("click", (event) => {
  const button = event.target.closest("[data-inline]");
  if (!button) return;
  const kind = button.dataset.inline;
  if (kind === "link") openLinkForm();
  else if (kind === "comment") startComment();
  else if (kind === "ai") openAssistant(state.doc?.canEdit ? "improve" : "summarize");
  else editor.toggleInline(kind);
});

function markFocusedLine() {
  const selection = editor.hasFocus() ? editor.selection() : null;
  const current = ui.preview.querySelector(".line.focused");
  const index = selection ? editor.lineIndexAt(selection.from) : -1;
  const next = index >= 0 ? ui.preview.children[index] : null;
  if (current !== next) {
    current?.classList.remove("focused");
    next?.classList.add("focused");
  }
}

document.addEventListener("selectionchange", () => {
  if (!editor.hasFocus()) {
    if (!ui.bubble.contains(document.activeElement)) ui.bubble.hidden = true;
    ui.preview.querySelector(".line.focused")?.classList.remove("focused");
    return;
  }
  state.surface = "editor";
  editor.updateSlash();
  markFocusedLine();
  updateBubble();
  sendCursor();
});

ui.preview.addEventListener("keydown", () => {
  ui.blockHandle.hidden = true;
});

// Clicking the empty space under the last block continues the page.
ui.editorWrap.addEventListener("mousedown", (event) => {
  if (event.target !== ui.editorWrap || !state.doc?.canEdit) return;
  const last = ui.preview.lastElementChild?.getBoundingClientRect();
  if (last && event.clientY > last.bottom) {
    event.preventDefault();
    editor.focus(state.content.length);
  }
});

// -- block handle ----------------------------------------------------------

const handle = { index: -1, dragging: false, startY: 0, target: -1 };

function lineAtY(clientY) {
  const lines = ui.preview.children;
  for (let i = 0; i < lines.length; i += 1) {
    const rect = lines[i].getBoundingClientRect();
    if (clientY >= rect.top - 2 && clientY <= rect.bottom + 2) return i;
  }
  return -1;
}

ui.editorWrap.addEventListener("mousemove", (event) => {
  if (!state.doc?.canEdit || handle.dragging || currentView() === "markdown") return;
  if (event.target.closest?.("#block-handle")) return;
  const index = lineAtY(event.clientY);
  if (index < 0) {
    ui.blockHandle.hidden = true;
    return;
  }
  handle.index = index;
  const line = ui.preview.children[index];
  const rect = line.getBoundingClientRect();
  const box = ui.editorWrap.getBoundingClientRect();
  const content = line.querySelector(".c") || line;
  const firstLine = parseFloat(window.getComputedStyle(content).lineHeight) || 24;
  const contentTop = content.getBoundingClientRect().top;
  ui.blockHandle.hidden = false;
  ui.blockHandle.style.top = `${contentTop - box.top + Math.max(0, (Math.min(firstLine, rect.height) - 24) / 2)}px`;
});

ui.editorWrap.addEventListener("mouseleave", () => {
  if (!handle.dragging && ui.blockMenu.hidden) ui.blockHandle.hidden = true;
});

el("handle-add").addEventListener("mousedown", (event) => event.preventDefault());
el("handle-add").addEventListener("click", () => {
  if (handle.index < 0) return;
  editor.insertBlockAfter(handle.index);
  // Open the block menu on the new line, as if "/" had been typed.
  const selection = editor.selection();
  if (selection) editor.insertText(selection.from, selection.to, "/");
});

const dragButton = el("handle-drag");
dragButton.dataset.popoverAnchor = "";

dragButton.addEventListener("pointerdown", (event) => {
  if (handle.index < 0) return;
  event.preventDefault();
  dragButton.setPointerCapture?.(event.pointerId);
  handle.dragging = false;
  handle.pressed = true;
  handle.startY = event.clientY;
  handle.target = -1;
});

dragButton.addEventListener("pointermove", (event) => {
  if (!handle.pressed) return;
  if (!handle.dragging && Math.abs(event.clientY - handle.startY) < 4) return;
  handle.dragging = true;
  ui.app.classList.add("dragging");
  const lines = ui.preview.children;
  let target = lines.length;
  for (let i = 0; i < lines.length; i += 1) {
    const rect = lines[i].getBoundingClientRect();
    if (event.clientY < rect.top + rect.height / 2) {
      target = i;
      break;
    }
  }
  handle.target = target;
  const box = ui.editorWrap.getBoundingClientRect();
  const reference = lines[Math.min(target, lines.length - 1)]?.getBoundingClientRect();
  if (!reference) return;
  const y = target < lines.length ? reference.top : reference.bottom;
  ui.dropLine.hidden = false;
  ui.dropLine.style.top = `${y - box.top - 1}px`;
});

dragButton.addEventListener("pointerup", () => {
  if (!handle.pressed) return;
  handle.pressed = false;
  ui.dropLine.hidden = true;
  ui.app.classList.remove("dragging");
  if (handle.dragging) {
    handle.dragging = false;
    if (handle.target >= 0) editor.moveLines([handle.index], handle.target);
    return;
  }
  openBlockMenu(handle.index);
});

const TURN_INTO = SLASH_ITEMS.filter((item) => !["divider", "date"].includes(item.id));

function menuButton(label, icon, action, className = "menu-item") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  const glyph = document.createElement("span");
  glyph.className = "menu-glyph";
  glyph.textContent = icon;
  button.append(glyph, label);
  button.addEventListener("mousedown", (event) => event.preventDefault());
  button.addEventListener("click", () => {
    ui.blockMenu.hidden = true;
    action();
  });
  return button;
}

function openBlockMenu(index) {
  if (!ui.blockMenu.hidden) {
    ui.blockMenu.hidden = true;
    return;
  }
  const line = state.content.split("\n")[index] ?? "";
  const parsed = parseLine(line);
  const label = (text) => {
    const node = document.createElement("div");
    node.className = "menu-label";
    node.textContent = text;
    return node;
  };
  const turnInto = TURN_INTO.map((item) => {
    const button = menuButton(item.label, item.icon, () => editor.setType(item.id, [index]));
    if (parsed.type === item.id) button.classList.add("current");
    return button;
  });
  const lineOffset = lineStart(index);
  ui.blockMenu.replaceChildren(
    label("Turn into"),
    ...(parsed.type === "divider" ? [] : turnInto),
    Object.assign(document.createElement("div"), { className: "menu-sep" }),
    menuButton("Comment", "💬", () => {
      editor.focus(lineOffset + line.length);
      startComment(line.trim());
    }),
    menuButton("Duplicate", "⧉", () => editor.duplicate([index])),
    menuButton("Move up", "↑", () => editor.moveLines([index], index - 1)),
    menuButton("Move down", "↓", () => editor.moveLines([index], index + 2)),
    menuButton("Delete", "🗑", () => editor.deleteLines([index]), "menu-item danger")
  );
  closeMenus(ui.blockMenu);
  placePopover(ui.blockMenu, dragButton.getBoundingClientRect());
}

// --------------------------------------------------------------------------
// inspector tabs
// --------------------------------------------------------------------------

function activeTab() {
  return document.querySelector("#tabs button.active")?.dataset.tab;
}

el("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (!button) return;
  document
    .querySelectorAll("#tabs button")
    .forEach((tab) => tab.classList.toggle("active", tab === button));
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.hidden = panel.dataset.panel !== button.dataset.tab;
  });
  const tab = button.dataset.tab;
  if (tab === "history") loadVersions();
  if (tab === "comments") loadComments();
  if (tab === "activity") {
    renderPageStats();
    loadActivity();
  }
  if (tab === "share") {
    loadPeople();
    loadShareLink();
  }
});

// -- comments ----------------------------------------------------------------

let pendingAnchor = "";

function setAnchor(anchor) {
  pendingAnchor = anchor.slice(0, 300);
  ui.commentAnchor.hidden = !pendingAnchor;
  ui.commentAnchor.textContent = pendingAnchor;
  ui.commentHint.textContent = pendingAnchor
    ? "Commenting on the quoted text."
    : "Select text on the page to comment on it.";
}

function startComment(anchor) {
  if (anchor === undefined) {
    const selection = editor.selection();
    anchor = selection ? state.content.slice(selection.from, selection.to).split("\n")[0].trim() : "";
  }
  hideBubble();
  focusPanel("comments", { toggle: false });
  setAnchor(anchor);
  ui.commentBody.focus();
}

async function loadComments() {
  if (!state.doc) return;
  const documentId = state.doc.id;
  let comments;
  try {
    comments = await api(`/api/documents/${documentId}/comments`);
  } catch {
    return;
  }
  if (state.doc?.id !== documentId) return;
  state.comments = comments;
  renderThreads();
  renderPreview();
}

/** Which line each open thread's quoted text is on now. */
function locateThreads() {
  state.commentLines = new Map();
  const lines = state.content.split("\n");
  for (const thread of state.comments) {
    if (thread.parent_id || thread.resolved || !thread.anchor) continue;
    // An edited quote is found again by similarity, so a typo fix does not
    // orphan the discussion about that sentence.
    const found = locateQuote(lines, thread.anchor, thread.line ?? -1);
    const index = found.index;
    thread.line = index;
    thread.moved = index >= 0 && !found.exact;
    if (index >= 0) state.commentLines.set(index, (state.commentLines.get(index) || 0) + 1);
  }
}

function threadsByRoot() {
  const roots = state.comments.filter((comment) => !comment.parent_id);
  const replies = new Map();
  for (const comment of state.comments) {
    if (!comment.parent_id) continue;
    if (!replies.has(comment.parent_id)) replies.set(comment.parent_id, []);
    replies.get(comment.parent_id).push(comment);
  }
  return roots.map((root) => ({ root, replies: replies.get(root.id) || [] }));
}

function commentNode(comment) {
  const item = document.createElement("div");
  item.className = "comment";
  const head = document.createElement("div");
  head.className = "comment-head";
  const name = document.createElement("b");
  name.textContent = comment.user.name;
  const when = document.createElement("span");
  when.textContent = formatWhen(comment.created_at);
  when.title = parseTime(comment.created_at).toLocaleString();
  head.append(makeAvatar(comment.user), name, when);
  const body = document.createElement("p");
  body.textContent = comment.body;
  item.append(head, body, reactionRow(comment));
  return item;
}

const REACTIONS = ["👍", "❤️", "🎉", "😄", "👀", "🚀"];

/** Emoji chips under a comment: click one to add or take back yours. */
function reactionRow(comment) {
  const row = document.createElement("div");
  row.className = "reactions";
  const reactions = comment.reactions || {};
  const toggle = async (emoji) => {
    const mine = (reactions[emoji] || []).includes(state.user?.id);
    try {
      const updated = await api(
        `/api/documents/${state.doc.id}/comments/${comment.id}/reactions/${encodeURIComponent(emoji)}`,
        { method: mine ? "DELETE" : "PUT" }
      );
      comment.reactions = updated.reactions;
      renderThreads();
    } catch (error) {
      toast(error.message, true);
    }
  };
  for (const [emoji, people] of Object.entries(reactions)) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "reaction";
    chip.classList.toggle("mine", people.includes(state.user?.id));
    chip.textContent = `${emoji} ${people.length}`;
    chip.title = people.map((id) => findPerson(id)?.name || "Someone").join(", ");
    chip.addEventListener("click", () => toggle(emoji));
    row.append(chip);
  }
  const add = document.createElement("button");
  add.type = "button";
  add.className = "reaction add";
  add.title = "Add a reaction";
  add.textContent = "☺+";
  const picker = document.createElement("span");
  picker.className = "reaction-picker";
  picker.hidden = true;
  for (const emoji of REACTIONS) {
    const option = document.createElement("button");
    option.type = "button";
    option.textContent = emoji;
    option.addEventListener("click", () => {
      picker.hidden = true;
      toggle(emoji);
    });
    picker.append(option);
  }
  add.addEventListener("click", () => (picker.hidden = !picker.hidden));
  row.append(add, picker);
  return row;
}

function renderThreads() {
  const threads = threadsByRoot();
  const open = threads.filter((thread) => !thread.root.resolved);
  ui.commentCount.hidden = open.length === 0;
  ui.commentCount.textContent = String(open.length);
  const visible = ui.showResolved.checked ? threads : open;
  // Newest threads first; resolved ones sink.
  visible.sort(
    (a, b) =>
      Number(a.root.resolved) - Number(b.root.resolved) ||
      parseTime(b.root.created_at) - parseTime(a.root.created_at)
  );

  if (!visible.length) {
    const empty = document.createElement("li");
    empty.className = "empty-threads";
    empty.textContent = threads.length ? "No open comments." : "No comments yet.";
    ui.threadList.replaceChildren(empty);
    return;
  }

  ui.threadList.replaceChildren(
    ...visible.map(({ root, replies }) => {
      const item = document.createElement("li");
      item.className = root.resolved ? "thread resolved" : "thread";
      item.dataset.id = root.id;
      if (root.anchor) {
        const quote = document.createElement("blockquote");
        quote.className = "anchor-quote";
        quote.textContent = root.anchor;
        if (root.line === -1 && !root.resolved) {
          quote.classList.add("orphan");
          quote.title = "The quoted text has since been removed";
        } else {
          if (root.moved) quote.classList.add("moved");
          quote.title = root.moved ? "The quote was edited; showing the closest match" : "Show on page";
          quote.addEventListener("click", () => {
            const found = locateQuote(state.content.split("\n"), root.anchor, root.line ?? -1);
            if (found.index >= 0) revealLine(found.index);
          });
        }
        item.append(quote);
      }
      item.append(commentNode(root), ...replies.map(commentNode));

      const actions = document.createElement("div");
      actions.className = "thread-actions";
      const reply = document.createElement("button");
      reply.type = "button";
      reply.className = "link";
      reply.textContent = "Reply";
      const canResolve = state.doc?.canEdit || root.user.id === state.user?.id;
      const resolve = document.createElement("button");
      resolve.type = "button";
      resolve.className = "link";
      resolve.textContent = root.resolved ? "Reopen" : "Resolve";
      resolve.hidden = !canResolve;
      resolve.addEventListener("click", () => updateThread(root.id, { resolved: !root.resolved }));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "link subtle";
      remove.textContent = "Delete";
      remove.hidden = !(root.user.id === state.user?.id || state.doc?.role === "OWNER");
      remove.addEventListener("click", () => deleteThread(root.id, remove));
      actions.append(reply, resolve, remove);
      item.append(actions);

      const form = document.createElement("form");
      form.className = "reply-form";
      form.hidden = true;
      const input = document.createElement("input");
      input.placeholder = "Reply…";
      input.maxLength = 4000;
      const send = document.createElement("button");
      send.type = "submit";
      send.className = "primary small";
      send.textContent = "Reply";
      form.append(input, send);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!input.value.trim()) return;
        await postComment({ body: input.value, parent_id: root.id });
      });
      reply.addEventListener("click", () => {
        form.hidden = !form.hidden;
        if (!form.hidden) input.focus();
      });
      item.append(form);
      return item;
    })
  );
}

async function postComment(payload) {
  try {
    await api(`/api/documents/${state.doc.id}/comments`, { method: "POST", body: payload });
  } catch (error) {
    toast(error.message, true);
    return false;
  }
  await loadComments();
  return true;
}

async function updateThread(id, body) {
  try {
    await api(`/api/documents/${state.doc.id}/comments/${id}`, { method: "PATCH", body });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  loadComments();
}

async function deleteThread(id, control) {
  if (control.dataset.confirm !== "1") {
    control.dataset.confirm = "1";
    control.textContent = "Click again to delete";
    setTimeout(() => {
      control.dataset.confirm = "";
      control.textContent = "Delete";
    }, 3000);
    return;
  }
  try {
    await api(`/api/documents/${state.doc.id}/comments/${id}`, { method: "DELETE" });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  loadComments();
}

ui.commentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = ui.commentBody.value.trim();
  if (!body || !state.doc) return;
  if (await postComment({ body, anchor: pendingAnchor })) {
    ui.commentBody.value = "";
    setAnchor("");
  }
});

ui.commentBody.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    ui.commentForm.requestSubmit();
  }
  if (event.key === "Escape") setAnchor("");
});

ui.showResolved.addEventListener("change", renderThreads);

function showThreadsForLine(index) {
  focusPanel("comments", { toggle: false });
  const line = state.content.split("\n")[index] ?? "";
  const threads = state.comments.filter(
    (comment) => !comment.parent_id && !comment.resolved && comment.anchor && line.includes(comment.anchor)
  );
  const first = threads[0] && ui.threadList.querySelector(`[data-id="${threads[0].id}"]`);
  for (const thread of threads) {
    const node = ui.threadList.querySelector(`[data-id="${thread.id}"]`);
    node?.classList.remove("flash");
    void node?.offsetWidth;
    node?.classList.add("flash");
  }
  first?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// -- activity ----------------------------------------------------------------

let activityTimer = null;

function scheduleActivity() {
  clearTimeout(activityTimer);
  activityTimer = setTimeout(loadActivity, 1500);
}

function renderPageStats() {
  const stats = pageStats(state.content);
  const container = el("page-stats");
  container.replaceChildren(
    stat("Words", stats.words.toLocaleString()),
    stat("Reading time", `${stats.minutes} min`),
    stat("Blocks", stats.blocks.toLocaleString()),
    stat("To-dos done", stats.todos ? `${stats.done}/${stats.todos}` : "—")
  );

  const items = outline(state.content);
  const list = el("outline");
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "panel-hint";
    empty.textContent = "Add headings to build an outline.";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(
    ...items.map((item) => {
      const entry = document.createElement("li");
      entry.className = `level-${item.level}`;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = item.text;
      button.addEventListener("click", () => {
        const index = state.content.slice(0, item.offset).split("\n").length - 1;
        revealLine(index, item.offset);
      });
      entry.append(button);
      return entry;
    })
  );
}

async function loadActivity() {
  if (!state.doc) return;
  const documentId = state.doc.id;
  let activity;
  try {
    activity = await api(`/api/documents/${documentId}/activity`);
  } catch {
    return;
  }
  if (state.doc?.id !== documentId) return;
  el("activity-bucket").textContent = activity.buckets.length
    ? `per ${bucketLabel(activity.bucket_seconds)}`
    : "";
  renderActivityChart(el("activity-chart"), el("activity-tip"), activity);

  el("activity-legend").replaceChildren(
    ...activity.authors.map((author) => {
      const item = document.createElement("li");
      const key = document.createElement("i");
      key.style.background = author.presence_color;
      item.append(key, author.name);
      return item;
    })
  );
  const body = el("activity-table").querySelector("tbody");
  body.replaceChildren(
    ...activity.authors.map((author) => {
      const row = document.createElement("tr");
      const cells = [
        author.name,
        author.sessions.toLocaleString(),
        formatDuration(author.active_seconds),
        author.inserted ? `+${author.inserted.toLocaleString()}` : "—",
        author.deleted ? `−${author.deleted.toLocaleString()}` : "—",
      ];
      for (const value of cells) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      }
      return row;
    })
  );
  el("activity-table").hidden = activity.authors.length === 0;

  // Sharing and lifecycle events are editor-only (they name everyone who
  // was ever given access); viewers see the editing sessions alone.
  let audit = [];
  if (state.doc.canEdit) {
    try {
      audit = await api(`/api/documents/${documentId}/audit?limit=30`);
    } catch {
      audit = [];
    }
    if (state.doc?.id !== documentId) return;
  }
  renderActivityFeed(activityFeed(activity.sessions || [], audit));
}

function renderActivityFeed(items) {
  const list = el("activity-feed");
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "panel-hint";
    empty.textContent = "Nothing has happened on this page yet.";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(
    ...items.map((item) => {
      const entry = document.createElement("li");
      entry.className = `feed-${item.kind}`;
      const body = document.createElement("span");
      body.className = "feed-text";
      const who = document.createElement("b");
      who.textContent = item.user.id === state.user?.id ? "You" : item.user.name;
      body.append(who, ` ${item.text}`);
      const when = document.createElement("span");
      when.className = "feed-when";
      when.textContent = formatWhen(item.at);
      when.title = item.at.toLocaleString();
      entry.append(makeAvatar(item.user), body, when);
      return entry;
    })
  );
}

// -- version history (WBS 4.2) --------------------------------------------

async function loadVersions() {
  if (!state.doc) return;
  let versions;
  try {
    versions = await api(`/api/documents/${state.doc.id}/versions?limit=100`);
  } catch {
    return;
  }
  if (!versions.length) {
    const empty = document.createElement("li");
    empty.className = "v";
    empty.textContent = "No edits yet.";
    ui.versionList.replaceChildren(empty);
    return;
  }
  ui.versionList.replaceChildren(
    ...versions.map((entry) => {
      const item = document.createElement("li");
      const person = findPerson(entry.user_id) || {
        name: entry.user_name || "Unknown",
        presence_color: null,
      };
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = entry.user_name || person.name;
      const version = document.createElement("span");
      version.className = "v";
      version.textContent = `v${entry.number} · ${
        entry.label
          ? `“${entry.label}”`
          : `${entry.operation_count} ${entry.operation_count === 1 ? "edit" : "edits"}`
      }`;
      const when = document.createElement("span");
      when.className = "v";
      when.textContent = formatWhen(entry.timestamp);
      when.title = parseTime(entry.timestamp).toLocaleString();
      item.append(makeAvatar(person), who, version, when);
      item.addEventListener("click", () => showVersion(entry.version, entry));
      return item;
    })
  );
}

async function showVersion(version, entry) {
  let payload;
  try {
    payload = await api(`/api/documents/${state.doc.id}/versions/${version}`);
  } catch (error) {
    toast(error.message, true);
    return;
  }
  const name = entry ? `Version ${entry.number}` : "Version";
  ui.versionTitle.textContent = entry?.label ? `${name} · “${entry.label}”` : name;
  state.version = { content: payload.content || "", version, entry };
  renderVersionBody();
  ui.versionRestore.hidden = !state.doc.canEdit;
  ui.versionRestore.onclick = async () => {
    try {
      await api(`/api/documents/${state.doc.id}/revert`, {
        method: "POST",
        body: { version },
      });
      closeVersion();
      toast(entry ? `Restored version ${entry.number}` : "Restored");
      loadVersions();
    } catch (error) {
      toast(error.message, true);
    }
  };
  ui.versionModal.hidden = false;
}

function closeVersion() {
  ui.versionModal.hidden = true;
}

el("version-close").addEventListener("click", closeVersion);
el("version-cancel").addEventListener("click", closeVersion);

// -- sharing (WBS 4.2) -----------------------------------------------------

async function loadPeople() {
  if (!state.doc) return;
  let people;
  try {
    people = await api(`/api/documents/${state.doc.id}/collaborators`);
  } catch {
    return;
  }
  state.collaborators = new Map(people.map((entry) => [entry.user.id, entry.user]));
  renderDocMeta();
  if (state.blocks.length) {
    renderPreview();
    renderAttributionPanel();
  }

  ui.peopleList.replaceChildren(
    ...people.map((entry) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.className = "grow";
      const name = document.createElement("b");
      name.textContent =
        entry.user.id === state.user?.id ? `${entry.user.name} (you)` : entry.user.name;
      const email = document.createElement("span");
      email.textContent = entry.user.email;
      label.append(name, email);

      item.append(makeAvatar(entry.user), label);

      if (state.doc.role === "OWNER" && entry.role !== "OWNER" && !entry.inherited) {
        const select = document.createElement("select");
        for (const role of ["EDITOR", "VIEWER"]) {
          const option = document.createElement("option");
          option.value = role;
          option.textContent = roleLabel(role);
          option.selected = entry.role === role;
          select.append(option);
        }
        select.addEventListener("change", async () => {
          try {
            await api(`/api/documents/${state.doc.id}/collaborators/${entry.user.id}`, {
              method: "PATCH",
              body: { role: select.value },
            });
          } catch (error) {
            toast(error.message, true);
            return;
          }
          toast(`${entry.user.name} ${roleLabel(select.value).toLowerCase()} now`);
          loadDocuments();
        });

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "link";
        remove.textContent = "Remove";
        remove.addEventListener("click", async () => {
          try {
            await api(`/api/documents/${state.doc.id}/collaborators/${entry.user.id}`, {
              method: "DELETE",
            });
          } catch (error) {
            toast(error.message, true);
            return;
          }
          toast(`Removed ${entry.user.name}`);
          loadPeople();
          loadDocuments();
        });
        item.append(select, remove);
      } else {
        const chip = document.createElement("span");
        chip.className = "role-chip";
        chip.textContent = roleLabel(entry.role);
        if (entry.inherited) {
          chip.textContent += " · via parent";
          chip.title = "Access comes from a page this one is nested in";
        }
        item.append(chip);
      }
      return item;
    })
  );
}

el("invite-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api(`/api/documents/${state.doc.id}/collaborators`, {
      method: "POST",
      body: { email: el("invite-email").value.trim(), role: el("invite-role").value },
    });
    el("invite-email").value = "";
    toast("Invited");
    loadPeople();
    loadDocuments();
  } catch (error) {
    toast(error.message, true);
  }
});

async function loadShareLink() {
  if (!state.doc || state.doc.role !== "OWNER") {
    ui.linkUrl.value = "";
    ui.shareNote.textContent = "Only the owner can manage link sharing.";
    ui.linkRole.disabled = true;
    return;
  }
  ui.linkRole.disabled = false;
  let link;
  try {
    link = await api(`/api/documents/${state.doc.id}/share`);
  } catch {
    return;
  }
  ui.linkRole.value = link.share_role || "";
  ui.linkUrl.value = link.url || "";
  ui.shareNote.textContent = link.url
    ? "Anyone with this link can join at the role above."
    : "Link sharing is off. Turning it on issues a new link; the previous one stops working.";
}

ui.linkRole.addEventListener("change", async () => {
  let link;
  try {
    link = await api(`/api/documents/${state.doc.id}/share`, {
      method: "PUT",
      body: { role: ui.linkRole.value || null },
    });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  ui.linkUrl.value = link.url || "";
  ui.shareNote.textContent = link.url
    ? "Anyone with this link can join at the role above."
    : "Link sharing is off. The previous link no longer works.";
});

async function copyText(text, done) {
  try {
    await navigator.clipboard.writeText(text);
    toast(done);
    return true;
  } catch {
    return false;
  }
}

el("copy-link").addEventListener("click", async () => {
  if (!ui.linkUrl.value) return;
  if (!(await copyText(ui.linkUrl.value, "Link copied"))) {
    ui.linkUrl.select();
    toast("Press Ctrl+C to copy");
  }
});

// --------------------------------------------------------------------------
// page menu: export, duplicate, delete
// --------------------------------------------------------------------------

const moreButton = el("page-more");
moreButton.dataset.popoverAnchor = "";

moreButton.addEventListener("click", () => {
  if (!state.doc) return;
  const stats = pageStats(state.content);
  el("more-stats").textContent = `${stats.words.toLocaleString()} words · ${stats.minutes} min read · ${stats.blocks} blocks`;
  togglePopover(ui.moreMenu, moreButton, { align: "right" });
});

ui.moreMenu.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || !state.doc) return;
  const action = button.dataset.action;
  const title = ui.docTitle.value.trim() || "Untitled";
  if (action === "delete-page") {
    const listed = state.documents.find((doc) => doc.id === state.doc.id);
    if (listed) confirmDelete(listed, button);
    return;
  }
  ui.moreMenu.hidden = true;
  if (action === "copy-link") {
    const url = `${location.origin}${location.pathname}?doc=${state.doc.id}`;
    if (!(await copyText(url, "Page link copied"))) toast(url);
  } else if (action === "integrity") {
    await openIntegrity();
  } else if (action === "duplicate-page") {
    const created = await createDocument({
      title: `${title} (copy)`,
      content: state.content,
      icon: state.doc.icon,
    });
    if (created) toast("Page duplicated");
  } else if (action === "duplicate-tree") {
    await duplicatePage(true);
  } else if (action === "toggle-lock") {
    await toggleLock();
  } else if (action === "add-subpage") {
    setExpanded(state.doc.id, true);
    await createDocument({ parentId: state.doc.id });
  } else if (action === "move-top") {
    await movePage(state.doc.id, null);
  } else if (action === "export-md") {
    download(fileName(title, "md"), "text/markdown", toMarkdown(title, state.doc.icon, state.content));
  } else if (action === "export-html") {
    download(fileName(title, "html"), "text/html", toHtml(title, state.doc.icon, state.content, await inlineImages()));
  } else if (action === "print") {
    setView("page");
    frame(() => window.print());
  }
});

// --------------------------------------------------------------------------
// publish to web
// --------------------------------------------------------------------------

function showPublished(info) {
  ui.publishToggle.checked = info.published;
  ui.publishUrl.value = info.url || "";
  ui.openPublish.hidden = !info.url;
  if (info.url) ui.openPublish.href = info.url;
  ui.publishNote.textContent = info.published
    ? "Search engines are asked not to index it. Turn this off to take it down at once."
    : "A read-only copy, rendered by the server -- comments, history and authorship stay private.";
}

async function loadPublish() {
  const owner = state.doc?.role === "OWNER";
  ui.publishToggle.disabled = !owner;
  if (!owner) {
    showPublished({ published: false, url: null });
    ui.publishNote.textContent = "Only the owner can publish this page.";
    return;
  }
  try {
    showPublished(await api(`/api/documents/${state.doc.id}/publish`));
  } catch {
    /* the toggle stays as it was */
  }
}

ui.publishToggle.addEventListener("change", async () => {
  if (!state.doc) return;
  try {
    const info = await api(`/api/documents/${state.doc.id}/publish`, {
      method: "PUT",
      body: { published: ui.publishToggle.checked },
    });
    showPublished(info);
    toast(info.published ? "Published — anyone with the link can read it" : "Unpublished");
    loadDocuments();
  } catch (error) {
    ui.publishToggle.checked = !ui.publishToggle.checked;
    toast(error.message, true);
  }
});

el("copy-publish").addEventListener("click", async () => {
  if (!ui.publishUrl.value) return;
  if (!(await copyText(ui.publishUrl.value, "Public link copied"))) {
    ui.publishUrl.select();
    toast("Press Ctrl+C to copy");
  }
});

// --------------------------------------------------------------------------
// inbox: mentions, replies, invitations
// --------------------------------------------------------------------------

const NOTIFICATION_VERB = {
  MENTION: "mentioned you in",
  COMMENT_REPLY: "replied to your comment in",
  SHARED: "shared",
};

function renderInboxCount() {
  const unread = state.notifications.filter((note) => !note.read).length;
  ui.inboxCount.hidden = unread === 0;
  ui.inboxCount.textContent = unread > 99 ? "99+" : String(unread);
  ui.inbox.title = unread ? `${unread} unread` : "Inbox";
}

async function loadNotifications() {
  try {
    state.notifications = await api("/api/notifications");
  } catch {
    return;
  }
  renderInboxCount();
  if (!ui.inboxMenu.hidden) renderInbox();
}

function renderInbox() {
  if (!state.notifications.length) {
    const empty = document.createElement("li");
    empty.className = "inbox-empty";
    empty.textContent = "You're all caught up. Mentions, replies and invitations land here.";
    ui.inboxList.replaceChildren(empty);
    return;
  }
  ui.inboxList.replaceChildren(
    ...state.notifications.map((note) => {
      const item = document.createElement("li");
      item.className = note.read ? "inbox-item" : "inbox-item unread";
      const body = document.createElement("div");
      body.className = "inbox-body";
      const headline = document.createElement("div");
      const who = document.createElement("b");
      who.textContent = note.actor.name;
      const page = document.createElement("b");
      page.textContent = `${note.document_icon || "📄"} ${note.document_title || "Untitled"}`;
      headline.append(who, ` ${NOTIFICATION_VERB[note.kind] || "updated"} `, page);
      body.append(headline);
      if (note.snippet && note.kind !== "SHARED") {
        const snippet = document.createElement("div");
        snippet.className = "inbox-snippet";
        snippet.textContent = note.snippet;
        body.append(snippet);
      }
      const when = document.createElement("div");
      when.className = "inbox-when";
      when.textContent = formatWhen(note.created_at);
      body.append(when);
      item.append(makeAvatar(note.actor), body);
      item.addEventListener("click", () => openNotification(note));
      return item;
    })
  );
}

async function markRead(ids) {
  for (const note of state.notifications) if (!ids || ids.includes(note.id)) note.read = true;
  renderInboxCount();
  if (!ui.inboxMenu.hidden) renderInbox();
  try {
    await api("/api/notifications/read", { method: "POST", body: ids ? { ids } : {} });
  } catch {
    /* the next load will show the true state */
  }
}

async function openNotification(note) {
  ui.inboxMenu.hidden = true;
  if (!note.read) markRead([note.id]);
  if (!state.documents.some((doc) => doc.id === note.document_id)) await loadDocuments();
  await openDocument(note.document_id);
  if (note.comment_id) focusPanel("comments", { toggle: false });
}

ui.inbox.addEventListener("click", (event) => {
  event.stopPropagation();
  if (togglePopover(ui.inboxMenu, ui.inbox)) {
    renderInbox();
    loadNotifications();
  }
});
el("inbox-read-all").addEventListener("click", () => markRead(null));

function closeUserSocket() {
  const socket = state.userSocket;
  state.userSocket = null;
  if (socket) {
    socket.onclose = null;
    socket.close();
  }
}

/** The per-user socket: notifications arrive here while the app is open. */
function connectUserSocket(delay = 1500) {
  closeUserSocket();
  if (!state.token || typeof WebSocket === "undefined") return;
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  let socket;
  try {
    socket = new WebSocket(`${protocol}://${location.host}/ws/me?token=${encodeURIComponent(state.token)}`);
  } catch {
    return;
  }
  state.userSocket = socket;
  socket.onmessage = (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type !== "notification") return;
    const note = message.notification;
    state.notifications = [note, ...state.notifications.filter((n) => n.id !== note.id)];
    renderInboxCount();
    if (!ui.inboxMenu.hidden) renderInbox();
    toast(`${note.actor.name} ${NOTIFICATION_VERB[note.kind] || "updated"} ${note.document_title || "a page"}`);
    // A page shared with you belongs in the sidebar straight away.
    if (note.kind === "SHARED") loadDocuments();
  };
  socket.onclose = (event) => {
    if (state.userSocket !== socket) return;
    state.userSocket = null;
    if (event.code === 4401) return; // the session ended; signing in reconnects
    // Back off, so a server restart is not met with a reconnect storm.
    setTimeout(() => {
      if (state.token && !state.userSocket) connectUserSocket(Math.min(delay * 2, 30000));
    }, delay);
  };
}

// --------------------------------------------------------------------------
// import Markdown
// --------------------------------------------------------------------------

/** A .md file as a page: its first "# " heading becomes the title. */
function pageFromMarkdown(name, text) {
  let content = text.replace(/\r\n?/g, "\n").replace(/^﻿/, "");
  let title = name.replace(/\.(md|markdown|txt)$/i, "").trim() || "Imported page";
  const heading = /^# (.+)\n*/.exec(content);
  if (heading) {
    title = heading[1].trim();
    content = content.slice(heading[0].length);
  }
  // Markdown separates blocks with blank lines; the editor treats every line
  // as a block, so collapse paragraph breaks -- outside code blocks, whose
  // blank lines are content.
  let inCode = false;
  const lines = [];
  for (const line of content.split("\n")) {
    if (/^\s*```/.test(line)) inCode = !inCode;
    if (!inCode && !line.trim() && lines.length && !lines[lines.length - 1].trim()) continue;
    lines.push(line);
  }
  return { title: title.slice(0, 255), content: lines.join("\n").trim() };
}

el("import-md").addEventListener("click", () => ui.importInput.click());
ui.importInput.addEventListener("change", async () => {
  const files = [...ui.importInput.files];
  ui.importInput.value = "";
  for (const file of files) {
    if (file.size > 2 * 1024 * 1024) {
      toast(`${file.name} is too large to import`, true);
      continue;
    }
    const page = pageFromMarkdown(file.name, await file.text());
    const created = await createDocument(page);
    if (created) toast(`Imported ${page.title}`);
  }
});

// --------------------------------------------------------------------------
// version compare
// --------------------------------------------------------------------------

/** The previewed version, plain or as a line diff against the page now. */
function renderVersionBody() {
  if (!state.version) return;
  const { content } = state.version;
  let nodes;
  if (ui.versionCompare.checked) {
    const changes = diffLines(content, state.content);
    nodes = changes
      .filter((change) => change.type !== "same" || change.text.trim())
      .map((change) => {
        const node = renderLine(change.text);
        node.classList.add(`diff-${change.type}`);
        return node;
      });
    const added = changes.filter((change) => change.type === "add").length;
    const removed = changes.filter((change) => change.type === "del").length;
    ui.versionSub.textContent = added || removed
      ? `Since this version: ${added} line${added === 1 ? "" : "s"} added, ${removed} removed`
      : "Identical to the current page";
  } else {
    nodes = renderDocument(content || "", { skipBlank: true });
    const entry = state.version.entry;
    ui.versionSub.textContent = entry
      ? `${entry.user_name || "Unknown"} · ${parseTime(entry.timestamp).toLocaleString()}`
      : "";
  }
  if (!nodes.length) {
    const empty = document.createElement("p");
    empty.className = "panel-hint";
    empty.textContent = "The page was empty at this version.";
    nodes.push(empty);
  }
  ui.versionContent.classList.toggle("diff", ui.versionCompare.checked);
  ui.versionContent.replaceChildren(...nodes);
}

ui.versionCompare.addEventListener("change", renderVersionBody);

// --------------------------------------------------------------------------
// templates, shortcuts, and the command palette
// --------------------------------------------------------------------------

function openModal(modal) {
  closeMenus();
  modal.hidden = false;
}

document.querySelectorAll(".modal").forEach((modal) => {
  modal.addEventListener("mousedown", (event) => {
    if (event.target === modal) modal.hidden = true;
  });
  modal.querySelectorAll("[data-close]").forEach((button) =>
    button.addEventListener("click", () => (modal.hidden = true))
  );
});

el("template-grid").replaceChildren(
  ...TEMPLATES.map((template) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "template-card";
    const preview = document.createElement("div");
    preview.className = "template-preview";
    const icon = document.createElement("span");
    icon.className = "template-icon";
    icon.textContent = template.icon;
    const lines = document.createElement("div");
    lines.className = "template-lines";
    lines.append(...renderDocument(template.content(), { skipBlank: true }).slice(0, 6));
    preview.append(icon, lines);
    const name = document.createElement("b");
    name.textContent = template.title;
    const description = document.createElement("span");
    description.textContent = template.description;
    card.append(preview, name, description);
    card.addEventListener("click", async () => {
      ui.templates.hidden = true;
      const created = await createDocument({
        title: template.title,
        content: template.content(),
        icon: template.id === "blank" ? null : template.icon,
      });
      if (created && template.id === "blank") {
        ui.docTitle.focus();
        ui.docTitle.select();
      }
    });
    return card;
  })
);

el("open-templates").addEventListener("click", () => openModal(ui.templates));
el("empty-templates").addEventListener("click", () => openModal(ui.templates));
el("show-shortcuts").addEventListener("click", () => openModal(ui.shortcuts));

const palette = { items: [], index: 0, timer: null, query: "" };

function paletteActions() {
  const dark = effectiveTheme() === "dark";
  const actions = [
    { label: "New page", icon: "＋", run: createBlank },
    { label: "Browse templates", icon: "▦", run: () => openModal(ui.templates) },
    { label: dark ? "Switch to light theme" : "Switch to dark theme", icon: dark ? "☀" : "☾", run: () => applyTheme(dark ? "light" : "dark") },
    { label: "Toggle sidebar", icon: "☰", run: toggleSidebar },
    { label: "Keyboard shortcuts", icon: "⌨", run: () => openModal(ui.shortcuts) },
    { label: "Open inbox", icon: "🔔", run: () => ui.inbox.click() },
    { label: "Open trash", icon: "🗑", run: openTrash },
    { label: "Import Markdown file", icon: "⬆", run: () => ui.importInput.click() },
    { label: "Home", icon: "⌂", run: goHome },
    { label: "My tasks", icon: "☑", run: openTasks },
    { label: "Graph view", icon: "🕸", run: openGraph },
    { label: "Security & API tokens", icon: "🔐", run: openSecurity },
  ];
  if (state.doc) {
    actions.push(
      { label: ui.showAttribution.checked ? "Hide author colours" : "Show author colours", icon: "◐", run: () => ui.showAttribution.click() },
      { label: "Open comments", icon: "💬", run: () => focusPanel("comments", { toggle: false }) },
      { label: "Open activity", icon: "📈", run: () => focusPanel("activity", { toggle: false }) },
      { label: "Version history", icon: "🕘", run: () => focusPanel("history", { toggle: false }) },
      { label: "Share page", icon: "👥", run: () => focusPanel("share", { toggle: false }) },
      { label: "Export as Markdown", icon: "⬇", run: () => ui.moreMenu.querySelector('[data-action="export-md"]').click() },
      { label: "Export as HTML", icon: "⬇", run: () => ui.moreMenu.querySelector('[data-action="export-html"]').click() },
      { label: "Print or save as PDF", icon: "⎙", run: () => ui.moreMenu.querySelector('[data-action="print"]').click() },
      { label: "AI: Summarize this page", icon: "✨", run: () => openAssistant("summarize") },
      { label: "Find in page", icon: "🔍", run: () => openFind(false) },
      { label: "Replay the page's history", icon: "▶", run: openReplay },
      { label: "Duplicate with sub-pages", icon: "⧉", run: () => duplicatePage(true) },
      { label: currentView() === "markdown" ? "Show formatted page" : "Show Markdown", icon: "⇄", run: () => setView(currentView() === "markdown" ? "page" : "markdown") }
    );
    if (state.doc.canEdit) {
      actions.push(
        { label: "Save a version", icon: "💾", run: saveVersion },
        { label: "AI: Continue writing", icon: "✨", run: () => openAssistant("continue") },
        { label: "AI: Improve writing", icon: "✨", run: () => openAssistant("improve") },
        { label: "Find and replace", icon: "🔁", run: () => openFind(true) },
        { label: "Add a sub-page", icon: "📄", run: () => createDocument({ parentId: state.doc.id }) }
      );
    }
  }
  return actions;
}

function openPalette() {
  openModal(ui.palette);
  ui.paletteInput.value = "";
  palette.index = 0;
  renderPalette([]);
  ui.paletteInput.focus();
}

function closePalette() {
  ui.palette.hidden = true;
  clearTimeout(palette.timer);
}

function matches(text, query) {
  const haystack = text.toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((word) => haystack.includes(word));
}

function renderPalette(hits) {
  const query = ui.paletteInput.value.trim();
  palette.query = query;
  const pages = [];
  const seen = new Set();
  const recent = state.documents;
  for (const doc of recent) {
    if (query && !matches(doc.title, query)) continue;
    seen.add(doc.id);
    pages.push({
      kind: "page",
      label: doc.title || "Untitled",
      icon: doc.icon || "📄",
      hint: doc.id === state.doc?.id ? "Current page" : formatWhen(doc.updated_at),
      run: () => openDocument(doc.id),
    });
  }
  for (const hit of hits) {
    if (seen.has(hit.document_id)) {
      const existing = pages.find((page) => page.label === hit.title);
      if (existing && hit.snippet) existing.snippet = hit.snippet;
      continue;
    }
    pages.push({
      kind: "page",
      label: hit.title,
      icon: hit.icon || "📄",
      snippet: hit.snippet,
      hint: formatWhen(hit.updated_at),
      run: () => openDocument(hit.document_id),
    });
  }
  const actions = paletteActions()
    .filter((action) => !query || matches(action.label, query))
    .map((action) => ({ ...action, kind: "action" }));

  const items = [...pages.slice(0, query ? 12 : 6), ...actions.slice(0, query ? 8 : 5)];
  palette.items = items;
  palette.index = Math.min(palette.index, Math.max(0, items.length - 1));

  const nodes = [];
  let lastKind = null;
  items.forEach((item, index) => {
    if (item.kind !== lastKind) {
      const label = document.createElement("li");
      label.className = "palette-label";
      label.textContent = item.kind === "page" ? (query ? "Pages" : "Recent pages") : "Actions";
      nodes.push(label);
      lastKind = item.kind;
    }
    const row = document.createElement("li");
    row.className = index === palette.index ? "palette-item active" : "palette-item";
    row.setAttribute("role", "option");
    const icon = document.createElement("span");
    icon.className = "palette-icon";
    icon.textContent = item.icon;
    const text = document.createElement("span");
    text.className = "palette-text";
    const label = document.createElement("b");
    label.textContent = item.label;
    text.append(label);
    if (item.snippet) {
      const snippet = document.createElement("span");
      snippet.textContent = item.snippet.replace(/[*_`~#>]|\[ \]|\[x\]/g, "");
      text.append(snippet);
    }
    row.append(icon, text);
    if (item.hint) {
      const hint = document.createElement("span");
      hint.className = "palette-hint";
      hint.textContent = item.hint;
      row.append(hint);
    }
    row.addEventListener("mousemove", () => {
      if (palette.index === index) return;
      palette.index = index;
      ui.paletteResults
        .querySelectorAll(".palette-item")
        .forEach((node, k) => node.classList.toggle("active", k === index));
    });
    row.addEventListener("click", () => runPaletteItem(item));
    nodes.push(row);
  });
  if (!items.length) {
    const empty = document.createElement("li");
    empty.className = "palette-empty";
    empty.textContent = `No results for “${query}”`;
    nodes.push(empty);
  }
  ui.paletteResults.replaceChildren(...nodes);
  ui.paletteResults.querySelector(".active")?.scrollIntoView({ block: "nearest" });
}

function runPaletteItem(item) {
  closePalette();
  item.run();
}

ui.paletteInput.addEventListener("input", () => {
  palette.index = 0;
  renderPalette([]);
  clearTimeout(palette.timer);
  const query = ui.paletteInput.value.trim();
  if (query.length < 2) return;
  palette.timer = setTimeout(async () => {
    let hits = [];
    try {
      hits = await api(`/api/search?q=${encodeURIComponent(query)}`);
    } catch {
      return;
    }
    if (ui.paletteInput.value.trim() === query && !ui.palette.hidden) renderPalette(hits);
  }, 180);
});

ui.paletteInput.addEventListener("keydown", (event) => {
  const count = palette.items.length;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    if (!count) return;
    palette.index = (palette.index + (event.key === "ArrowDown" ? 1 : -1) + count) % count;
    ui.paletteResults
      .querySelectorAll(".palette-item")
      .forEach((node, k) => node.classList.toggle("active", k === palette.index));
    ui.paletteResults.querySelector(".active")?.scrollIntoView({ block: "nearest" });
  } else if (event.key === "Enter") {
    event.preventDefault();
    const item = palette.items[palette.index];
    if (item) runPaletteItem(item);
  } else if (event.key === "Escape") {
    event.preventDefault();
    closePalette();
  }
});

el("open-search").addEventListener("click", openPalette);

// --------------------------------------------------------------------------
// writing assistant
// --------------------------------------------------------------------------

const ai = { action: null, target: null, result: null, request: 0 };

/**
 * Open the assistant on the selection (or the whole page) and run `action`.
 *
 * The target passage is remembered by its text as well as its offsets, so
 * an Insert or Replace after collaborators have edited the page still lands
 * on the right words -- or is refused if they are gone.
 */
function openAssistant(action) {
  if (!state.doc) return;
  const selection = editor.hasFocus() ? editor.selection() : null;
  const from = selection && selection.from !== selection.to ? selection.from : null;
  ai.target = from === null
    ? { from: 0, to: state.content.length, text: "", whole: true }
    : { from: selection.from, to: selection.to, text: state.content.slice(selection.from, selection.to), whole: false };
  openModal(el("ai-modal"));
  runAssistant(state.doc.canEdit ? action : "summarize");
}

async function runAssistant(action) {
  ai.action = action;
  ai.result = null;
  const request = (ai.request += 1);
  el("ai-actions").querySelectorAll("[data-ai]").forEach((button) => {
    button.classList.toggle("active", button.dataset.ai === action);
    button.disabled = !state.doc.canEdit && button.dataset.ai !== "summarize";
  });
  el("ai-sub").textContent = ai.target.whole ? "Working on the whole page" : `Working on the selection (${ai.target.text.length} characters)`;
  el("ai-status").textContent = "Thinking…";
  el("ai-status").className = "ai-status busy";
  el("ai-result").replaceChildren();
  el("ai-source").textContent = "";
  setAssistantButtons(false);
  let reply;
  try {
    reply = await api(`/api/documents/${state.doc.id}/assist`, {
      method: "POST",
      body: { action, text: ai.target.text },
    });
  } catch (error) {
    if (request !== ai.request) return;
    el("ai-status").textContent = error.message;
    el("ai-status").className = "ai-status error";
    return;
  }
  if (request !== ai.request) return;
  ai.result = reply.text;
  el("ai-status").textContent = "";
  el("ai-status").className = "ai-status";
  el("ai-result").replaceChildren(...renderDocument(reply.text, { skipBlank: true }));
  el("ai-source").textContent = reply.source === "claude" ? `Written by Claude${reply.model ? ` (${reply.model})` : ""}` : "Local summary (no AI service configured)";
  el("ai-source").className = `ai-source ${reply.source}`;
  setAssistantButtons(true);
}

function setAssistantButtons(ready) {
  const canEdit = Boolean(state.doc?.canEdit);
  el("ai-copy").disabled = !ready;
  el("ai-insert").disabled = !ready || !canEdit;
  el("ai-replace").disabled = !ready || !canEdit || ai.target?.whole;
  el("ai-replace").hidden = Boolean(ai.target?.whole);
}

/** Where the remembered passage is now, or null if it has been edited away. */
function assistantTarget() {
  const { from, to, text, whole } = ai.target;
  if (whole) return { from: 0, to: state.content.length };
  if (state.content.slice(from, to) === text) return { from, to };
  const at = state.content.indexOf(text);
  return at >= 0 ? { from: at, to: at + text.length } : null;
}

el("ai-actions").addEventListener("click", (event) => {
  const button = event.target.closest("[data-ai]");
  if (button && !button.disabled) runAssistant(button.dataset.ai);
});

el("ai-insert").addEventListener("click", () => {
  const target = assistantTarget();
  if (!ai.result || !target) {
    toast("That passage has changed since you asked", true);
    return;
  }
  // Below the last line of the passage, as its own block(s).
  const lineEnd = state.content.indexOf("\n", target.to);
  const at = lineEnd < 0 ? state.content.length : lineEnd;
  const insert = `${at > 0 ? "\n" : ""}${ai.result}`;
  el("ai-modal").hidden = true;
  editor.focus();
  editor.replace(at, at, insert);
  toast("Inserted — Ctrl+Z to undo");
});

el("ai-replace").addEventListener("click", () => {
  const target = assistantTarget();
  if (!ai.result || !target) {
    toast("That passage has changed since you asked", true);
    return;
  }
  el("ai-modal").hidden = true;
  editor.focus();
  editor.replace(target.from, target.to, ai.result);
  toast("Replaced — Ctrl+Z to undo");
});

el("ai-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(ai.result || "");
    toast("Copied");
  } catch {
    toast("Could not copy", true);
  }
});

el("ai-button").addEventListener("mousedown", (event) => event.preventDefault());
el("ai-button").addEventListener("click", () => openAssistant("summarize"));

// --------------------------------------------------------------------------
// sign-in: providers, redirects and the second factor
// --------------------------------------------------------------------------

let pendingMfa = null;

function showMfaStep(token) {
  pendingMfa = token;
  el("mfa-field").hidden = false;
  ui.authHeading.textContent = "Two-factor sign-in";
  ui.authSubmit.textContent = "Verify";
  el("auth-code").value = "";
  el("auth-code").focus();
  for (const input of [ui.authEmail, ui.authPassword]) input.closest(".field").hidden = true;
  ui.authEmail.required = false;
  ui.authPassword.required = false;
}

function endMfaStep() {
  pendingMfa = null;
  el("mfa-field").hidden = true;
  for (const input of [ui.authEmail, ui.authPassword]) input.closest(".field").hidden = false;
  ui.authEmail.required = true;
  ui.authPassword.required = true;
  ui.authHeading.textContent = registerMode ? "Create your account" : "Welcome back";
  ui.authSubmit.textContent = registerMode ? "Create account" : "Continue";
}

async function loadProviders() {
  let providers = [];
  try {
    providers = await (await fetch("/api/auth/providers")).json();
  } catch {
    providers = [];
  }
  if (!Array.isArray(providers)) providers = [];
  el("oauth").hidden = !providers.length;
  el("oauth-buttons").replaceChildren(
    ...providers.map((provider) => {
      const link = document.createElement("a");
      link.className = `oauth-button ${provider.id}`;
      link.href = `/api/auth/oauth/${encodeURIComponent(provider.id)}/start`;
      link.textContent = `Continue with ${provider.label}`;
      return link;
    })
  );
}

/** A provider sign-in comes back as /#oauth_token=… (or an error, or a 2FA step). */
function takeRedirect() {
  const found = hashParams(location.hash);
  if (!Object.keys(found).length) return;
  history.replaceState({}, "", location.pathname + location.search);
  if (found.oauth_token) {
    state.token = found.oauth_token;
    localStorage.setItem("collabdoc.token", state.token);
  } else if (found.mfa_token) {
    showMfaStep(found.mfa_token);
  } else if (found.oauth_error) {
    ui.authError.textContent = found.oauth_error;
    ui.authError.hidden = false;
  }
}

// --------------------------------------------------------------------------
// security: two-factor sign-in and API tokens
// --------------------------------------------------------------------------

const security = { enabled: false, enrolling: false };

async function openSecurity() {
  closeMenus();
  openModal(el("security-modal"));
  try {
    const me = await api("/api/auth/me");
    security.enabled = Boolean(me.totp_enabled);
  } catch {
    return;
  }
  security.enrolling = false;
  renderSecurity();
  loadTokens();
}

function renderSecurity() {
  el("mfa-state").textContent = security.enabled ? "On" : "Off";
  el("mfa-state").className = `tag ${security.enabled ? "owner" : ""}`;
  el("mfa-setup").hidden = !security.enrolling;
  el("mfa-code").hidden = !(security.enabled || security.enrolling);
  el("mfa-code").value = "";
  el("mfa-submit").textContent = security.enabled ? "Turn off" : security.enrolling ? "Confirm" : "Turn on";
  el("mfa-submit").classList.toggle("danger", security.enabled);
  el("mfa-hint").textContent = security.enabled
    ? "Signing in asks for a code from your authenticator app. Enter a current code to turn it off."
    : "After your password, also ask for a 6-digit code from an authenticator app.";
}

el("open-security").addEventListener("click", openSecurity);

el("mfa-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = el("mfa-code").value.trim();
  try {
    if (security.enabled) {
      await api("/api/auth/2fa/disable", { method: "POST", body: { code } });
      security.enabled = false;
      toast("Two-factor sign-in is off");
    } else if (security.enrolling) {
      await api("/api/auth/2fa/enable", { method: "POST", body: { code } });
      security.enabled = true;
      security.enrolling = false;
      toast("Two-factor sign-in is on");
    } else {
      const setup = await api("/api/auth/2fa/setup", { method: "POST" });
      el("mfa-secret").textContent = setup.secret.replace(/(.{4})/g, "$1 ").trim();
      el("mfa-uri").href = setup.uri;
      security.enrolling = true;
    }
  } catch (error) {
    toast(error.message, true);
    return;
  }
  renderSecurity();
  if (security.enrolling) el("mfa-code").focus();
});

async function loadTokens() {
  let tokens = [];
  try {
    tokens = await api("/api/me/tokens");
  } catch {
    return;
  }
  const list = el("token-list");
  if (!tokens.length) {
    const empty = document.createElement("li");
    empty.className = "panel-hint";
    empty.textContent = "No tokens yet.";
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(
    ...tokens.map((token) => {
      const item = document.createElement("li");
      const label = document.createElement("span");
      label.className = "grow";
      const name = document.createElement("b");
      name.textContent = token.name;
      const meta = document.createElement("span");
      meta.textContent = `${token.prefix}… · ${token.last_used_at ? `used ${formatWhen(token.last_used_at)}` : "never used"}`;
      label.append(name, meta);
      const revoke = document.createElement("button");
      revoke.type = "button";
      revoke.className = "small danger";
      revoke.textContent = "Revoke";
      revoke.addEventListener("click", async () => {
        try {
          await api(`/api/me/tokens/${token.id}`, { method: "DELETE" });
        } catch (error) {
          toast(error.message, true);
          return;
        }
        toast(`Revoked ${token.name}`);
        loadTokens();
      });
      item.append(label, revoke);
      return item;
    })
  );
}

el("token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  let created;
  try {
    created = await api("/api/me/tokens", { method: "POST", body: { name: el("token-name").value.trim() } });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  el("token-name").value = "";
  el("token-value").textContent = created.token;
  el("token-new").hidden = false;
  loadTokens();
});

el("token-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(el("token-value").textContent);
    toast("Token copied. It will not be shown again.");
  } catch {
    toast("Could not copy", true);
  }
});

// --------------------------------------------------------------------------
// backlinks
// --------------------------------------------------------------------------

async function loadBacklinks() {
  const box = el("backlinks");
  box.hidden = true;
  if (!state.doc) return;
  const documentId = state.doc.id;
  let links;
  try {
    links = await api(`/api/documents/${documentId}/backlinks`);
  } catch {
    return;
  }
  if (state.doc?.id !== documentId || !links.length) return;
  el("backlinks-list").replaceChildren(
    ...links.map((link) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "backlink";
      button.textContent = `${link.icon || "📄"} ${link.title}`;
      button.title = link.snippet;
      button.addEventListener("click", () => openDocument(link.document_id));
      return button;
    })
  );
  box.hidden = false;
}

// --------------------------------------------------------------------------
// follow a collaborator
// --------------------------------------------------------------------------

function startFollowing(person) {
  if (state.following?.user_id === person.user_id) {
    stopFollowing();
    return;
  }
  state.following = { user_id: person.user_id, name: person.name };
  paintAvatar(el("follow-avatar"), person);
  el("follow-name").textContent = person.name;
  el("follow-banner").hidden = false;
  el("follow-banner").style.setProperty("--follow-color", person.presence_color);
  renderPresence();
  // Jump to wherever they are now, if their caret is known.
  const caret = [...state.people].find((p) => p.user_id === person.user_id);
  if (caret) followCaret(caret.connection_id);
  toast(`Following ${person.name}. Scroll or press Esc to stop.`);
}

function stopFollowing() {
  if (!state.following) return;
  state.following = null;
  el("follow-banner").hidden = true;
  renderPresence();
}

function followCaret(connectionId) {
  const caret = state.carets.get(connectionId);
  if (!caret) return;
  const index = state.content.slice(0, caret.position).split("\n").length - 1;
  const target =
    currentView() === "markdown" ? ui.highlight.querySelectorAll(".hl-line")[index] : ui.preview.children[index];
  if (!target) return;
  state.followScrolling = true;
  target.scrollIntoView({ block: "center", behavior: "smooth" });
  clearTimeout(state.followTimer);
  state.followTimer = setTimeout(() => (state.followScrolling = false), 700);
}

el("follow-stop").addEventListener("click", stopFollowing);
// Scrolling yourself, or typing, ends follow mode.
ui.scroller.addEventListener("wheel", () => stopFollowing(), { passive: true });
ui.scroller.addEventListener("touchmove", () => stopFollowing(), { passive: true });
ui.preview.addEventListener("keydown", () => stopFollowing());

// --------------------------------------------------------------------------
// find and replace
// --------------------------------------------------------------------------

const finder = { matches: [], index: -1 };

function openFind(replace) {
  if (!state.doc) return;
  const bar = el("find-bar");
  bar.hidden = false;
  el("find-replace-row").hidden = !(replace && state.doc.canEdit);
  bar.classList.toggle("with-replace", Boolean(replace && state.doc.canEdit));
  const selection = editor.hasFocus() ? editor.selection() : null;
  if (selection && selection.from !== selection.to && selection.to - selection.from < 100) {
    el("find-input").value = state.content.slice(selection.from, selection.to);
  }
  el("find-input").focus();
  el("find-input").select();
  runFind();
}

function closeFind() {
  el("find-bar").hidden = true;
  finder.matches = [];
  finder.index = -1;
  paintFind();
}

function runFind(keepIndex = false) {
  if (el("find-bar").hidden) return;
  const query = el("find-input").value;
  finder.matches = findMatches(state.content, query, { caseSensitive: el("find-case").checked });
  if (!keepIndex || finder.index >= finder.matches.length) finder.index = finder.matches.length ? 0 : -1;
  paintFind();
}

/** Highlight matches with the CSS Custom Highlight API, which leaves the DOM alone. */
function paintFind() {
  const count = el("find-count");
  const query = el("find-input").value;
  count.textContent = !query ? "" : finder.matches.length ? `${finder.index + 1} of ${finder.matches.length}` : "No results";
  count.classList.toggle("none", Boolean(query) && !finder.matches.length);
  if (!window.CSS?.highlights || typeof Highlight === "undefined") return;
  CSS.highlights.delete("find");
  CSS.highlights.delete("find-current");
  if (el("find-bar").hidden || !finder.matches.length) return;
  const all = [];
  let current = null;
  finder.matches.forEach((match, i) => {
    const start = editor.pointAt(match.from);
    const end = editor.pointAt(match.to);
    if (!start || !end) return;
    const range = document.createRange();
    try {
      range.setStart(start.node, start.offset);
      range.setEnd(end.node, end.offset);
    } catch {
      return;
    }
    if (i === finder.index) current = range;
    else all.push(range);
  });
  CSS.highlights.set("find", new Highlight(...all));
  if (current) CSS.highlights.set("find-current", new Highlight(current));
}

function stepFind(direction) {
  if (!finder.matches.length) return;
  finder.index = (finder.index + direction + finder.matches.length) % finder.matches.length;
  paintFind();
  const match = finder.matches[finder.index];
  const line = state.content.slice(0, match.from).split("\n").length - 1;
  ui.preview.children[line]?.scrollIntoView({ block: "center", behavior: "smooth" });
}

function replaceCurrent() {
  if (!state.doc?.canEdit || finder.index < 0) return;
  const match = finder.matches[finder.index];
  editor.replace(match.from, match.to, el("replace-input").value);
  runFind(true);
}

function replaceAll() {
  if (!state.doc?.canEdit || !finder.matches.length) return;
  const count = finder.matches.length;
  const next = replaceMatches(state.content, finder.matches, el("replace-input").value);
  // One operation for the lot, so a single undo brings them all back.
  editor.rewrite(next);
  runFind();
  toast(`Replaced ${count} match${count === 1 ? "" : "es"}`);
}

el("find-input").addEventListener("input", () => runFind());
el("find-case").addEventListener("change", () => runFind());
el("find-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    stepFind(event.shiftKey ? -1 : 1);
  }
});
el("replace-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    if (event.ctrlKey || event.metaKey) replaceAll();
    else replaceCurrent();
  }
});
el("find-next").addEventListener("click", () => stepFind(1));
el("find-prev").addEventListener("click", () => stepFind(-1));
el("replace-one").addEventListener("click", replaceCurrent);
el("replace-all").addEventListener("click", replaceAll);
el("find-close").addEventListener("click", closeFind);
el("find-button").addEventListener("click", () => openFind(Boolean(state.doc?.canEdit)));

// --------------------------------------------------------------------------
// history replay (a time-lapse of the operation log)
// --------------------------------------------------------------------------

const replay = { ops: [], names: {}, colors: {}, texts: [], index: 0, timer: null, speed: 4, token: 0 };

async function openReplay() {
  if (!state.doc) return;
  openModal(el("replay-modal"));
  stopReplay();
  const token = (replay.token += 1);
  el("replay-sub").textContent = "Loading the operation log…";
  el("replay-content").replaceChildren();
  replay.ops = [];
  replay.names = {};
  replay.colors = {};
  let after = 0;
  try {
    for (;;) {
      const page = await api(`/api/documents/${state.doc.id}/operations?after=${after}&limit=5000`);
      if (token !== replay.token) return;
      replay.ops.push(...page.operations);
      Object.assign(replay.names, page.names);
      Object.assign(replay.colors, page.colors);
      if (page.next_after === null) break;
      after = page.next_after;
      if (replay.ops.length > 50000) break;
    }
  } catch (error) {
    el("replay-sub").textContent = error.message;
    return;
  }
  // Keyframes every 200 operations make scrubbing instant on long logs.
  replay.keyframes = [""];
  let text = "";
  try {
    replay.ops.forEach((op, i) => {
      text = applyServerDelta(op.delta, text);
      if ((i + 1) % 200 === 0) replay.keyframes.push(text);
    });
  } catch (error) {
    el("replay-sub").textContent = `Could not replay this history: ${error.message}`;
    return;
  }
  const scrub = el("replay-scrub");
  scrub.max = String(replay.ops.length);
  scrub.value = "0";
  replay.index = 0;
  el("replay-sub").textContent = replay.ops.length
    ? `${replay.ops.length.toLocaleString()} operations, ${formatWhen(replay.ops[0].timestamp)} to ${formatWhen(replay.ops[replay.ops.length - 1].timestamp)}`
    : "Nothing has been written on this page yet.";
  drawReplay();
}

/** Apply a logged operation: its offsets count code points, JavaScript's count UTF-16 units. */
function applyServerDelta(delta, text) {
  return fromServerOperation(TextOperation.fromJSON(delta), text).apply(text);
}

function replayText(index) {
  const key = Math.floor(index / 200);
  let text = replay.keyframes[key] ?? "";
  for (let i = key * 200; i < index; i += 1) text = applyServerDelta(replay.ops[i].delta, text);
  return text;
}

function drawReplay() {
  const text = replayText(replay.index);
  el("replay-content").replaceChildren(...renderDocument(text, { skipBlank: true }));
  el("replay-scrub").value = String(replay.index);
  const op = replay.ops[Math.max(0, replay.index - 1)];
  const who = el("replay-who");
  if (op && replay.index > 0) {
    const person = { name: replay.names[op.user_id] || "Unknown", presence_color: replay.colors[op.user_id] || "#9b9a97" };
    const label = document.createElement("span");
    label.textContent = `${person.name} · ${parseTime(op.timestamp).toLocaleString()} · ${replay.index.toLocaleString()} / ${replay.ops.length.toLocaleString()}`;
    who.replaceChildren(makeAvatar(person), label);
    who.style.setProperty("--who-color", person.presence_color);
  } else {
    who.replaceChildren();
  }
}

function stopReplay() {
  clearInterval(replay.timer);
  replay.timer = null;
  el("replay-play").textContent = "▶ Play";
}

function playReplay() {
  if (replay.timer) {
    stopReplay();
    return;
  }
  if (replay.index >= replay.ops.length) replay.index = 0;
  el("replay-play").textContent = "❚❚ Pause";
  replay.timer = setInterval(() => {
    replay.index = Math.min(replay.ops.length, replay.index + replay.speed);
    drawReplay();
    if (replay.index >= replay.ops.length) stopReplay();
  }, 50);
}

function closeReplay() {
  stopReplay();
  replay.token += 1;
  el("replay-modal").hidden = true;
}

el("open-replay").addEventListener("click", openReplay);
el("replay-play").addEventListener("click", playReplay);
el("replay-scrub").addEventListener("input", () => {
  replay.index = Number(el("replay-scrub").value);
  drawReplay();
});
el("replay-speed").addEventListener("click", (event) => {
  const button = event.target.closest("[data-speed]");
  if (!button) return;
  replay.speed = Number(button.dataset.speed);
  el("replay-speed").querySelectorAll("[data-speed]").forEach((b) => b.classList.toggle("active", b === button));
});
el("replay-modal").querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", stopReplay));

// --------------------------------------------------------------------------
// integrity report
// --------------------------------------------------------------------------

let integrityReport = null;

async function openIntegrity() {
  if (!state.doc) return;
  const documentId = state.doc.id;
  openModal(el("integrity-modal"));
  const body = el("integrity-body");
  const downloadButton = el("integrity-download");
  downloadButton.disabled = true;
  integrityReport = null;
  el("integrity-sub").textContent = "Replaying the operation log and re-checking every passage…";
  body.replaceChildren();
  let report;
  try {
    report = await api(`/api/documents/${documentId}/integrity`);
  } catch (error) {
    el("integrity-sub").textContent = error.message;
    return;
  }
  if (state.doc?.id !== documentId) return;
  integrityReport = report;
  el("integrity-sub").textContent = `Who wrote this page, and what a reviewer should look at · version ${report.version}`;
  renderIntegrity(body, report, {
    onJump: (flag) => {
      el("integrity-modal").hidden = true;
      const offset = fromServerOffset(state.content, flag.start);
      revealLine(state.content.slice(0, offset).split("\n").length - 1);
    },
  });
  downloadButton.disabled = false;
}

el("open-integrity").addEventListener("click", openIntegrity);
el("integrity-download").addEventListener("click", () => {
  if (!integrityReport) return;
  const title = ui.docTitle.value.trim() || "Untitled";
  download(fileName(`${title} - integrity report`, "html"), "text/html", integrityHtml(integrityReport));
});

// --------------------------------------------------------------------------
// graph view
// --------------------------------------------------------------------------

const graphView = {
  graph: null,
  positions: new Map(), // id -> {x, y}, kept across redraws so pages do not jump
  transform: { k: 1, x: 0, y: 0 },
  selected: null,
  frame: 0,
  sim: null,
  draw: null, // repaint the current drawing from the simulation
};

async function openGraph() {
  closeMenus();
  openModal(el("graph-modal"));
  const wrap = el("graph-wrap");
  wrap.replaceChildren();
  let graph;
  try {
    graph = await api("/api/graph");
  } catch (error) {
    toast(error.message, true);
    return;
  }
  graphView.graph = graph;
  graphView.positions = new Map();
  graphView.transform = { k: 1, x: 0, y: 0 };
  graphView.selected = null;
  el("graph-search").value = "";
  el("graph-local").disabled = !graph.nodes.some((node) => node.id === state.doc?.id);
  if (el("graph-local").disabled) el("graph-local").checked = false;
  el("graph-sub").textContent = `${graph.nodes.length} page${graph.nodes.length === 1 ? "" : "s"}, ${
    graph.edges.length
  } connection${graph.edges.length === 1 ? "" : "s"}`;
  drawGraph(wrap);
}

/** The pages and edges the current filters let through. */
function visibleGraph() {
  const graph = graphView.graph;
  const kinds = { parent: el("graph-show-parent").checked, link: el("graph-show-link").checked };
  let edges = graph.edges.filter((edge) => kinds[edge.kind]);
  let nodes = graph.nodes;
  if (el("graph-local").checked && state.doc) {
    const keep = neighbourhood(edges, state.doc.id, 2);
    nodes = nodes.filter((node) => keep.has(node.id));
    edges = edges.filter((edge) => keep.has(edge.source) && keep.has(edge.target));
  }
  return { nodes, edges };
}

function drawGraph(wrap) {
  cancelAnimationFrame(graphView.frame);
  const SVGNS = "http://www.w3.org/2000/svg";
  const make = (tag, attributes = {}) => {
    const node = document.createElementNS(SVGNS, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    return node;
  };
  const width = Math.max(480, wrap.clientWidth || 900);
  const height = Math.max(360, wrap.clientHeight || 560);
  const { nodes, edges } = visibleGraph();
  if (!nodes.length) {
    const empty = document.createElement("p");
    empty.className = "panel-hint graph-empty";
    empty.textContent = graphView.graph.nodes.length ? "No pages match these filters." : "No pages yet.";
    wrap.replaceChildren(empty);
    graphView.sim = null;
    return;
  }

  // First open: start from a settled layout, then let it breathe.
  if (!graphView.positions.size) {
    graphView.positions = forceLayout(nodes, edges, { width, height, ticks: 120 });
  }
  const sim = new GraphSimulation(nodes, edges, { width, height, positions: graphView.positions });
  sim.alpha = 0.4;
  graphView.sim = sim;

  const svg = make("svg", { viewBox: `0 0 ${width} ${height}`, class: "graph", role: "img", "aria-label": "Page graph" });
  const defs = make("defs");
  const marker = make("marker", {
    id: "graph-arrow",
    viewBox: "0 0 10 10",
    refX: "9",
    refY: "5",
    markerWidth: "7",
    markerHeight: "7",
    orient: "auto-start-reverse",
  });
  marker.append(make("path", { d: "M0 1 9 5 0 9z", class: "arrow" }));
  defs.append(marker);
  const viewport = make("g", { class: "viewport" });
  const edgeLayer = make("g", { class: "edges" });
  const nodeLayer = make("g", { class: "nodes" });
  viewport.append(edgeLayer, nodeLayer);
  svg.append(defs, viewport);

  // Who touches whom over the visible edges, for hover and the info card.
  const around = new Map(graphView.graph.nodes.map((node) => [node.id, new Set()]));
  for (const edge of edges) {
    around.get(edge.source)?.add(edge.target);
    around.get(edge.target)?.add(edge.source);
  }
  const maxWords = Math.max(1, ...nodes.map((node) => node.words));
  const radius = new Map(nodes.map((node) => [node.id, 7 + 15 * Math.sqrt(node.words / maxWords)]));

  const lines = edges.map((edge) => {
    const line = make("line", { class: `edge ${edge.kind}` });
    if (edge.kind === "link") line.setAttribute("marker-end", "url(#graph-arrow)");
    edgeLayer.append(line);
    return { edge, line };
  });
  const groups = new Map();

  const toGraph = (event) => {
    const box = svg.getBoundingClientRect();
    const scale = width / (box.width || width);
    const t = graphView.transform;
    return {
      x: ((event.clientX - box.left) * scale - t.x) / t.k,
      y: ((event.clientY - box.top) * scale - t.y) / t.k,
    };
  };

  const paint = () => {
    const t = graphView.transform;
    viewport.setAttribute("transform", `translate(${t.x},${t.y}) scale(${t.k})`);
    svg.classList.toggle("zoomed-out", t.k < 0.6);
    for (const [id, group] of groups) {
      const p = sim.points.get(id);
      group.setAttribute("transform", `translate(${p.x},${p.y})`);
    }
    for (const { edge, line } of lines) {
      const a = sim.points.get(edge.source);
      const b = sim.points.get(edge.target);
      // Stop at the circles' rims so arrowheads land on the edge of a page.
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.max(1, Math.hypot(dx, dy));
      const from = radius.get(edge.source);
      const to = radius.get(edge.target) + (edge.kind === "link" ? 3 : 0);
      line.setAttribute("x1", a.x + (dx / d) * from);
      line.setAttribute("y1", a.y + (dy / d) * from);
      line.setAttribute("x2", b.x - (dx / d) * to);
      line.setAttribute("y2", b.y - (dy / d) * to);
    }
  };
  graphView.draw = paint;

  const run = () => {
    cancelAnimationFrame(graphView.frame);
    const step = () => {
      if (el("graph-modal").hidden || graphView.sim !== sim) return;
      sim.tick();
      paint();
      for (const [id, p] of sim.points) graphView.positions.set(id, { x: p.x, y: p.y });
      graphView.frame = sim.settled ? 0 : requestAnimationFrame(step);
    };
    graphView.frame = requestAnimationFrame(step);
  };

  const light = (id) => {
    svg.classList.toggle("hovering", Boolean(id));
    const near = id ? around.get(id) : new Set();
    for (const [other, group] of groups) group.classList.toggle("lit", other === id || near.has(other));
    for (const { edge, line } of lines) {
      line.classList.toggle("lit", Boolean(id) && (edge.source === id || edge.target === id));
    }
  };

  for (const node of nodes) {
    const r = radius.get(node.id);
    const group = make("g", {
      class: `node${node.id === state.doc?.id ? " current" : ""}`,
      tabindex: "0",
      "data-id": node.id,
    });
    const circle = make("circle", { r });
    const label = make("text", { y: r + 14, "text-anchor": "middle" });
    label.textContent = `${node.icon || ""} ${node.title}`.trim().slice(0, 32);
    const title = make("title");
    title.textContent = `${node.title} · ${node.words.toLocaleString()} words`;
    group.append(circle, label, title);
    groups.set(node.id, group);

    let drag = null;
    group.addEventListener("pointerdown", (event) => {
      event.stopPropagation();
      drag = { x: event.clientX, y: event.clientY, moved: false };
      group.setPointerCapture?.(event.pointerId);
    });
    group.addEventListener("pointermove", (event) => {
      if (!drag) return;
      drag.moved ||= Math.abs(event.clientX - drag.x) + Math.abs(event.clientY - drag.y) > 3;
      if (!drag.moved) return;
      const at = toGraph(event);
      sim.pin(node.id, at.x, at.y);
      sim.reheat(0.3);
      group.classList.add("dragging");
      if (!graphView.frame) run();
    });
    const release = () => {
      if (!drag) return;
      const moved = drag.moved;
      drag = null;
      group.classList.remove("dragging");
      sim.unpin(node.id);
      sim.reheat(0.1);
      if (!graphView.frame) run();
      if (!moved) selectGraphNode(node.id);
    };
    group.addEventListener("pointerup", release);
    group.addEventListener("pointercancel", release);
    group.addEventListener("dblclick", () => openFromGraph(node.id));
    group.addEventListener("pointerenter", () => !drag && light(node.id));
    group.addEventListener("pointerleave", () => !drag && light(null));
    group.addEventListener("focus", () => light(node.id));
    group.addEventListener("blur", () => light(null));
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter") openFromGraph(node.id);
      else if (event.key === " ") {
        event.preventDefault();
        selectGraphNode(node.id);
      }
    });
    nodeLayer.append(group);
  }

  // Pan by dragging the background; zoom with the wheel, about the pointer.
  let pan = null;
  svg.addEventListener("pointerdown", (event) => {
    pan = { x: event.clientX, y: event.clientY, moved: false };
    svg.setPointerCapture?.(event.pointerId);
    svg.classList.add("panning");
  });
  svg.addEventListener("pointermove", (event) => {
    if (!pan) return;
    const box = svg.getBoundingClientRect();
    const scale = width / (box.width || width);
    graphView.transform.x += (event.clientX - pan.x) * scale;
    graphView.transform.y += (event.clientY - pan.y) * scale;
    pan.moved ||= Math.abs(event.clientX - pan.x) + Math.abs(event.clientY - pan.y) > 2;
    pan.x = event.clientX;
    pan.y = event.clientY;
    paint();
  });
  const endPan = () => {
    if (pan && !pan.moved) selectGraphNode(null);
    pan = null;
    svg.classList.remove("panning");
  };
  svg.addEventListener("pointerup", endPan);
  svg.addEventListener("pointercancel", endPan);
  svg.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const box = svg.getBoundingClientRect();
      const scale = width / (box.width || width);
      zoomGraph(Math.exp(-event.deltaY * 0.0015), {
        x: (event.clientX - box.left) * scale,
        y: (event.clientY - box.top) * scale,
      });
    },
    { passive: false }
  );

  const card = document.createElement("div");
  card.className = "graph-card";
  card.id = "graph-card";
  card.hidden = true;
  wrap.replaceChildren(svg, card);
  graphView.svg = svg;
  graphView.groups = groups;
  graphView.around = around;
  graphView.size = { width, height };
  applyGraphSearch();
  if (graphView.selected && groups.has(graphView.selected)) selectGraphNode(graphView.selected);
  paint();
  run();
}

function zoomGraph(factor, about = null) {
  const t = graphView.transform;
  const { width, height } = graphView.size || { width: 900, height: 560 };
  const point = about || { x: width / 2, y: height / 2 };
  const k = Math.min(4, Math.max(0.2, t.k * factor));
  t.x = point.x - (point.x - t.x) * (k / t.k);
  t.y = point.y - (point.y - t.y) * (k / t.k);
  t.k = k;
  graphView.draw?.();
}

/** Zoom and pan so every visible page (or just ``ids``) fits in view. */
function fitGraph(ids = null) {
  const sim = graphView.sim;
  if (!sim) return;
  const points = [...sim.points].filter(([id]) => !ids || ids.includes(id)).map(([, p]) => p);
  if (!points.length) return;
  const { width, height } = graphView.size;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const pad = 60;
  const spanX = Math.max(1, Math.max(...xs) - Math.min(...xs));
  const spanY = Math.max(1, Math.max(...ys) - Math.min(...ys));
  const k = Math.min(ids ? 1.6 : 2, Math.max(0.2, Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY)));
  const cx = (Math.max(...xs) + Math.min(...xs)) / 2;
  const cy = (Math.max(...ys) + Math.min(...ys)) / 2;
  graphView.transform = { k, x: width / 2 - cx * k, y: height / 2 - cy * k };
  graphView.draw?.();
}

function selectGraphNode(id) {
  graphView.selected = id;
  for (const [other, group] of graphView.groups || []) group.classList.toggle("selected", other === id);
  const card = el("graph-card");
  if (!card) return;
  const node = id && graphView.graph.nodes.find((entry) => entry.id === id);
  if (!node) {
    card.hidden = true;
    return;
  }
  const edges = graphView.graph.edges;
  const count = (test) => edges.filter(test).length;
  const children = count((e) => e.kind === "parent" && e.source === id);
  const out = count((e) => e.kind === "link" && e.source === id);
  const into = count((e) => e.kind === "link" && e.target === id);
  const names = new Map(graphView.graph.nodes.map((entry) => [entry.id, entry.title]));
  const neighbours = [...(graphView.around.get(id) || [])].map((other) => names.get(other)).filter(Boolean);

  const heading = document.createElement("h4");
  heading.textContent = `${node.icon || "📄"} ${node.title}`;
  const facts = document.createElement("p");
  facts.className = "graph-facts";
  facts.textContent = [
    `${node.words.toLocaleString()} words`,
    `${children} sub-page${children === 1 ? "" : "s"}`,
    `links to ${out}`,
    `linked from ${into}`,
  ].join(" · ");
  const near = document.createElement("p");
  near.className = "graph-near";
  near.textContent = neighbours.length
    ? `Connected to ${neighbours.slice(0, 5).join(", ")}${neighbours.length > 5 ? ` and ${neighbours.length - 5} more` : ""}`
    : "Not connected to any other page yet.";
  const actions = document.createElement("div");
  actions.className = "graph-card-actions";
  const open = document.createElement("button");
  open.type = "button";
  open.className = "primary inline";
  open.id = "graph-open";
  open.textContent = "Open page";
  open.addEventListener("click", () => openFromGraph(id));
  const focus = document.createElement("button");
  focus.type = "button";
  focus.className = "small";
  focus.textContent = "Focus";
  focus.title = "Zoom to this page and its neighbours";
  focus.addEventListener("click", () => fitGraph([id, ...(graphView.around.get(id) || [])]));
  actions.append(open, focus);
  card.replaceChildren(heading, facts, near, actions);
  card.hidden = false;
}

function openFromGraph(id) {
  el("graph-modal").hidden = true;
  openDocument(id);
}

function applyGraphSearch() {
  const query = el("graph-search").value.trim().toLowerCase();
  const svg = graphView.svg;
  if (!svg || !graphView.graph) return [];
  const matches = [];
  for (const node of graphView.graph.nodes) {
    const group = graphView.groups.get(node.id);
    if (!group) continue;
    const hit = Boolean(query) && node.title.toLowerCase().includes(query);
    group.classList.toggle("match", hit);
    if (hit) matches.push(node.id);
  }
  svg.classList.toggle("searching", Boolean(query));
  return matches;
}

el("graph-search").addEventListener("input", applyGraphSearch);
el("graph-search").addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  const [first] = applyGraphSearch();
  if (!first) return;
  selectGraphNode(first);
  fitGraph([first, ...(graphView.around.get(first) || [])]);
});
for (const id of ["graph-show-parent", "graph-show-link", "graph-local"]) {
  el(id).addEventListener("change", () => {
    if (!graphView.graph) return;
    drawGraph(el("graph-wrap"));
    if (id === "graph-local") setTimeout(() => fitGraph(), 600);
  });
}
el("graph-zoom-in").addEventListener("click", () => zoomGraph(1.25));
el("graph-zoom-out").addEventListener("click", () => zoomGraph(0.8));
el("graph-fit").addEventListener("click", () => fitGraph());

el("open-graph").addEventListener("click", openGraph);

// --------------------------------------------------------------------------
// my tasks
// --------------------------------------------------------------------------

async function loadTasks() {
  try {
    state.tasks = await api("/api/me/tasks");
  } catch {
    state.tasks = [];
  }
  const urgent = state.tasks.filter((task) => task.due && ["overdue", "today"].includes(dueState(task.due))).length;
  el("tasks-count").hidden = !urgent;
  el("tasks-count").textContent = String(urgent);
  return state.tasks;
}

function taskItem(task, { compact = false } = {}) {
  const item = document.createElement("li");
  item.className = "task";
  const button = document.createElement("button");
  button.type = "button";
  const text = document.createElement("span");
  text.className = "task-text";
  text.textContent = task.text;
  const where = document.createElement("span");
  where.className = "task-where";
  where.textContent = `${task.icon || "📄"} ${task.title}`;
  button.append(text, where);
  if (task.due) {
    const due = document.createElement("span");
    due.className = `due ${dueState(task.due)}`;
    due.textContent = `📅 ${task.due}`;
    button.append(due);
  }
  button.addEventListener("click", async () => {
    el("tasks-modal").hidden = true;
    if (state.doc?.id !== task.document_id) await openDocument(task.document_id);
    revealLine(task.line);
  });
  item.append(button);
  if (compact) item.classList.add("compact");
  return item;
}

async function openTasks() {
  closeMenus();
  openModal(el("tasks-modal"));
  const tasks = await loadTasks();
  const groups = [
    ["Overdue", (t) => t.due && dueState(t.due) === "overdue"],
    ["Today", (t) => t.due && dueState(t.due) === "today"],
    ["Upcoming", (t) => t.due && ["soon", "later"].includes(dueState(t.due))],
    ["No date", (t) => !t.due],
  ];
  const sections = groups
    .map(([name, test]) => [name, tasks.filter(test)])
    .filter(([, list]) => list.length)
    .map(([name, list]) => {
      const section = document.createElement("section");
      const head = document.createElement("h4");
      head.textContent = `${name} · ${list.length}`;
      const items = document.createElement("ul");
      items.className = "task-list";
      items.append(...list.map((task) => taskItem(task)));
      section.append(head, items);
      return section;
    });
  if (!sections.length) {
    const empty = document.createElement("p");
    empty.className = "panel-hint";
    empty.textContent = "Nothing to do. Assign a to-do with @someone and give it a date with /due.";
    sections.push(empty);
  }
  el("task-groups").replaceChildren(...sections);
}

el("open-tasks").addEventListener("click", openTasks);

// --------------------------------------------------------------------------
// home
// --------------------------------------------------------------------------

function goHome() {
  closeSocket();
  state.doc = null;
  history.replaceState({}, "", location.pathname);
  showPage(false);
  renderDocumentList();
}

async function loadHome() {
  if (!state.user) return;
  const hour = new Date().getHours();
  const part = hour < 12 ? "morning" : hour < 18 ? "afternoon" : "evening";
  el("home-greeting").textContent = `Good ${part}, ${state.user.name.split(" ")[0]}`;
  el("home-sub").textContent = state.documents.length
    ? "Here is what is happening across your pages."
    : "Start from a template, or begin with a blank page.";

  const recent = [...state.documents].sort((a, b) => parseTime(b.updated_at) - parseTime(a.updated_at)).slice(0, 5);
  fillHomeList(
    "home-recent",
    recent.map((doc) => homeItem(`${doc.icon || "📄"} ${doc.title}`, formatWhen(doc.updated_at), () => openDocument(doc.id))),
    "No pages yet."
  );
  const mentions = (state.notifications || []).filter((n) => !n.read).slice(0, 5);
  fillHomeList(
    "home-mentions",
    mentions.map((n) => homeItem(`${n.actor.name}: ${n.snippet || n.document_title}`, formatWhen(n.created_at), () => openDocument(n.document_id))),
    "You are all caught up."
  );

  const [tasks, stats] = await Promise.all([loadTasks(), api("/api/me/stats").catch(() => null)]);
  const soon = tasks.filter((task) => task.due).slice(0, 5);
  fillHomeList("home-tasks", soon.map((task) => taskItem(task, { compact: true })), "Nothing due.");
  if (stats) {
    el("home-words").textContent = stats.words_this_week.toLocaleString();
    el("home-streak").textContent = stats.streak ? `🔥 ${stats.streak}-day writing streak` : "Write today to start a streak.";
    const max = Math.max(1, ...stats.days.map((day) => day.words));
    el("home-spark").replaceChildren(
      ...stats.days.map((day) => {
        const bar = document.createElement("span");
        bar.style.height = `${Math.max(4, (day.words / max) * 100)}%`;
        bar.title = `${parseTime(`${day.day}T12:00:00`).toLocaleDateString([], { weekday: "short" })}: ${day.words} words`;
        return bar;
      })
    );
  }
}

function homeItem(text, meta, onClick) {
  const item = document.createElement("li");
  const button = document.createElement("button");
  button.type = "button";
  const label = document.createElement("span");
  label.className = "task-text";
  label.textContent = text;
  const when = document.createElement("span");
  when.className = "task-where";
  when.textContent = meta;
  button.append(label, when);
  button.addEventListener("click", onClick);
  item.append(button);
  return item;
}

function fillHomeList(id, items, emptyText) {
  if (items.length) {
    el(id).replaceChildren(...items);
    return;
  }
  const empty = document.createElement("li");
  empty.className = "panel-hint";
  empty.textContent = emptyText;
  el(id).replaceChildren(empty);
}

el("open-home").addEventListener("click", goHome);

// --------------------------------------------------------------------------
// duplicate and lock
// --------------------------------------------------------------------------

async function duplicatePage(includeSubpages) {
  if (!state.doc) return;
  let copy;
  try {
    copy = await api(`/api/documents/${state.doc.id}/duplicate`, {
      method: "POST",
      body: { include_subpages: includeSubpages },
    });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  await loadDocuments();
  await openDocument(copy.id);
  toast(includeSubpages ? "Page and sub-pages duplicated" : "Page duplicated");
}

async function toggleLock() {
  if (!state.doc) return;
  const locked = !state.doc.locked;
  try {
    await api(`/api/documents/${state.doc.id}/lock`, { method: "PUT", body: { locked } });
  } catch (error) {
    toast(error.message, true);
    return;
  }
  state.doc.locked = locked;
  applyRole(state.doc.role);
  toast(locked ? "Locked: only you can edit this page now" : "Unlocked: editors can edit again");
}

// --------------------------------------------------------------------------
// read aloud and dictation (Web Speech API)
// --------------------------------------------------------------------------

const speech = { speaking: false, recognition: null };

if ("speechSynthesis" in window && typeof SpeechSynthesisUtterance !== "undefined") {
  el("speak-button").hidden = false;
  el("speak-button").addEventListener("click", () => {
    if (speech.speaking) {
      speechSynthesis.cancel();
      return;
    }
    const selection = editor.selection();
    const text = selection && selection.from !== selection.to ? state.content.slice(selection.from, selection.to) : state.content;
    const spoken = text
      .split("\n")
      .map((line) => line.replace(/^\s*([#>]+|[-*+] \[[ xX]\]|[-*+]|\d+[.)])\s*/, "").replace(/@\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/[*_`~|]/g, ""))
      .filter((line) => line.trim())
      .join(". ");
    if (!spoken.trim()) return;
    const utterance = new SpeechSynthesisUtterance(spoken);
    utterance.onstart = () => {
      speech.speaking = true;
      el("speak-button").classList.add("on");
    };
    utterance.onend = utterance.onerror = () => {
      speech.speaking = false;
      el("speak-button").classList.remove("on");
    };
    speechSynthesis.speak(utterance);
  });
}

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (Recognition) {
  el("dictate-button").hidden = false;
  el("dictate-button").addEventListener("mousedown", (event) => event.preventDefault());
  el("dictate-button").addEventListener("click", () => {
    if (speech.recognition) {
      speech.recognition.stop();
      return;
    }
    if (!state.doc?.canEdit) return;
    const recognition = new Recognition();
    recognition.continuous = true;
    recognition.interimResults = false;
    recognition.lang = navigator.language || "en-US";
    recognition.onresult = (event) => {
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        if (!event.results[i].isFinal) continue;
        const words = event.results[i][0].transcript.trim();
        if (!words) continue;
        const selection = editor.selection() || { from: state.content.length, to: state.content.length };
        const before = state.content[selection.from - 1];
        const text = (before && !/\s/.test(before) ? " " : "") + words;
        editor.replace(selection.from, selection.to, text);
      }
    };
    recognition.onend = () => {
      speech.recognition = null;
      el("dictate-button").classList.remove("on");
    };
    recognition.onerror = (event) => toast(`Dictation stopped: ${event.error}`, true);
    speech.recognition = recognition;
    el("dictate-button").classList.add("on");
    editor.focus();
    recognition.start();
    toast("Listening… click the microphone again to stop");
  });
}

// --------------------------------------------------------------------------
// offline banner and the service worker
// --------------------------------------------------------------------------

function renderOnline() {
  el("offline-banner").hidden = navigator.onLine !== false;
}
window.addEventListener("online", renderOnline);
window.addEventListener("offline", renderOnline);
renderOnline();

if ("serviceWorker" in navigator && location.protocol !== "file:") {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* installability is a nicety; the app works without it */
    });
  });
}

// --------------------------------------------------------------------------
// start
// --------------------------------------------------------------------------

setView(prefs.get("view", "page"));

window.addEventListener("resize", () => {
  fitSource();
  renderCarets();
  hideBubble();
});
ui.scroller.addEventListener("scroll", () => {
  if (!ui.slashMenu.hidden) editor.updateSlash();
  if (!ui.bubble.hidden && ui.linkForm.hidden) updateBubble();
  closeMenus();
});

// The Markdown tints are refreshed lazily when that surface comes into view.
new (window.MutationObserver || class { observe() {} })(() => {
  if (ui.highlight.dataset.stale && currentView() !== "page") renderHighlights();
}).observe(ui.page, { attributes: true, attributeFilter: ["data-view"] });

document.addEventListener("keydown", (event) => {
  const mod = event.ctrlKey || event.metaKey;
  if (event.key === "Escape") {
    if (!ui.versionModal.hidden) closeVersion();
    for (const modal of [ui.templates, ui.shortcuts, ui.trash]) modal.hidden = true;
    for (const id of ["ai-modal", "graph-modal", "tasks-modal", "security-modal", "integrity-modal"]) {
      el(id).hidden = true;
    }
    if (!el("replay-modal").hidden) closeReplay();
    if (!el("find-bar").hidden) {
      closeFind();
      if (state.doc) editor.focus();
    }
    stopFollowing();
    if (!ui.palette.hidden) closePalette();
    closeMenus();
    if (ui.bubble.contains(document.activeElement)) return;
    hideBubble();
  }
  if (ui.app.hidden) return;
  if (mod && (event.key === "k" || event.key === "K" || event.key === "p" || event.key === "P")) {
    event.preventDefault();
    if (ui.palette.hidden) openPalette();
    else closePalette();
  }
  if (mod && event.key === "\\") {
    event.preventDefault();
    toggleSidebar();
  }
  if (mod && !event.shiftKey && (event.key === "f" || event.key === "F") && state.doc) {
    event.preventDefault();
    openFind(false);
  }
  if (mod && (event.key === "h" || event.key === "H") && state.doc) {
    event.preventDefault();
    openFind(true);
  }
  if (mod && (event.key === "j" || event.key === "J") && state.doc) {
    event.preventDefault();
    openAssistant(state.doc.canEdit ? "continue" : "summarize");
  }
});

(async function start() {
  takeRedirect();
  loadProviders();
  if (!state.token) return;
  try {
    state.user = await api("/api/auth/me");
    await enterApp();
  } catch {
    signOut();
  }
})();
