"""Effect facts: the table of effectful APIs by category, and every direct use of one in a module.

The same pass also lists the writes to module-level mutable state that no lock of the module encloses (``StateWrite``, #232).

A use is a name or attribute chain that resolves (through the import alias table) to an effectful target, a method
called on a path-valued name, a ``global`` mutation, or a static ``patch(...)`` target. A reference held in a parameter
default is recorded as an injection seam, a type annotation is not a use, and ``random.Random(<seed>)`` is deterministic.
A use that runs while the module is imported is flagged ``at_import`` (#230)."""

from __future__ import annotations

import ast
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import NamedTuple, cast

from .call_facts import dotted_name
from .effect_categories import (
    INJECTABLE_EFFECTS,
    MODULE_STATE_CATEGORIES,
    OS_BRANCH_ATTRS,
    effect_category_of,
)
from .purity_facts import (
    is_container,
    local_names,
    module_containers,
    module_level_defs,
    mutated_container,
    write_receiver,
)

__all__ = [
    "EffectSite", "EffectUse", "ModuleEffects", "SharedState", "SharedWrite", "StateWrite", "merge_sites", "module_effects",
    "rebound_names", "running_ids", "runs", "state_category",
]


_PATH_IO_METHODS = frozenset({
    "read_text", "write_text", "read_bytes", "write_bytes", "open", "mkdir", "touch", "unlink", "rmdir", "rename", "replace",
    "iterdir", "glob", "rglob", "stat",
})


# Path methods that touch file contents or the directory tree, and ones that only ask the filesystem a question.
_PATH_QUERY_METHODS = frozenset({"exists", "is_file", "is_dir", "is_symlink", "is_mount", "resolve", "absolute", "expanduser", "samefile"})
_PATH_CONSTRUCTORS = frozenset({"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.WindowsPath"})
# Methods that return another path when called on a path, so the result is still path-valued.
_PATH_RETURNING = frozenset({"resolve", "absolute", "expanduser", "with_name", "with_suffix", "with_stem", "joinpath", "relative_to", "readlink"})
_PATH_ATTRIBUTES = frozenset({"parent", "parents"})


@dataclass(frozen=True, order=True)
class EffectUse:
    """A direct use of an effectful API; ``target`` is its canonical dotted name or static patch target."""

    line: int
    category: str
    target: str
    seam: int = 0  # 0: a use; 1: a parameter default whose annotation is a Callable/Mapping; 2: a parameter default otherwise
    at_import: bool = False  # the effect runs while the module is imported (#230); set by ``merge_sites``

    @property
    def is_module_state(self) -> bool:
        """A `registry`/`cache` use (#194): shared state, not I/O, so the readers that describe a module's I/O skip it."""
        return self.category in MODULE_STATE_CATEGORIES


EffectSite = tuple[ast.AST | None, EffectUse]  # the node that is the use; None when no one expression is (a ``global`` mutation)


@dataclass(frozen=True, order=True)
class StateWrite:
    """A write inside a function to a module-level container or ``global`` name that no lock of its own module encloses (#232).

    Whether another module's lock or a guard decorator covers it is the rule's question, so both travel with the write."""

    line: int
    name: str  # the module-level name written
    function: str  # the innermost function around the write
    held: tuple[str, ...] = ()  # dotted names of the `with` contexts around it inside that function (a call names its callee)
    decorators: tuple[str, ...] = ()  # dotted names of the decorators of the functions around it; relative imports keep their dots


@dataclass(frozen=True, order=True)
class SharedWrite:
    """A write inside a function to something an import binds (#194): whether that is a container is known only once the
    other module has been read (``facts.link_shared_state``). ``target`` is dotted through the aliases; relative imports keep their dots."""

    line: int
    function: str  # the innermost function around the write
    target: str


@dataclass(frozen=True)
class SharedState:
    containers: tuple[str, ...] = ()  # "REG" or "Class.attr": the containers another module can write (#194)
    writes: tuple[SharedWrite, ...] = ()


class ModuleEffects(NamedTuple):
    sites: list[EffectSite]  # every effect use with its node; ``merge_sites`` turns them into the distinct uses
    state_writes: tuple[StateWrite, ...]
    locks: tuple[str, ...]  # module-level names bound to a thread or process lock
    shared: SharedState
    # What a function does when it is called that no call expression performs (#194): (node inside it, or the function itself
    # for a cache loader; category; target), and the same for writes to imported names (node; dotted target).
    state_runs: tuple[tuple[ast.AST, str, str], ...]
    foreign_runs: tuple[tuple[ast.AST, str], ...]

_SEAM_ANNOTATIONS = frozenset({"Callable", "Mapping", "MutableMapping"})


def _is_seam_annotation(node: ast.expr | None) -> bool:
    """Whether a parameter annotation says "a callable or a mapping", looking through Optional/Union/`|` and string forms."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _is_seam_string(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _is_seam_annotation(node.left) or _is_seam_annotation(node.right)
    if isinstance(node, ast.Subscript):
        return _is_seam_subscript(node)
    if isinstance(node, ast.Attribute):
        return node.attr in _SEAM_ANNOTATIONS
    return isinstance(node, ast.Name) and node.id in _SEAM_ANNOTATIONS


def _is_seam_string(text: str) -> bool:
    try:
        return _is_seam_annotation(ast.parse(text, mode="eval").body)
    except SyntaxError:
        return False


def _is_seam_subscript(node: ast.Subscript) -> bool:
    base = node.value
    name = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""
    if name in {"Optional", "Union", "Annotated"}:
        inner = node.slice
        return any(_is_seam_annotation(item) for item in (inner.elts if isinstance(inner, ast.Tuple) else [inner]))
    return _is_seam_annotation(base)


def _injection_defaults(nodes: Sequence[ast.AST]) -> dict[int, int]:
    """Node id -> seam kind for every node inside a bare Name/Attribute parameter default: a reference there is the injection seam."""
    seams: dict[int, int] = {}
    for node in nodes:
        if isinstance(node, ast.Lambda):
            pairs = [(None, d) for d in [*node.args.defaults, *node.args.kw_defaults]]
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            positional = [*node.args.posonlyargs, *node.args.args]
            pairs = [*zip(positional[len(positional) - len(node.args.defaults):], node.args.defaults, strict=True),
                     *zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)]
            pairs = [(arg.annotation, default) for arg, default in pairs]
        else:
            continue
        for annotation, default in pairs:
            held = _seam_expression(default)
            if held is not None:
                kind = 1 if _is_seam_annotation(annotation) else 2
                seams.update((id(inner), kind) for inner in ast.walk(held))  # `os.environ.get` also holds `os.environ`
    return seams


def _seam_expression(default: ast.expr | None) -> ast.expr | None:
    """The part of a parameter default that is the seam: a bare Name/Attribute, or the callee of ``lambda: clock()``.

    A lambda default whose body is a call defers that call until the parameter is used, exactly as a bare reference
    does; the call's own arguments are not part of the seam, so an effect passed to it still counts."""
    if isinstance(default, ast.Name | ast.Attribute):
        return default
    if isinstance(default, ast.Lambda) and isinstance(default.body, ast.Call):
        return default.body.func
    return None


def rebound_names(nodes: Sequence[ast.AST]) -> set[str]:
    """Names the module assigns, defines or takes as parameters (imports are resolved by `import_aliases`): a bare builtin name among them is not the builtin."""
    bound: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            bound.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def _annotation_is_path(node: ast.expr | None, aliases: dict[str, str]) -> bool:
    """Whether an annotation names a ``pathlib`` path, looking through ``Optional``/``Union``/``|`` and string forms."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _annotation_is_path(ast.parse(node.value, mode="eval").body, aliases)
        except SyntaxError:
            return False
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_is_path(node.left, aliases) or _annotation_is_path(node.right, aliases)
    if isinstance(node, ast.Subscript):
        base = dotted_name(node.value, aliases) or ""
        if base.rsplit(".", 1)[-1] in {"Optional", "Union", "Annotated"}:
            inner = node.slice
            return any(_annotation_is_path(item, aliases) for item in (inner.elts if isinstance(inner, ast.Tuple) else [inner]))
        return False
    return isinstance(node, ast.Name | ast.Attribute) and dotted_name(node, aliases) in _PATH_CONSTRUCTORS


def _is_pathish(node: ast.expr, bound: set[str], aliases: dict[str, str]) -> bool:
    """Whether an expression is a path: a ``Path(...)`` call, a name bound from one, ``a / b`` with a path side, ``p.parent`` or ``p.resolve()``."""
    if isinstance(node, ast.Name):
        return node.id in bound
    if isinstance(node, ast.Call):
        callee = dotted_name(node.func, aliases)
        if callee in _PATH_CONSTRUCTORS or callee in {"pathlib.Path.cwd", "pathlib.Path.home"}:
            return True
        return isinstance(node.func, ast.Attribute) and node.func.attr in _PATH_RETURNING and _is_pathish(node.func.value, bound, aliases)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_pathish(node.left, bound, aliases) or _is_pathish(node.right, bound, aliases)
    return isinstance(node, ast.Attribute) and node.attr in _PATH_ATTRIBUTES and _is_pathish(node.value, bound, aliases)


def _path_binding(node: ast.AST, aliases: dict[str, str]) -> tuple[list[str], ast.expr | None]:
    """(names, value) for a node that may bind a path: a ``Path``-annotated name comes with no value to inspect (``None``)."""
    if isinstance(node, ast.arg):
        return ([node.arg], None) if _annotation_is_path(node.annotation, aliases) else ([], None)
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        if _annotation_is_path(node.annotation, aliases):
            return [node.target.id], None
        return ([node.target.id], node.value) if node.value is not None else ([], None)
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)], node.value
    return [], None


def _scope_local_names(scope: ast.AST) -> set[str]:
    """Names one scope binds itself (parameters and plain stores), which hide a module-level name of the same spelling."""
    return {
        node.arg if isinstance(node, ast.arg) else node.id for node in _own_scope_nodes(scope)
        if isinstance(node, ast.arg) or (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
    }


def _scope_path_names(scope: ast.AST, aliases: dict[str, str], inherited: Collection[str] = ()) -> set[str]:
    """Names one scope binds to a path: a parameter or variable annotated ``Path``, or assigned a path expression.

    ``inherited``: path-valued module-level names the scope can read (``REGISTRY = Path(...)`` then ``REGISTRY.read_text()``
    inside a function), already minus the ones this scope rebinds."""
    bound: set[str] = set(inherited)
    assigns: list[tuple[list[str], ast.expr]] = []
    for node in _own_scope_nodes(scope):
        names, value = _path_binding(node, aliases)
        if value is None:
            bound.update(names)
        else:
            assigns.append((names, value))
    for _ in range(3):  # `a = Path(x); b = a / "y"; c = b.parent` settles in a few passes
        before = len(bound)
        for names, value in assigns:
            if names and _is_pathish(value, bound, aliases):
                bound.update(names)
        if len(bound) == before:
            break
    return bound


def _path_receiver_uses(tree: ast.AST, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> list[EffectSite]:
    """``p.read_text()``, ``p.exists()`` on a name this scope bound to a path (a ``Path(...)`` value or a ``Path`` annotation).

    Scope by scope, so a ``str`` named ``path`` in another function is not mistaken for one. A module that never
    imports ``pathlib`` has no path to find and is skipped without a second walk."""
    if not any(target == "pathlib" or target.startswith("pathlib.") for target in aliases.values()):
        return []
    uses: list[EffectSite] = []
    scopes = [tree, *(n for n in nodes if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef))]
    module_paths = _scope_path_names(tree, aliases)
    for scope in scopes:
        inherited = module_paths - _scope_local_names(scope) if scope is not tree else ()
        bound = _scope_path_names(scope, aliases, inherited) if scope is not tree else module_paths
        if not bound:
            continue
        for node in _own_scope_nodes(scope):
            if isinstance(node, ast.Attribute) and (node.attr in _PATH_IO_METHODS or node.attr in _PATH_QUERY_METHODS) and _is_pathish(node.value, bound, aliases):
                use = _path_method_use(node, f"pathlib.Path.{node.attr}")
                if use is not None:
                    uses.append((node, use))
    return uses


def _path_method_use(node: ast.Attribute, target: str) -> EffectUse | None:
    if node.attr in _PATH_IO_METHODS:
        return EffectUse(node.lineno, "filesystem", target)
    if node.attr in _PATH_QUERY_METHODS:
        return EffectUse(node.lineno, "fs-query", target)
    return None


def _path_io_use(node: ast.AST, aliases: dict[str, str]) -> EffectUse | None:
    """``Path(...).read_text()`` and friends: the receiver is a call, which `dotted_name` cannot name."""
    if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call)):
        return None
    constructor = dotted_name(node.value.func, aliases)
    if constructor not in _PATH_CONSTRUCTORS:
        return None
    return _path_method_use(node, f"{constructor}.{node.attr}")


def _os_branch_imports(node: ast.ImportFrom) -> list[tuple[EffectUse, str]]:
    """``from sys import platform``: each OS-branching name an import binds, with the local name it is bound to."""
    if node.level != 0 or node.module is None:
        return []
    return [
        (EffectUse(node.lineno, "os-branching", f"{node.module}.{item.name}"), item.asname or item.name)
        for item in node.names
        if f"{node.module}.{item.name}" in OS_BRANCH_ATTRS
    ]


def _reference_use(node: ast.Attribute | ast.Name, aliases: dict[str, str], seams: dict[int, int]) -> EffectUse | None:
    """A name or attribute chain that resolves to an effectful target, with whether it sits in an injection default."""
    target = dotted_name(node, aliases)
    category = effect_category_of(target) if target is not None else None
    if target is None or category is None:
        return None
    seam = seams.get(id(node), 0) if category in INJECTABLE_EFFECTS else 0
    return EffectUse(node.lineno, category, target, seam)


_SCOPE_BOUNDARIES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)


def _own_scope_nodes(func: ast.AST) -> Iterable[ast.AST]:
    """Every node directly in ``func``'s own scope: its body, but not a nested function/lambda/class/comprehension's
    (each of those is its own scope, so a ``global`` declared in ``func`` cannot reach an assignment inside one)."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPE_BOUNDARIES):
            stack.extend(ast.iter_child_nodes(node))


def _global_writes(
    functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef],
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, ast.Name]]:
    """(function, name, node) for each assignment or ``del`` of a name its own function declares ``global``."""
    found: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, ast.Name]] = []
    for func in functions:
        declared: set[str] = set()
        bound: list[ast.Name] = []
        for node in _own_scope_nodes(func):
            if isinstance(node, ast.Global):
                declared.update(node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
                bound.append(node)
        found.extend((func, node.id, node) for node in bound if node.id in declared)
    return found


def _global_runs(writes: Sequence[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, ast.Name]]) -> list[tuple[ast.AST, str, str]]:
    return [(node, "global", f"{func.name}.{name}") for func, name, node in writes if isinstance(node.ctx, ast.Store)]


def _global_effect_uses(writes: Sequence[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str, ast.Name]]) -> list[EffectUse]:
    """A function that both declares ``global x`` and assigns ``x`` (plain, augmented or in a ``for``/``with``
    target) mutates module state from inside a call; one that only reads a global is not a use."""
    first: dict[tuple[ast.FunctionDef | ast.AsyncFunctionDef, str], int] = {}  # by node: two methods may share a name
    for func, name, node in writes:
        if isinstance(node.ctx, ast.Store):
            first.setdefault((func, name), node.lineno)
    return [EffectUse(line, "global", f"{func.name}.{name}") for (func, name), line in first.items()]


_CACHE_DECORATORS = frozenset({"functools.cache", "functools.lru_cache"})  # not `cached_property`: that cache is per instance
_CACHE_NAME = re.compile(r"cache|memo", re.IGNORECASE)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)  # a tuple: a `|` union is rebuilt at every check, and these run once per node
_WRITE_NODES = frozenset({ast.Call, ast.Subscript})  # the only node types `mutated_container` can answer for
# Not a semaphore (it can admit more than one writer) and not `asyncio.Lock` (it excludes tasks, not threads).
_LOCK_FACTORIES = frozenset({
    "threading.Lock", "threading.RLock", "threading.Condition", "multiprocessing.Lock", "multiprocessing.RLock",
})


_Function = ast.FunctionDef | ast.AsyncFunctionDef
_Span = tuple[int, int, int, _Function]  # (first line, last line, column, function)


def _spans(functions: Sequence[_Function]) -> list[_Span]:
    return sorted(
        ((min([f.lineno, *(d.lineno for d in f.decorator_list)]), f.end_lineno or f.lineno, f.col_offset, f) for f in functions),
        key=lambda span: span[:3],
    )


def _around(spans: Sequence[_Span], node: ast.AST) -> list[_Function]:
    """The functions around ``node``, outermost first, found by source position so the tree is not walked again."""
    line, column = getattr(node, "lineno", 0), getattr(node, "col_offset", 0)
    return [f for start, end, indent, f in spans if start <= line <= end and (line > start or column >= indent)]


def state_category(name: str) -> str:
    """A written container is a ``cache`` when its name says so, else a ``registry``."""
    return "cache" if _CACHE_NAME.search(name) else "registry"


def _cache_loaders(functions: Sequence[_Function], aliases: dict[str, str]) -> list[tuple[_Function, int, str]]:
    return [
        (func, decorator.lineno, target)
        for func in functions
        for decorator in func.decorator_list
        if (target := dotted_name(decorator.func if isinstance(decorator, ast.Call) else decorator, aliases)) in _CACHE_DECORATORS
    ]


def _container_writes(
    writers: Sequence[ast.AST], spans: Sequence[_Span], containers: set[str],
) -> list[tuple[str, ast.AST, list[_Function]]]:
    """(name, node, functions around it) for the function-level writes to ``containers``. A name one of those functions binds
    itself is a local, not the module's container; local names are computed only when a write is found inside a function."""
    bound: dict[ast.AST, set[str]] = {}
    found: list[tuple[str, ast.AST, list[_Function]]] = []
    for node in writers:
        if (name := mutated_container(node, containers)) is None:
            continue
        scopes = _around(spans, node)
        for scope in scopes:
            if scope not in bound:
                bound[scope] = local_names(scope)
        if scopes and not any(name in bound[scope] for scope in scopes):
            found.append((name, node, scopes))
    return found


def _module_level_locks(body: Sequence[ast.stmt], aliases: dict[str, str]) -> set[str]:
    """Names bound to a lock at module level, ``if``/``try``/``with`` blocks included."""
    locks: set[str] = set()
    pending = list(body)  # a stack, so a long ``elif`` ladder cannot overflow it (order does not matter for a set)
    while pending:
        stmt = pending.pop()
        if isinstance(stmt, ast.Assign | ast.AnnAssign):
            if isinstance(stmt.value, ast.Call) and dotted_name(stmt.value.func, aliases) in _LOCK_FACTORIES:
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                locks.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(stmt, ast.If | ast.With):
            pending += [*stmt.body, *getattr(stmt, "orelse", [])]
        elif isinstance(stmt, ast.Try | ast.TryStar):
            pending += [*stmt.body, *stmt.orelse, *stmt.finalbody, *(s for h in stmt.handlers for s in h.body)]
    return locks


def _held(withs: Sequence[ast.With], node: ast.AST, function: _Function, aliases: dict[str, str]) -> tuple[str, ...]:
    """The contexts of the ``with`` blocks inside ``function`` whose body contains ``node``: a block around the ``def``
    itself holds nothing when the function is later called."""
    at = (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))
    names: set[str] = set()
    for block in withs:
        first = block.body[0]
        inside = (block.lineno, block.col_offset) > (function.lineno, function.col_offset)
        if not (inside and (first.lineno, first.col_offset) <= at < (block.end_lineno or 0, block.end_col_offset or 0)):
            continue
        for item in block.items:
            context = item.context_expr
            name = dotted_name(context.func if isinstance(context, ast.Call) else context, aliases)
            if name is not None:
                names.add(name)
    return tuple(sorted(names))


def _unguarded_writes(
    nodes: Sequence[ast.AST], writes: Sequence[tuple[str, ast.AST, list[_Function]]], locks: set[str], aliases: dict[str, str],
) -> tuple[StateWrite, ...]:
    """The writes no lock of this module encloses. ``aliases`` keep a relative import's dots, for the rule to resolve."""
    if not writes:
        return ()
    withs = [n for n in nodes if n.__class__ is ast.With]
    found: set[StateWrite] = set()
    for name, node, scopes in writes:
        held = _held(withs, node, scopes[-1], aliases)
        if locks.isdisjoint(held):
            decorators = {
                target for scope in scopes for d in scope.decorator_list
                if (target := dotted_name(d.func if isinstance(d, ast.Call) else d, aliases)) is not None
            }
            found.add(StateWrite(getattr(node, "lineno", 0), name, scopes[-1].name, held, tuple(sorted(decorators))))
    return tuple(sorted(found))


_IMPLICIT_CLASS_METHODS = frozenset({"__init_subclass__", "__new__", "__class_getitem__"})
_Resolved = tuple[bool, str]  # (imported, "REG" / "Class.attr" here, or the dotted target of an import)
_Bindings = dict[str, tuple[int, ast.expr | None]]


def _decorator_tails(node: _Function) -> set[str]:
    names = (d.func if isinstance(d, ast.Call) else d for d in node.decorator_list)
    return {n.attr if isinstance(n, ast.Attribute) else n.id for n in names if isinstance(n, ast.Name | ast.Attribute)}


def _first_parameter(function: _Function) -> str:
    positional = [*function.args.posonlyargs, *function.args.args]
    return positional[0].arg if positional and "staticmethod" not in _decorator_tails(function) else ""


def _is_class_var(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.Constant):
        return str(annotation.value).rpartition(".")[2].startswith("ClassVar") or str(annotation.value).startswith("ClassVar")
    base = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    name = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""
    return name == "ClassVar"


def _class_containers(node: ast.ClassDef) -> dict[str, bool]:
    """Class-body container attributes -> whether the framework behind the class may copy it per instance (an annotated field
    of a class with a base or a decorator, as in a pydantic or attrs model)."""
    found: dict[str, bool] = {}
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and is_container(stmt.value):
            found.update({t.id: False for t in stmt.targets if isinstance(t, ast.Name)})
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None and is_container(stmt.value):
            found[stmt.target.id] = bool(node.bases or node.decorator_list) and not _is_class_var(stmt.annotation)
    return found


def _rebound_on_instances(node: ast.ClassDef) -> set[str]:
    """Attributes a method assigns on its instance, which then hides the class's container."""
    found: set[str] = set()
    for method in (s for s in node.body if isinstance(s, _DEFS)):
        first = _first_parameter(method)
        found.update(
            n.attr for n in ast.walk(method)
            if first and isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) and n.value.id == first
        )
    return found


def _assigned_reference(node: ast.AST) -> tuple[str, ast.expr] | None:
    """``(name, source)`` of ``name = other`` or ``name = a.b``: the one form of binding that makes an alias."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target, value = node.targets[0], node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        target, value = node.target, node.value
    else:
        return None
    return (target.id, value) if isinstance(target, ast.Name) and isinstance(value, ast.Name | ast.Attribute) else None


def _bindings(nodes: Iterable[ast.AST]) -> _Bindings:
    """Name -> (how often ``nodes`` bind it, what it stands for when its one binding made it an alias)."""
    counts: dict[str, int] = {}
    sources: dict[str, ast.expr] = {}
    for node in nodes:
        if isinstance(node, ast.arg):
            counts[node.arg] = counts.get(node.arg, 0) + 1
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            counts[node.id] = counts.get(node.id, 0) + 1
        elif (found := _assigned_reference(node)) is not None:
            sources[found[0]] = found[1]
    return {name: (count, sources.get(name) if count == 1 else None) for name, count in counts.items()}


def _module_statements(body: Sequence[ast.stmt]) -> Iterable[ast.AST]:
    """The nodes of the module's own statements: function, class and comprehension bodies are not entered."""
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if not isinstance(node, _SCOPE_BOUNDARIES):
            yield node
            pending.extend(ast.iter_child_nodes(node))


def _type_argument(node: ast.expr) -> ast.expr | None:
    """``x`` in ``type(x)``."""
    return node.args[0] if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type" and len(node.args) == 1 else None


def _head(node: ast.expr) -> str:
    """The name a receiver chain starts from, looking into ``type(x)``."""
    while True:
        inner = node.value if isinstance(node, ast.Attribute) else _type_argument(node)
        if inner is None:
            return node.id if isinstance(node, ast.Name) else ""
        node = inner


class _Receivers:
    """What the receiver of a write stands for (#194): a class's container, an alias of a container, or something imported."""

    def __init__(self, tree: ast.Module, containers: set[str], call_names: dict[str, str]) -> None:
        self.containers = containers
        self.call_names = call_names
        defs = module_level_defs(tree.body)
        self.classes = {d.name: d for d in defs if isinstance(d, ast.ClassDef)}
        self.class_containers = {name: _class_containers(node) for name, node in self.classes.items()}
        self.methods: dict[ast.AST, str] = {s: name for name, node in self.classes.items() for s in node.body if isinstance(s, _DEFS)}
        self.module = _bindings(_module_statements(tree.body))
        self.own = {*self.module, *(d.name for d in defs)}
        self.locals: dict[ast.AST, set[str]] = {}
        self.bound: dict[ast.AST, _Bindings] = {}
        self.rebound: dict[str, set[str]] = {}  # class -> attributes its methods assign on the instance
        self.stored: dict[ast.AST, set[str]] = {}  # method -> names it assigns anywhere inside

    def _is_local(self, scope: _Function, name: str) -> bool:
        if scope not in self.locals:
            self.locals[scope] = local_names(scope)
        return name in self.locals[scope]

    def resolve(self, receiver: ast.expr, scopes: Sequence[_Function], follow: bool = True) -> _Resolved | None:
        """``scopes``: the functions around the write, outermost first. ``follow``: whether an alias may still be followed."""
        if isinstance(receiver, ast.Name):
            return self._name(receiver.id, scopes, follow)
        if not isinstance(receiver, ast.Attribute):
            return None
        if (found := self._class_access(receiver.value, scopes)) is not None:
            return self._class_attribute(found[0], receiver.attr, found[1])
        head = _head(receiver)
        if head in self.own or head not in self.call_names or any(self._is_local(s, head) for s in scopes):
            return None
        target = dotted_name(receiver, self.call_names)
        return None if target is None else (True, target)

    def _name(self, name: str, scopes: Sequence[_Function], follow: bool) -> _Resolved | None:
        for depth in range(len(scopes), 0, -1):
            scope = scopes[depth - 1]
            if not self._is_local(scope, name):
                continue
            if scope not in self.bound:
                self.bound[scope] = _bindings(ast.walk(scope))
            source = self.bound[scope].get(name, (0, None))[1]
            return self.resolve(source, scopes[:depth], False) if follow and source is not None else None
        if name in self.containers:
            return False, name
        source = self.module.get(name, (0, None))[1]
        if follow and source is not None:
            return self.resolve(source, (), False)
        return (True, self.call_names[name]) if name in self.call_names and name not in self.own else None

    def _class_access(self, base: ast.expr, scopes: Sequence[_Function]) -> tuple[str, bool] | None:
        """``(class, through an instance)`` for ``Class``, ``cls``, ``self``, ``type(self)`` and ``self.__class__`` of a module-level class."""
        if isinstance(base, ast.Name) and base.id in self.classes and not any(self._is_local(s, base.id) for s in scopes):
            return base.id, False
        of_class = _type_argument(base)
        if of_class is None and isinstance(base, ast.Attribute) and base.attr == "__class__":
            of_class = base.value
        name = of_class if of_class is not None else base
        at = next((index for index, scope in enumerate(scopes) if scope in self.methods), None)
        if at is None or not isinstance(name, ast.Name) or name.id != _first_parameter(scopes[at]):
            return None
        method = scopes[at]
        if method not in self.stored:
            self.stored[method] = {n.id for n in ast.walk(method) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        if name.id in self.stored[method] or any(self._is_local(s, name.id) for s in scopes[at + 1:]):
            return None
        takes_class = method.name in _IMPLICIT_CLASS_METHODS or "classmethod" in _decorator_tails(method)
        return self.methods[method], of_class is None and not takes_class

    def _chain(self, name: str) -> list[str]:
        """The class and its bases defined in this module, nearest first."""
        found: list[str] = []
        pending = [name]
        while pending:
            current = pending.pop(0)
            if current not in found:
                found.append(current)
                pending += [b.id for b in self.classes[current].bases if isinstance(b, ast.Name) and b.id in self.classes]
        return found

    def _rebinds(self, name: str, attr: str) -> bool:
        if name not in self.rebound:
            self.rebound[name] = _rebound_on_instances(self.classes[name])
        return attr in self.rebound[name]

    def _class_attribute(self, name: str, attr: str, through_instance: bool) -> _Resolved | None:
        chain = self._chain(name)
        owner = next((c for c in chain if attr in self.class_containers[c]), None)
        if owner is None:
            return None
        if through_instance and (self.class_containers[owner][attr] or any(self._rebinds(c, attr) for c in chain)):
            return None
        return False, f"{owner}.{attr}"

    def exported(self) -> tuple[str, ...]:
        """The containers another module can name: module-level ones, aliases of this module's own, and class attributes."""
        aliases = [
            name for name, (_, source) in self.module.items()
            if source is not None and (found := self.resolve(source, (), False)) is not None and not found[0]
        ]
        return tuple(sorted({*self.containers, *aliases, *(f"{c}.{a}" for c, attrs in self.class_containers.items() for a in attrs)}))


def _indirect_writes(
    nodes: Sequence[ast.AST], writers: Sequence[ast.AST], spans: Sequence[_Span], receivers: _Receivers,
) -> list[tuple[_Resolved, ast.AST, _Function]]:
    """The writes ``_container_writes`` does not see: to class containers, through aliases and to imported names."""
    heads = {  # the names a receiver can start from and still resolve: anything else is skipped without looking further
        *receivers.call_names, *receivers.classes,
        *(found[0] for node in nodes if (found := _assigned_reference(node)) is not None),
        *(_first_parameter(cast("_Function", method)) for method in receivers.methods),
    }
    found_writes: list[tuple[_Resolved, ast.AST, _Function]] = []
    for node in writers:
        receiver = write_receiver(node)
        if receiver is None or _head(receiver) not in heads or (isinstance(receiver, ast.Name) and receiver.id in receivers.containers):
            continue
        scopes = _around(spans, node)
        if scopes and (resolved := receivers.resolve(receiver, scopes)) is not None:
            found_writes.append((resolved, node, scopes[-1]))
    return found_writes


class _State(NamedTuple):
    uses: tuple[EffectUse, ...]
    writes: tuple[StateWrite, ...]
    locks: tuple[str, ...]
    shared: SharedState
    runs: tuple[tuple[ast.AST, str, str], ...]
    foreign: tuple[tuple[ast.AST, str], ...]


def _module_state(tree: ast.AST, nodes: Sequence[ast.AST], aliases: dict[str, str], call_names: dict[str, str]) -> _State:
    """Module state written from functions.

    As effect uses (#194): a ``global`` rebinding is ``global``; a function that writes a module-level ``dict``/``list``/``set``
    (item assignment, ``del`` or a mutating method call), directly or through an alias, or a class's container, is a ``registry``
    use, or a ``cache`` use when the container's name says cache or memo; a ``functools.cache``/``lru_cache`` decorator is a
    ``cache`` use. Import-time fills and reads are not uses. A write to an imported name is only noted (``SharedWrite``).
    Recorded in every module, failing only where a ring lists the category in ``forbid`` (see ``MODULE_STATE_CATEGORIES``).

    As ``StateWrite``s (#232): the module's own container and ``global`` writes, minus those inside a ``with <module lock>``."""
    functions = [node for node in nodes if isinstance(node, _DEFS)]
    # most modules have no `global`: do not walk every function's scope for one
    rebinds = _global_writes(functions) if any(node.__class__ is ast.Global for node in nodes) else []
    loaders = _cache_loaders(functions, aliases)
    uses = _global_effect_uses(rebinds) + [EffectUse(line, "cache", target) for _, line, target in loaders]
    runs = _global_runs(rebinds) + [(func, "cache", target) for func, _, target in loaders]
    if not isinstance(tree, ast.Module):
        return _State(tuple(uses), (), (), SharedState(), tuple(runs), ())
    locks = _module_level_locks(tree.body, aliases)
    containers = module_containers(tree, aliases, dotted_name)
    receivers = _Receivers(tree, containers, call_names)
    writers = [n for n in nodes if n.__class__ in _WRITE_NODES]  # nearly every module has `__all__`, so this runs over most: filter first
    spans = _spans(functions) if writers or rebinds else []
    contained = _container_writes(writers, spans, containers) if containers else []
    indirect = _indirect_writes(nodes, writers, spans, receivers) if spans else []
    local = [(node, state_category(name), f"{scopes[-1].name}.{name}") for name, node, scopes in contained]
    local += [(node, state_category(name.rpartition(".")[2]), f"{func.name}.{name}") for (imported, name), node, func in indirect if not imported]
    uses += [EffectUse(getattr(node, "lineno", 0), category, target) for node, category, target in local]
    foreign = [(node, func.name, name) for (imported, name), node, func in indirect if imported]
    shared = SharedState(
        receivers.exported(), tuple(sorted({SharedWrite(getattr(node, "lineno", 0), func, name) for node, func, name in foreign}))
    )
    writes = contained + [(name, node, _around(spans, node)) for _, name, node in rebinds]
    return _State(
        tuple(uses), _unguarded_writes(nodes, writes, locks, call_names), tuple(sorted(locks)), shared, (*runs, *local),
        tuple((node, name) for node, _, name in foreign),
    )


def _annotation_node_ids(nodes: Sequence[ast.AST]) -> set[int]:
    """Ids of every node inside a type annotation (parameter, return, variable): a hint names a type, it does not use it."""
    ids: set[int] = set()
    for node in nodes:
        annotation: ast.expr | None = None
        if isinstance(node, ast.arg | ast.AnnAssign):
            annotation = node.annotation
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            annotation = node.returns
        if annotation is not None:
            ids.update(id(inner) for inner in ast.walk(annotation))
    return ids


def _seeded_random_ids(nodes: Sequence[ast.AST], aliases: dict[str, str]) -> set[int]:
    """Ids of the callee in ``random.Random(<seed>)``: a generator built from a seed is deterministic, so it is no randomness use."""
    seeded: set[int] = set()
    for node in nodes:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)):
            continue
        if dotted_name(node.func, aliases) != "random.Random":
            continue
        args = [*node.args, *(keyword.value for keyword in node.keywords)]
        if any(not (isinstance(arg, ast.Constant) and arg.value is None) for arg in args):
            seeded.add(id(node.func))
    return seeded


def module_effects(
    tree: ast.AST, nodes: Sequence[ast.AST], aliases: dict[str, str], rebound: set[str], call_names: dict[str, str],
) -> ModuleEffects:
    """Every effect use with its node, so a pass can ask where it sits (import time, a function body), and the module-state writes.

    ``nodes`` is the walk of ``tree``; ``call_names`` is ``aliases`` with relative imports kept."""
    seams = _injection_defaults(nodes)
    hints = _annotation_node_ids(nodes)
    seeded = _seeded_random_ids(nodes, aliases)
    state = _module_state(tree, nodes, aliases, call_names)
    sites: list[EffectSite] = [(None, use) for use in state.uses]
    sites += _path_receiver_uses(tree, nodes, aliases)
    imported_os_names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            for use, bound_name in _os_branch_imports(node):
                sites.append((node, use))
                imported_os_names.add(bound_name)
        found = [_path_io_use(node, aliases), _patch_use(node, aliases)]
        if isinstance(node, ast.Attribute | ast.Name) and id(node) not in hints and not _is_shadowed(node, imported_os_names, rebound):
            found.append(None if id(node) in seeded else _reference_use(node, aliases, seams))
        sites += [(node, use) for use in found if use is not None]
    return ModuleEffects(sites, state.writes, state.locks, state.shared, state.runs, state.foreign)


# Data rather than callables: reading the name is the effect. Every other reference runs nothing until it is called.
_DATA_READS = frozenset({"os.environ", "sys.argv", "sys.stdin"})


def running_ids(nodes: Sequence[ast.AST]) -> set[int]:
    """Ids of the nodes that execute something when evaluated: a callee, a decorator, and a call itself (``patch(...)``)."""
    ids: set[int] = set()
    for node in nodes:
        if isinstance(node, ast.Call):
            ids.update((id(node), id(node.func)))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            ids.update(id(decorator) for decorator in node.decorator_list)
    return ids


def runs(site: EffectSite, running: set[int]) -> bool:
    """Whether evaluating the site performs the effect, as opposed to naming it (``NOW = time.time``, an injection default)."""
    node, use = site
    return node is not None and use.seam == 0 and (id(node) in running or use.target in _DATA_READS)


def merge_sites(sites: Sequence[EffectSite], import_ids: set[int], running: set[int]) -> tuple[EffectUse, ...]:
    """The distinct uses; one that runs at import anywhere on its line is flagged, without becoming a second use."""
    merged: dict[EffectUse, bool] = {}
    for site in sites:
        node, use = site
        merged[use] = merged.get(use, False) or (id(node) in import_ids and runs(site, running))
    return tuple(sorted(replace(use, at_import=at_import) for use, at_import in merged.items()))


_SHADOWABLE_BUILTINS = frozenset({"open", "print", "input"})


def _is_shadowed(node: ast.Attribute | ast.Name, imported_os_names: set[str], rebound: set[str]) -> bool:
    """A bare name that is an imported OS-branching name (already reported at its import) or a rebound builtin
    (``open``, ``print`` or ``input``) the module defines its own meaning for."""
    return isinstance(node, ast.Name) and (
        node.id in imported_os_names or (node.id in _SHADOWABLE_BUILTINS and node.id in rebound)
    )


def _patch_use(node: ast.AST, aliases: dict[str, str]) -> EffectUse | None:
    """``patch("pkg.mod.name")`` / ``monkeypatch.setattr("pkg.mod.name", ...)``: the static patch target."""
    if not isinstance(node, ast.Call):
        return None
    function = dotted_name(node.func, aliases)
    if function is None or not node.args:
        return None
    is_mock_patch = function in {"unittest.mock.patch", "mock.patch"} or function.endswith(".mock.patch")
    is_monkeypatch = function.endswith(".setattr") and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "monkeypatch"
    if not (is_mock_patch or is_monkeypatch):
        return None
    arg = node.args[0]
    target = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else dotted_name(arg, aliases)
    return EffectUse(node.lineno, "patch", target) if target is not None else None
