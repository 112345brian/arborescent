"""What ArchLint needs from the world. Adapters implement these; tests substitute fakes (``archlint.testing``)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from .domain import Policy, ToolResult
from .facts import ModuleInfo

__all__ = ["ModuleScanner", "ScanResult", "ToolRunner", "TypeChecker"]


class ToolRunner(Protocol):
    """Runs an external engine (lint-imports, tach, a type checker, git). Never raises for a missing tool."""

    def available(self, executable: str) -> bool: ...

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult: ...


class TypeChecker(Protocol):
    """A swappable type checker. The policy renders its strict configuration; ``command`` runs it."""

    @property
    def name(self) -> str: ...

    @property
    def executable(self) -> str: ...

    @property
    def config_file(self) -> str: ...

    def render_config(self, policy: Policy, paths: Sequence[str]) -> str: ...

    def command(self, files: Sequence[str]) -> list[str]: ...

    def passed(self, result: ToolResult) -> bool: ...


class ScanResult(Protocol):
    @property
    def known(self) -> frozenset[str]: ...  # every module name under the policy's sources

    @property
    def modules(self) -> Mapping[str, ModuleInfo]: ...  # the ones parsed (all, or only the changed ones)

    @property
    def entry_deps(self) -> Mapping[str, tuple[str, ...] | None]: ...  # entry script -> PEP 723 dependencies

    @property
    def paths(self) -> Mapping[str, str]: ...  # module name -> posix path relative to the root

    @property
    def parsed(self) -> int: ...  # files parsed this run (cache misses)


class ModuleScanner(Protocol):
    """Finds the policy's modules and parses each at most once (content-hash cached)."""

    def scan(self, policy: Policy, root: Path, only: frozenset[str] | None = None) -> ScanResult: ...

    def uncached(self) -> ModuleScanner: ...  # same scanner without persistent state (mutation tests)
