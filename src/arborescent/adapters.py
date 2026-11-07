"""Compatibility exports for Arborescent's runtime adapters.

New code should import adapters from their responsibility-specific modules.
"""

from .clock_adapter import SystemClock
from .filesystem_adapter import LocalFileSystem
from .parallel_adapter import ProcessPoolParallel
from .scanner import FsScanner, ScanData
from .tool_runner import SubprocessToolRunner, resolve_executable

__all__ = ["FsScanner", "LocalFileSystem", "ProcessPoolParallel", "ScanData", "SubprocessToolRunner", "SystemClock", "resolve_executable"]
