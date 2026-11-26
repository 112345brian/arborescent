"""Call facts: the calls a module makes, resolved through its import alias table.

Call sites on an imported or module-level name, calls used as whole statements (a discarded result), the imported
callables each function or method calls, ``discard(value, reason=...)`` lines, the arguments each such call passes
(``call_shapes``, with the statically known type of a ``dataclasses.replace`` subject), and ``absolute_callee``, which
turns a recorded call (relative imports keep their dots) back into an absolute dotted name."""

from __future__ import annotations

import ast
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .import_syntax import child_nodes, walk_nodes

__all__ = [
    "REPLACE_CALLS", "CallShape", "FunctionCalls", "FunctionSpan", "absolute_callee", "annotation_types", "bare_calls", "call_aliases",
    "call_shapes", "call_sites", "discard_call_lines", "dotted_name", "dynamic_imports", "function_calls", "function_spans",
    "is_private_name", "patch_targets", "private_attributes", "unqualified_from_calls",
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

    pending: list[tuple[ast.AST, str]] = [(child, "") for child in child_nodes(tree)][::-1]
    while pending:  # a stack in source order, so a long ``elif`` ladder cannot overflow it
        child, prefix = pending.pop()
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
            out.append((prefix + child.name, child.lineno, child.end_lineno or child.lineno, start))
        elif isinstance(child, ast.stmt | ast.excepthandler | ast.match_case):
            inner = f"{prefix}{child.name}." if isinstance(child, ast.ClassDef) else prefix
            pending += [(item, inner) for item in child_nodes(child)][::-1]
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
        self.pending.extend((child, scope) for child in child_nodes(node))

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
            pending.extend(child_nodes(node))
    return found


def unqualified_from_calls(tree: ast.Module, nodes: Sequence[ast.AST], rebound: set[str]) -> tuple[tuple[int, str, str], ...]:
    """``(first line, origin "X.name", local name)`` for each name bound by a module-level ``from X import name`` and called bare
    inside a function (#226). A call that runs at import time, or a name the module rebinds or also imports lazily, is left out."""
    bound = _module_from_imports(tree)
    if not bound:
        return ()
    candidates = [  # filtered from the shared node list first: the function bodies are only located when one could count
        (node, node.func.id) for node in nodes
        if node.__class__ is ast.Call and node.func.__class__ is ast.Name and node.func.id in bound and node.func.id not in rebound
    ]
    if not candidates:
        return ()
    inside = _inside_function_bodies(nodes)
    lazy = {item.asname or item.name for node in nodes if isinstance(node, ast.Import | ast.ImportFrom) and inside(node) for item in node.names}
    first: dict[str, int] = {}
    for node, name in candidates:
        if name not in lazy and inside(node):
            first[name] = min(node.lineno, first.get(name, node.lineno))
    return tuple(sorted((line, bound[local], local) for local, line in first.items()))


def _inside_function_bodies(nodes: Sequence[ast.AST]) -> Callable[[ast.stmt | ast.expr], bool]:
    """Whether a node lies in the body of a function or ``lambda`` (not in its defaults, decorators or annotations).

    Answered by source position, so the bodies are not walked a second time."""
    spans: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for node in nodes:
        if node.__class__ is ast.Lambda:
            first = last = node.body
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            first, last = node.body[0], node.body[-1]
        else:
            continue
        starts = [first, *getattr(first, "decorator_list", [])]  # a decorator sits above its `def` line
        spans.append((min((item.lineno, item.col_offset) for item in starts), (last.end_lineno or last.lineno, last.end_col_offset or 0)))
    merged: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    begins = [start for start, _ in merged]

    def inside(node: ast.stmt | ast.expr) -> bool:
        position = (node.lineno, node.col_offset)
        slot = bisect_right(begins, position) - 1
        return slot >= 0 and position < merged[slot][1]

    return inside


def _imported_calls(func: ast.FunctionDef | ast.AsyncFunctionDef, aliases: dict[str, str]) -> tuple[str, ...]:
    calls: set[str] = set()
    for node in walk_nodes(func):
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
