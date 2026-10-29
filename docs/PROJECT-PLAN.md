# Project plan against delivery

The WBS and six-week schedule from the planning document, set beside what was
actually built. Milestone dates are the plan's; the status column is the code.

---

## 1. Work breakdown, by deliverable

| WBS | Work package | Status | Where it lives |
|---|---|---|---|
| 1.1 | Project charter and WBS | Done | this document |
| 1.2 | Risk plan and status tracking | Done | risk register below |
| 2.1 | High-level cloud architecture | Done | `docs/ARCHITECTURE.md`, `Dockerfile`, `docker-compose.yml` |
| 2.2 | Database schema | Done | `server/app/models.py` |
| 2.3 | API and WebSocket contracts | Done | `schemas.py`, `realtime/ws.py`, `GET /docs` |
| 3.1 | JWT authentication | Done | `security.py`, `routers/auth.py` |
| 3.2 | Role-based access control | Done | `deps.py`, `routers/collaborators.py` |
| 4.1 | Document CRUD APIs | Done | `routers/documents.py` |
| 4.2 | Sharing and version history | Done | `routers/collaborators.py`, `session.content_at`/`revert` |
| 5.1 | Operational transformation engine | Done | `realtime/ot.py` (+ `web/ot.js`) |
| 5.2 | WebSocket sync server | Done | `realtime/ws.py`, `realtime/hub.py` |
| 5.3 | User presence indicators | Done | `hub.Participant`, avatars and carets in `web/app.js` |
| 6.1 | Stylometric feature extraction | Done | `attribution/features.py` — 44 features |
| 6.2 | ML model training | Done | `attribution/classifier.py` — softmax regression |
| 6.3 | Real-time attribution service | Done | `attribution/engine.py`, `session.refresh_attribution` |
| 7.1 | Rich-text editor component | Done, with a caveat | `web/app.js` — markup over plain text (ARCHITECTURE §8) |
| 7.2 | Authorship highlighting UI | Done | per-block tint, confidence, evidence breakdown |
| 7.3 | Presence avatars and cursors | Done | mirror-measured remote carets |
| 8.1 | Data encryption | Partial | TLS in transit; at rest is the database's job (TRACEABILITY) |
| 8.2 | Performance optimisation | Done | 2.3ms median / 3.9ms p95 against a 200ms budget |
| 9.1 | Unit and integration tests | Done | 77 tests across the engine, auth, documents and attribution |
| 9.2 | End-to-end collaborative tests | Done | 21 tests: `test_realtime.py` (13) and `client.test.mjs` (8) |
| 9.3 | AI attribution accuracy tests | Done | 3 accuracy tests against an explicit 85% floor; 100% held-out on the synthetic corpora |
| 10.1 | Cloud deployment and CI/CD | Done | `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml` |
| 10.2 | User documentation | Done | `README.md` and `docs/` |

## 2. Milestones

| Milestone | Target | Deliverable | Status |
|---|---|---|---|
| M1 — Design complete | Day 6 | Architecture, DB schema, API contracts | Met |
| M2 — Core backend ready | Day 13 | Auth and document CRUD functional | Met |
| M3 — Collaboration live | Day 21 | OT + WebSocket sync with presence | Met |
| M4 — AI attribution integrated | Day 23 | Author highlighting visible in the editor | Met |
| M5 — QA complete | Day 28 | All tests passing, performance verified | Met — 106 tests in ~26s, plus 24 live-browser checks (174 and 55 after M7) |
| M6 — Production launch | Day 30 | Cloud deploy with CI/CD and docs | Met, single-node (Kafka fan-out outstanding) |
| M7 — Workspace features | Review 2 | Nested pages and trash; @mentions, notifications and inbox; code blocks, tables and images; publish to web; version compare; server-side favourites; Markdown import | Met — 174 tests, plus 55 live-browser checks |

## 3. WBS dictionary — the key packages and their acceptance criteria

| WBS | Milestone in the plan | Result |
|---|---|---|
| 5.1 OT engine | Unit tests pass | 20 unit tests, 2000 randomised convergence cases, and cross-language parity with the browser engine |
| 5.2 WebSocket sync | <200ms latency | 2.3ms median, 3.9ms p95 end to end |
| 6.2 ML model training | ≥85% accuracy on a test set | 100% held-out on the synthetic corpora — see the caveat in the README |
| 6.3 Real-time attribution | Attribution visible in the editor | Per-block tint with author, confidence, and the style/ledger evidence behind it |
| 7.1 Rich-text editor | Formatting works single-user | Bold, italic, underline, code, H1/H2, bulleted and numbered lists, with keyboard shortcuts |
| 8.2 Performance optimisation | Load test passes | Fan-out to 8 concurrent readers inside budget; no load test at the thousands-of-users scale |
| 9.3 AI accuracy test | Test report signed off | `test_attribution.py`, with the 85% floor as an explicit constant |

## 4. Risk register (WBS 1.2)

| Risk identified in the plan | What actually happened |
|---|---|
| **OT algorithm complexity** | The real risk was not writing `transform` but keeping two implementations in step. Addressed by cross-language parity testing rather than by review. |
| **WebSocket scaling issues** | Not reached at this scale. The fan-out path goes through the broadcaster interface so the multi-node change is localised. |
| **Limited ML training time** | Sidestepped: a small hand-written classifier trains in milliseconds, so there is no training job to schedule. |
| **Attribution latency** | Handled by debouncing the pass off the edit path and running it in a worker thread. |
| **Browser bugs (rich text)** | Contained rather than avoided: the page is a `contenteditable` block editor, but every `beforeinput` is cancelled and turned into a text operation, so the browser never edits the DOM on its own and the plain-text buffer stays the source of truth. The browser bugs that did appear were CSS and module-loading ones, found only once two real browsers were driven against the app — see the README. |
| **Bottlenecks** | The one found was not CPU: a disconnect cancels the handler task, so awaiting a durable write during teardown silently lost data. Fixed with a synchronous flush (ARCHITECTURE §3). |
| **Model reliability** | The engine abstains rather than guessing when it has too little writing, and reports style/ledger agreement as an honest runtime quality signal. |

## 5. Test summary

| Suite | Tests |
|---|---|
| `test_ot.py` | 20 |
| `test_auth_rbac.py` | 16 |
| `test_documents.py` | 15 |
| `test_realtime.py` | 15 |
| `test_attribution.py` | 28 |
| `test_performance.py` | 6 |
| `test_client_parity.py` | 1 (400 cross-checked cases) |
| `test_client_syntax.py` | 5 (one per browser module) |
| `test_workspace.py` | 9 |
| `test_pages.py` | 18 |
| `test_notifications.py` | 11 |
| `test_files.py` | 11 |
| `test_client_ui.py` → `client.test.mjs` | 19 |
| **Subtotal** | **174** — `pytest` reports 156 items, running the 19 jsdom tests as one. ~60 seconds. |
| `scripts/browser_check.py` | 55 checks in two live Chromium browsers |
