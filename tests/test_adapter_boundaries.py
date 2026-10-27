from __future__ import annotations

from arborescent.clock_adapter import SystemClock
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.scanner import FsScanner
from arborescent.tool_runner import SubprocessToolRunner


def test_runtime_adapters_live_in_responsibility_specific_modules() -> None:
    assert SystemClock.__module__ == "arborescent.clock_adapter"
    assert LocalFileSystem.__module__ == "arborescent.filesystem_adapter"
    assert FsScanner.__module__ == "arborescent.scanner"
    assert SubprocessToolRunner.__module__ == "arborescent.tool_runner"
