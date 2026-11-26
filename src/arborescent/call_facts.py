"""Call facts: the calls a module makes, resolved through its import alias table.

Call sites on an imported or module-level name, calls used as whole statements (a discarded result), the imported
callables each function or method calls, ``discard(value, reason=...)`` lines, the arguments each such call passes
(``call_shapes``, with the statically known type of a ``dataclasses.replace`` subject), and ``absolute_callee``, which
turns a recorded call (relative imports keep their dots) back into an absolute dotted name."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "REPLACE_CALLS", "CallShape", "FunctionCalls", "absolute_callee", "annotation_types", "bare_calls", "call_aliases", "call_shapes",
    "call_sites", "discard_call_lines", "dotted_name", "dynamic_imports", "function_calls",
]

REPLACE_CALLS = frozenset({"dataclasses.replace", "copy.replace"})  # build a new instance of their first argument's type
_WRAPPERS = frozenset({"Optional", "Union", "Annotated"})
_MAX_ALIASES = 50  # ``b = a; c = b; ...``: how far a ``replace`` subject's type is traced


@dataclass(frozen=True)
class CallShape:
    """What one call passes, as far as it can be read statically (#234).

    ``positional`` counts the leading positional arguments whose position is certain; a ``*value`` of unknown length ends
    the count. A literal ``*(a, b)`` or ``**{"k": v}`` is expanded; any other ``**value`` sets ``open_keywords``.
    """

    line: int
    callee: str  # like ``calls``: a relative import keeps its leading dots
    positional: int
    keywords: tuple[str, ...]  # sorted, unique
    open_keywords: bool = False
    replaced: tuple[str, ...] = ()  # for ``replace``: the dotted types its first argument may statically have


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


def annotation_types(node: ast.expr | None, aliases: dict[str, str]) -> set[str]:
    """The dotted classes a value annotated ``node`` may be: through strings, ``X | Y``, ``Optional``, ``Annotated`` and ``X[T]``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return annotation_types(ast.parse(node.value.strip(), mode="eval").body, aliases)
        except (SyntaxError, ValueError):
            return set()
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return annotation_types(node.left, aliases) | annotation_types(node.right, aliases)
    if isinstance(node, ast.Subscript):
        return _subscript_types(node, aliases)
    name = dotted_name(node, aliases) if node is not None else None
    return {name} if name is not None else set()


def _subscript_types(node: ast.Subscript, aliases: dict[str, str]) -> set[str]:
    outer = (dotted_name(node.value, aliases) or "").rpartition(".")[2]
    if outer not in _WRAPPERS:
        return annotation_types(node.value, aliases)  # a generic class: ``Box[int]`` is a ``Box``
    items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    return {name for item in (items[:1] if outer == "Annotated" else items) for name in annotation_types(item, aliases)}


@dataclass
class _Scope:
    """What each name bound in one function, class body or module may be. A bound name with no entry is unknown."""

    parent: _Scope | None
    bindings: dict[str, list[tuple[str, ast.expr | str | None]]] = field(default_factory=lambda: {})
    attributes: dict[str, ast.expr] = field(default_factory=lambda: {})  # the enclosing class's annotated attributes

    def bind(self, name: str, kind: str = "", value: ast.expr | str | None = None) -> None:
        entries = self.bindings.setdefault(name, [])
        if kind:
            entries.append((kind, value))

    def owner(self, name: str) -> _Scope | None:
        scope: _Scope | None = self
        while scope is not None and name not in scope.bindings:
            scope = scope.parent
        return scope


class _ReplaceTypes:
    """Flow-insensitive, one-module type guess for each ``replace`` subject: annotations, constructor calls and ``self``."""

    def __init__(self, tree: ast.Module, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.calls: list[tuple[ast.Call, _Scope]] = []
        module = _Scope(None)
        self.pending: list[tuple[ast.AST, _Scope]] = [(stmt, module) for stmt in tree.body]
        while self.pending:  # a stack, not recursion: a long ``a + b + ...`` is a very deep tree
            self._collect(*self.pending.pop())

    def types(self) -> dict[int, tuple[str, ...]]:
        return {id(call): tuple(sorted(self._value(call.args[0], scope, set()))) for call, scope in self.calls if call.args}

    def _collect(self, node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            self._function(node, scope, None)
            return
        if isinstance(node, ast.ClassDef):
            self._class(node, scope)
            return
        self._record(node, scope)
        self.pending.extend((child, scope) for child in ast.iter_child_nodes(node))

    def _record(self, node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, ast.Call) and dotted_name(node.func, self.aliases) in REPLACE_CALLS:
            self.calls.append((node, scope))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    scope.bind(target.id, "value", node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            scope.bind(node.target.id, "annotation", node.annotation)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            scope.bind(node.id)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, outer: _Scope, owner: ast.ClassDef | None) -> None:
        scope = _Scope(outer, attributes=outer.attributes if owner is None else _class_annotations(owner))
        args = node.args
        ordered = [*args.posonlyargs, *args.args]
        for arg in [*ordered, *args.kwonlyargs, *(a for a in (args.vararg, args.kwarg) if a is not None)]:
            scope.bind(arg.arg, "annotation" if arg.annotation is not None else "", arg.annotation)
        if owner is not None and ordered and not _is_static(node, self.aliases):
            scope.bind(ordered[0].arg, "class", owner.name)
        self.pending.extend((child, scope) for child in (node.body if isinstance(node.body, list) else [node.body]))

    def _class(self, node: ast.ClassDef, outer: _Scope) -> None:
        body = _Scope(outer)
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self._function(stmt, outer, node)  # a method does not see the class body's names
            else:
                self.pending.append((stmt, body))

    def _value(self, node: ast.expr, scope: _Scope, seen: set[tuple[int, str]]) -> set[str]:
        if isinstance(node, ast.Name):
            return self._name(node.id, scope, seen)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and self._is_instance(node.value.id, scope):
            return annotation_types(scope.attributes.get(node.attr), self.aliases)
        if not isinstance(node, ast.Call):
            return set()
        target = dotted_name(node.func, self.aliases)
        if target in REPLACE_CALLS:
            return self._value(node.args[0], scope, seen) if node.args else set()
        if isinstance(node.func, ast.Name) and self._is_instance(node.func.id, scope):
            return self._name(node.func.id, scope, seen)  # ``cls(...)``
        return {target} if target is not None else set()

    def _name(self, name: str, scope: _Scope, seen: set[tuple[int, str]]) -> set[str]:
        owner = scope.owner(name)
        if owner is None or (id(owner), name) in seen or len(seen) >= _MAX_ALIASES:
            return set()
        seen.add((id(owner), name))
        found: set[str] = set()
        for kind, value in owner.bindings[name]:
            if isinstance(value, str) and kind == "class":
                found.add(value)
            elif isinstance(value, ast.expr):
                found |= annotation_types(value, self.aliases) if kind == "annotation" else self._value(value, owner, seen)
        return found

    def _is_instance(self, name: str, scope: _Scope) -> bool:
        owner = scope.owner(name)
        return owner is not None and any(kind == "class" for kind, _ in owner.bindings[name])


def _class_annotations(node: ast.ClassDef) -> dict[str, ast.expr]:
    return {s.target.id: s.annotation for s in node.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}


def _is_static(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, aliases: dict[str, str]) -> bool:
    return not isinstance(node, ast.Lambda) and any(dotted_name(d, aliases) == "staticmethod" for d in node.decorator_list)


def _positional_count(args: Sequence[ast.expr]) -> int:
    count = 0
    for arg in args:
        if not isinstance(arg, ast.Starred):
            count += 1
        elif isinstance(arg.value, ast.Tuple | ast.List) and not any(isinstance(item, ast.Starred) for item in arg.value.elts):
            count += len(arg.value.elts)
        else:
            break
    return count


def _splat_keys(value: ast.expr) -> tuple[set[str], bool]:
    """The keys a ``**value`` certainly passes, and whether it may pass others."""
    if not isinstance(value, ast.Dict):
        return set(), True
    keys: set[str] = set()
    unknown = False
    for key, item in zip(value.keys, value.values, strict=True):
        if key is None:
            nested, open_nested = _splat_keys(item)
            keys |= nested
            unknown = unknown or open_nested
        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
        else:
            unknown = True
    return keys, unknown


def _keywords(node: ast.Call) -> tuple[tuple[str, ...], bool]:
    names: set[str] = set()
    unknown = False
    for keyword in node.keywords:
        if keyword.arg is not None:
            names.add(keyword.arg)
            continue
        keys, open_splat = _splat_keys(keyword.value)
        names |= keys
        unknown = unknown or open_splat
    return tuple(sorted(names)), unknown


def call_shapes(tree: ast.Module, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[CallShape, ...]:
    """The arguments of every call ``call_sites`` would record (and of ``Name[T](...)``), for the construction rule (#234)."""
    heads = set(aliases) | {stmt.name for stmt in tree.body if isinstance(stmt, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))}
    calls = [node for node in nodes if isinstance(node, ast.Call)]
    replaces = any(dotted_name(node.func, aliases) in REPLACE_CALLS for node in calls)
    subjects = _ReplaceTypes(tree, aliases).types() if replaces else {}
    out: set[CallShape] = set()
    for node in calls:
        function = node.func.value if isinstance(node.func, ast.Subscript) else node.func
        head = function
        while isinstance(head, ast.Attribute):
            head = head.value
        if isinstance(head, ast.Name) and head.id in heads and (name := dotted_name(function, aliases)) is not None:
            keywords, unknown = _keywords(node)
            out.add(CallShape(node.lineno, name, _positional_count(node.args), keywords, unknown, subjects.get(id(node), ())))
    return tuple(sorted(out, key=lambda s: (s.line, s.callee, s.positional, s.keywords, s.open_keywords, s.replaced)))


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
