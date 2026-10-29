# Architecture

How the system is put together, and why each part went the way it did. The SRS
sections and WBS work packages each decision answers to are named inline.

---

## 1. The shape of a request

```
browser  ──HTTP──▶  FastAPI routers  ──▶  SQLAlchemy  ──▶  SQLite / PostgreSQL
   │                                                            ▲
   └──WebSocket──▶  DocumentRoom ──▶ DocumentSession ───────────┘
                         │                │
                         │                ├─ TextOperation  (transform, apply)
                         │                ├─ AuthorTrail    (who typed what)
                         │                └─ AttributionEngine
                         │
                         └──▶ Broadcaster ──▶ every socket on this document
```

Two paths into the same state. REST handles everything that is not a keystroke
— accounts, documents, sharing, history — and the WebSocket handles editing,
presence, and attribution updates. Both read through `DocumentSession`, so a
`GET /api/documents/{id}` issued mid-edit returns the text as the room has it,
not a stale row.

## 2. Operational transformation (WBS 5.1)

### Why OT and not a CRDT

A CRDT converges without a server arbitrating, which is the right answer for
offline-first or peer-to-peer editing. This system already has an
authoritative server — the SRS puts version control, permissions, and the
attribution engine there — so the coordination a CRDT buys is coordination
already paid for. OT gives, in exchange, an operation log that is *the* version
history (SRS 6.4): a linear, replayable sequence of small deltas, each one
attributable to a user. Reconstructing "the document at version 14" is
replaying fourteen rows. The same question against a CRDT means reasoning about
a merge lattice.

### The operation model

Three components — retain, insert, delete — over a plain-text buffer, serialised
as a flat JSON array (`[5, -2, "hi", 9]`) that goes straight into
`document_operations.delta_json`. Operations are kept canonical by the
builders: adjacent retains merge, and an insert is always ordered ahead of a
delete at the same position, so two operations with the same effect have the
same representation.

The property everything rests on:

```
a', b' = transform(a, b)   ⟹   apply(apply(d, a), b') == apply(apply(d, b), a')
```

Concurrent inserts at the same offset are broken deterministically in favour of
the first argument, so every replica resolves the tie identically.

### Server-side rebasing

The server holds `content`, `version`, and the operation log. A client sends
`{version, operation}`; the server transforms it against `history[version:]`,
applies it, appends it, and broadcasts the *transformed* form. The sender gets
an `ack` rather than an echo, which lets the client run the standard
three-state machine — synchronised, awaiting-confirm, awaiting-with-buffer —
with exactly one operation in flight.

### Two implementations, one behaviour

`web/ot.js` is a port of `server/app/realtime/ot.py`. A divergence between them
would not raise an error; it would silently leave two editors with different
text. `server/tests/test_client_parity.py` therefore generates random
concurrent edits in Python, transforms them in Node, and requires the outputs
to match component for component.

## 3. Latency (SRS 5.1, WBS 8.2)

The budget is 200ms. The ordering inside `DocumentSession.submit` follows the
parallel block of the SRS 3.2 sequence diagram:

1. transform and apply in memory;
2. append to the in-process log and return;
3. acknowledge the sender, then fan out to everyone else;
4. **afterwards**, a background saver drains the operation queue to the
   database on a one-second cadence;
5. **separately**, an attributor debounces 1.5s after the last edit, retrains,
   and pushes new block attributions.

Durability is traded for latency only in a bounded way: at most one second of
operations is queued, and the queue is drained synchronously on teardown.

### The cancellation bug that shaped the teardown path

The first implementation flushed on disconnect with `await session.flush()`.
It lost data, reproducibly, whenever a second editor was in the room. The cause
is that the ASGI server cancels the WebSocket handler task the instant the
socket drops, so every `await` in the `finally` block raises `CancelledError`
before the write lands.

The fix is that the durable half of teardown contains no `await` at all:

```python
def close_now(self) -> int:
    self._closing = True
    written = self.session.flush_blocking()   # no suspension point
    ...
```

`flush_blocking` costs a few milliseconds on a path nobody is waiting on, and
is the difference between keeping the last second of typing and losing it.
`test_realtime.py::test_edits_survive_everyone_disconnecting` covers it.

## 4. Authorship attribution (WBS 6.1–6.3)

### Two witnesses

**The keystroke ledger** (`attribution/trail.py`) is a run-length map from
character offset to the user who typed it, advanced by the same operations the
OT engine applies, so it never drifts from the text. It is exact about
mechanical authorship and blind to intellectual authorship: it cannot see that
a user pasted someone else's paragraph or retyped it from a draft.

**The stylometric model** (`attribution/features.py`, `classifier.py`) extracts
44 scale-free measurements — every one a rate or a ratio, so a one-line comment
and a three-paragraph section land in the same feature space and the model
learns style rather than length. Function words carry authorial habit rather
than topic, which is why they dominate the feature set.

The ledger also supplies the model's *training labels*: text the ledger proves
a user typed is that user's labelled corpus. The AI learns from the mechanical
record, then forms an opinion independent of it.

### Blending

```
style_weight  = 0.65 · min(1, len(block) / min_chars)
ledger_weight = 1 − style_weight
score[user]   = style_weight · P_style[user] + ledger_weight · ledger_share[user]
```

Stylometry's vote fades out as a block gets shorter than one training sample,
because a four-word heading carries no measurable style. Its share is capped
below 1.0 so a confident model can never completely overrule recorded
keystrokes. The winning score, normalised, is the `confidence_score` persisted
on `document_blocks` (SRS 6.5).

### Why a hand-written classifier

scikit-learn would be one import. It would also be a 100MB dependency and a
model-serving decision, for a problem with 44 features and typically two to
five classes. Softmax regression by gradient descent is ~200 lines; a fitted
model is a few hundred floats that JSON-serialise into a row; training takes
milliseconds. That is what lets the attribution pass run inline on the server
rather than behind a network hop — which is what makes SRS 5.1's "near
real-time" attribution achievable at all.

### When it declines to answer

Below two authors with three samples each, `train()` returns
`trained=False` with a reason, and attribution falls back to the ledger with
`method="ledger"`. The UI says "model: keystroke ledger only". A confident
wrong answer would be worse than an honest abstention.

## 5. Data model (SRS 6)

The hybrid model of SRS 6 is mapped through one ORM so the deployment chooses
its storage with an environment variable:

| Table | Role |
|---|---|
| `users` | profiles, credentials, presence colour anchors |
| `documents` | master record, current version, share token |
| `document_collaborators` | RBAC grants (OWNER / EDITOR / VIEWER) |
| `document_operations` | the delta log — one row per accepted operation |
| `document_blocks` | parsed layout plus the attribution log |
| `document_snapshots` | periodic materialisations, so rollback need not replay from zero |
| `author_samples` | known-authorship writing used to train the model |

`document_blocks.sort_order` is a lexical rank rather than an index: inserting
a block between two others needs only a midpoint, so concurrent inserts never
renumber the blocks after them.

Version reconstruction has two paths. Versions still in memory replay forward
from the session's load base; older ones replay from the nearest snapshot in
the database. A rollback is not a rewrite — it appends the operation that
undoes the interval, so the log stays append-only and auditable.

## 6. Real-time fan-out (SRS 2.4)

SRS 2.4 places several backends behind a load balancer with Kafka between them.
Every event a room emits leaves through a `Broadcaster` and arrives back
through a subscription, *even when the only subscriber is the process that
published it*. That costs one dictionary lookup on the single-node path and
buys the property that the multi-node path is not a different code path: a
Kafka implementation of the same interface is the whole change.

`KafkaBroadcaster` declares the shape — one topic keyed by document id, so a
document's events keep their order on one partition, with each backend in its
own consumer group — and raises rather than pretending to work without a
broker.

## 7. Security (SRS 5.3)

- **Passwords**: PBKDF2-HMAC-SHA256, 260,000 rounds, per-password salt,
  constant-time comparison.
- **Tokens**: HS256 JWTs with `exp` and `jti`. The WebSocket takes its token as
  a query parameter because the browser's WebSocket API cannot set headers.
- **Authorisation**: a role check on every document route. A caller with no
  grant gets **404, not 403** — a 403 confirms the document id is real.
- **Login**: identical response for an unknown email and a wrong password.
- **Share links**: enabling link sharing mints a fresh token, so revoking
  access really revokes it; a link upgrades a weaker existing grant but never
  downgrades one.
- **Rendering**: document text is HTML-escaped before inline markup is applied,
  so a document can never inject markup into another user's page. Covered by
  `web/tests/client.test.mjs`.

## 8. The editor (WBS 7.1–7.3)

Rich text over a plain-text OT engine is a deliberate trade. The buffer is
plain text carrying lightweight markup (`#`, `-`, `**bold**`); formatting is a
rendering concern, not a synchronisation one. Character operations stay simple
and provably convergent, at the cost of not synchronising styled ranges as
first-class objects — the upgrade path is attributed-range OT or a CRDT, and it
is a real rewrite of the engine, not an extension.

Remote carets are positioned by laying the same text out in a hidden mirror
element that shares the textarea's exact box and font, then reading the offset
back — a textarea cannot hold markup, so this is the only way to turn a
character offset into pixels.

Undo holds inverted operations. When a remote edit arrives, the whole undo and
redo stack is rebased over it, because those entries describe a document that
no longer exists.

## 9. What would need to change to scale

1. **Rooms are per-process.** Sticky sessions or a Kafka broadcaster are
   required before a second backend is useful.
2. **The operation log grows without bound.** Snapshots exist; compaction of
   superseded operations does not.
3. **Model training is per-document and in-process.** Shared cross-document
   author models would need a model store and a retraining schedule.
4. **SQLite is the default.** Correct for development, wrong above a handful of
   concurrent writers; `COLLABDOC_DATABASE_URL` switches to PostgreSQL.
