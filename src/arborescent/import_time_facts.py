"""What runs while a module is imported, and what each of its functions runs when called (#230).

Which expressions count as effects is ``effect_facts``'s business; this module only says *when* an expression is evaluated.
At import: module-level statements, class bodies, parameter defaults, decorators and comprehensions. Not at import: function
and lambda bodies (a lambda called on the spot is the exception), annotations, ``if TYPE_CHECKING:`` and
``if __name__ == "__main__":`` blocks. The calls made at import and the effects each module-level function or method runs
itself are recorded so ``effect-at-import`` can follow a call one hop."""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from .definition_facts import is_main_guard
from .effect_facts import EffectSite, runs

__all__ = ["ImportCall", "ImportTimeFacts", "Walked", "evaluated", "import_time_facts"]

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
# `call_facts.dotted_name`, handed in by `facts`, which already imports it.
Dotted = Callable[[ast.expr, dict[str, str]], str | None]


@dataclass(frozen=True, order=True)
class ImportCall:
    """A call made at import: ``callee`` is a dotted name through the import aliases, or, when ``local``, one defined here."""

    line: int
    callee: str
    local: bool


@dataclass(frozen=True)
class ImportTimeFacts:
    calls: tuple[ImportCall, ...] = ()
    function_effects: tuple[tuple[str, str, str], ...] = ()  # ("f" or "Class.method", category, target)


def _never_runs_on_import(test: ast.expr) -> bool:
    name = test.attr if isinstance(test, ast.Attribute) else test.id if isinstance(test, ast.Name) else ""
    return name == "TYPE_CHECKING" or is_main_guard(test)


def _children(node: ast.AST) -> list[ast.AST]:
    """The nodes evaluated when ``node`` is: a function or lambda gives its decorators and defaults, never its body."""
    if isinstance(node, (*_FUNCTIONS, ast.Lambda)):
        defaults = [d for d in (*node.args.defaults, *node.args.kw_defaults) if d is not None]
        return [*([] if isinstance(node, ast.Lambda) else node.decorator_list), *defaults]
    if isinstance(node, ast.If) and _never_runs_on_import(node.test):
        return list(node.orelse)
    if isinstance(node, ast.AnnAssign):
        return [node.target, *([node.value] if node.value is not None else [])]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Lambda):
        return [node.func.body, *ast.iter_child_nodes(node)]
    return list(ast.iter_child_nodes(node))


def evaluated(statements: Sequence[ast.AST]) -> Iterator[ast.AST]:
    """Every node that is evaluated when ``statements`` run."""
    stack = list(statements)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(_children(node))


def _functions(body: Sequence[ast.stmt], prefix: str = "") -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Module-level functions and the methods of module-level classes, including ones defined under ``if``/``try``/``with``."""
    for stmt in body:
        if isinstance(stmt, _FUNCTIONS):
            yield prefix + stmt.name, stmt
        elif isinstance(stmt, ast.ClassDef) and not prefix:
            yield from _functions(stmt.body, f"{stmt.name}.")
        elif isinstance(stmt, ast.If | ast.With | ast.AsyncWith):
            yield from _functions([*stmt.body, *getattr(stmt, "orelse", [])], prefix)
        elif isinstance(stmt, ast.Try | ast.TryStar):
            yield from _functions([*stmt.body, *stmt.orelse, *stmt.finalbody, *(s for h in stmt.handlers for s in h.body)], prefix)


def _defined_names(tree: ast.Module) -> set[str]:
    """Classes and functions the module itself defines at its top level."""
    return {node.name for node in evaluated(tree.body) if isinstance(node, (*_FUNCTIONS, ast.ClassDef))}


class _Names(NamedTuple):
    dotted: Dotted
    aliases: dict[str, str]
    defined: set[str]  # the module's own top-level classes and functions
    rebound: set[str]  # names it assigns: an imported name among them may no longer be the import


def _callee(expression: ast.expr, names: _Names) -> tuple[str, bool] | None:
    """``(name, local)`` for a callee that can be resolved to a definition; a builtin, a variable or a result cannot."""
    head = expression
    while isinstance(head, ast.Attribute):
        head = head.value
    if not isinstance(head, ast.Name) or head.id in names.rebound:
        return None
    if head.id in names.defined:
        local = names.dotted(expression, {})
        return None if local is None else (local, True)
    if head.id not in names.aliases:
        return None
    name = names.dotted(expression, names.aliases)
    return None if name is None or "." not in name else (name, False)


def _import_calls(nodes: Sequence[ast.AST], tree: ast.Module, aliases: dict[str, str], dotted: Dotted) -> tuple[ImportCall, ...]:
    rebound = {node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    names = _Names(dotted, aliases, _defined_names(tree), rebound)
    calls: set[ImportCall] = set()
    for node in nodes:
        called: list[ast.expr] = []
        if isinstance(node, ast.Call):
            called = [node.func]
        elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            called = [d for d in node.decorator_list if not isinstance(d, ast.Call)]
        for expression in called:
            found = _callee(expression, names)
            if found is not None:
                calls.add(ImportCall(expression.lineno, *found))
    return tuple(sorted(calls))


def _function_effects(tree: ast.Module, sites: Sequence[EffectSite], running: set[int]) -> tuple[tuple[str, str, str], ...]:
    by_node: dict[int, list[tuple[str, str]]] = {}
    for site in sites:
        if runs(site, running):
            by_node.setdefault(id(site[0]), []).append((site[1].category, site[1].target))
    if not by_node:
        return ()
    return tuple(sorted({
        (name, category, target)
        for name, function in _functions(tree.body)
        for node in evaluated(function.body)
        for category, target in by_node.get(id(node), ())
    }))


class Walked(NamedTuple):
    """What ``facts.extract`` already computed for the module, so nothing here is computed twice."""

    tree: ast.Module
    at_import: Sequence[ast.AST]  # ``evaluated(tree.body)``
    sites: Sequence[EffectSite]
    running: set[int]


def import_time_facts(walked: Walked, aliases: dict[str, str], dotted: Dotted) -> ImportTimeFacts:
    """``aliases`` keeps a relative import's leading dots (``call_facts.call_aliases``)."""
    return ImportTimeFacts(
        _import_calls(walked.at_import, walked.tree, aliases, dotted), _function_effects(walked.tree, walked.sites, walked.running)
    )
