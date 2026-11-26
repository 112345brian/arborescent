"""Call facts: the calls a module makes, resolved through its import alias table.

Call sites on an imported or module-level name, calls used as whole statements (a discarded result), the imported
callables each function or method calls, ``discard(value, reason=...)`` lines, and ``absolute_callee``, which turns a
recorded call (relative imports keep their dots) back into an absolute dotted name."""

from __future__ import annotations

import ast
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "FunctionCalls", "FunctionSpan", "absolute_callee", "bare_calls", "call_aliases", "call_sites", "discard_call_lines", "dotted_name",
    "dynamic_imports", "function_calls", "function_spans",
]


@dataclass(frozen=True)
class FunctionCalls:
    """The imported callables one function or method calls, as canonical dotted names (``alias.attr()`` resolved).

    ``name`` is ``f`` or ``Class.method``. Only calls whose root name was imported are kept, so a local variable's
    method call is not recorded; a function that calls nothing imported has no entry. Reused by the test-reference check (#104).
    """

    name: str
    line: int
    calls: tuple[str, ...]  # sorted, unique


@dataclass(frozen=True)
class FunctionSpan:
    """Where one function or method is, so a rule can say which of the module's recorded call sites are inside it (#229).

    ``name`` is ``f``, ``Class.method`` or ``Outer.Inner.method``. A nested function or ``lambda`` has no span of its own:
    its calls belong to the function around it. Decorators lie before ``line``: they run when the module defines the function.
    ``method_calls`` are the calls the module's recorded call sites leave out, a method on a value, as ``(first line, receiver.name)``
    with ``?`` for a receiver that is not a plain name (``make().send`` is ``?.send``): the companion rule matches them by name (#231).
    """

    name: str
    line: int  # the `def` line
    end_line: int
    start: int  # the first decorator's line, else `line`: where a comment above the function lands
    method_calls: tuple[tuple[int, str], ...] = ()


def _method_calls(
    nodes: Sequence[ast.AST], heads: set[str], spans: Sequence[tuple[str, int, int, int]],
) -> list[dict[str, int]]:
    """For each span, ``receiver.name`` -> first line, for the attribute calls whose head ``call_sites`` does not record."""
    starts = [span[1] for span in spans]
    found: list[dict[str, int]] = [{} for _ in spans]
    for node in nodes:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        slot = bisect_right(starts, node.lineno) - 1
        if slot < 0 or node.lineno > spans[slot][2] or _root_name(node.func) in heads:
            continue
        text = dotted_name(node.func, {}) or f"?.{node.func.attr}"
        found[slot][text] = min(node.lineno, found[slot].get(text, node.lineno))
    return found


def function_spans(tree: ast.Module, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[FunctionSpan, ...]:
    """Every function and method that is not inside another function, however the module or class body nests it."""
    out: list[tuple[str, int, int, int]] = []  # (name, def line, end line, first decorator line)

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                out.append((prefix + child.name, child.lineno, child.end_lineno or child.lineno, start))
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.stmt | ast.excepthandler | ast.match_case):
                visit(child, prefix)

    visit(tree, "")
    methods = _method_calls(nodes, _heads(tree, aliases), out)
    return tuple(
        FunctionSpan(*span, tuple(sorted((line, text) for text, line in found.items())))
        for span, found in zip(out, methods, strict=True)
    )


def _heads(tree: ast.Module, aliases: dict[str, str]) -> set[str]:
    """The names a call can start with and be resolved: imported ones, and functions and classes defined at module level."""
    return set(aliases) | {stmt.name for stmt in tree.body if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}


def discard_call_lines(nodes: Sequence[ast.AST]) -> tuple[int, ...]:
    lines: list[int] = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        is_discard = (isinstance(function, ast.Name) and function.id == "discard") or (
            isinstance(function, ast.Attribute) and function.attr == "discard"
        )
        if is_discard and node.args and any(keyword.arg == "reason" for keyword in node.keywords):
            lines.append(node.lineno)
    return tuple(sorted(lines))


def call_aliases(nodes: Sequence[ast.AST], imported: dict[str, str]) -> dict[str, str]:
    """Like ``import_aliases`` but a relative import keeps its leading dots (``from ..a import B`` -> ``B: "..a.B"``) for the rule to resolve."""
    aliases = dict(imported)
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.level:
            prefix = "." * node.level + (node.module or "")
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{prefix}{'.' if node.module is not None else ''}{item.name}"
    return aliases


def dotted_name(node: ast.expr, aliases: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent is not None else None
    return None


_DYNAMIC_IMPORT_CALLS = frozenset({"importlib.import_module", "importlib.__import__", "__import__", "builtins.__import__"})


def dynamic_imports(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[tuple[int, str], ...]:
    """Every ``importlib.import_module(...)`` / ``__import__(...)`` call: its literal absolute target, or ``""`` when it cannot be read statically (#161)."""
    out: set[tuple[int, str]] = set()
    for node in nodes:
        if not isinstance(node, ast.Call) or dotted_name(node.func, aliases) not in _DYNAMIC_IMPORT_CALLS:
            continue
        target = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "name"), None)
        literal = target.value if isinstance(target, ast.Constant) and isinstance(target.value, str) else ""
        out.add((node.lineno, literal if literal and not literal.startswith(".") and "\x00" not in literal else ""))
    return tuple(sorted(out))


def call_sites(tree: ast.Module, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[tuple[int, str], ...]:
    """Calls whose head name is imported or defined at module level: what a construction rule can resolve; a method on a value is not."""
    heads = _heads(tree, aliases)
    out: set[tuple[int, str]] = set()
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        head = node.func
        while isinstance(head, ast.Attribute):
            head = head.value
        if isinstance(head, ast.Name) and head.id in heads and (name := dotted_name(node.func, aliases)) is not None:
            out.add((node.lineno, name))
    return tuple(sorted(out))


def bare_calls(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[tuple[int, str], ...]:
    found: list[tuple[int, str]] = []
    for node in nodes:
        if not isinstance(node, ast.Expr):
            continue
        value = node.value.value if isinstance(node.value, ast.Await) else node.value
        target = dotted_name(value.func, aliases) if isinstance(value, ast.Call) else None
        if target is not None:
            found.append((node.lineno, target))
    return tuple(sorted(found))


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _imported_calls(func: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str]) -> tuple[str, ...]:
    calls: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _root_name(node.func) in aliases and (target := dotted_name(node.func, aliases)) is not None:
            calls.add(target)
    return tuple(sorted(calls))


def function_calls(tree: ast.Module, aliases: dict[str, str]) -> tuple[FunctionCalls, ...]:
    out: list[FunctionCalls] = []

    def visit(func: ast.FunctionDef | ast.AsyncFunctionDef, qualified: str) -> None:
        if calls := _imported_calls(func, aliases):
            out.append(FunctionCalls(qualified, func.lineno, calls))

    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            visit(stmt, stmt.name)
        elif isinstance(stmt, ast.ClassDef):
            for member in stmt.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    visit(member, f"{stmt.name}.{member.name}")
    return tuple(out)


def absolute_callee(module: str, is_package: bool, callee: str) -> str | None:
    """A call recorded by ``extract`` as a dotted name: relative imports keep their dots, a bare name is defined in this module."""
    if not callee.startswith("."):
        return callee if "." in callee else f"{module}.{callee}"
    level = len(callee) - len(callee.lstrip("."))
    parts = module.split(".")
    package = parts if is_package else parts[:-1]
    if level - 1 > len(package):
        return None
    return ".".join([*package[: len(package) - (level - 1)], callee[level:]])
