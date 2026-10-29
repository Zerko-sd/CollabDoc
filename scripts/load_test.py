"""Load test: many simultaneous editors on one document.

N clients sign up, join the same page over WebSockets and type random
characters at random positions at a fixed rate, all at once. Each client is a
full OT client -- one operation in flight, later keystrokes buffered and
composed, remote operations transformed against both -- exactly like the
browser's ``OTClient``. When the typing stops, the script checks that every
client and the server hold the same text (SRS 3.3: convergence) and reports
how long the server took to acknowledge each keystroke (SRS 5.1: latency).

    python -m uvicorn app.main:app --app-dir server --port 8000
    python scripts/load_test.py --base http://127.0.0.1:8000 --clients 10 50 100

Results from a run are recorded in docs/LOAD-TEST.md.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import string
import sys
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import Manager
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

try:
    import websockets
except ImportError:  # pragma: no cover
    sys.exit("This script needs the websockets package: pip install websockets")

from app.realtime.ot import TextOperation  # noqa: E402

PASSWORD = "load-test-password-1"


def http(base: str, method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode()
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as error:
        raise SystemExit("%s %s -> %d: %s" % (method, path, error.code, error.read().decode()[:300]))


@dataclass
class Client:
    """One editor: a socket plus the OT client state machine."""

    name: str
    token: str
    version: int = 0
    text: str = ""
    outstanding: TextOperation | None = None
    buffer: TextOperation | None = None
    sent_at: float = 0.0
    latencies: list[float] = field(default_factory=list)
    server_latencies: list[float] = field(default_factory=list)
    typed: int = 0
    resyncs: int = 0
    socket: object = None
    last_rx: float = 0.0

    async def send_op(self, operation: TextOperation) -> None:
        self.sent_at = time.perf_counter()
        await self.socket.send(json.dumps({"type": "op", "version": self.version, "operation": operation.to_json()}))

    async def type_one(self, rng: random.Random) -> None:
        position = rng.randint(0, len(self.text))
        char = rng.choice(string.ascii_lowercase + "  ")
        operation = TextOperation().retain(position).insert(char).retain(len(self.text) - position)
        self.text = operation.apply(self.text)
        self.typed += 1
        if self.outstanding is None:
            self.outstanding = operation
            await self.send_op(operation)
        else:
            self.buffer = operation if self.buffer is None else TextOperation.compose(self.buffer, operation)

    async def on_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "ack":
            self.latencies.append((time.perf_counter() - self.sent_at) * 1000)
            if "server_latency_ms" in message:
                self.server_latencies.append(message["server_latency_ms"])
            self.version = message["version"]
            self.outstanding, self.buffer = self.buffer, None
            if self.outstanding is not None:
                await self.send_op(self.outstanding)
        elif kind == "op":
            operation = TextOperation.from_json(message["operation"])
            if self.outstanding is not None:
                self.outstanding, operation = TextOperation.transform(self.outstanding, operation)
                if self.buffer is not None:
                    self.buffer, operation = TextOperation.transform(self.buffer, operation)
            self.text = operation.apply(self.text)
            self.version = message["version"]
        elif kind == "error":
            if message.get("code") == "resync_required":
                self.resyncs += 1
            raise RuntimeError("%s got an error: %s" % (self.name, message))

    @property
    def idle(self) -> bool:
        return self.outstanding is None and self.buffer is None


async def run_client(client: Client, url: str, rate: float, seconds: float, seed: int, connected, go: asyncio.Event) -> None:
    rng = random.Random(seed)
    async with websockets.connect(url, max_size=None, open_timeout=60, ping_timeout=None) as socket:
        client.socket = socket
        init = json.loads(await socket.recv())
        assert init["type"] == "init", init
        client.version, client.text = init["version"], init["content"]
        client.last_rx = time.time()

        async def reader() -> None:
            async for raw in socket:
                client.last_rx = time.time()
                await client.on_message(json.loads(raw))

        read_task = asyncio.create_task(reader())
        connected()
        await go.wait()
        start_at = time.time()
        deadline = start_at + seconds
        interval = 1.0 / rate
        next_at = time.time() + rng.random() * interval  # stagger the clients
        while time.time() < deadline:
            await asyncio.sleep(max(0.0, next_at - time.time()))
            await client.type_one(rng)
            next_at += interval
        # Stay connected until our own edits are acknowledged and everyone
        # else's have stopped arriving.
        settle_by = time.time() + 180
        while time.time() < settle_by:
            if client.idle and time.time() - client.last_rx > 2.0:
                break
            await asyncio.sleep(0.1)
        read_task.cancel()


async def _worker(base: str, document_id: str, slots: list[tuple[str, str, int]], rate: float, seconds: float, barrier) -> list[dict]:
    ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
    clients = [Client(name=name, token=token) for name, token, _ in slots]
    go = asyncio.Event()
    count = 0
    all_connected = asyncio.Event()

    def connected() -> None:
        nonlocal count
        count += 1
        if count == len(clients):
            all_connected.set()

    async def coordinate() -> None:
        # Every client in every process is connected before anyone types.
        await all_connected.wait()
        await asyncio.to_thread(barrier.wait)
        go.set()

    await asyncio.gather(
        coordinate(),
        *(
            run_client(client, "%s/ws/documents/%s?token=%s" % (ws_base, document_id, client.token), rate, seconds, seed, connected, go)
            for client, (_, _, seed) in zip(clients, slots)
        ),
    )
    return [
        {"text": c.text, "version": c.version, "latencies": c.latencies, "server": c.server_latencies, "typed": c.typed}
        for c in clients
    ]


def worker(args: tuple) -> list[dict]:
    """One load-generating process: a share of the clients, on its own event loop."""
    return asyncio.run(_worker(*args))


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q / 100 * (len(ordered) - 1))))
    return ordered[index]


def scenario(base: str, clients: int, rate: float, seconds: float, processes: int) -> dict:
    tag = uuid.uuid4().hex[:8]
    owner = http(base, "POST", "/api/auth/register", {"email": "owner-%s@example.com" % tag, "name": "Load owner", "password": PASSWORD})
    document = http(base, "POST", "/api/documents", {"title": "Load test %s (%d clients)" % (tag, clients)}, owner["access_token"])
    http(base, "PUT", "/api/documents/%s/share" % document["id"], {"role": "EDITOR"}, owner["access_token"])
    share_token = http(base, "GET", "/api/documents/%s/share" % document["id"], token=owner["access_token"])["share_token"]

    slots = []
    for i in range(clients):
        account = http(base, "POST", "/api/auth/register", {"email": "editor%d-%s@example.com" % (i, tag), "name": "Editor %d" % i, "password": PASSWORD})
        http(base, "POST", "/api/documents/join/%s" % share_token, {}, account["access_token"])
        slots.append(("editor%d" % i, account["access_token"], i))

    # The clients are split across processes so that the load generator --
    # which parses and transforms every broadcast, N x N of them -- is not
    # the thing being measured. All of them start typing at the same moment.
    processes = max(1, min(processes, clients))
    shares = [slots[i::processes] for i in range(processes)]
    with Manager() as manager, ProcessPoolExecutor(max_workers=processes) as pool:
        barrier = manager.Barrier(processes)
        results = [r for part in pool.map(worker, [(base, document["id"], share, rate, seconds, barrier) for share in shares]) for r in part]

    server_text = http(base, "GET", "/api/documents/%s" % document["id"], token=owner["access_token"])["content"]
    texts = {r["text"] for r in results}
    latencies = [value for r in results for value in r["latencies"]]
    server = [value for r in results for value in r["server"]]
    typed = sum(r["typed"] for r in results)
    return {
        "clients": clients,
        "keystrokes": typed,
        "throughput": round(typed / seconds, 1),
        "converged": len(texts) == 1 and server_text in texts,
        "length": len(server_text),
        "expected_length": typed,
        "ack_p50": percentile(latencies, 50),
        "ack_p95": percentile(latencies, 95),
        "ack_p99": percentile(latencies, 99),
        "ack_max": max(latencies) if latencies else float("nan"),
        "server_p50": percentile(server, 50),
        "server_p99": percentile(server, 99),
        "acks": len(latencies),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--clients", type=int, nargs="+", default=[10, 50, 100])
    parser.add_argument("--rate", type=float, default=5.0, help="keystrokes per second per client")
    parser.add_argument("--seconds", type=float, default=10.0, help="how long each client types")
    parser.add_argument("--processes", type=int, default=min(8, os.cpu_count() or 2), help="load-generating processes")
    args = parser.parse_args()

    print(
        "%g keystrokes/s per client for %.0f s, generated by %d processes\n"
        % (args.rate, args.seconds, args.processes)
    )
    header = "%8s %10s %10s %9s %9s %9s %9s %9s %10s" % (
        "clients", "keystrokes", "per sec", "ack p50", "ack p95", "ack p99", "ack max", "srv p50", "converged",
    )
    print(header)
    print("-" * len(header))
    failed = False
    for clients in args.clients:
        result = scenario(args.base, clients, args.rate, args.seconds, args.processes)
        ok = result["converged"] and result["length"] == result["expected_length"]
        failed |= not ok
        print(
            "%8d %10d %10.1f %7.1fms %7.1fms %7.1fms %7.1fms %7.2fms %10s"
            % (
                result["clients"], result["keystrokes"], result["throughput"], result["ack_p50"], result["ack_p95"],
                result["ack_p99"], result["ack_max"], result["server_p50"], "yes" if ok else "NO",
            )
        )
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
