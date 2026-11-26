"""Call facts: the calls a module makes, resolved through its import alias table.

Call sites on an imported or module-level name, calls used as whole statements (a discarded result), the imported
callables each function or method calls, ``discard(value, reason=...)`` lines, and ``absolute_callee``, which turns a
recorded call (relative imports keep their dots) back into an absolute dotted name."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["FunctionCalls", "absolute_callee", "bare_calls", "call_aliases", "call_sites", "discard_call_lines", "dotted_name", "dynamic_imports", "function_calls", "is_private_name", "patch_targets", "private_attributes", "unqualified_from_calls"]


@dataclass(frozen=True)
class FunctionCalls:
    """The imported callables one function or method calls, as canonical dotted names (``alias.attr()`` resolved).

    ``name`` is ``f`` or ``Class.method``. Only calls whose root name was imported are kept, so a local variable's
    method call is not recorded; a function that calls nothing imported has no entry. Reused by the test-reference check (#104).
    """

    name: str
    line: int
    calls: tuple[str, ...]  # sorted, unique


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
    heads = set(aliases) | {stmt.name for stmt in tree.body if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
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


def is_private_name(name: str) -> bool:
    """A leading underscore that is neither a dunder nor the bare ``_`` (gettext's alias, a throwaway)."""
    return name.startswith("_") and name != "_" and not (name.startswith("__") and name.endswith("__"))


def private_attributes(nodes: Sequence[ast.AST], aliases: dict[str, str], rebound: set[str]) -> tuple[tuple[int, str], ...]:
    """``(line, dotted)`` of every ``._name`` attribute reached through an import the module does not rebind (#218)."""
    found: set[tuple[int, str]] = set()
    for node in nodes:
        if not (isinstance(node, ast.Attribute) and is_private_name(node.attr)):
            continue
        root = _root_name(node)
        if root in aliases and root not in rebound and (target := dotted_name(node, aliases)) is not None:
            found.add((node.lineno, target))
    return tuple(sorted(found))


_MOCK_PATCH = frozenset({"unittest.mock.patch", "mock.patch", "mocker.patch"})


def _argument(call: ast.Call, position: int, keyword: str) -> ast.expr | None:
    if len(call.args) > position:
        return call.args[position]
    return next((item.value for item in call.keywords if item.arg == keyword), None)


def _text(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _monkeypatch_names(nodes: Sequence[ast.AST]) -> set[str]:
    """``monkeypatch`` and every name a ``with monkeypatch.context() as m`` binds."""
    names = {"monkeypatch"}
    for node in nodes:
        if not isinstance(node, ast.withitem) or not isinstance(node.optional_vars, ast.Name) or not isinstance(node.context_expr, ast.Call):
            continue
        function = node.context_expr.func
        if isinstance(function, ast.Attribute) and function.attr == "context" and _root_name(function) in {"monkeypatch", "pytest", "MonkeyPatch"}:
            names.add(node.optional_vars.id)
    return names


def _patched_attribute(call: ast.Call, aliases: dict[str, str], rebound: set[str]) -> str | None:
    """The dotted ``module.name`` of ``(module_object, "name")``, when the object is reached through an import."""
    owner = _argument(call, 0, "target")
    name = _text(_argument(call, 1, "attribute")) or _text(_argument(call, 1, "name"))  # `patch.object` / `setattr` spell it differently
    if owner is None or name is None or _root_name(owner) not in aliases or _root_name(owner) in rebound:
        return None
    base = dotted_name(owner, aliases)
    return None if base is None else f"{base}.{name}"


def _patch_target(call: ast.Call, aliases: dict[str, str], rebound: set[str], monkeypatches: set[str]) -> str | None:
    function = call.func
    called = dotted_name(function, aliases)
    if called in _MOCK_PATCH:
        target = _text(_argument(call, 0, "target"))
    elif called is not None and called.removesuffix(".object") in _MOCK_PATCH:
        return _patched_attribute(call, aliases, rebound)
    elif isinstance(function, ast.Attribute) and function.attr == "setattr" and isinstance(function.value, ast.Name) and function.value.id in monkeypatches:
        target = _text(_argument(call, 0, "target"))
        if target is None:
            return _patched_attribute(call, aliases, rebound)
    else:
        return None
    return target if target is not None and "." in target and not target.startswith(".") and "\x00" not in target else None


def patch_targets(nodes: Sequence[ast.AST], aliases: dict[str, str], rebound: set[str]) -> tuple[tuple[int, str], ...]:
    """``(line, dotted "module.name")`` of every attribute ``monkeypatch.setattr`` / ``mock.patch[.object]`` replaces (#226).

    A string target is taken as written; an object target must be reached through an import the module does not rebind."""
    monkeypatches = _monkeypatch_names(nodes)
    found = {
        (node.lineno, target) for node in nodes
        if isinstance(node, ast.Call) and (target := _patch_target(node, aliases, rebound, monkeypatches)) is not None
    }
    return tuple(sorted(found))


def _module_from_imports(tree: ast.Module) -> dict[str, str]:
    """Local name -> origin for each ``from X import name`` that runs when the module is imported (relative ones keep their dots)."""
    found: dict[str, str] = {}
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.ImportFrom):
            prefix = "." * node.level + ("" if node.module is None else f"{node.module}.")
            found.update({item.asname or item.name: prefix + item.name for item in node.names if item.name != "*"})
        elif isinstance(node, ast.If) and "TYPE_CHECKING" in (getattr(node.test, "id", ""), getattr(node.test, "attr", "")):
            pending.extend(node.orelse)
        elif not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.expr):
            pending.extend(ast.iter_child_nodes(node))
    return found


def unqualified_from_calls(tree: ast.Module, nodes: Sequence[ast.AST], rebound: set[str]) -> tuple[tuple[int, str, str], ...]:
    """``(first line, origin "X.name", local name)`` for each name bound by a module-level ``from X import name`` and called bare
    inside a function (#226). A call that runs at import time, or a name the module rebinds or also imports lazily, is left out."""
    bound = _module_from_imports(tree)
    if not bound:
        return ()
    functions = [node for node in nodes if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)]
    bodies = [item for node in functions for item in (node.body if isinstance(node.body, list) else [node.body])]
    inner = [child for body in bodies for child in ast.walk(body)]
    lazy = {item.asname or item.name for node in inner if isinstance(node, ast.Import | ast.ImportFrom) for item in node.names}
    usable = bound.keys() - rebound - lazy
    first: dict[str, int] = {}
    for node in inner:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in usable:
            first[node.func.id] = min(node.lineno, first.get(node.func.id, node.lineno))
    return tuple(sorted((line, bound[local], local) for local, line in first.items()))


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
