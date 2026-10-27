"""Tool-to-package metadata and interpreter-aware setup commands."""

from __future__ import annotations

from collections.abc import Sequence

from .ports import Engine, ToolRunner

__all__ = [
    "TOOL_REQUIREMENTS",
    "missing_tool_requirements",
    "tool_requirements",
]

# Keep these exact floors aligned with [project.optional-dependencies] in pyproject.toml.
TOOL_REQUIREMENTS = {
    "lint-imports": "import-linter>=2.0",
    "tach": "tach>=0.30",
    "ruff": "ruff>=0.16.3",
    "pyright": "pyright>=1.1.400",
    "basedpyright": "basedpyright>=1.30",
    "ty": "ty>=0.0.50",
    "pyrefly": "pyrefly>=0.40",
}


def tool_requirements(engines: Sequence[Engine]) -> tuple[tuple[str, str], ...]:
    """Return unique executable-to-package pairs for engines selected by this policy."""
    pairs: dict[str, str] = {}
    for engine in engines:
        executable = engine.executable
        requirement = TOOL_REQUIREMENTS.get(executable)
        if requirement is None:
            raise ValueError(f"no install requirement is registered for {engine.name} ({executable})")
        pairs[executable] = requirement
    return tuple(sorted(pairs.items()))


def missing_tool_requirements(engines: Sequence[Engine], runner: ToolRunner) -> tuple[tuple[str, str], ...]:
    """Filter selected tool packages through a ToolRunner-like availability callback."""
    return tuple((tool, requirement) for tool, requirement in tool_requirements(engines) if not runner.available(tool))
