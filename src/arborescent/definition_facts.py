"""Definition facts: the shape of what a module defines at its top level.

Module-level classes and functions with whether each is abstract, every class's bases and member names, ``typing.Protocol`` methods whose return type says
nothing, the public surface (a static ``__all__`` or public bindings) and which of it is re-exported, functions
annotated to return a ``*Result``/``*Outcome``, and the ``if __name__ == "__main__":`` guard."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass

from .call_facts import dotted_name
from .purity_facts import Helpers

__all__ = ["ClassShape", "TopLevelDef", "class_shapes", "main_guard_line", "purity_helpers", "result_defs", "surface", "top_level_defs", "weak_port_returns"]


_ABSTRACT_BASE_NAMES = frozenset({"ABC", "abc.ABC", "Protocol", "typing.Protocol", "typing_extensions.Protocol"})
_ABC_META_NAMES = frozenset({"ABCMeta", "abc.ABCMeta"})


@dataclass(frozen=True)
class TopLevelDef:
    """A module-level class or function. ``is_abstract`` is a static shape fact, not a policy decision:

    a class is abstract when it inherits (directly) from ``abc.ABC``/``typing.Protocol`` or declares
    ``metaclass=ABCMeta``; a function is abstract when its body is a stub (``...``, ``pass`` or
    ``raise NotImplementedError``, past an optional docstring).
    """

    name: str
    line: int
    is_class: bool
    is_abstract: bool


def _is_stub_statement(stmt: ast.stmt, aliases: dict[str, str]) -> bool:
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is ...:
        return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        target = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        return dotted_name(target, aliases) == "NotImplementedError"
    return False


def _is_stub_body(body: list[ast.stmt], aliases: dict[str, str]) -> bool:
    stmts = body[1:] if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str) else body
    if not stmts:
        return True
    return len(stmts) == 1 and _is_stub_statement(stmts[0], aliases)


def _is_abstract_class(node: ast.ClassDef, aliases: dict[str, str]) -> bool:
    for base in node.bases:
        if dotted_name(base, aliases) in _ABSTRACT_BASE_NAMES:
            return True
    for keyword in node.keywords:
        if keyword.arg == "metaclass" and dotted_name(keyword.value, aliases) in _ABC_META_NAMES:
            return True
    return False


def top_level_defs(tree: ast.AST, aliases: dict[str, str]) -> tuple[TopLevelDef, ...]:
    out: list[TopLevelDef] = []
    body = tree.body if isinstance(tree, ast.Module) else []
    for stmt in body:
        if isinstance(stmt, ast.ClassDef):
            out.append(TopLevelDef(stmt.name, stmt.lineno, True, _is_abstract_class(stmt, aliases)))
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            out.append(TopLevelDef(stmt.name, stmt.lineno, False, _is_stub_body(stmt.body, aliases)))
    return tuple(out)


_PROTOCOL_NAMES = frozenset({"Protocol", "typing.Protocol", "typing_extensions.Protocol"})
# Names a class may define without them being part of an interface it offers.
_NOT_MEMBERS = frozenset({
    "__init__", "__new__", "__doc__", "__module__", "__slots__", "__annotations__", "__class_getitem__",
    "__init_subclass__", "__post_init__", "__match_args__", "_",
})


@dataclass(frozen=True)
class ClassShape:
    """What a class statement says about itself, for counting a port's implementors (#219)."""

    name: str  # qualified by the enclosing classes and functions: "Outer.Inner"
    line: int
    bases: tuple[str, ...]  # dotted through import aliases; a relative import keeps its dots, a local name stays bare
    members: tuple[str, ...]  # methods, class attributes and `self.x` assignments it defines itself, sorted
    protocol: bool  # lists `typing.Protocol` directly
    abstract: bool  # a Protocol, lists `ABC`/`ABCMeta` directly, or declares an `@abstractmethod`


def _blocks(stmt: ast.stmt) -> Iterator[ast.stmt]:
    """The statements directly inside a compound statement (`if`, `try`, `with`, `match`, loops)."""
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, ast.stmt):
            yield child
        elif isinstance(child, ast.ExceptHandler | ast.match_case):
            yield from child.body


def _self_attributes(method: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[str]:
    params = [*method.args.posonlyargs, *method.args.args]
    if not params:
        return
    for node in ast.walk(method):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and isinstance(node.value, ast.Name) and node.value.id == params[0].arg:
            yield node.attr


def _members(body: Iterable[ast.stmt]) -> Iterator[str]:
    for stmt in body:
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            yield stmt.name
            yield from _self_attributes(stmt)
        elif isinstance(stmt, ast.Assign | ast.AnnAssign):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            yield from (t.id for t in targets if isinstance(t, ast.Name))
        elif not isinstance(stmt, ast.ClassDef):
            yield from _members(_blocks(stmt))


def _declares_abstract_method(node: ast.ClassDef, aliases: dict[str, str]) -> bool:
    return any(
        (dotted_name(decorator, aliases) or "").rpartition(".")[2] == "abstractmethod"
        for stmt in node.body if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        for decorator in stmt.decorator_list
    )


def _class_shape(node: ast.ClassDef, name: str, aliases: dict[str, str]) -> ClassShape:
    bases = tuple(
        dotted for base in node.bases
        if (dotted := dotted_name(base.value if isinstance(base, ast.Subscript) else base, aliases)) is not None
    )
    protocol = any(base in _PROTOCOL_NAMES for base in bases)
    return ClassShape(
        name, node.lineno, bases, tuple(sorted(set(_members(node.body)) - _NOT_MEMBERS)), protocol,
        protocol or _is_abstract_class(node, aliases) or _declares_abstract_method(node, aliases),
    )


def class_shapes(tree: ast.Module, aliases: dict[str, str]) -> tuple[ClassShape, ...]:
    """Every class in the module, nested and function-local ones included. Only statements are walked: a class is one."""
    out: list[ClassShape] = []

    def visit(body: Iterable[ast.stmt], prefix: str) -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                out.append(_class_shape(stmt, prefix + stmt.name, aliases))
                visit(stmt.body, f"{prefix}{stmt.name}.")
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(stmt.body, f"{prefix}{stmt.name}.")
            else:
                visit(_blocks(stmt), prefix)

    visit(tree.body, "")
    return tuple(out)
_ANY_NAMES = frozenset({"Any", "typing.Any", "typing_extensions.Any", "object", "builtins.object"})
_BARE_CONTAINERS = frozenset({"dict", "list", "tuple", "set", "frozenset"})
_BARE_TYPING_CONTAINERS = frozenset({"Dict", "List", "Tuple", "Set", "FrozenSet"})
_STRING_KEYED_MAPPINGS = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
_ANNOTATION_WRAPPERS = frozenset({"Optional", "Union", "Annotated"})


def _last_name(node: ast.expr) -> str:
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""


def _weak_annotation(node: ast.expr, aliases: dict[str, str]) -> str | None:
    """Why a return annotation says nothing, or None when it names a type. Looks through Optional/Union/`|`/Annotated and string forms."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _weak_string_annotation(node.value, aliases)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _weak_annotation(node.left, aliases) or _weak_annotation(node.right, aliases)
    if isinstance(node, ast.Subscript):
        return _weak_subscript(node, aliases)
    dotted = dotted_name(node, aliases) or ""
    name = _last_name(node)
    if dotted in _ANY_NAMES or dotted.split(".")[-1] == "Any":
        return f"returns {dotted.split('.')[-1]}"
    if name in _BARE_CONTAINERS or (name in _BARE_TYPING_CONTAINERS and dotted.startswith("typing")):
        return f"returns a bare {name.lower()}"
    return None


def _weak_string_annotation(text: str, aliases: dict[str, str]) -> str | None:
    try:
        return _weak_annotation(ast.parse(text, mode="eval").body, aliases)
    except SyntaxError:
        return None


def _weak_subscript(node: ast.Subscript, aliases: dict[str, str]) -> str | None:
    name = _last_name(node.value)
    items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    if name in _ANNOTATION_WRAPPERS:
        # Annotated's first item is the type; the rest are metadata
        return next(filter(None, (_weak_annotation(item, aliases) for item in (items[:1] if name == "Annotated" else items))), None)
    if name in _STRING_KEYED_MAPPINGS and len(items) == 2 and _last_name(items[0]) == "str":
        value = dotted_name(items[1], aliases)
        if value is not None and value.split(".")[-1] == "Any":
            return f"returns {name}[str, Any]"
    return None


def weak_port_returns(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[tuple[int, str, str], ...]:
    """Methods of ``typing.Protocol`` classes (module-level or nested) whose return annotation is missing, Any, object,
    a bare dict/list/tuple/set or ``Mapping[str, Any]`` (#140). ``__init__`` is skipped: it returns None by definition."""
    found: list[tuple[int, str, str]] = []
    for cls in (n for n in nodes if isinstance(n, ast.ClassDef)):
        if not any(dotted_name(base.value if isinstance(base, ast.Subscript) else base, aliases) in _PROTOCOL_NAMES for base in cls.bases):
            continue
        for stmt in cls.body:
            if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) or stmt.name == "__init__":
                continue
            why = "has no return annotation" if stmt.returns is None else _weak_annotation(stmt.returns, aliases)
            if why is not None:
                found.append((stmt.lineno, f"{cls.name}.{stmt.name}", why))
    return tuple(sorted(found))


def purity_helpers(aliases: dict[str, str]) -> Helpers:
    def is_stub(body: list[ast.stmt]) -> bool:
        return _is_stub_body(body, aliases)

    def is_abstract(node: ast.ClassDef) -> bool:
        return _is_abstract_class(node, aliases)

    return Helpers(dotted_name, is_stub, is_abstract)


def _bound_names(stmt: ast.stmt) -> list[tuple[str, tuple[int, str, str] | None]]:
    """Names a top-level statement binds; an import carries (level, module, imported name) so a re-export can be resolved."""
    if isinstance(stmt, ast.Import):
        return [(a.asname or a.name.split(".")[0], (0, a.name if a.asname is not None else a.name.split(".")[0], "")) for a in stmt.names]
    if isinstance(stmt, ast.ImportFrom):
        if stmt.module == "__future__":
            return []
        return [(a.asname or a.name, (stmt.level, stmt.module or "", a.name)) for a in stmt.names if a.name != "*"]
    if isinstance(stmt, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return [(stmt.name, None)]
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
    return [(t.id, None) for t in targets if isinstance(t, ast.Name)]


def _static_all(tree: ast.Module) -> tuple[int, list[str]] | None:
    """(line, names) of a `__all__` built only from string-literal lists/tuples; None when absent or dynamic."""
    line = 0
    names: list[str] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            hit = any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets)
        elif isinstance(stmt, ast.AnnAssign | ast.AugAssign):
            hit = isinstance(stmt.target, ast.Name) and stmt.target.id == "__all__"
        else:
            continue
        if not hit:
            continue
        value = stmt.value
        if not isinstance(value, ast.List | ast.Tuple) or not all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts):
            return None
        if not isinstance(stmt, ast.AugAssign):
            names = []
        line = line or stmt.lineno
        names += [str(e.value) for e in value.elts if isinstance(e, ast.Constant)]
    return (line, names) if line else None


def surface(tree: ast.AST) -> tuple[int, int, tuple[tuple[str, int, str, str], ...]]:
    """(size, line, re-exports) of a module's public surface (#136): a static `__all__`, else public top-level bindings."""
    if not isinstance(tree, ast.Module):
        return 0, 0, ()
    bound: dict[str, tuple[int, str, str] | None] = {}
    first = 0
    for stmt in tree.body:
        for name, source in _bound_names(stmt):
            bound[name] = source
            if not name.startswith("_") and not first:
                first = stmt.lineno
    declared = _static_all(tree)
    names = list(dict.fromkeys(declared[1])) if declared is not None else [n for n in bound if not n.startswith("_")]
    line = declared[0] if declared is not None else first
    return len(names), line, tuple((n, *source) for n in names if (source := bound.get(n)) is not None)


_RESULT_SUFFIXES = ("Result", "Outcome")


def _annotation_names(node: ast.expr | None) -> Iterator[str]:
    """The names a return annotation may stand for: ``X``, ``m.X``, ``"X"``, ``X | None``, ``Optional[X]``; never ``list[X]``."""
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute):
        yield node.attr
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            yield from _annotation_names(ast.parse(node.value.strip(), mode="eval").body)
        except SyntaxError:
            return
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        yield from _annotation_names(node.left)
        yield from _annotation_names(node.right)
    elif isinstance(node, ast.Subscript) and next(_annotation_names(node.value), "") == "Optional":
        yield from _annotation_names(node.slice)


def result_defs(tree: ast.AST) -> tuple[str, ...]:
    body = tree.body if isinstance(tree, ast.Module) else []
    return tuple(
        stmt.name for stmt in body
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(name.endswith(_RESULT_SUFFIXES) and name not in _RESULT_SUFFIXES for name in _annotation_names(stmt.returns))
    )


def _is_main_guard(test: ast.expr) -> bool:
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = (test.left, test.comparators[0])
    return any(isinstance(a, ast.Name) and a.id == "__name__" and isinstance(b, ast.Constant) and b.value == "__main__"
               for a, b in (sides, sides[::-1]))


def main_guard_line(tree: ast.Module) -> int:
    return next((stmt.lineno for stmt in tree.body if isinstance(stmt, ast.If) and _is_main_guard(stmt.test)), 0)
