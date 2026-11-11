"""The opt-in pytest plugin blocks real I/O in selected tests (#105). Each category proves it raises; each run is isolated."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = str(Path(__file__).resolve().parent.parent / "src")

# One body per blocked category; each must fail under the block with the plugin's message.
BLOCKED = {
    "open": "open(__file__.replace('.py', '.txt'), 'w')",
    "socket": "import socket; socket.socket().connect(('127.0.0.1', 9))",
    "subprocess": "import subprocess; subprocess.Popen([__import__('sys').executable, '-c', ''])",
    "system": "__import__('os').system('true')",
    "sqlite": "__import__('sqlite3').connect(':memory:')",
    "remove": "__import__('os').remove('nope.txt')",
    "rename": "__import__('os').rename('a.txt', 'b.txt')",
    "time": "__import__('time').time()",
    "datetime": "__import__('datetime').datetime.now()",
    "random": "__import__('random').random()",
    "environ": "__import__('os').environ['PATH']",
    "getenv": "__import__('os').getenv('PATH')",
}


def run(tmp_path: Path, body: str, *args: str, ini: str = "") -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_ring.py").write_text(textwrap.dedent(body), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n" + ini, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": SRC, "PYTEST_ADDOPTS": ""}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "arborescent.pytest_io_block", "-p", "no:cacheprovider", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("category", sorted(BLOCKED))
def test_each_category_raises_under_the_block(tmp_path: Path, category: str) -> None:
    result = run(tmp_path, f"def test_x():\n    {BLOCKED[category]}\n", "--arbor-io-block")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "IOBlockedError" in result.stdout


@pytest.mark.parametrize("category", sorted(BLOCKED))
def test_unselected_tests_run_normally(tmp_path: Path, category: str) -> None:
    body = f"def test_x():\n    try:\n        {BLOCKED[category]}\n    except (OSError, ValueError, KeyError):\n        pass\n"
    result = run(tmp_path, body)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ini_globs_select_by_rootdir_relative_posix_path(tmp_path: Path) -> None:
    body = "def test_x():\n    __import__('time').time()\n"
    assert run(tmp_path, body, ini="arbor_io_free =\n    test_r*.py\n").returncode == 1
    assert run(tmp_path, body, ini="arbor_io_free =\n    other/*.py\n").returncode == 0


def test_allow_io_marker_opts_one_test_out(tmp_path: Path) -> None:
    body = """
        import pytest
        @pytest.mark.arbor_allow_io
        def test_free():
            __import__('time').time()
        def test_blocked():
            __import__('time').time()
    """
    result = run(tmp_path, body, "--arbor-io-block")
    assert "1 failed, 1 passed" in result.stdout


def test_pure_code_fixtures_and_source_imports_still_work(tmp_path: Path) -> None:
    body = """
        import pytest
        @pytest.fixture
        def stamp():
            import time
            return time.time()   # setup is not blocked
        def test_pure(stamp, tmp_path):
            import json          # source-file reads are allowed
            assert json.dumps([1]) == "[1]" and stamp > 0
            assert tmp_path.exists()
    """
    result = run(tmp_path, body, "--arbor-io-block")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_patches_are_removed_when_the_blocked_test_ends(tmp_path: Path) -> None:
    """Review finding: the stand-ins used to stay installed for the rest of the session, so a later, unselected test saw a
    `datetime.datetime` that real datetimes are not instances of by type, and could not pickle one."""
    body = """
        import datetime, os, pickle, random, time
        import pytest

        def test_blocked():
            pass

        @pytest.mark.arbor_allow_io
        def test_after_the_block():
            assert datetime.datetime.__module__ == "datetime"
            assert type(datetime.datetime(2026, 1, 1)) is datetime.datetime
            pickle.dumps(datetime.datetime(2026, 1, 1))
            assert isinstance(os.environ, os._Environ)
            assert time.sleep.__module__ == "time"
            assert random.random.__module__ != "arborescent.pytest_io_block"
    """
    result = run(tmp_path, body, "--arbor-io-block")
    assert result.returncode == 0, result.stdout + result.stderr
