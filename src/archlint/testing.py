"""In-memory fakes for the ports, so tests (and your own tests of an ArchLint integration) need no subprocess."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .domain import ToolResult

__all__ = ["FakeToolRunner"]


@dataclass
class FakeToolRunner:
    """Answers by executable name; records every call. ``installed`` is the set of tools that 'exist'."""

    installed: set[str] = field(default_factory=lambda: set())
    results: dict[str, ToolResult] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def available(self, executable: str) -> bool:
        return executable in self.installed

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        self.calls.append(list(argv))
        if argv[0] not in self.installed:
            return ToolResult(None, "", f"{argv[0]}: not installed")
        return self.results.get(argv[0], ToolResult(0, "ok", ""))
