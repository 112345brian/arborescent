"""What tach cannot see (#59): imports resolved at run time never reach its static graph. Pure.

``tach check`` reads ``import`` statements. A call such as ``importlib.import_module(name)`` or ``__import__(name)`` creates
an edge it does not follow (the generated ``tach.toml`` leaves ``include_string_imports`` off, and a computed name could not
be followed anyway), so a clean tach run says nothing about those edges. The scan already records them as the
``dynamic-import`` effect; this reports them per module instead of leaving the silence to read as a pass.
"""

from __future__ import annotations

from collections.abc import Iterable

from .domain import Rule, Violation
from .facts import ModuleInfo

__all__ = ["invisible_import_violations"]

_TACH_EXCLUDED_PARTS = frozenset({"tests", ".venv", "venv", "node_modules", "__pycache__"})  # the generated tach.toml `exclude`


def invisible_import_violations(modules: Iterable[ModuleInfo]) -> list[Violation]:
    """One informational finding per scanned module that imports by a run-time name, outside folders tach itself skips."""
    found: list[Violation] = []
    for module in sorted(modules, key=lambda item: item.name):
        if _TACH_EXCLUDED_PARTS & set(module.path.split("/")):
            continue
        lines = sorted({use.line for use in module.facts.effect_uses if use.category == "dynamic-import"})
        if lines:
            where = ", ".join(map(str, lines))
            found.append(Violation(
                Rule.TACH_INVISIBLE_IMPORT, module.name,
                f"{len(lines)} run-time import call(s) (line {where}) create edges tach does not follow; "
                "its pass says nothing about them",
            ))
    return found
