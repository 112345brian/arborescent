"""Function-level imports as their own finding category (#149).

A function-level ``import`` is the usual workaround for an import cycle or a heavy dependency. It stops the
``ImportError`` and leaves the coupling in the graph, so each one is inventoried per module and ratcheted. A
``# deferred: <reason>`` comment on the import line, or alone on the line above it, records that it was a decision.
An import under ``if TYPE_CHECKING`` never runs and is not counted.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import Rule, Violation
from .facts import ModuleInfo, RawImport

__all__ = ["LazyImportCount", "lazy_import_counts", "lazy_import_violations", "lazy_imports_of"]


@dataclass(frozen=True)
class LazyImportCount:
    """How many function-level imports a module has and how many of them carry a ``# deferred:`` reason."""

    total: int
    deferred: int


def lazy_imports_of(info: ModuleInfo) -> list[RawImport]:
    """The module's function-level imports that run at call time, in source order."""
    return [imp for imp in info.facts.imports if imp.lazy and not imp.type_only]


def _label(imp: RawImport) -> str:
    return "." * imp.level + imp.module


def lazy_import_violations(info: ModuleInfo) -> list[Violation]:
    """One finding per function-level import with no ``# deferred:`` reason, keyed by what it imports and its ordinal."""
    justified = {line for line, _ in info.facts.deferred}
    seen: dict[str, int] = {}
    found: list[Violation] = []
    for imp in lazy_imports_of(info):
        label = _label(imp)
        seen[label] = seen.get(label, 0) + 1
        if imp.line not in justified:
            found.append(Violation(Rule.LAZY_IMPORT, info.name, f"{label}#{seen[label]}", imp.line))
    return found


def lazy_import_counts(info: ModuleInfo) -> LazyImportCount:
    justified = {line for line, _ in info.facts.deferred}
    imports = lazy_imports_of(info)
    return LazyImportCount(len(imports), sum(1 for imp in imports if imp.line in justified))
