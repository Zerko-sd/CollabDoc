"""The browser OT engine must agree with the server's, exactly (WBS 9.1).

`web/ot.js` is a port of `server/app/realtime/ot.py`. If the two ever disagree
about a single transform, two clients editing the same document silently end
up with different text -- the one failure mode operational transformation
exists to prevent, and the one hardest to notice in a running system.

This test generates random concurrent edits in Python, hands them to Node to
transform with the browser implementation, and requires the results to match
component for component. It is skipped when Node is not installed.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from app.realtime.ot import TextOperation

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

# Reads the cases on stdin, echoes back what the browser engine computes.
DRIVER = """
import { TextOperation } from %(module)s;

let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  const cases = JSON.parse(input);
  const results = cases.map(({ doc, a, b }) => {
    const opA = TextOperation.fromJSON(a);
    const opB = TextOperation.fromJSON(b);
    const [aPrime, bPrime] = TextOperation.transform(opA, opB);
    return {
      aPrime: aPrime.toJSON(),
      bPrime: bPrime.toJSON(),
      converged: bPrime.apply(opA.apply(doc)),
      composed: TextOperation.compose(opA, bPrime).toJSON(),
      inverted: opA.invert(doc).toJSON(),
      diff: TextOperation.fromDiff(doc, "rewritten entirely").toJSON(),
      cursor: opA.transformCursor(Math.floor(doc.length / 2)),
    };
  });
  process.stdout.write(JSON.stringify(results));
});
"""


def _random_operation(doc: str, rng: random.Random) -> TextOperation:
    op = TextOperation()
    i = 0
    while i < len(doc):
        roll = rng.random()
        span = min(len(doc) - i, rng.randint(1, 4))
        if roll < 0.4:
            op.retain(span)
            i += span
        elif roll < 0.7:
            op.delete(span)
            i += span
        else:
            op.insert("".join(rng.choice("abc ") for _ in range(rng.randint(1, 3))))
    if rng.random() < 0.5:
        op.insert("!")
    return op


def _run_node(cases: list[dict], tmp_path: Path) -> list[dict]:
    module = json.dumps((WEB_DIR / "ot.js").as_uri())
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER % {"module": module}, encoding="utf-8")
    completed = subprocess.run(
        [NODE, str(driver)],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_browser_engine_matches_the_server_engine(tmp_path):
    rng = random.Random(31337)
    cases = []
    expected = []
    for _ in range(400):
        doc = "".join(rng.choice("abcdef ") for _ in range(rng.randint(1, 30)))
        a = _random_operation(doc, rng)
        b = _random_operation(doc, rng)
        cases.append({"doc": doc, "a": a.to_json(), "b": b.to_json()})

        a_prime, b_prime = TextOperation.transform(a, b)
        expected.append(
            {
                "aPrime": a_prime.to_json(),
                "bPrime": b_prime.to_json(),
                "converged": b_prime.apply(a.apply(doc)),
                "composed": TextOperation.compose(a, b_prime).to_json(),
                "inverted": a.invert(doc).to_json(),
                "diff": TextOperation.from_diff(doc, "rewritten entirely").to_json(),
                "cursor": a.transform_cursor(len(doc) // 2),
            }
        )

    actual = _run_node(cases, tmp_path)
    assert len(actual) == len(expected)
    for index, (got, want) in enumerate(zip(actual, expected)):
        assert got == want, "case %d disagreed:\n  js=%r\n  py=%r" % (index, got, want)
