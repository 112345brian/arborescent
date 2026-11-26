"""Definition facts: the shape of what a module defines at its top level.

Module-level classes and functions with whether each is abstract, every class's bases and member names, the fields and locals that show what they hold, ``typing.Protocol`` methods whose return type says
nothing, the public surface (a static ``__all__`` or public bindings) and which of it is re-exported, functions
annotated to return a ``*Result``/``*Outcome``, and the ``if __name__ == "__main__":`` guard.

``class_shapes`` records what each module-level class's constructor accepts, and ``ClassIndex`` resolves that across
modules (inheritance and re-exports), for the construction rule (#234).

Two queries read a module's source again, only when their opt-in rule is on (#215): ``module_attributes`` (what a patch of the
module can replace) and ``shadowed_callees`` (a local assigned from a call to the same-named module-level name)."""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .call_facts import absolute_callee, annotation_types, dotted_name
from .purity_facts import Helpers

__all__ = [
    "ClassIndex", "ClassMembers", "ClassShape", "IndexedModule", "NameDecl", "Parameter", "Signature", "TopLevelDef", "class_shapes",
    "main_guard_line", "member_shapes", "module_attributes", "name_declarations", "purity_helpers", "result_defs",
    "shadowed_callees", "surface", "top_level_defs", "weak_port_returns",
]

_DATACLASS_NAMES = frozenset({"dataclasses.dataclass", "pydantic.dataclasses.dataclass"})
_FIELD_NAMES = frozenset({"dataclasses.field"})
_KW_ONLY_NAMES = frozenset({"dataclasses.KW_ONLY"})
_NAMED_TUPLE_NAMES = frozenset({"typing.NamedTuple", "typing_extensions.NamedTuple"})
_PLAIN_BASES = frozenset({"object", "abc.ABC", "typing.Generic", "typing.Protocol", "typing_extensions.Protocol"})
_NEUTRAL_DECORATORS = frozenset({"typing.final", "typing_extensions.final", "typing.runtime_checkable", "functools.total_ordering"})
_DERIVED_MARKER = ("arbor", "derived")  # ``field(metadata={"arbor": "derived"})``


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
class ClassMembers:
    """What a class statement says about itself, for counting a port's implementors (#219)."""

    name: str  # qualified by the enclosing classes and functions: "Outer.Inner"
    line: int
    bases: tuple[str, ...]  # dotted through import aliases; a relative import keeps its dots, a local name stays bare
    members: tuple[str, ...]  # methods, class attributes and `self.x` assignments it defines itself, sorted
    protocol: bool  # lists `typing.Protocol` directly
    abstract: bool  # a Protocol, lists `ABC`/`ABCMeta` directly, or declares an `@abstractmethod`


def _blocks(stmt: ast.stmt) -> Iterator[ast.stmt]:
    """The statements directly inside a compound statement (`if`, `try`, `with`, `match`, loops).

    An ``elif`` ladder is read arm by arm, so its length does not become recursion depth in the visitors."""
    while isinstance(stmt, ast.If) and len(stmt.orelse) == 1 and isinstance(stmt.orelse[0], ast.If):
        yield from stmt.body
        stmt = stmt.orelse[0]
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


def _class_members(node: ast.ClassDef, name: str, aliases: dict[str, str]) -> ClassMembers:
    bases = tuple(
        dotted for base in node.bases
        if (dotted := dotted_name(base.value if isinstance(base, ast.Subscript) else base, aliases)) is not None
    )
    protocol = any(base in _PROTOCOL_NAMES for base in bases)
    return ClassMembers(
        name, node.lineno, bases, tuple(sorted(set(_members(node.body)) - _NOT_MEMBERS)), protocol,
        protocol or _is_abstract_class(node, aliases) or _declares_abstract_method(node, aliases),
    )


def member_shapes(tree: ast.Module, aliases: dict[str, str]) -> tuple[ClassMembers, ...]:
    """Every class in the module, nested and function-local ones included. Only statements are walked: a class is one."""
    out: list[ClassMembers] = []

    def visit(body: Iterable[ast.stmt], prefix: str) -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                out.append(_class_members(stmt, prefix + stmt.name, aliases))
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


# An annotation made only of these says nothing about what the value is.
_SILENT_ANNOTATIONS = frozenset({"Any", "object", "Self"})
_WRAPPING_ANNOTATIONS = frozenset({"Optional", "Union", "Annotated", "ClassVar", "Final", "typing", "None"})
_LITERAL_VALUES = (ast.JoinedStr, ast.List, ast.Dict, ast.Set, ast.Tuple, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


@dataclass(frozen=True)
class NameDecl:
    """A field or local that shows what it holds: by its annotation, or else by a literal value (#221)."""

    line: int
    kind: str  # "field" | "local"
    owner: str  # the qualified class (field) or function (local)
    name: str
    annotation: tuple[str, ...]  # the identifiers its annotation names; empty when the evidence is a literal value


def _identifiers(node: ast.expr) -> Iterator[str]:
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            yield child.id
        elif isinstance(child, ast.Attribute):
            yield child.attr
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            try:
                yield from _identifiers(ast.parse(child.value.strip(), mode="eval").body)
            except SyntaxError:
                yield "Any"  # unreadable: no evidence


def _evidence(stmt: ast.Assign | ast.AnnAssign) -> tuple[str, ...] | None:
    """The annotation's identifiers, ``()`` for an unannotated literal value, None when nothing shows what is held."""
    if isinstance(stmt, ast.AnnAssign):
        named = tuple(sorted(set(_identifiers(stmt.annotation)) - _WRAPPING_ANNOTATIONS))
        return named if set(named) - _SILENT_ANNOTATIONS else None
    value = stmt.value
    literal = isinstance(value, _LITERAL_VALUES) or (
        isinstance(value, ast.Constant) and value.value is not None and value.value is not ...
    )
    return () if literal else None


_Scope = tuple[str, str, str]  # (enclosing qualified name, "module" | "class" | "function", the method's `self` or "")


def _declared(stmt: ast.Assign | ast.AnnAssign, scope: _Scope, class_name: str) -> Iterator[NameDecl]:
    owner, kind, self_name = scope
    evidence = _evidence(stmt)
    if evidence is None:
        return
    for target in stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]:
        if isinstance(target, ast.Name) and kind == "function":
            yield NameDecl(stmt.lineno, "local", owner, target.id, evidence)
        elif isinstance(target, ast.Name) and kind == "class" and isinstance(stmt, ast.AnnAssign):
            yield NameDecl(stmt.lineno, "field", owner, target.id, evidence)  # unannotated: an enum member or a constant
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == self_name:
            yield NameDecl(stmt.lineno, "field", class_name, target.attr, evidence)


def name_declarations(tree: ast.Module) -> tuple[NameDecl, ...]:
    """Class fields, ``self.x`` assignments and function locals that carry evidence of what they hold, in source order."""
    out: list[NameDecl] = []

    def visit(body: Iterable[ast.stmt], scope: _Scope, class_name: str) -> None:
        owner, kind, _ = scope
        prefix = f"{owner}." if owner else ""
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                visit(stmt.body, (prefix + stmt.name, "class", ""), prefix + stmt.name)
            elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                params = [*stmt.args.posonlyargs, *stmt.args.args]
                self_name = params[0].arg if kind == "class" and params else ""
                visit(stmt.body, (prefix + stmt.name, "function", self_name), class_name)
            else:
                if isinstance(stmt, ast.Assign | ast.AnnAssign):
                    out.extend(_declared(stmt, scope, class_name))
                visit(_blocks(stmt), scope, class_name)

    visit(tree.body, ("", "module", ""), "")
    return tuple(out)


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


def is_main_guard(test: ast.expr) -> bool:
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = (test.left, test.comparators[0])
    return any(isinstance(a, ast.Name) and a.id == "__name__" and isinstance(b, ast.Constant) and b.value == "__main__"
               for a, b in (sides, sides[::-1]))


def main_guard_line(tree: ast.Module) -> int:
    return next((stmt.lineno for stmt in tree.body if isinstance(stmt, ast.If) and is_main_guard(stmt.test)), 0)


@dataclass(frozen=True)
class Parameter:
    name: str
    positional: bool  # False for keyword-only parameters and ``field(init=False)``


@dataclass(frozen=True)
class ClassShape:
    """One module-level class, as far as its constructor is concerned (#234)."""

    name: str
    bases: tuple[str, ...]  # dotted, relative imports keep their dots; "?" for a base that is not a name
    fields: tuple[Parameter, ...]  # own annotated fields, in order
    init: tuple[Parameter, ...] | None  # the parameters of an explicit ``__init__``, without ``self``
    is_dataclass: bool = False
    opaque: bool = False  # a decorator or metaclass that may change the constructor
    open_keywords: bool = False  # ``__init__`` takes ``**kwargs``
    derived: tuple[str, ...] = ()  # fields marked ``field(metadata={"arbor": "derived"})``


def _literal_keyword(call: ast.Call, name: str) -> object:
    return next((k.value.value for k in call.keywords if k.arg == name and isinstance(k.value, ast.Constant)), None)


def _is_marked(call: ast.Call) -> bool:
    metadata = next((k.value for k in call.keywords if k.arg == "metadata"), None)
    return isinstance(metadata, ast.Dict) and any(
        isinstance(key, ast.Constant) and isinstance(value, ast.Constant) and (key.value, value.value) == _DERIVED_MARKER
        for key, value in zip(metadata.keys, metadata.values, strict=True)
    )


def _field_parameter(name: str, spec: ast.Call | None, kw_only: bool) -> Parameter:
    if spec is None:
        return Parameter(name, not kw_only)
    override = _literal_keyword(spec, "kw_only")
    keyword_only = kw_only if override is None else override is True
    return Parameter(name, _literal_keyword(spec, "init") is not False and not keyword_only)


def _class_fields(node: ast.ClassDef, aliases: dict[str, str], kw_only: bool) -> tuple[tuple[Parameter, ...], tuple[str, ...]]:
    fields: list[Parameter] = []
    derived: list[str] = []
    for stmt in node.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
            continue
        types = annotation_types(stmt.annotation, aliases)
        if types & _KW_ONLY_NAMES:
            kw_only = True
            continue
        if any(name.rpartition(".")[2] == "ClassVar" for name in types):
            continue
        spec = stmt.value if isinstance(stmt.value, ast.Call) and dotted_name(stmt.value.func, aliases) in _FIELD_NAMES else None
        if spec is not None and _is_marked(spec):
            derived.append(stmt.target.id)
        fields.append(_field_parameter(stmt.target.id, spec, kw_only))
    return tuple(fields), tuple(derived)


def _init_parameters(node: ast.ClassDef) -> tuple[tuple[Parameter, ...] | None, bool]:
    init = next((s for s in reversed(node.body) if isinstance(s, ast.FunctionDef) and s.name == "__init__"), None)  # after any @overload
    if init is None:
        return None, False
    args = init.args
    ordered = [*args.posonlyargs, *args.args][1:]
    params = [Parameter(a.arg, True) for a in ordered] + [Parameter(a.arg, False) for a in args.kwonlyargs]
    return tuple(params), args.kwarg is not None


def _decorators(node: ast.ClassDef, aliases: dict[str, str]) -> tuple[ast.expr | None, bool]:
    """(the ``dataclass`` decorator if there is one, whether anything else may change the constructor)."""
    found: ast.expr | None = None
    opaque = any(k.arg == "metaclass" and dotted_name(k.value, aliases) not in _ABC_META_NAMES for k in node.keywords)
    for item in node.decorator_list:
        name = dotted_name(item.func if isinstance(item, ast.Call) else item, aliases)
        if name in _DATACLASS_NAMES:
            found = item
        elif name not in _NEUTRAL_DECORATORS:
            opaque = True
    return found, opaque


def _class_shape(node: ast.ClassDef, aliases: dict[str, str]) -> ClassShape:
    decorator, opaque = _decorators(node, aliases)
    options = decorator if isinstance(decorator, ast.Call) else None
    fields, derived = _class_fields(node, aliases, options is not None and _literal_keyword(options, "kw_only") is True)
    init, open_keywords = _init_parameters(node)
    no_init = options is not None and init is None and _literal_keyword(options, "init") is False
    own_new = any(isinstance(s, ast.FunctionDef) and s.name == "__new__" for s in node.body)  # decides what a call takes
    bases = tuple(dotted_name(b.value if isinstance(b, ast.Subscript) else b, aliases) or "?" for b in node.bases)
    return ClassShape(node.name, bases, fields, init, decorator is not None, opaque or no_init or own_new, open_keywords, derived)


def class_shapes(tree: ast.Module, aliases: dict[str, str]) -> tuple[ClassShape, ...]:
    return tuple(_class_shape(stmt, aliases) for stmt in tree.body if isinstance(stmt, ast.ClassDef))


@dataclass(frozen=True)
class Signature:
    """What a class's constructor accepts. ``None`` means it cannot be read statically (an unknown base, decorator or metaclass)."""

    parameters: tuple[Parameter, ...] | None
    open_keywords: bool = False
    derived: frozenset[str] = frozenset()

    @property
    def positional(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.parameters or () if p.positional)

    def lacks(self, name: str) -> bool:
        """Whether ``name`` is certainly not a keyword this constructor takes."""
        return self.parameters is not None and not self.open_keywords and all(p.name != name for p in self.parameters)


class _Facts(Protocol):
    @property
    def class_shapes(self) -> tuple[ClassShape, ...]: ...

    @property
    def reexports(self) -> tuple[tuple[str, int, str, str], ...]: ...


class IndexedModule(Protocol):
    """What the index reads of ``facts.ModuleInfo``, which cannot be imported here (``facts`` imports this module)."""

    @property
    def is_package(self) -> bool: ...

    @property
    def facts(self) -> _Facts: ...


class ClassIndex:
    """Classes and re-exports of every parsed module."""

    def __init__(self, modules: Mapping[str, IndexedModule]) -> None:
        self.modules = modules
        self.classes = {f"{name}.{c.name}": (name, c) for name, info in modules.items() for c in info.facts.class_shapes}
        self._reexports = {f"{name}.{item[0]}": (name, item) for name, info in modules.items() for item in info.facts.reexports}
        self._signatures: dict[str, Signature] = {}
        self.has_markers = any(shape.derived for _, shape in self.classes.values())

    def canonical(self, dotted: str) -> str:
        """``dotted`` with re-exports (``pkg/__init__`` importing the class) followed to where the name is defined.

        Each re-export is followed once: ``from pkg.helper import helper`` in ``pkg`` rewrites ``pkg.helper`` to
        ``pkg.helper.helper``, which starts with ``pkg.helper`` again."""
        followed: set[str] = set()
        while dotted not in self.classes:
            parts = dotted.split(".")
            prefixes = (".".join(parts[:cut]) for cut in range(len(parts), 0, -1))
            key = next((p for p in prefixes if p in self._reexports and p not in followed), None)
            if key is None:
                break
            followed.add(key)
            target = self._reexport_target(key)
            if target is None:
                break
            dotted = ".".join([target, *parts[key.count(".") + 1:]])
        return dotted

    def _reexport_target(self, key: str) -> str | None:
        module, (_, level, source, imported) = self._reexports[key]
        path = "." * level + source + ("." if source and imported else "") + imported
        return absolute_callee(module, self.modules[module].is_package, path)

    def absolute(self, module: str, name: str) -> str | None:
        """The canonical class or callable a name recorded in ``module`` refers to."""
        target = absolute_callee(module, self.modules[module].is_package, name)
        return None if target is None else self.canonical(target)

    def signature(self, dotted: str) -> Signature:
        """The constructor of the class ``canonical`` returned; unknown when it is not a parsed class."""
        if dotted not in self.classes:
            return Signature(None)
        cached = self._signatures.get(dotted)
        if cached is None:
            self._signatures[dotted] = Signature(None)  # a class that is its own base must not loop
            cached = self._signatures[dotted] = self._resolve(dotted)
        return cached

    def _bases(self, module: str, shape: ClassShape) -> list[str]:
        ignored = _PLAIN_BASES | _NAMED_TUPLE_NAMES
        return [self.absolute(module, base) or "?" for base in shape.bases if base not in ignored]

    def _resolve(self, dotted: str) -> Signature:
        module, shape = self.classes[dotted]
        bases = self._bases(module, shape)
        inherited = {name for base in bases for name in self.signature(base).derived}
        derived = frozenset((inherited - {p.name for p in shape.fields}) | set(shape.derived))
        if shape.opaque:
            return Signature(None, derived=derived)
        if shape.init is not None:
            return Signature(shape.init, shape.open_keywords, derived)
        if _NAMED_TUPLE_NAMES.intersection(shape.bases):
            return Signature(shape.fields, derived=derived)
        if shape.is_dataclass:
            fields = self._fields(dotted, set())
            return Signature(None if fields is None else tuple(fields.values()), derived=derived)
        if not bases:
            return Signature((), derived=derived)
        parent = self.signature(bases[0]) if len(bases) == 1 else Signature(None)
        return Signature(parent.parameters, parent.open_keywords, derived)

    def _fields(self, dotted: str, seen: set[str]) -> dict[str, Parameter] | None:
        """A dataclass's fields in ``__init__`` order: each base's, last base first, then its own (an override keeps its place)."""
        if dotted not in self.classes or dotted in seen:
            return None
        module, shape = self.classes[dotted]
        merged: dict[str, Parameter] = {}
        for base in reversed(self._bases(module, shape)):
            found = self._fields(base, {*seen, dotted})
            if found is None:
                return None
            merged.update(found)
        if shape.is_dataclass:
            merged.update({p.name: p for p in shape.fields})
        return merged


_Function = ast.FunctionDef | ast.AsyncFunctionDef
# A new scope, whose own names are not the enclosing one's. (A comprehension is one too, but ``:=`` in it binds outside.)
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _parsed(source: str) -> ast.Module | None:
    try:
        return ast.parse(source.removeprefix("\ufeff"))
    except (SyntaxError, ValueError, RecursionError):
        return None


def _scope_nodes(statements: Iterable[ast.AST]) -> Iterator[ast.AST]:
    """Every node of one scope: a nested function or class is yielded (it binds a name) but not entered.

    An explicit stack, so a long ``elif`` ladder cannot overflow it. The ``if TYPE_CHECKING:`` body never runs: skipped."""
    pending = list(statements)
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, _SCOPES):
            continue
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            pending += node.orelse
        else:
            pending += ast.iter_child_nodes(node)


def _is_type_checking(test: ast.expr) -> bool:
    name = test.id if isinstance(test, ast.Name) else test.attr if isinstance(test, ast.Attribute) else ""
    return name == "TYPE_CHECKING"


def _bound_by(node: ast.AST) -> Iterator[str]:
    """The names one node binds in its scope."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        yield node.id
    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        yield node.name
    elif isinstance(node, ast.Import | ast.ImportFrom):
        yield from (item.asname or item.name.split(".")[0] for item in node.names)
    elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name is not None:
        yield node.name
    elif isinstance(node, ast.MatchMapping) and node.rest is not None:
        yield node.rest


def _is_dynamic(node: ast.AST) -> bool:
    """Code that can give a module attributes no statement names."""
    if isinstance(node, ast.Name):
        return node.id in {"globals", "__getattr__"}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        # `vars(obj)` and `exec(code, names)` work on another namespace; bare, they reach the module's
        return (node.func.id == "vars" and not node.args) or (node.func.id == "exec" and len(node.args) < 2 and not node.keywords)
    if isinstance(node, ast.ImportFrom):
        return any(item.name == "*" for item in node.names)
    if isinstance(node, ast.Subscript):  # ``sys.modules[__name__]``
        return isinstance(node.slice, ast.Name) and node.slice.id == "__name__"
    return False


def module_attributes(source: str) -> frozenset[str] | None:
    """Every name the module's own code can bind on the module, or None when that cannot be known from its source.

    Unknown: it does not parse, it has a ``*`` import or a module ``__getattr__``, or it reaches its own namespace through
    ``globals``, ``vars``, ``exec`` or ``sys.modules[__name__]``. An annotation without a value binds nothing."""
    tree = _parsed(source)
    if tree is None:
        return None
    nodes = list(ast.walk(tree))
    if any(_is_dynamic(node) for node in nodes) or any(isinstance(node, _Function) and node.name == "__getattr__" for node in tree.body):
        return None
    declared = {name for node in nodes if isinstance(node, ast.Global) for name in node.names}
    scope = list(_scope_nodes(tree.body))
    unvalued = {id(node.target) for node in scope if isinstance(node, ast.AnnAssign) and node.value is None}
    return frozenset(declared | {name for node in scope if id(node) not in unvalued for name in _bound_by(node)})


def _global_callables(tree: ast.Module) -> frozenset[str]:
    """Names the module imports or defines at its top level."""
    return frozenset(
        name
        for node in _scope_nodes(tree.body)
        if isinstance(node, ast.Import | ast.ImportFrom | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        for name in _bound_by(node)
        if name != "*"
    )


def _called_names(value: ast.expr) -> Iterator[str]:
    """Names called while ``value`` is evaluated: not in a lambda or generator, nor where a comprehension rebinds the name."""
    pending: list[tuple[ast.AST, frozenset[str]]] = [(value, frozenset())]
    while pending:
        node, hidden = pending.pop()
        if isinstance(node, ast.Lambda | ast.GeneratorExp):
            continue
        if isinstance(node, ast.ListComp | ast.SetComp | ast.DictComp):
            hidden = hidden | {name.id for clause in node.generators for name in ast.walk(clause.target) if isinstance(name, ast.Name)}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id not in hidden:
            yield node.func.id
        pending += [(child, hidden) for child in ast.iter_child_nodes(node)]


def _assignment(node: ast.AST) -> tuple[list[ast.expr], ast.expr] | None:
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign | ast.NamedExpr) and node.value is not None:
        return [node.target], node.value
    return None


def _position(node: ast.AST) -> tuple[int, int]:
    """Where a statement, expression or import alias starts; (0, 0) for a node with no position."""
    if isinstance(node, ast.stmt | ast.expr | ast.pattern):
        return node.lineno, node.col_offset
    return 0, 0


def _outer_names(function: ast.FunctionDef | ast.AsyncFunctionDef, nodes: Sequence[ast.AST]) -> set[str]:
    """Parameters, ``global`` and ``nonlocal`` names: assigning one of these does not leave it unbound before the call."""
    arguments = function.args
    names = {arg.arg for arg in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)}
    names |= {arg.arg for arg in (arguments.vararg, arguments.kwarg) if arg is not None}
    return names | {name for node in nodes if isinstance(node, ast.Global | ast.Nonlocal) for name in node.names}


def _first_bindings(nodes: Sequence[ast.AST]) -> dict[str, tuple[int, int]]:
    first: dict[str, tuple[int, int]] = {}
    for node in nodes:
        for name in _bound_by(node):
            first[name] = min(first.get(name, _position(node)), _position(node))
    return first


def _shadows_in(function: ast.FunctionDef | ast.AsyncFunctionDef, callables: frozenset[str]) -> Iterator[tuple[int, str]]:
    """``(line, name)`` of each assignment that calls the local it is about to bind for the first time."""
    nodes = list(_scope_nodes(function.body))
    candidates = callables - _outer_names(function, nodes)
    first = _first_bindings(nodes)
    for node in nodes:
        found = _assignment(node)
        if found is None:
            continue
        targets = {
            item.id for target in found[0] for item in ast.walk(target)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        }
        for name in sorted(targets & candidates):
            # nothing binds it before this statement, so the call reads a local that has no value yet
            if first[name] >= _position(node) and name in set(_called_names(found[1])):
                yield _position(node)[0], name


def shadowed_callees(source: str) -> tuple[tuple[int, str, str], ...]:
    """``(line, function, name)``: a function assigns a local from a call to the module-level name it shadows (#215).

    Python makes ``name`` local to the whole function, so the call raises ``UnboundLocalError``. Reported once per function
    and name; empty when the source does not parse. Module and class bodies are left out: there the call finds the global."""
    tree = _parsed(source)
    if tree is None:
        return ()
    callables = _global_callables(tree)
    found: dict[tuple[str, str], int] = {}
    pending: list[tuple[ast.AST, str]] = [(tree, "")]
    while pending:
        node, path = pending.pop()
        if isinstance(node, _Function):
            for line, name in _shadows_in(node, callables):
                found[path, name] = min(line, found.get((path, name), line))
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                pending.append((child, f"{path}.{child.name}" if path else child.name))
            else:
                pending.append((child, path))
    return tuple(sorted((line, function, name) for (function, name), line in found.items()))
