"""The top-level help path leaves command implementations unloaded."""

from __future__ import annotations

import subprocess
import sys


def test_help_defers_runtime_adapters_and_command_handlers() -> None:
    probe = """
import runpy
import sys

sys.argv = ["arborescent", "--help"]
try:
    runpy.run_module("arborescent", run_name="__main__")
except SystemExit as exc:
    assert exc.code == 0, exc.code
else:
    raise AssertionError("expected argparse to exit after printing help")

deferred = {
    "arborescent.adapters",
    "arborescent.cli_cycles",
    "arborescent.package_checks",
    "arborescent.selftest",
}
assert not deferred.intersection(sys.modules), sorted(deferred.intersection(sys.modules))
"""
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "usage: arborescent" in result.stdout
