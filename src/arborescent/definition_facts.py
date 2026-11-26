"""Definition facts: the shape of what a module defines at its top level.

Module-level classes and functions with whether each is abstract, ``typing.Protocol`` methods whose return type says
nothing, the public surface (a static ``__all__`` or public bindings) and which of it is re-exported, functions
annotated to return a ``*Result``/``*Outcome``, and the ``if __name__ == "__main__":`` guard.

``class_shapes`` records what each module-level class's constructor accepts, and ``ClassIndex`` resolves that across
modules (inheritance and re-exports), for the construction rule (#234)."""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .call_facts import absolute_callee, annotation_types, dotted_name
from .purity_facts import Helpers

__all__ = [
    "ClassIndex", "ClassShape", "IndexedModule", "Parameter", "Signature", "TopLevelDef", "class_shapes", "main_guard_line", "purity_helpers",
    "result_defs", "surface", "top_level_defs", "weak_port_returns",
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
