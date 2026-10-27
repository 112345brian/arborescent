from __future__ import annotations

from archlint.clock_adapter import SystemClock
from archlint.filesystem_adapter import LocalFileSystem
from archlint.scanner import FsScanner
from archlint.tool_runner import SubprocessToolRunner


def test_runtime_adapters_live_in_responsibility_specific_modules() -> None:
    assert SystemClock.__module__ == "archlint.clock_adapter"
    assert LocalFileSystem.__module__ == "archlint.filesystem_adapter"
    assert FsScanner.__module__ == "archlint.scanner"
    assert SubprocessToolRunner.__module__ == "archlint.tool_runner"
