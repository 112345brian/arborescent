"""Call facts: the calls a module makes, resolved through its import alias table.

Call sites on an imported or module-level name, calls used as whole statements (a discarded result), the imported
callables each function or method calls, ``discard(value, reason=...)`` lines, the arguments each such call passes
(``call_shapes``, with the statically known type of a ``dataclasses.replace`` subject), the methods called on values known to
be a ``pathlib.Path``, each branching function's control flow with ``unrecorded_path`` over it, and ``absolute_callee``, which
turns a recorded call (relative imports keep their dots) back into an absolute dotted name."""

from __future__ import annotations

import ast
import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Collection, Iterator, Sequence
from dataclasses import dataclass, field, replace

from .effect_categories import dotted_name

__all__ = [
    "REPLACE_CALLS",
    "CallShape",
    "FlowCall",
    "FlowEdge",
    "FlowNode",
    "FunctionCalls",
    "FunctionSpan",
    "absolute_callee",
    "annotation_types",
    "bare_calls",
    "call_aliases",
    "call_shapes",
    "call_sites",
    "discard_call_lines",
    "dotted_name",
    "dynamic_imports",
    "function_calls",
    "function_spans",
    "is_private_name",
    "optional_patch_lines",
    "patch_targets",
    "path_calls",
    "private_attributes",
    "unqualified_from_calls",
    "unrecorded_path",
]

REPLACE_CALLS = frozenset({"dataclasses.replace", "copy.replace"})  # build a new instance of their first argument's type
_WRAPPERS = frozenset({"Optional", "Union", "Annotated"})
_MAX_ALIASES = 50  # ``b = a; c = b; ...``: how far a ``replace`` subject's type is traced
_PATH_CLASSES = frozenset({"pathlib.Path", "pathlib.PosixPath", "pathlib.WindowsPath"})
_PATH_FACTORIES = frozenset(f"{cls}.{name}" for cls in _PATH_CLASSES for name in ("cwd", "home"))
_PATH_TO_PATH = frozenset({  # methods of a Path that return a Path
    "absolute", "expanduser", "joinpath", "readlink", "relative_to", "rename", "replace", "resolve", "with_name", "with_segments",
    "with_stem", "with_suffix",
})
_PATH_IO = frozenset({  # the methods of a Path that read or write the filesystem: the ones recorded
    "chmod", "hardlink_to", "lchmod", "mkdir", "open", "read_bytes", "read_text", "rename", "replace", "rmdir", "symlink_to", "touch",
    "unlink", "write_bytes", "write_text",
})
_NEVER_RETURNS = frozenset({"assert_never", "typing.assert_never", "typing_extensions.assert_never", "sys.exit", "os._exit", "exit", "quit"})
_EXIT = -1  # the edge target that means the function returned or ran off its end
_MAX_FLOW_NODES = 10_000  # nested ``finally`` blocks are copied per way out: past this the function is not judged path by path
_MAX_FLOW_STATES = 20_000  # likewise for the combinations of conditions one walk remembers
_WORD = re.compile(r"[A-Za-z_][\w.]*")


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
class FlowCall:
    """One call in a function's flow, spelled as ``calls`` records it when ``resolved``, else as ``method_calls`` does."""

    line: int
    text: str
    resolved: bool


@dataclass(frozen=True)
class FlowEdge:
    """One way out of a block. ``target`` is a block's index, or -1 for leaving the function normally (at ``line``, 0 for its end)."""

    target: int
    condition: str = ""  # a repeated test that is ``truth`` on this edge, so the same test cannot later be the opposite
    truth: bool = True
    absent: str = ""  # a name that is ``None`` or false on this edge: an optional recorder that is not there
    unless: tuple[FlowCall, ...] = ()  # into an ``except``: the calls the ``try`` body always makes; not taken if one records
    line: int = 0


@dataclass(frozen=True)
class FlowNode:
    """A run of statements without a branch: its calls, the condition names it rebinds, and where it goes next."""

    calls: tuple[FlowCall, ...] = ()
    assigned: tuple[str, ...] = ()
    edges: tuple[FlowEdge, ...] = ()  # none: the block leaves by an exception


@dataclass(frozen=True)
class FunctionSpan:
    """Where one function or method is, so a rule can say which of the module's recorded call sites are inside it (#229).

    ``name`` is ``f``, ``Class.method`` or ``Outer.Inner.method``. A nested function or ``lambda`` has no span of its own:
    its calls belong to the function around it. Decorators lie before ``line``: they run when the module defines the function.
    ``method_calls`` are the calls the module's recorded call sites leave out, a method on a value, as ``(first line, receiver.name)``
    with ``?`` for a receiver that is not a plain name (``make().send`` is ``?.send``): the companion rule matches them by name (#231).
    ``flow`` is the control flow, block 0 first, of a function with an ``if``, ``try`` or ``match`` and at least two calls; empty
    means every call of the function is on its one path.
    """

    name: str
    line: int  # the `def` line
    end_line: int
    start: int  # the first decorator's line, else `line`: where a comment above the function lands
    method_calls: tuple[tuple[int, str], ...] = ()
    flow: tuple[FlowNode, ...] = ()


def _method_calls(
    nodes: Sequence[ast.AST], heads: set[str], spans: Sequence[tuple[str, int, int, int]],
) -> tuple[list[dict[str, int]], dict[int, list[ast.Call]]]:
    """For each span, ``receiver.name`` -> first line, for the attribute calls whose head ``call_sites`` does not record; and
    the calls of each span whose paths can differ (a branch and at least two calls), by its index."""
    starts = [span[1] for span in spans]
    found: list[dict[str, int]] = [{} for _ in spans]
    calls: list[list[ast.Call]] = [[] for _ in spans]
    branching: set[int] = set()
    for node in nodes:
        if not isinstance(node, ast.Call | ast.If | ast.Try | ast.TryStar | ast.Match):
            continue
        slot = bisect_right(starts, node.lineno) - 1
        if slot < 0 or node.lineno > spans[slot][2]:
            continue
        if not isinstance(node, ast.Call):
            branching.add(slot)
            continue
        calls[slot].append(node)
        if not isinstance(node.func, ast.Attribute) or _root_name(node.func) in heads:
            continue
        text = dotted_name(node.func, {}) or f"?.{node.func.attr}"
        found[slot][text] = min(node.lineno, found[slot].get(text, node.lineno))
    return found, {slot: calls[slot] for slot in branching if len(calls[slot]) > 1}


def function_spans(
    tree: ast.Module, nodes: Sequence[ast.AST], aliases: dict[str, str], paths: Collection[tuple[int, str]] = (),
) -> tuple[FunctionSpan, ...]:
    """Every function and method that is not inside another function, however the module or class body nests it.

    ``paths`` is what ``path_calls`` found, so a flow names a ``Path`` method both ways."""
    out: list[tuple[str, int, int, int]] = []  # (name, def line, end line, first decorator line)
    defs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    pending: list[tuple[ast.AST, str]] = [(child, "") for child in ast.iter_child_nodes(tree)][::-1]
    while pending:  # a stack in source order, so a long ``elif`` ladder cannot overflow it
        child, prefix = pending.pop()
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
            out.append((prefix + child.name, child.lineno, child.end_lineno or child.lineno, start))
            defs.append(child)
        elif isinstance(child, ast.stmt | ast.excepthandler | ast.match_case):
            inner = f"{prefix}{child.name}." if isinstance(child, ast.ClassDef) else prefix
            pending += [(item, inner) for item in ast.iter_child_nodes(child)][::-1]
    heads = _heads(tree, aliases)
    methods, branching = _method_calls(nodes, heads, out)
    flows = _FlowBuilder(heads, aliases, set(paths))
    return tuple(
        FunctionSpan(*span, tuple(sorted((line, text) for text, line in found.items())), flows.build(func, branching[slot]) if slot in branching else ())
        for slot, (span, func, found) in enumerate(zip(out, defs, methods, strict=True))
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
    stores: dict[str, int] = field(default_factory=lambda: {})  # every assignment of a name, however it is spelled
    explained: dict[str, int] = field(default_factory=lambda: {})  # those of ``name = value`` / ``name: T``

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

    def __init__(self, tree: ast.Module, aliases: dict[str, str], wanted: set[int] | None = None) -> None:
        self.aliases = aliases
        self.calls: list[tuple[ast.Call, _Scope]] = []
        self.wanted: set[int] = wanted or set()  # the ``id`` of each method call ``_PathValues`` types
        self.methods: list[tuple[ast.expr, int, str, _Scope]] = []  # (receiver, line, method, scope) of those
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
        if id(node) in self.wanted and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            self.methods.append((node.func.value, node.lineno, node.func.attr, scope))
        if isinstance(node, ast.Call) and dotted_name(node.func, self.aliases) in REPLACE_CALLS:
            self.calls.append((node, scope))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    scope.bind(target.id, "value", node.value)
                    scope.explained[target.id] = scope.explained.get(target.id, 0) + 1
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            scope.bind(node.target.id, "annotation", node.annotation)
            scope.explained[node.target.id] = scope.explained.get(node.target.id, 0) + 1
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            scope.bind(node.id)
            scope.stores[node.id] = scope.stores.get(node.id, 0) + 1

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, outer: _Scope, owner: ast.ClassDef | None) -> None:
        scope = _Scope(outer, attributes=outer.attributes if owner is None else _class_annotations(owner))
        args = node.args
        ordered = [*args.posonlyargs, *args.args]
        for arg in [*ordered, *args.kwonlyargs, *(a for a in (args.vararg, args.kwarg) if a is not None)]:
            scope.bind(arg.arg, "annotation" if arg.annotation is not None else "unknown", arg.annotation)
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


class _PathValues(_ReplaceTypes):
    """The file method calls on a value that can only be a ``pathlib.Path``: every binding of the name says so (#229).

    One doubtful binding (a ``str | Path`` annotation, an untyped parameter, a loop target) and the name is left alone, so a
    ``str.replace`` is never taken for ``Path.replace``."""

    def __init__(self, tree: ast.Module, aliases: dict[str, str], wanted: set[int]) -> None:
        self.known: dict[tuple[int, str], bool] = {}
        super().__init__(tree, aliases, wanted)

    def found(self) -> tuple[tuple[int, str], ...]:
        return tuple(sorted({
            (line, f"pathlib.Path.{method}") for receiver, line, method, scope in self.methods
            if self._is_path(receiver, scope, set()) is True
        }))

    def _is_path(self, node: ast.expr, scope: _Scope, seen: set[tuple[int, str]]) -> bool | None:
        """``None`` for a name defined by itself, which says nothing either way."""
        step: ast.expr | bool = node
        while isinstance(step, ast.expr) and not isinstance(step, ast.Name):  # down a chain, however long
            step = self._step(step, scope)
        return self._named_path(step.id, scope, seen) if isinstance(step, ast.Name) else step

    def _step(self, node: ast.expr, scope: _Scope) -> ast.expr | bool:
        """The expression that decides whether ``node`` is a ``Path``, or the verdict itself."""
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return node.right if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str) else node.left
        if isinstance(node, ast.Call):
            return self._call_step(node)
        if not isinstance(node, ast.Attribute):
            return False
        if node.attr == "parent":
            return node.value
        instance = isinstance(node.value, ast.Name) and self._is_instance(node.value.id, scope)
        return instance and self._annotated_path(scope.attributes.get(node.attr))

    def _call_step(self, node: ast.Call) -> ast.expr | bool:
        target = dotted_name(node.func, self.aliases)
        if target in _PATH_CLASSES or target in _PATH_FACTORIES:
            return True
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _PATH_TO_PATH:
            return False
        if node.func.attr == "replace" and len(node.args) + len(node.keywords) != 1:
            return False
        return node.func.value

    def _named_path(self, name: str, scope: _Scope, seen: set[tuple[int, str]]) -> bool | None:
        owner = scope.owner(name)
        if owner is None or len(seen) >= _MAX_ALIASES:
            return False
        key = (id(owner), name)
        if key in self.known:
            return self.known[key]
        if key in seen:
            return None
        outermost = not seen
        seen.add(key)
        verdict: bool | None = False if owner.stores.get(name, 0) > owner.explained.get(name, 0) else None
        for kind, value in owner.bindings[name]:
            if verdict is False:
                break
            if kind == "annotation" and isinstance(value, ast.expr):
                found = self._annotated_path(value)
            else:
                found = self._is_path(value, owner, seen) if kind == "value" and isinstance(value, ast.expr) else False
            verdict = found if found is not None else verdict
        if outermost:
            self.known[key] = verdict is True  # only a whole answer is kept: one reached through a cycle may be partial
        return verdict

    def _annotated_path(self, annotation: ast.expr | None) -> bool:
        types = annotation_types(annotation, self.aliases)
        return bool(types) and types <= _PATH_CLASSES


def _may_be_path_io(node: ast.Call, aliases: dict[str, str]) -> bool:
    function = node.func
    if not isinstance(function, ast.Attribute) or function.attr not in _PATH_IO:
        return False
    if function.attr == "replace" and len(node.args) + len(node.keywords) != 1:
        return False  # ``Path.replace`` takes the target and nothing else; ``str.replace`` takes two
    # ``os.replace``, ``Path.replace(a, b)``: spelled on an import, so ``calls`` already has it
    return not (isinstance(function.value, ast.Name | ast.Attribute) and _root_name(function) in aliases)


def path_calls(tree: ast.Module, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[tuple[int, str], ...]:
    """``(line, "pathlib.Path.<method>")`` of every file method called on a value known to be a ``Path``.

    Only a module that imports ``pathlib`` and calls such a method on a value is walked for its types."""
    if not any(origin == "pathlib" or origin.startswith("pathlib.") for origin in aliases.values()):
        return ()
    wanted = {id(node) for node in nodes if isinstance(node, ast.Call) and _may_be_path_io(node, aliases)}
    return _PathValues(tree, aliases, wanted).found() if wanted else ()


class _TooBig(Exception):
    """A function whose flow is not worth following: it is judged as a whole, as one without branches is."""


@dataclass
class _Block:
    calls: list[FlowCall] = field(default_factory=lambda: [])
    parts: list[ast.AST] = field(default_factory=lambda: [])  # what may rebind a name, read only when a condition repeats
    edges: list[FlowEdge] = field(default_factory=lambda: [])


_Jump = Callable[[int, int], None]  # (the block that leaves, the line it leaves at)


@dataclass(frozen=True)
class _Ways:
    """Where ``return``, ``raise``, ``break`` and ``continue`` go from here, through every ``finally`` on the way."""

    returns: _Jump
    raises: _Jump
    breaks: _Jump
    continues: _Jump


def _dropped(block: int, line: int) -> None:
    """Out of the function by an exception: not a path that has to have recorded anything."""


def _condition(test: ast.expr) -> tuple[str, bool, str]:
    """``(text, truth when the test passes, dotted name the text is about)``; ``""`` for a test that is not compared.

    Only names, attributes, constants and one comparison of them: a call may answer differently the second time."""
    truth = True
    while isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test, truth = test.operand, not truth
    if isinstance(test, ast.BoolOp):  # ``a and b``: the same words twice mean the same thing; nested ones are not compared
        parts = [_condition(item) for item in test.values if not isinstance(item, ast.BoolOp)]
        if len(parts) != len(test.values) or not all(text for text, _, _ in parts):
            return "", truth, ""
        word = " and " if isinstance(test.op, ast.And) else " or "
        return word.join(f"{'' if value else 'not '}{text}" for text, value, _ in parts), truth, ""
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        left, right = _operand(test.left), _operand(test.comparators[0])
        operator = type(test.ops[0])
        word = {ast.Is: "is", ast.IsNot: "is", ast.Eq: "==", ast.NotEq: "=="}.get(operator)
        if left is None or right is None or word is None:
            return "", truth, ""
        truth = truth != (operator in (ast.IsNot, ast.NotEq))
        return f"{left} {word} {right}", truth, left if right == "None" else ""
    name = dotted_name(test, {})
    return (name, truth, name) if name is not None else ("", truth, "")


def _operand(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant):
        return repr(node.value) if isinstance(node.value, str | bool | type(None)) or node.value.__class__ is int else None
    return dotted_name(node, {})


def _branch(target: int, test: ast.expr, passed: bool) -> FlowEdge:
    text, truth, subject = _condition(test)
    value = truth == passed  # what ``text`` is on this edge
    absent = ""
    if subject:
        absent = subject if (text == subject and not value) or (text != subject and value) else ""
    return FlowEdge(target, text, value, absent)


def _exhaustive(cases: Sequence[ast.match_case]) -> bool:
    """A ``case _``, or at least two unguarded cases that all name members, classes, ``None`` or a boolean: such a ``match``
    is taken to cover its subject, as a type checker would require of one over an enum or a union."""
    if any(case.guard is None and isinstance(case.pattern, ast.MatchAs) and case.pattern.pattern is None for case in cases):
        return True

    def named(pattern: ast.pattern) -> bool:
        if isinstance(pattern, ast.MatchOr):
            return all(named(item) for item in pattern.patterns)
        if isinstance(pattern, ast.MatchValue):
            return isinstance(pattern.value, ast.Attribute)
        return isinstance(pattern, ast.MatchClass | ast.MatchSingleton)

    return len(cases) > 1 and all(case.guard is None and named(case.pattern) for case in cases)


def _stored(node: ast.AST) -> str | None:
    """The dotted name ``node`` rebinds, when it is an assignment or deletion target."""
    if isinstance(node, ast.Name | ast.Attribute) and not isinstance(node.ctx, ast.Load):
        return dotted_name(node, {})
    if isinstance(node, ast.MatchAs | ast.MatchStar):
        return node.name
    return None


def _always_run(stmts: Sequence[ast.stmt]) -> Iterator[ast.AST]:
    """The parts of a block that run whenever the block runs to its end: not what is under a branch or a loop."""
    pending = list(stmts)
    while pending:
        stmt = pending.pop()
        if isinstance(stmt, ast.With | ast.AsyncWith):
            pending += stmt.body
            yield from stmt.items
        elif isinstance(stmt, ast.Try | ast.TryStar):
            pending += [*stmt.body, *stmt.finalbody]
        elif not isinstance(stmt, ast.If | ast.Match | ast.For | ast.AsyncFor | ast.While):
            yield stmt


class _FlowBuilder:
    """One function's statements as blocks and edges. A loop is taken to run its body at least once."""

    def __init__(self, heads: set[str], aliases: dict[str, str], paths: set[tuple[int, str]]) -> None:
        self.heads = heads
        self.aliases = aliases
        self.paths: dict[tuple[int, str], str] = {(line, name.rpartition(".")[2]): name for line, name in paths}
        self.blocks: list[_Block] = []
        self.tests: dict[str, set[int]] = {}  # condition text -> the tests that spell it

    def build(self, func: ast.FunctionDef | ast.AsyncFunctionDef, calls: Sequence[ast.Call]) -> tuple[FlowNode, ...]:
        """``calls`` are the calls on the function's lines, in any order."""
        self.blocks, self.tests = [], {}
        self.found = sorted((((node.lineno, node.col_offset), self._spelled(node)) for node in calls), key=lambda item: item[0])
        self.positions = [position for position, _ in self.found]
        try:
            entry = self._new()
            end = self._body(func.body, entry, _Ways(self._leave, _dropped, _dropped, _dropped))
        except (_TooBig, RecursionError):
            return ()
        if end is not None:
            self._leave(end, 0)
        return self._frozen()

    def _frozen(self) -> tuple[FlowNode, ...]:
        """Only a test spelled twice can contradict itself, so only those conditions, and what rebinds their names, are kept."""
        repeated = {text for text, sites in self.tests.items() if len(sites) > 1}
        nodes = [
            FlowNode(
                tuple(block.calls),
                tuple(sorted({
                    name for part in block.parts for node in ast.walk(part)
                    if (name := _stored(node)) is not None and any(_mentions(text, [name]) for text in repeated)
                })) if repeated else (),
                tuple(edge if not edge.condition or edge.condition in repeated else replace(edge, condition="", truth=True) for edge in block.edges),
            )
            for block in self.blocks
        ]
        return _compact(nodes)

    def _new(self) -> int:
        if len(self.blocks) >= _MAX_FLOW_NODES:
            raise _TooBig
        self.blocks.append(_Block())
        return len(self.blocks) - 1

    def _go(self, source: int, target: int) -> None:
        self.blocks[source].edges.append(FlowEdge(target))

    def _leave(self, block: int, line: int) -> None:
        self.blocks[block].edges.append(FlowEdge(_EXIT, line=line))

    def _spelled(self, node: ast.Call) -> tuple[FlowCall, ...]:
        """One call as the module's facts name it: nothing for a bare local name, two names for a ``Path`` method."""
        function = node.func
        out: list[FlowCall] = []
        if _root_name(function) in self.heads and (name := dotted_name(function, self.aliases)) is not None:
            out.append(FlowCall(node.lineno, name, True))
        elif isinstance(function, ast.Attribute):
            out.append(FlowCall(node.lineno, dotted_name(function, {}) or f"?.{function.attr}", False))
        if isinstance(function, ast.Attribute) and (path := self.paths.get((node.lineno, function.attr))) is not None:
            out.append(FlowCall(node.lineno, path, True))
        return tuple(out)

    def _calls(self, part: ast.AST) -> list[FlowCall]:
        """The calls written inside ``part``, found by position so no statement is walked a second time."""
        if isinstance(part, ast.withitem):
            return [*self._calls(part.context_expr), *(self._calls(part.optional_vars) if part.optional_vars is not None else [])]
        if not isinstance(part, ast.stmt | ast.expr | ast.pattern):
            return []
        decorators = getattr(part, "decorator_list", [])  # a decorator sits above its `def` line
        first = min((item.lineno, item.col_offset) for item in (part, *decorators))
        start = bisect_left(self.positions, first)
        end = bisect_left(self.positions, (part.end_lineno or part.lineno, part.end_col_offset or 0))
        return [call for _, calls in self.found[start:end] for call in calls]

    def _run(self, block: int, part: ast.AST | None) -> None:
        """What ``part`` calls and may rebind, in ``block``. A nested function or ``lambda`` counts where it is written."""
        if part is None:
            return
        self.blocks[block].calls += self._calls(part)
        self.blocks[block].parts.append(part)

    def _body(self, stmts: Sequence[ast.stmt], block: int, ways: _Ways) -> int | None:
        """The block control is in after ``stmts``; ``None`` when it cannot get there."""
        current: int | None = block
        for stmt in stmts:
            if current is None:
                break
            current = self._statement(stmt, current, ways)
        return current

    def _statement(self, stmt: ast.stmt, block: int, ways: _Ways) -> int | None:
        if isinstance(stmt, ast.If):
            return self._if(stmt, block, ways)
        if isinstance(stmt, ast.Try | ast.TryStar):
            return self._try(stmt, block, ways)
        if isinstance(stmt, ast.For | ast.AsyncFor | ast.While):
            return self._loop(stmt, block, ways)
        if isinstance(stmt, ast.Match):
            return self._match(stmt, block, ways)
        if isinstance(stmt, ast.With | ast.AsyncWith):
            for item in stmt.items:
                self._run(block, item)
            return self._body(stmt.body, block, ways)
        return self._simple(stmt, block, ways)

    def _simple(self, stmt: ast.stmt, block: int, ways: _Ways) -> int | None:
        self._run(block, stmt)
        jump = self._jump(stmt, ways)
        if jump is None:
            return block
        jump(block, stmt.lineno)
        return None

    def _jump(self, stmt: ast.stmt, ways: _Ways) -> _Jump | None:
        """Where ``stmt`` sends control, when it does not just go on to the next statement."""
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            return ways.raises if dotted_name(stmt.value.func, self.aliases) in _NEVER_RETURNS else None
        by_type = {ast.Break: ways.breaks, ast.Continue: ways.continues, ast.Return: ways.returns, ast.Raise: ways.raises}
        return by_type.get(type(stmt))

    def _if(self, stmt: ast.If, block: int, ways: _Ways) -> int:
        after = self._new()
        while True:  # an ``elif`` ladder of any length, without recursing
            self._run(block, stmt.test)
            text = _condition(stmt.test)[0]
            self.tests.setdefault(text, set()).add(id(stmt))
            self.tests.pop("", None)
            self._arm(stmt.body, block, _branch(self._new(), stmt.test, True), after, ways)
            if len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.If):
                otherwise = self._new()
                self.blocks[block].edges.append(_branch(otherwise, stmt.test, False))
                block, stmt = otherwise, stmt.orelse[0]
                continue
            self._arm(stmt.orelse, block, _branch(self._new(), stmt.test, False), after, ways)
            return after

    def _arm(self, stmts: Sequence[ast.stmt], source: int, edge: FlowEdge, after: int, ways: _Ways) -> None:
        self.blocks[source].edges.append(edge)
        end = self._body(stmts, edge.target, ways)
        if end is not None:
            self._go(end, after)

    def _match(self, stmt: ast.Match, block: int, ways: _Ways) -> int:
        self._run(block, stmt.subject)
        after = self._new()
        for case in stmt.cases:
            start = self._new()
            self._run(start, case.pattern)
            self._run(start, case.guard)
            self._arm(case.body, block, FlowEdge(start), after, ways)
        if not _exhaustive(stmt.cases):
            self._go(block, after)
        return after

    def _loop(self, stmt: ast.For | ast.AsyncFor | ast.While, block: int, ways: _Ways) -> int:
        body, again, after = self._new(), self._new(), self._new()
        head = stmt.test if isinstance(stmt, ast.While) else stmt.iter
        self._run(block, head)
        if isinstance(stmt, ast.While):
            self._run(again, head)
        else:
            self._run(body, stmt.target)
        self._go(block, body)
        end = self._body(stmt.body, body, replace(ways, breaks=lambda b, _: self._go(b, after), continues=lambda b, _: self._go(b, again)))
        if end is not None:
            self._go(end, again)
        self._go(again, body)
        if not (isinstance(stmt, ast.While) and isinstance(stmt.test, ast.Constant) and bool(stmt.test.value)):  # `while True`
            done = self._new()
            self._go(again, done)
            end = self._body(stmt.orelse, done, ways)
            if end is not None:
                self._go(end, after)
        return after

    def _try(self, stmt: ast.Try | ast.TryStar, block: int, ways: _Ways) -> int:
        after, body = self._new(), self._new()
        starts = [self._new() for _ in stmt.handlers]
        outer = self._through(stmt.finalbody, ways)

        def caught(source: int, line: int) -> None:
            for start in starts:
                self._go(source, start)

        # an implicit exception: nothing the body did is assumed, except that a record it always makes was at least tried
        unless = tuple(call for part in _always_run(stmt.body) for call in self._calls(part))
        for handler, start in zip(stmt.handlers, starts, strict=True):
            self.blocks[block].edges.append(FlowEdge(start, unless=unless))
            self.blocks[start].parts += stmt.body  # what the body may have rebound before it raised
            self._run(start, handler.type)
            self._close(self._body(handler.body, start, outer), stmt.finalbody, after, ways)
        self._go(block, body)
        end = self._body(stmt.body, body, replace(outer, raises=caught) if starts else outer)
        if end is not None:
            end = self._body(stmt.orelse, end, outer)
        self._close(end, stmt.finalbody, after, ways)
        return after

    def _close(self, end: int | None, finalbody: Sequence[ast.stmt], after: int, ways: _Ways) -> None:
        if end is not None:
            end = self._body(finalbody, end, ways)
        if end is not None:
            self._go(end, after)

    def _through(self, finalbody: Sequence[ast.stmt], ways: _Ways) -> _Ways:
        """``ways`` with the ``finally`` block run first: a copy for each way out, so each still goes where it was going."""
        if not finalbody:
            return ways

        def wrap(jump: _Jump) -> _Jump:
            def leave(block: int, line: int) -> None:
                start = self._new()
                self._go(block, start)
                end = self._body(finalbody, start, ways)
                if end is not None:
                    jump(end, line)

            return leave

        return _Ways(wrap(ways.returns), wrap(ways.raises), wrap(ways.breaks), wrap(ways.continues))


def _compact(nodes: list[FlowNode]) -> tuple[FlowNode, ...]:
    """The same flow without the blocks that only pass control on, or that nothing reaches. A flow whose calls are all in
    its first block has them on every path, so it says nothing and is dropped."""
    forward = {
        index: node.edges[0].target for index, node in enumerate(nodes)
        if index and not node.calls and not node.assigned and len(node.edges) == 1
        and node.edges[0].target != _EXIT and node.edges[0] == FlowEdge(node.edges[0].target)
    }

    def final(target: int) -> int:
        passed: set[int] = set()
        while target in forward and target not in passed:  # an empty loop passes to itself
            passed.add(target)
            target = forward[target]
        return target

    numbers = {0: 0}
    order = [0]
    read = 0
    while read < len(order):  # it grows as it is read: every block reachable from the first
        for edge in nodes[order[read]].edges:
            target = final(edge.target)
            if target != _EXIT and target not in numbers:
                numbers[target] = len(order)
                order.append(target)
        read += 1
    if not any(nodes[index].calls for index in order[1:]):
        return ()
    return tuple(
        replace(nodes[index], edges=tuple(dict.fromkeys(
            edge if edge.target == _EXIT else replace(edge, target=numbers[final(edge.target)]) for edge in nodes[index].edges
        )))
        for index in order
    )


def unrecorded_path(
    flow: Sequence[FlowNode], actions: Collection[tuple[str, bool]], records: Collection[tuple[str, bool]],
) -> tuple[int, int] | None:
    """``(action line, line left at or 0 for the end)`` of a path that does an action and leaves normally with no record.

    ``actions`` and ``records`` are ``(text, resolved)`` of the flow's calls. A record before the action counts too. ``None``
    when there is no such path, or when the flow has too many states to follow."""
    recorders = {text for text, _ in records}
    pending: list[tuple[int, int, frozenset[tuple[str, bool]]]] = [(0, 0, frozenset())]  # (block, action line or 0, conditions)
    seen = set(pending)
    found: list[tuple[int, int]] = []
    while pending:
        index, acted, held = pending.pop()
        node = flow[index]
        if any((call.text, call.resolved) in records for call in node.calls):
            continue  # recorded: nothing after this can lose it
        acted = acted or min((call.line for call in node.calls if (call.text, call.resolved) in actions), default=0)
        held = frozenset(item for item in held if not _mentions(item[0], node.assigned))
        for edge in node.edges:
            if not _open_edge(edge, held, records, recorders):
                continue
            if edge.target == _EXIT and acted:
                found.append((acted, edge.line))
            state = (edge.target, acted, held | {(edge.condition, edge.truth)} if edge.condition else held)
            if edge.target != _EXIT and state not in seen:
                if len(seen) >= _MAX_FLOW_STATES:
                    return None
                seen.add(state)
                pending.append(state)
    return min(found, default=None)


def _open_edge(
    edge: FlowEdge, held: frozenset[tuple[str, bool]], records: Collection[tuple[str, bool]], recorders: Collection[str],
) -> bool:
    """Whether an unrecorded path can take ``edge``: not against a test already answered, not into an ``except`` after a
    ``try`` body that records, and not where the recorder is known to be missing."""
    if (edge.condition, not edge.truth) in held or any((call.text, call.resolved) in records for call in edge.unless):
        return False
    return not edge.absent or not any(text == edge.absent or text.startswith(edge.absent + ".") for text in recorders)


def _mentions(condition: str, names: Sequence[str]) -> bool:
    return any(word == name or word.startswith(name + ".") for word in _WORD.findall(condition) for name in names)


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


def _may_create(call: ast.Call) -> bool:
    """``create=`` (mock) or ``raising=`` (monkeypatch) that is not literally strict, or ``setattr``'s positional ``raising``."""
    for keyword in call.keywords:
        strict = {"create": False, "raising": True}.get(keyword.arg or "")
        if strict is not None and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is strict):
            return True
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "setattr" and call.args):
        return False
    position = 2 if _text(call.args[0]) is not None else 3  # ("a.b", value, raising) or (obj, "name", value, raising)
    if len(call.args) <= position:
        return False
    flag = call.args[position]
    return not (isinstance(flag, ast.Constant) and flag.value is True)


def optional_patch_lines(source: str) -> frozenset[int]:
    """Lines of patch calls that succeed when the attribute is absent, so a missing target is on purpose (#215)."""
    try:
        tree = ast.parse(source.removeprefix("\ufeff"))
    except (SyntaxError, ValueError, RecursionError):
        return frozenset[int]()
    return frozenset(node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and _may_create(node))


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
