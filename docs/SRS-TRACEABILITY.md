# SRS traceability

Every requirement in the SRS, mapped to the code that implements it and the
test that proves it. Anything not fully built says so in the last section
rather than being quietly marked done.

---

## 2.2 Product features

| Feature | Implementation | Test |
|---|---|---|
| Real-time collaborative editing | `realtime/ot.py`, `realtime/session.py`, `realtime/ws.py` | `test_realtime.py::test_concurrent_edits_at_the_same_version_converge` |
| AI-powered authorship identification | `attribution/engine.py`, `classifier.py`, `features.py` | `test_attribution.py::test_engine_attributes_blocks_to_the_right_author` |
| Version history and rollback | `routers/documents.py`, `session.content_at`, `session.revert` | `test_documents.py::test_version_history_reconstructs_earlier_states`, `::test_revert_appends_rather_than_rewriting_history` |
| Rich text formatting | `web/editor.js` (`renderInline`, `toggleInline`) | `client.test.mjs::markup is escaped…` |
| User and permission management | `models.Role`, `deps.py`, `routers/collaborators.py` | `test_auth_rbac.py::test_a_viewer_can_read_but_not_write`, `::test_an_editor_can_write_but_not_manage_access` |
| Document sharing (links and invitations) | `routers/collaborators.py` | `test_auth_rbac.py::test_share_link_grants_access_and_can_be_revoked` |

## 2.4 Operating environment

| Requirement | Implementation |
|---|---|
| Web-based, modern browsers | `web/` — plain ES modules, no build step |
| Scalable cloud backend | `Dockerfile`, `docker-compose.yml`, one worker per container |
| PostgreSQL / MongoDB | SQLAlchemy over `COLLABDOC_DATABASE_URL`; SQLite default, PostgreSQL supported |
| Kafka between backends | `realtime/broadcaster.py` — every event travels the interface; `KafkaBroadcaster` declares the shape |
| AI/ML service integration | `attribution/` — in-process, for the latency reason in ARCHITECTURE §4 |

## 2.5 Design and implementation constraints

| Constraint | How it is met | Test |
|---|---|---|
| Data integrity across concurrent edits | OT transform against server history | `test_ot.py::test_transform_converges_under_random_edits` (2000 cases) |
| Accurate, performant attribution | Blended stylometry + keystroke ledger | `test_attribution.py::test_classifier_meets_the_accuracy_floor_on_held_out_text` |
| Minimal attribution latency | Debounced off the edit path, in a worker thread | `test_performance.py::test_attribution_pass_is_not_on_the_critical_path` |
| Security and access control | PBKDF2, JWT, per-route role checks | `test_auth_rbac.py` (16 tests) |

## 3.3 Functional requirements

| Requirement | Implementation | Test |
|---|---|---|
| Real-time text synchronisation | `session.submit`, `hub.DocumentRoom`, `ws.py` | `test_realtime.py::test_an_edit_is_acknowledged_and_broadcast` |
| AI authorship attribution, visually represented | `attribution/engine.py`; `web/app.js` `renderPreview` | `test_attribution.py`, `client.test.mjs::attribution tints each block…` |
| User presence indicators | `hub.Participant`, `publish_presence`; avatars and carets in `web/app.js` | `test_realtime.py::test_presence_lists_everyone_in_the_room`, `::test_a_remote_cursor_is_carried_over_later_edits` |
| Undo/redo per user | `TextOperation.invert`; stacks in `web/app.js` | `test_ot.py::test_invert_restores_the_original`, `client.test.mjs::undo reverses a local edit…` |
| Document saving, automatic and manual | `hub._save_loop` (auto), `session.save_snapshot` (manual) | `test_documents.py::test_manual_save_records_an_operation`, `test_realtime.py::test_save_over_the_socket_pins_a_snapshot` |

## 4. External interfaces

| Requirement | Implementation |
|---|---|
| 4.1 Clean, responsive UI with authorship cues | `web/index.html`, `styles.css` — three-pane layout, per-block author tint, dark mode; verified in Chromium by `scripts/browser_check.py` |
| 4.2 Standard peripherals only | keyboard and mouse; `Ctrl+B/I/U/Z/S` bound |
| 4.3 Browser and backend APIs | `GET /docs` — generated OpenAPI for all 25 operations across 19 paths, plus the WebSocket protocol in `realtime/ws.py` |
| 4.4 WebSockets for real-time, HTTPS for transfer | `/ws/documents/{id}`; TLS terminates at the load balancer |

## 5.1 Performance

| Requirement | Result | Test |
|---|---|---|
| Updates under 200ms | 2.3ms median, 3.9ms p95 measured end to end | `test_performance.py::test_edit_round_trip_stays_inside_the_budget` |
| Thousands of concurrent users | Per-document rooms, async fan-out; verified to 8 concurrent readers | `test_performance.py::test_fan_out_to_many_readers_stays_inside_the_budget` |
| Near real-time attribution | Debounced 1.5s after typing settles, off the edit path | `test_performance.py::test_attribution_pass_is_not_on_the_critical_path` |

The thousands-of-users figure is an architectural claim, not a measured one:
the tests establish the per-operation cost and the fan-out shape, not a load
test at that scale. See "Not fully delivered" below.

## 5.2 Safety

| Requirement | Implementation | Test |
|---|---|---|
| Data loss prevention | Append-only operation log, periodic snapshots, uninterruptible teardown flush | `test_realtime.py::test_edits_survive_everyone_disconnecting` |
| Protection against corruption | `apply` rejects an operation whose base length does not match the document | `test_ot.py::test_apply_rejects_a_mismatched_document` |

## 5.3 Security

| Requirement | Implementation | Test |
|---|---|---|
| Secure authentication (OAuth, JWT) | JWT (HS256) + PBKDF2-HMAC-SHA256, 260k rounds | `test_auth_rbac.py::test_login_with_the_wrong_password_fails` |
| Role-based authorisation | `Role.can`, `require_editor`, `require_owner`; 404 for non-collaborators | `test_auth_rbac.py::test_an_outsider_cannot_see_that_a_document_exists` |
| Encryption at rest and in transit | TLS at the load balancer; disk encryption is a deployment concern | — (see below) |

## 5.4 Software quality attributes

| Attribute | How it is addressed |
|---|---|
| Availability | Stateless containers behind a load balancer; rooms rebuild from the operation log |
| Reliability | 174 automated tests plus 55 live-browser checks; convergence fuzzed over 2000 random cases; cross-language engine parity |
| Maintainability | Layered modules, docstrings citing the requirement each part answers to, `/docs` API reference |
| Usability | Single-page editor, live preview, presence, no build step or database setup to run it |

## 6. Database schema

Every table and column of the SRS ER design is implemented in
`server/app/models.py`: `users`, `documents`, `document_collaborators`,
`document_operations`, `document_blocks`, with the field names and types as
specified (`presence_color`, `delta_json`, `sort_order` as a lexical rank,
`attributed_user_id`, `confidence_score`).

Tables added beyond the SRS:

- **`document_snapshots`** — the "operational snapshot blocks" the SRS 6
  hybrid model calls for, so rollback does not replay the log from zero.
- **`author_samples`** — known-authorship writing for training the classifier.
  The SRS specifies the attribution engine's *output* (`attributed_user_id`,
  `confidence_score`) but not its input; this is where the input lives.
- **`notifications`**, **`attachments`**, **`user_favorites`** — the M7
  workspace features: the inbox, uploaded images, and favourites that
  follow a user between devices. `documents` also gained `parent_id`
  (nested pages), `deleted_at` (the trash) and `published`.

## M7 workspace features

| Feature | Implementation | Verified by |
|---|---|---|
| Nested pages; sharing a page shares its sub-pages | `deps.role_on`, `deps.accessible_documents`, `routers/documents.py` | `test_pages.py::test_sharing_a_page_shares_its_sub_pages` |
| Trash, restore, delete forever, 30-day purge | `routers/documents.py` (`delete_document`, `restore_document`, `purge_trash`) | `test_pages.py::test_deleting_moves_a_page_and_its_sub_pages_to_the_trash` |
| @mentions, reply and invite notifications | `notifications.py`, `routers/notifications.py` | `test_notifications.py::test_comment_mentions_and_replies_notify_once` |
| Live inbox | `/ws/me` in `realtime/ws.py` | `test_notifications.py::test_notifications_arrive_live_on_the_user_socket` |
| Code blocks, tables, images | `web/editor.js` (`lineContexts`, `renderLine`), `routers/files.py` | `client.test.mjs`, `test_files.py` |
| Publish to web | `publish.py`, `routers/public.py` | `test_pages.py::test_publishing_serves_a_read_only_page_without_sign_in` |
| Version compare | `extras.diffLines` | `client.test.mjs` — diffLines |
| Server-side favourites, Markdown import | `routers/me.py`, `web/app.js` | `test_pages.py::test_favourites_follow_the_user_and_keep_their_order` |

---

## Not fully delivered

Stated plainly, because a traceability matrix that claims everything is done is
not worth reading.

1. **Kafka fan-out is an interface, not an implementation.** `KafkaBroadcaster`
   fixes the topic and partitioning design and raises `NotImplementedError`;
   every test and both deployment paths use the in-process broadcaster. Wiring
   `aiokafka` behind the existing interface is the remaining work.

2. **MongoDB is not used.** SRS 6 proposes a hybrid relational + document
   store. The delta log is relational here — it is small, strictly ordered, and
   queried by `(document_id, version)`, which a relational index serves well.
   The abstraction that would allow a document store is the ORM boundary, not a
   separate driver.

3. **Encryption at rest is a deployment concern.** The application does not
   encrypt columns; it relies on the database's encryption. Field-level
   encryption of document content would defeat the server-side OT the design
   depends on.

4. **Scalability is argued, not load-tested.** "Thousands of concurrent users"
   is supported by the architecture (per-document rooms, async I/O, stateless
   containers) and by per-operation costs measured up to 8 concurrent readers.
   No load test at that scale was run.

5. **OAuth providers are not integrated.** SRS 5.3 offers "OAuth, JWT" as
   alternatives; JWT with local credentials is implemented. Adding a provider
   is a new route issuing the same tokens.

6. **Rich text is markup over plain text.** Styled ranges are not synchronised
   as first-class objects; see ARCHITECTURE §8.
