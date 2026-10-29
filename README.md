# Collaborative Document Server with AI Integration

An implementation of the SRS for a real-time collaborative document editor with
AI-driven authorship attribution — Nitish Kumar, 24BCE1520.

Several people edit one document at the same time; every keystroke is
synchronised through an operational transformation engine, and a stylometric
model decides who wrote each block of the result and how confident it is about
that answer.

```
pip install -e ".[dev]"
uvicorn app.main:app --app-dir server --reload
# open http://127.0.0.1:8000
```

No database to set up: it runs on SQLite out of the box and switches to
PostgreSQL with one environment variable.

---

## What it does

| Capability | Where it lives |
|---|---|
| Real-time collaborative editing, convergent under concurrent edits | `server/app/realtime/ot.py`, `session.py` |
| AI authorship attribution with confidence scores | `server/app/attribution/` |
| A Notion-style block editor: slash commands, drag handles, selection toolbar | `web/editor.js` |
| Headings, lists, nested lists, to-dos, quotes, callouts, dividers, links | `web/editor.js` |
| Comments anchored to passages, with replies and resolve, synced live | `routers/workspace.py`, `web/app.js` |
| Search across every page you can open, in a command palette | `routers/workspace.py`, `web/app.js` |
| Per-author activity timeline, page statistics and outline | `routers/workspace.py`, `web/extras.js` |
| Version history, rollback, named save points | `routers/documents.py`, `realtime/session.py` |
| Presence avatars and live remote carets | `realtime/hub.py`, `web/app.js` |
| JWT auth and role-based access (owner / editor / viewer) | `security.py`, `deps.py` |
| Sharing by invitation or revocable link | `routers/collaborators.py` |
| Page icons, templates, favorites, export to Markdown / HTML / PDF | `web/extras.js`, `web/app.js` |
| Light and dark themes, keyboard shortcuts, undo/redo, auto-save | `web/app.js`, `web/styles.css` |
| Nested pages in a sidebar tree, breadcrumbs, drag to move; sharing a page shares its sub-pages | `deps.py`, `routers/documents.py`, `web/app.js` |
| Trash with restore and delete-forever; emptied after 30 days | `routers/documents.py`, `web/app.js` |
| @mentions in pages and comments, reply and invite notifications, a live inbox | `notifications.py`, `routers/notifications.py`, `realtime/ws.py` |
| Code blocks with highlighting, tables with Tab navigation, image upload by paste, drop or `/image` | `web/editor.js`, `routers/files.py` |
| Publish a read-only page to the web; compare any version with the current page | `publish.py`, `routers/public.py`, `web/extras.js` |
| Favourites kept on the server; import Markdown files as pages | `routers/me.py`, `web/app.js` |
| Integrity report: each person's share and effort, pasted text, and passages typed by one person in another's style | `routers/integrity.py`, `web/extras.js` |

## How the interesting parts work

### Convergence

Each client sends operations stamped with the version they were written
against. The server rebases an incoming operation over everything that landed
in the meantime, applies it, and broadcasts the rebased form:

```python
for concurrent in self.history[base_version - self.history_base:]:
    transformed, _ = TextOperation.transform(transformed, concurrent)
```

`transform` guarantees that `a then b'` and `b then a'` produce the same text,
so no ordering of arrivals can leave two clients disagreeing. That property is
tested directly against 2000 random concurrent edit pairs, and the browser's
port of the engine is tested against the Python one operation for operation —
if the two implementations ever disagreed, two editors would silently drift
apart, which is the one failure OT exists to prevent.

### Attribution

Two independent lines of evidence decide who wrote a block:

- **the keystroke ledger** — a run-length map from character offset to the user
  who typed it, advanced by the same operations the OT engine applies;
- **stylometry** — 44 scale-free features (sentence length, function-word
  habit, punctuation rates, contractions, vocabulary richness) fed to a
  multinomial logistic regression trained on each collaborator's own writing.

Neither is sufficient alone. The ledger cannot tell that someone pasted another
person's sentence; the model cannot separate two collaborators who have not
written enough yet. They are blended, with stylometry's share of the vote
scaled by how much text the block actually offers — a four-word heading is
decided mostly by keystrokes, a full paragraph mostly by style.

The classifier is ~200 lines of dependency-free Python. A fitted model is a few
hundred floats and training takes milliseconds, which is what lets attribution
run inline on the server instead of behind a model-serving hop.

### The integrity report

Page menu → *Integrity report* (or the button on the Authors panel) answers
the question a marker actually has about a group document: who wrote it, and
is any of it not what it seems? It is built only from evidence the server
already keeps:

- **share of the final text** from the keystroke ledger, and **effort** --
  characters typed and deleted, time active, writing sessions -- from the
  operation log;
- **pastes**: an insert of 120+ characters in one operation was not typed;
- **style mismatches**: a block one person typed that reads like another
  collaborator's writing.

The style check is *leave-one-out*. The live model trains on what the ledger
says each person typed, so a passage Bob copied from Alice sits in Bob's
training data and pulls the verdict towards Bob: the copy vouches for
itself. The report instead judges each block with a model retrained without
it, asking whether it reads like the typist's *other* writing. In a
two-author demo this moved a copied passage from 59% (below the flag threshold) to 98%
"reads like Alice". A paste that also reads like someone else becomes one
high-severity flag. Every flag jumps to its passage, and the report
downloads as a printable page. The flags point a reviewer at passages to
read; they are not proof of misconduct.

### A block editor over plain text

The OT engine synchronises characters and the attribution engine reads the
document line by line, so the buffer stays plain text with lightweight markup
(`# heading`, `- [ ] task`, `**bold**`). The page is a `contenteditable`
surface that renders each line as a block and keeps every markup character in
the DOM inside a hidden span. A block's text content is therefore exactly its
line, which makes a caret position and a buffer offset the same number,
counted two ways.

The browser never edits that DOM itself. Every `beforeinput` is cancelled and
turned into a text operation -- Enter continues a list, Backspace at the start
of a block turns it back into text, `[] ` becomes a to-do -- and the page is
re-rendered from the text. Typing Markdown formats as you go, the Markdown
view edits the same buffer raw, and nothing about synchronisation changed.

Offsets travel as code points. Python counts `"💡"` as one character and
JavaScript as two, so without conversion the first emoji in a document would
make every later position disagree. The client converts each operation at the
socket against its copy of the server's text (`web/units.js`).

### Staying inside the latency budget

The SRS allows 200ms for a real-time update. Work is ordered so the broadcast
never waits on durable storage: an operation is transformed, applied in memory,
acknowledged, and fanned out; the write to the operation log is drained by a
background saver, and the attribution pass is debounced until typing settles.
Measured end-to-end round trip on a development machine is **2.3ms median,
3.9ms p95**.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

uvicorn app.main:app --app-dir server --reload
```

Open <http://127.0.0.1:8000>, create an account, and open the same document in
a second browser profile to see collaboration, presence and attribution live.

`GET /docs` serves the generated OpenAPI reference for every endpoint.

### Configuration

Copy `.env.example` to `.env`. Everything has a working default; the two worth
knowing about:

```bash
COLLABDOC_SECRET_KEY=...        # required in production; signs the JWTs
COLLABDOC_DATABASE_URL=postgresql+psycopg://user:pass@host:5432/collabdoc
```

### A scripted demonstration

```bash
python scripts/demo.py --base http://127.0.0.1:8000
```

Two clients connect, type in deliberately different styles, send overlapping
edits written against the same version, and the script checks that both clients
and the server agree on the result before printing the attribution report:

```
--- convergence ---
  PASS Alice matches the server
  PASS Bob matches the server
  PASS Both clients match each other

--- latency (SRS 5.1 budget: 200ms) ---
  median 2.3ms, p95 3.9ms over 8 operations

--- AI authorship attribution ---
  model trained: True (8 samples, training accuracy 100%)
  [HEADING  ] Alice  100%  style=Alice  ledger=Alice   # Quarterly Research Summary
  [PARAGRAPH] Bob    100%  style=Bob    ledger=Bob     We shipped it. It works! Nobody's compl...
  [PARAGRAPH] Alice   99%  style=Alice  ledger=Alice   It should be noted that the underlying ...

  style/keystroke agreement: 100% over 6 blocks
```

## Tests

```bash
npm install --prefix web/tests             # once, for the jsdom suite
pytest -q                                  # 156 items, ~60s
```

174 tests in total: 155 Python plus the 19 jsdom tests, which `pytest` runs as
one item by shelling out to `node --test`. It skips them when Node or jsdom is
absent, so the Python suite stays runnable without a JavaScript toolchain.

On top of that, `scripts/browser_check.py` drives two real Chromium browsers
against a running server — 55 checks covering sign-up, sharing, presence,
live typing in both directions through the block editor, slash commands,
to-dos, the formatting toolbar, attribution rendering, undo, version history,
live comments, search, the activity chart, mentions and the inbox, code
blocks, tables, image upload, sub-pages, the trash, publishing, version
compare and viewer permissions:

```bash
pip install playwright && playwright install chromium
python -m uvicorn app.main:app --app-dir server --port 8000
python scripts/browser_check.py --base http://127.0.0.1:8000
```

| Suite | Covers |
|---|---|
| `test_ot.py` | transform, compose, invert, cursors; 2000 randomised convergence cases plus 1000 compose/invert cases |
| `test_client_parity.py` | the browser engine agreeing with the server's, case for case |
| `test_auth_rbac.py` | registration, login, tokens, the three roles, share links |
| `test_documents.py` | CRUD, saving, version reconstruction, rollback |
| `test_realtime.py` | two live sockets: broadcast, rebasing, presence, cursors, disconnects |
| `test_attribution.py` | features, classifier accuracy, ledger, blended attribution |
| `test_performance.py` | the 200ms budget, fan-out to 8 readers, long-backlog rebase |
| `test_workspace.py` | page icons, comment threads and permissions, search, activity |
| `test_pages.py` | sub-pages, inherited sharing, moving, the trash and its purge, favourites, publishing |
| `test_notifications.py` | mention parsing, mention/reply/invite notifications, read state, the live `/ws/me` socket |
| `test_files.py` | image upload and access, refused types and sizes, non-prose blocks, the published renderer |
| `web/tests/client.test.mjs` | the client under jsdom: typing, undo, remote edits, escaping, block rendering, emoji offsets, code/table/image/mention blocks, the @ picker, the page tree, diffs |
| `test_client_syntax.py` | every browser module parses as an ES module |
| `scripts/browser_check.py` | two real browsers end to end: 55 checks, screenshots |

On the deliberately separable synthetic corpora in `server/tests/corpora.py`,
held-out classifier accuracy is 100% for both two and three authors, against
the WBS 9.3 floor of 85%. Those corpora are caricatures — three writing styles
built to differ on exactly the features the model measures — so that number
demonstrates the pipeline is sound, not that real collaborators are this easy
to tell apart. The honest runtime measure is the `agreement_rate` on
`/attribution/metrics`: how often stylometry independently reaches the same
answer as the keystroke ledger.

## Deployment

```bash
docker build -t collabdoc .
COLLABDOC_SECRET_KEY=$(openssl rand -hex 32) docker compose up
```

One worker per container: the room registry and its OT state are per-process,
so horizontal scale comes from more containers behind the load balancer plus a
Kafka broadcaster (`COLLABDOC_BROADCASTER`), not from more workers sharing a
socket. `server/app/realtime/broadcaster.py` is the seam — every event already
travels through it, so the multi-node path is not a different code path.

## Layout

```
server/app/
  realtime/     ot.py  session.py  hub.py  ws.py  broadcaster.py
  attribution/  features.py  classifier.py  trail.py  blocks.py  engine.py
  routers/      auth.py  documents.py  collaborators.py  attribution.py  workspace.py
  models.py  schemas.py  security.py  deps.py  db.py  config.py  main.py
web/            index.html  app.js  editor.js  extras.js  units.js  ot.js  styles.css
docs/           ARCHITECTURE.md  SRS-TRACEABILITY.md  PROJECT-PLAN.md
```

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the design decisions and why
  they went the way they did
- [`docs/SRS-TRACEABILITY.md`](docs/SRS-TRACEABILITY.md) — every SRS
  requirement mapped to the code that implements it and the test that proves it
- [`docs/PROJECT-PLAN.md`](docs/PROJECT-PLAN.md) — the WBS and six-week schedule
  against what was actually built

## What browser testing caught

The jsdom suite checks the client's logic but not how it renders, and three
defects lived in exactly that gap until two real browsers were pointed at the
app:

- **A hidden element that was not hidden.** `.app { display: flex }` and
  `.modal { display: grid }` both override the `hidden` attribute's default
  `display: none`, so the workspace and the version modal were laid out on top
  of the sign-in form and swallowed its clicks. One rule — `[hidden] { display:
  none !important }` — fixes the whole class. jsdom could not see it because it
  reads `el.hidden`, not the computed style.
- **A syntax error that failed silently.** A broken string literal in `app.js`
  meant the module never evaluated, so no listener was ever attached and the
  page rendered as inert HTML — with nothing in the console but a parse error.
  `test_client_syntax.py` now parses every browser module on its own.
- **A sidebar frozen at v0.** The document list showed the version and
  collaborator count from the moment it was fetched, so after two hundred
  edits and an invitation it still read "v0 · 1 person".

- **Two people opening a page at once could blank it.** Both connections
  loaded the room's session in parallel threads; one reset the text while
  the other replayed the log, leaving the room at version 0 and rejecting
  every edit. The load is now single-flight
  (`test_two_concurrent_loads_leave_the_session_consistent`).

## Known limits

- **Rich text is markup over plain text.** The OT engine transforms characters,
  not styled ranges, so `**bold**` is rendering rather than synchronisation,
  and formatting cannot span lines. Real styled-range OT (or a CRDT) is the
  upgrade path.
- **Comments anchor to quoted text.** A thread follows its passage through
  edits by matching the quote; if the quoted text is rewritten, the thread
  stays in the panel but loses its place on the page.
- **Search reads the parsed block table,** which the attribution pass
  refreshes a moment after typing stops, so the newest words can take a
  second or two to become searchable.
- **Attribution needs writing to learn from.** Below two authors with three
  samples each, the engine says so and falls back to the keystroke ledger
  rather than guessing.
- **Kafka fan-out is an interface, not an implementation.** `KafkaBroadcaster`
  defines the shape and raises; single-node deployments use the in-process
  broadcaster that every test runs against.
- **Page links show the title they were made with.** `[Title](page:id)`
  keeps working after a rename, but its text does not follow the new title.
- **Uploads live on local disk** (`COLLABDOC_UPLOAD_DIR`). A multi-node
  deployment needs shared storage for them, the same way it needs Kafka
  for fan-out.
- **Rooms are in-process.** A backend restart reloads a document from its
  operation log, but in-flight unacknowledged operations from a client that
  disconnects at that exact moment are lost.
