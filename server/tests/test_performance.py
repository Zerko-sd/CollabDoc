"""Latency and throughput budgets (WBS 8.2 / 9.2, SRS 5.1).

The SRS budget is 200ms for a real-time update. These tests measure the part
of that budget the server controls -- transform, apply, log, fan out -- and
assert the 95th percentile stays inside it with room to spare for the network.
"""
from __future__ import annotations

import time

import pytest

from app.attribution.engine import AttributionEngine
from app.attribution.trail import AuthorTrail
from app.realtime.ot import TextOperation

LATENCY_BUDGET_MS = 200.0
# What the server is allowed to spend of that budget, leaving the rest for the
# round trip over the network.
SERVER_BUDGET_MS = 50.0


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def test_edit_round_trip_stays_inside_the_budget(alice):
    """Full client round trip: send an operation, wait for the acknowledgement."""
    document_id = alice.new_document("Perf", content="x" * 2000)
    latencies: list[float] = []

    with alice.client.websocket_connect(
        "/ws/documents/%s?token=%s" % (document_id, alice.token)
    ) as ws:
        init = ws.receive_json()
        version = init["version"]
        for i in range(100):
            started = time.perf_counter()
            ws.send_json(
                {
                    "type": "op",
                    "version": version,
                    "operation": [2000 + i, "y"],
                }
            )
            while True:
                message = ws.receive_json()
                if message["type"] == "ack":
                    version = message["version"]
                    break
            latencies.append((time.perf_counter() - started) * 1000)

    p95 = percentile(latencies, 0.95)
    assert p95 < LATENCY_BUDGET_MS, "p95 round trip was %.1fms" % p95
    assert percentile(latencies, 0.5) < SERVER_BUDGET_MS


def test_fan_out_to_many_readers_stays_inside_the_budget(alice, register):
    """One edit reaching eight other open tabs (SRS 5.1 scalability)."""
    readers = [register("Reader%d" % i) for i in range(8)]
    document_id = alice.new_document("Perf", content="shared text")
    for reader in readers:
        alice.post(
            "/api/documents/%s/collaborators" % document_id,
            json={"email": reader.email, "role": "VIEWER"},
        )

    import contextlib

    with contextlib.ExitStack() as stack:
        sockets = [
            stack.enter_context(
                reader.client.websocket_connect(
                    "/ws/documents/%s?token=%s" % (document_id, reader.token)
                )
            )
            for reader in readers
        ]
        for socket in sockets:
            socket.receive_json()

        author = stack.enter_context(
            alice.client.websocket_connect(
                "/ws/documents/%s?token=%s" % (document_id, alice.token)
            )
        )
        version = author.receive_json()["version"]

        latencies = []
        for i in range(20):
            started = time.perf_counter()
            author.send_json(
                {"type": "op", "version": version, "operation": [11 + i, "!"]}
            )
            while True:
                message = author.receive_json()
                if message["type"] == "ack":
                    version = message["version"]
                    break
            for socket in sockets:
                while True:
                    message = socket.receive_json()
                    if message["type"] == "op":
                        break
            latencies.append((time.perf_counter() - started) * 1000)

    p95 = percentile(latencies, 0.95)
    assert p95 < LATENCY_BUDGET_MS, "p95 fan-out to 8 readers was %.1fms" % p95


def test_transform_against_a_long_backlog_is_fast():
    """Rebasing a client that is 200 operations behind."""
    base = "word " * 2000
    # A real server history is sequential: each operation is written against
    # the document the previous one produced.
    document = base
    history = []
    for i in range(200):
        operation = TextOperation().retain(i).insert("+").retain(len(document) - i)
        history.append(operation)
        document = operation.apply(document)

    # The straggler wrote its edit against version 0 and has seen nothing since.
    incoming = TextOperation().retain(10).insert("hello").retain(len(base) - 10)

    started = time.perf_counter()
    transformed = incoming
    for concurrent in history:
        transformed, _ = TextOperation.transform(transformed, concurrent)
    elapsed = (time.perf_counter() - started) * 1000

    # The rebased operation has to fit the document as it stands now, and it
    # must still land next to the same text: every backlog operation inserted
    # ahead of the straggler's offset, so the insertion point moved from 10 to
    # 210 without the text around it changing.
    assert transformed.base_length == len(document)
    rebased = transformed.apply(document)
    assert rebased == document[:210] + "hello" + document[210:]
    assert "word word helloword" in rebased
    assert elapsed < LATENCY_BUDGET_MS, "rebase over 200 operations took %.1fms" % elapsed


def test_applying_an_operation_to_a_large_document_is_fast():
    document = "x" * 200_000
    operation = TextOperation().retain(100_000).insert("hello").retain(100_000)
    started = time.perf_counter()
    for _ in range(50):
        operation.apply(document)
    per_call = (time.perf_counter() - started) * 1000 / 50
    assert per_call < 10.0, "apply on a 200KB document took %.2fms" % per_call


@pytest.mark.parametrize("authors,blocks", [(2, 20), (3, 40)])
def test_attribution_pass_is_not_on_the_critical_path(authors, blocks):
    """Training plus attribution has to finish between keystroke bursts.

    It is debounced off the edit path, so the bar is the 1.5s quiet period the
    room waits before retraining, not the 200ms edit budget.
    """
    import corpora

    styles = ["academic", "blogger", "analyst"][:authors]
    rng_corpus = corpora.build(styles, blocks // authors + 2, seed=21)

    document = ""
    trail = AuthorTrail()
    for style, texts in rng_corpus.items():
        for text in texts:
            line = text + "\n"
            operation = TextOperation().retain(len(document)).insert(line)
            document = operation.apply(document)
            trail.apply(operation, style)

    engine = AttributionEngine(min_chars=120, min_samples_per_author=3)
    started = time.perf_counter()
    engine.train(trail.text_by_author(document))
    engine.attribute_document(document, trail)
    elapsed = (time.perf_counter() - started) * 1000
    assert elapsed < 1500.0, "attribution pass took %.0fms" % elapsed
