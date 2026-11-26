"""Per-function facts for ``duplicate-logic`` (#228): a digest of each body with local names erased, and of each two-way branch.

Pure: source text in, plain data out. Every traversal is iterative, so a deeply chained expression or ``elif`` ladder cannot
overflow the stack, and each function is serialised once.

What is erased: parameter and local names (numbered by first appearance), the function's own name where it calls itself,
docstrings, comments and annotations. What is kept: literals, operators, attribute and keyword names, and every name that
is not bound inside the function (so the called names).
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from typing import cast

__all__ = ["BranchShape", "FunctionShape", "function_shapes"]

# A branch larger than this many AST nodes is not a "same narrow rule" and is skipped. The bound also keeps a long ``elif``
# ladder linear: every `if` in it would otherwise walk all the arms below it.
MAX_BRANCH_NODES = 200
_LABEL_NAMES = 4  # called names shown per side of a branch label
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_Func = ast.FunctionDef | ast.AsyncFunctionDef
_ERASED_FIELDS = frozenset({"ctx", "annotation", "returns", "type_comment", "type_params", "kind"})


@dataclass(frozen=True)
class BranchShape:
    digest: str  # the condition (locals erased) plus the names each side calls
    label: str  # readable form of the same
    line: int


@dataclass(frozen=True)
class FunctionShape:
    qualname: str  # ``Class.method``; a repeated name in one module gets ``#2``, ``#3``
    line: int
    statements: int  # 0 for a stub: nothing to compare
    digest: str
    branches: tuple[BranchShape, ...]


def function_shapes(source: str) -> tuple[FunctionShape, ...]:
    """The module-level functions and class methods of ``source``; a nested ``def`` is part of its parent's body.

    Raises ``SyntaxError``, ``ValueError`` or ``RecursionError`` for source Python cannot parse."""
    out: list[FunctionShape] = []
    seen: dict[str, int] = {}
    for func, prefix, in_class in _functions(ast.parse(source)):
        name = prefix + func.name
        seen[name] = seen.get(name, 0) + 1
        out.append(_shape(func, name if seen[name] == 1 else f"{name}#{seen[name]}", in_class))
    return tuple(out)


def _functions(tree: ast.Module) -> Iterator[tuple[_Func, str, bool]]:
    """(function, class prefix, is a method) in source order, looking through ``if``/``try``/``with`` and classes."""
    stack: list[tuple[ast.AST, str, bool]] = [(node, "", False) for node in reversed(tree.body)]
    while stack:
        node, prefix, in_class = stack.pop()
        if isinstance(node, _DEFS):
            yield node, prefix, in_class
            continue
        if isinstance(node, ast.ClassDef):
            prefix, in_class = f"{prefix}{node.name}.", True
        children = [child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt | ast.excepthandler | ast.match_case)]
        stack.extend((child, prefix, in_class) for child in reversed(children))


def _shape(func: _Func, qualname: str, in_class: bool) -> FunctionShape:
    body = _without_docstring(func.body)
    if all(_is_stub(statement) for statement in body):
        return FunctionShape(qualname, func.lineno, 0, "", ())
    local = _local_names(func)
    self_name = "" if in_class or func.name in local else func.name
    kind = type(func).__name__
    digest = _digest(f"{kind}\x1f{_serialise(body, local, self_name)}")
    return FunctionShape(qualname, func.lineno, _count_statements(body), digest, _branches(func, local))


def _without_docstring(body: Sequence[ast.stmt]) -> list[ast.stmt]:
    first = body[0] if body else None
    is_doc = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
    return list(body[1:] if is_doc else body)


def _is_stub(node: ast.stmt) -> bool:
    if isinstance(node, ast.Pass):
        return True
    if isinstance(node, ast.Expr):
        return isinstance(node.value, ast.Constant)
    if isinstance(node, ast.Return):
        return isinstance(node.value, ast.Name) and node.value.id == "NotImplemented"
    if isinstance(node, ast.Raise) and node.exc is not None:
        raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        return isinstance(raised, ast.Name) and raised.id == "NotImplementedError"
    return False


def _bound_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id if isinstance(node.ctx, ast.Store | ast.Del) else None
    if isinstance(node, ast.arg):
        return node.arg
    if isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name
    return node.rest if isinstance(node, ast.MatchMapping) else None


def _local_names(func: _Func) -> frozenset[str]:
    """Every name bound inside ``func`` (nested scopes included), except those declared ``global``. Imports keep their names."""
    bound: set[str] = set()
    declared: set[str] = set()
    class_attributes: set[int] = set()  # a nested class body binds attributes, whose names are part of what the code does
    for node in ast.walk(func):
        if isinstance(node, ast.Global):
            declared.update(node.names)
        if isinstance(node, ast.ClassDef):
            class_attributes.update(id(inner) for statement in node.body for inner in _assigned(statement))
        name = _bound_name(node) if node is not func and id(node) not in class_attributes else None
        if name is not None:
            bound.add(name)
    return frozenset(bound - declared)


def _assigned(statement: ast.stmt) -> list[ast.AST]:
    if isinstance(statement, ast.Assign):
        return list(statement.targets)
    if isinstance(statement, ast.AnnAssign):
        return [statement.target]
    return [statement] if isinstance(statement, _DEFS) else []


def _count_statements(body: Sequence[ast.stmt]) -> int:
    """Statements at any depth; docstrings and annotation-only declarations are not statements here."""
    total = 0
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.stmt) and not (isinstance(node, ast.AnnAssign) and node.value is None):
            total += 1
        if isinstance(node, (*_DEFS, ast.ClassDef)):
            stack.extend(_without_docstring(node.body))
        else:
            stack.extend(ast.iter_child_nodes(node))
    return total


class _Writer:
    """An iterative pre-order serialiser. ``stack`` holds literal text and the nodes still to expand."""

    def __init__(self, local: Collection[str], self_name: str) -> None:
        self.local = local
        self.self_name = self_name
        self.numbers: dict[str, int] = {}
        self.stack: list[str | ast.AST] = []

    def name(self, value: str) -> str:
        if value == self.self_name:
            return "$self"
        if value not in self.local:
            return ascii(value)
        return f"${self.numbers.setdefault(value, len(self.numbers))}"

    def expand(self, node: ast.AST) -> str:
        """Push ``node``'s fields, return its opening text."""
        if isinstance(node, ast.AnnAssign):  # the annotation is not behaviour
            node = ast.Assign(targets=[node.target], value=node.value) if node.value is not None else ast.Pass()
        if isinstance(node, ast.Constant):
            return f"Constant:{_literal(node.value)};"
        pending: list[str | ast.AST] = []
        for field, value in ast.iter_fields(node):
            if field not in _ERASED_FIELDS and value is not None and value != []:
                pending.append(f"{field}=")
                pending.extend(self.field(node, field, value))
        pending.append(")")
        self.stack.extend(reversed(pending))
        return f"{type(node).__name__}("

    def field(self, node: ast.AST, field: str, value: object) -> list[str | ast.AST]:
        if isinstance(value, list):
            items = cast("list[object]", value)
            if field == "body" and isinstance(node, (*_DEFS, ast.ClassDef)):
                items = list(_without_docstring(node.body))
            return [f"[{len(items)}:", *(part for item in items for part in self.item(node, field, item)), "]"]
        return self.item(node, field, value)

    def item(self, node: ast.AST, field: str, value: object) -> list[str | ast.AST]:
        if isinstance(value, ast.AST):
            return [value]
        if isinstance(value, str) and _names_a_binding(node, field):
            return [self.name(value) + ";"]
        return [f"{value!a};"]


def _names_a_binding(node: ast.AST, field: str) -> bool:
    if isinstance(node, ast.Name):
        return field == "id"
    if isinstance(node, ast.arg):
        return field == "arg"
    if isinstance(node, ast.MatchMapping):
        return field == "rest"
    return field == "name" and not isinstance(node, ast.alias)


def _literal(value: object) -> str:
    # `repr` of an int beyond the digit limit raises; `hex` does not. The type keeps 1, 1.0 and True apart.
    return f"{type(value).__name__}:{hex(value) if type(value) is int else ascii(value)}"


def _serialise(nodes: Sequence[ast.AST], local: Collection[str], self_name: str) -> str:
    writer = _Writer(local, self_name)
    writer.stack.extend(reversed(nodes))
    parts: list[str] = []
    while writer.stack:
        item = writer.stack.pop()
        parts.append(item if isinstance(item, str) else writer.expand(item))
    return "".join(parts)


def _digest(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


# --- two-way branches ---------------------------------------------------------------------------------------------------


def _branches(func: _Func, local: Collection[str]) -> tuple[BranchShape, ...]:
    found: list[BranchShape] = []
    for test, then, other, line in _two_way(func):
        shape = _branch_shape(test, (then, other), local, line)
        if shape is not None:
            found.append(shape)
    return tuple(sorted(found, key=lambda item: (item.line, item.digest)))


def _two_way(func: _Func) -> Iterator[tuple[ast.expr, Sequence[ast.AST], Sequence[ast.AST], int]]:
    """``if``/``else``, ``a if c else b``, and a guard (``if c: return a`` followed by a final ``return b``)."""
    for node in ast.walk(func):
        if isinstance(node, ast.IfExp):
            yield node.test, [node.body], [node.orelse], node.lineno
        elif isinstance(node, ast.If) and node.orelse:
            yield node.test, node.body, node.orelse, node.lineno
        for _, block in ast.iter_fields(node):
            if isinstance(block, list):
                yield from _guard(cast("list[object]", block))


def _guard(block: Sequence[object]) -> Iterator[tuple[ast.expr, Sequence[ast.AST], Sequence[ast.AST], int]]:
    match list(block[-2:]):
        case [ast.If(orelse=[]) as guard, ast.Return() as last] if isinstance(guard.body[-1], ast.Return):
            yield guard.test, guard.body, [last], guard.lineno
        case _:
            return


def _bounded_nodes(roots: Sequence[ast.AST]) -> list[ast.AST] | None:
    """Every node under ``roots``, or ``None`` when there are more than the bound."""
    out: list[ast.AST] = []
    stack = list(reversed(roots))
    while stack:
        node = stack.pop()
        out.append(node)
        if len(out) > MAX_BRANCH_NODES:
            return None
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return out


def _callee(node: ast.expr, local: Collection[str]) -> str:
    attributes: list[str] = []
    while isinstance(node, ast.Attribute):
        attributes.append(node.attr)
        node = node.value
    root = ("_" if node.id in local else node.id) if isinstance(node, ast.Name) else "?"
    return ".".join([root, *reversed(attributes)])


def _called(nodes: Sequence[ast.AST], local: Collection[str]) -> list[str]:
    return sorted({_callee(node.func, local) for node in nodes if isinstance(node, ast.Call)})


def _shown(names: Sequence[str]) -> str:
    extra = len(names) - _LABEL_NAMES
    return ", ".join(names[:_LABEL_NAMES]) + (f" and {extra} more" if extra > 0 else "")


def _branch_shape(
    test: ast.expr, sides: tuple[Sequence[ast.AST], Sequence[ast.AST]], local: Collection[str], line: int,
) -> BranchShape | None:
    """``None`` unless both sides call something: a branch that only assigns values repeats by nature."""
    condition, then, other = _bounded_nodes([test]), _bounded_nodes(sides[0]), _bounded_nodes(sides[1])
    if condition is None or then is None or other is None:
        return None
    first, second = _called(then, local), _called(other, local)
    if not first or not second:
        return None
    asked = _called(condition, local) or sorted(
        {_callee(node, local) for node in condition if isinstance(node, ast.Name | ast.Attribute)}
    )
    digest = _digest("\x1f".join([_serialise([test], local, ""), *first, "|", *second]))
    label = f"on `{_shown(asked)}` calling `{_shown(first)}` else `{_shown(second)}`"
    return BranchShape(digest, label, line)
