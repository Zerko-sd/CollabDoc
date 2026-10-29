"""Every browser module must parse as an ES module (WBS 9.1).

A syntax error in `app.js` does not fail loudly: the module simply never
evaluates, so no event listener is ever attached and the page renders as dead
HTML. The jsdom suite catches it, but only because it imports the module --
this parses each file on its own so the failure names the file and the line.

`node --check` infers the module system from the file extension, so each file
is copied to a `.mjs` temporary before checking.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "web"
NODE = shutil.which("node")
MODULES = sorted(WEB.glob("*.js"))


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
@pytest.mark.parametrize("module", MODULES, ids=lambda path: path.name)
def test_module_parses(module: Path, tmp_path: Path):
    copy = tmp_path / (module.stem + ".mjs")
    copy.write_bytes(module.read_bytes())
    completed = subprocess.run(
        [NODE, "--check", str(copy)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, "%s does not parse:\n%s" % (
        module.name,
        completed.stderr,
    )
