"""Function-level imports as their own finding category (#149).

A function-level ``import`` is the usual workaround for an import cycle or a heavy dependency. It stops the
``ImportError`` and leaves the coupling in the graph, so each one is inventoried per module and ratcheted. A
``# deferred: <reason>`` comment on any line of the import statement, or alone on the line above it, records that it
was a decision. ``[lazy_import] rings`` limits the rule to some rings (#199); a module outside them has no findings and no count.
An import under ``if TYPE_CHECKING`` never runs and is not counted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .command_imports import COMMAND_BODY_HINT
from .facts import ModuleInfo
from .findings import Rule, Violation
from .import_syntax import RawImport

__all__ = ["LazyImportCount", "finding_lines", "lazy_import_adoption_hint", "lazy_import_counts", "lazy_import_in_scope", "lazy_import_lines", "lazy_import_violations", "lazy_imports_of"]


@dataclass(frozen=True)
class LazyImportCount:
    """How many function-level imports a module has and how many of them carry a ``# deferred:`` reason."""

    total: int
    deferred: int


def lazy_import_in_scope(rings: tuple[str, ...], ring: str | None) -> bool:
    """Whether a module in ``ring`` is covered by ``[lazy_import] rings``. No list means every module, ringless ones included."""
    return not rings or ring in rings


def lazy_imports_of(info: ModuleInfo) -> list[RawImport]:
    """The module's function-level imports that run at call time, in source order."""
    return [imp for imp in info.facts.imports if imp.lazy and not imp.type_only]


def _label(imp: RawImport) -> str:
    return "." * imp.level + imp.module


def _is_justified(imp: RawImport, justified: set[int]) -> bool:
    """Any line of the statement counts, so a formatter that wraps it cannot move the reason out of reach (#213)."""
    return any(line in justified for line in range(imp.line, max(imp.line, imp.end_line) + 1))


def lazy_import_violations(info: ModuleInfo) -> list[Violation]:
    """One finding per function-level import with no ``# deferred:`` reason, keyed by what it imports and its ordinal.

    The root cause is the imported module, so the importers of one heavy dependency group together."""
    justified = {line for line, _ in info.facts.deferred}
    seen: dict[str, int] = {}
    found: list[Violation] = []
    for imp in lazy_imports_of(info):
        label = _label(imp)
        seen[label] = seen.get(label, 0) + 1
        if not _is_justified(imp, justified):
            cause = f"{Rule.LAZY_IMPORT}|{imp.absolute_module(info.name, info.is_package)}"  # what every importer of it shares (#201)
            found.append(Violation(Rule.LAZY_IMPORT, info.name, f"{label}#{seen[label]}", imp.line, root_cause=cause))
    return found


def lazy_import_counts(info: ModuleInfo) -> LazyImportCount:
    justified = {line for line, _ in info.facts.deferred}
    imports = lazy_imports_of(info)
    return LazyImportCount(len(imports), sum(1 for imp in imports if _is_justified(imp, justified)))


_FIX = " (put each at module level, or record why it stays with `# deferred: <reason>` on the statement or above it)"


def lazy_import_lines(found: Sequence[Violation], counts: Mapping[str, LazyImportCount]) -> list[str]:
    """One line per module for ``lazy-import`` findings: ``total/deferred`` of its function-level imports, then the unjustified ones.

    Presentation only: each finding keeps its own baseline key."""
    by_module: dict[str, list[Violation]] = {}
    for v in found:
        by_module.setdefault(v.subject, []).append(v)
    lines: list[str] = []
    for module, items in sorted(by_module.items()):
        count = counts.get(module)
        totals = f"{count.total}/{count.deferred}" if count is not None else f"{len(items)}/0"
        hint = next((v.hint for v in items if v.hint), "")
        lines.append(f"{module}: [{Rule.LAZY_IMPORT}] {totals} (total/deferred), unjustified: {', '.join(v.detail for v in items)}{_FIX}{hint}")
    return lines


def lazy_import_adoption_hint(new: int, baselined: int) -> str | None:
    """How to start a ratchet when the rule has findings and none of them is baselined yet."""
    if not new or baselined:
        return None
    return f"{Rule.LAZY_IMPORT}: {new} new, 0 baselined; run 'arborescent baseline --adopt {Rule.LAZY_IMPORT}' to start a ratchet"


def finding_lines(shown: Sequence[Violation], counts: Mapping[str, LazyImportCount]) -> list[str]:
    """``check``'s findings as printed lines; the ``lazy-import`` ones are folded to one line per module (#199).

    A module's several findings inside command bodies share a line of their own, with the command-module advice (#215)."""
    others = [v.render() for v in shown if v.rule != Rule.LAZY_IMPORT]
    lazy = [v for v in shown if v.rule == Rule.LAZY_IMPORT]
    bodies = _command_body_groups(lazy)
    rest = [v for v in lazy if not (v.hint == COMMAND_BODY_HINT and v.subject in bodies)]
    commands = [
        f"{module}: [{Rule.LAZY_IMPORT}] {len(details)} function-level imports in command bodies: {', '.join(details)}{COMMAND_BODY_HINT}"
        for module, details in bodies.items()
    ]
    return others + lazy_import_lines(rest, counts) + commands


def _command_body_groups(lazy: Sequence[Violation]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for v in lazy:
        if v.hint == COMMAND_BODY_HINT:
            found.setdefault(v.subject, []).append(v.detail)
    return {module: details for module, details in found.items() if len(details) > 1}

