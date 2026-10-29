"""Runs the browser client's jsdom suite as part of one test command (WBS 9.2).

The client tests live in JavaScript because that is the language of the thing
they test. This wrapper means `pytest` still reports the whole project's
health, rather than leaving half of it to a command someone has to remember.

Skipped when Node or jsdom is not installed: the Python suite must stay
runnable on a machine with no JavaScript toolchain.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT_TESTS = ROOT / "web" / "tests" / "client.test.mjs"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
@pytest.mark.skipif(
    not (ROOT / "web" / "tests" / "node_modules" / "jsdom").is_dir(),
    reason="jsdom is not installed (run: npm install --prefix web/tests)",
)
def test_browser_client_suite():
    completed = subprocess.run(
        [NODE, "--test", str(CLIENT_TESTS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, (
        "the browser client suite failed:\n%s\n%s"
        % (completed.stdout[-4000:], completed.stderr[-2000:])
    )
