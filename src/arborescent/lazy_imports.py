"""Function-level imports as their own finding category (#149).

A function-level ``import`` is the usual workaround for an import cycle or a heavy dependency. It stops the
``ImportError`` and leaves the coupling in the graph, so each one is inventoried per module and ratcheted. A
``# deferred: <reason>`` comment on the import line, or alone on the line above it, records that it was a decision.
An import under ``if TYPE_CHECKING`` never runs and is not counted.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import Rule, Violation
from .facts import ModuleInfo, RawImport, absolute_callee

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


def _imported(info: ModuleInfo, imp: RawImport) -> str:
    """The absolute dotted name a function-level import brings in: the one root cause every module importing it shares (#201)."""
    return imp.module if not imp.level else absolute_callee(info.name, info.is_package, _label(imp)) or _label(imp)


def lazy_import_violations(info: ModuleInfo) -> list[Violation]:
    """One finding per function-level import with no ``# deferred:`` reason, keyed by what it imports and its ordinal.

    The root cause is the imported module, so the importers of one heavy dependency group together."""
    justified = {line for line, _ in info.facts.deferred}
    seen: dict[str, int] = {}
    found: list[Violation] = []
    for imp in lazy_imports_of(info):
        label = _label(imp)
        seen[label] = seen.get(label, 0) + 1
        if imp.line not in justified:
            cause = f"{Rule.LAZY_IMPORT}|{_imported(info, imp).rstrip('.')}"
            found.append(Violation(Rule.LAZY_IMPORT, info.name, f"{label}#{seen[label]}", imp.line, root_cause=cause))
    return found


def lazy_import_counts(info: ModuleInfo) -> LazyImportCount:
    justified = {line for line, _ in info.facts.deferred}
    imports = lazy_imports_of(info)
    return LazyImportCount(len(imports), sum(1 for imp in imports if imp.line in justified))
