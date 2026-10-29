"""Drive a real collaborative session against a running server.

Two clients connect to the same document over WebSockets, type in noticeably
different styles, and deliberately send overlapping edits written against the
same version. The script then checks that both clients and the server agree on
the text, and prints the authorship attribution the AI engine produced.

    python -m uvicorn app.main:app --app-dir server --port 8000
    python scripts/demo.py --base http://127.0.0.1:8000

It is a demonstration and a smoke test: every assertion it makes is one the
SRS asks for -- convergence under concurrent edits (3.3), presence (3.3), and
attribution with confidence scores (6.5).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

try:
    import websockets
except ImportError:  # pragma: no cover
    sys.exit("This script needs the websockets package: pip install websockets")

from app.realtime.ot import TextOperation  # noqa: E402

ALICE_LINES = [
    "# Quarterly Research Summary\n",
    "It should be noted that the underlying methodology, which has been "
    "extensively documented in the preceding literature, remains fundamentally "
    "sound; furthermore, the observed correlation between these variables "
    "warrants considerable further investigation.\n",
    "Consequently, one observes that such considerations, taken together with "
    "the theoretical framework established earlier, substantiate the original "
    "hypothesis to a satisfactory degree.\n",
]

BOB_LINES = [
    "Here's the thing. It's simple.\n",
    "We shipped it. It works! Nobody's complained yet.\n",
    "Look, I don't buy the long version. That's it. That's the whole trick!\n",
]


def call(base: str, path: str, payload=None, token=None, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        base + path, data=data, method=method or ("POST" if data else "GET")
    )
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        sys.exit("%s %s failed: %s %s" % (request.get_method(), path, error.code, error.read().decode()))
    except urllib.error.URLError as error:
        sys.exit("Could not reach %s -- is the server running? (%s)" % (base, error.reason))


class Client:
    """A minimal OT client: one operation in flight, the rest buffered."""

    def __init__(self, name: str, token: str, socket) -> None:
        self.name = name
        self.token = token
        self.socket = socket
        self.content = ""
        self.version = 0
        self.outstanding: TextOperation | None = None
        self.buffer: TextOperation | None = None
        self.acked = asyncio.Event()
        self.latencies: list[float] = []
        self._sent_at = 0.0

    async def send_operation(self, operation: TextOperation) -> None:
        self._sent_at = time.perf_counter()
        await self.socket.send(
            json.dumps(
                {"type": "op", "version": self.version, "operation": operation.to_json()}
            )
        )

    async def type(self, text: str, at: int | None = None) -> None:
        """Queue an insert the way the editor would on a keystroke burst."""
        position = len(self.content) if at is None else at
        operation = (
            TextOperation()
            .retain(position)
            .insert(text)
            .retain(len(self.content) - position)
        )
        self.content = operation.apply(self.content)
        if self.outstanding is None:
            self.outstanding = operation
            await self.send_operation(operation)
        elif self.buffer is None:
            self.buffer = operation
        else:
            self.buffer = TextOperation.compose(self.buffer, operation)

    async def pump(self, until: float) -> None:
        """Read messages until the deadline, keeping the local document current."""
        while True:
            remaining = until - time.perf_counter()
            if remaining <= 0:
                return
            try:
                raw = await asyncio.wait_for(self.socket.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            message = json.loads(raw)
            kind = message.get("type")

            if kind == "init":
                self.content = message["content"]
                self.version = message["version"]
            elif kind == "ack":
                self.latencies.append((time.perf_counter() - self._sent_at) * 1000)
                self.version += 1
                self.outstanding, self.buffer = self.buffer, None
                if self.outstanding is not None:
                    await self.send_operation(self.outstanding)
                else:
                    self.acked.set()
            elif kind == "op":
                incoming = TextOperation.from_json(message["operation"])
                self.version += 1
                if self.outstanding is not None:
                    self.outstanding, incoming = TextOperation.transform(
                        self.outstanding, incoming
                    )
                    if self.buffer is not None:
                        self.buffer, incoming = TextOperation.transform(
                            self.buffer, incoming
                        )
                self.content = incoming.apply(self.content)
            elif kind == "error":
                print("  ! %s: %s" % (self.name, message["message"]))

    async def settle(self, seconds: float = 2.5) -> None:
        await self.pump(time.perf_counter() + seconds)


async def main(base: str) -> int:
    ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
    tag = uuid.uuid4().hex[:6]

    print("Registering two authors...")
    alice = call(base, "/api/auth/register", {
        "email": "alice-%s@example.com" % tag, "name": "Alice Chen", "password": "demo-password-1"
    })
    bob = call(base, "/api/auth/register", {
        "email": "bob-%s@example.com" % tag, "name": "Bob Ortiz", "password": "demo-password-2"
    })

    document = call(base, "/api/documents", {"title": "Demo: two styles"}, alice["access_token"])
    call(
        base,
        "/api/documents/%s/collaborators" % document["id"],
        {"email": bob["user"]["email"], "role": "EDITOR"},
        alice["access_token"],
    )
    print("Document %s created and shared." % document["id"])

    url = "%s/ws/documents/%s?token=%s"
    async with websockets.connect(url % (ws_base, document["id"], alice["access_token"])) as a_ws, \
        websockets.connect(url % (ws_base, document["id"], bob["access_token"])) as b_ws:
        a = Client("Alice", alice["access_token"], a_ws)
        b = Client("Bob", bob["access_token"], b_ws)
        await asyncio.gather(a.settle(1.0), b.settle(1.0))

        print("Typing (interleaved, both writing against the version they last saw)...")
        for a_line, b_line in zip(ALICE_LINES, BOB_LINES):
            await a.type(a_line)
            await b.type(b_line)
            await asyncio.gather(a.settle(0.6), b.settle(0.6))

        # One deliberately concurrent pair: both edit at offset 0 at once.
        await a.type("(A) ", at=0)
        await b.type("(B) ", at=0)
        await asyncio.gather(a.settle(2.0), b.settle(2.0))
        # Give the debounced attribution pass time to run and broadcast.
        await asyncio.gather(a.settle(3.0), b.settle(3.0))

    server = call(base, "/api/documents/%s" % document["id"], token=alice["access_token"])

    print("\n--- convergence ---")
    checks = [
        ("Alice matches the server", a.content == server["content"]),
        ("Bob matches the server", b.content == server["content"]),
        ("Both clients match each other", a.content == b.content),
    ]
    for label, ok in checks:
        print("  %s %s" % ("PASS" if ok else "FAIL", label))

    latencies = sorted(a.latencies + b.latencies)
    if latencies:
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print("\n--- latency (SRS 5.1 budget: 200ms) ---")
        print("  median %.1fms, p95 %.1fms over %d operations"
              % (latencies[len(latencies) // 2], p95, len(latencies)))

    attribution = call(
        base,
        "/api/documents/%s/attribution?refresh=true" % document["id"],
        token=alice["access_token"],
    )
    names = {alice["user"]["id"]: "Alice", bob["user"]["id"]: "Bob"}
    model = attribution["model"]
    print("\n--- AI authorship attribution ---")
    print("  model trained: %s (%d samples, training accuracy %.0f%%)"
          % (model["trained"], model["samples"], model["training_accuracy"] * 100))
    for block in attribution["blocks"]:
        evidence = block["evidence"]
        print(
            "  [%-9s] %-6s %3d%%  style=%-6s ledger=%-6s  %s"
            % (
                block["block_type"],
                names.get(block["attributed_user_id"], "?"),
                round(block["confidence_score"] * 100),
                names.get(evidence["style_user_id"], "--"),
                names.get(evidence["ledger_user_id"], "--"),
                (block["content"][:46] + "...") if len(block["content"]) > 46 else block["content"],
            )
        )

    metrics = call(
        base,
        "/api/documents/%s/attribution/metrics" % document["id"],
        token=alice["access_token"],
    )
    print("\n  style/keystroke agreement: %.0f%% over %d blocks"
          % (metrics["agreement_rate"] * 100, metrics["block_count"]))

    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="server base URL")
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(arguments.base.rstrip("/"))))
