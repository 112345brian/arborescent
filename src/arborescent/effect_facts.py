"""Effect facts: the table of effectful APIs by category, and every direct use of one in a module.

A use is a name or attribute chain that resolves (through the import alias table) to an effectful target, a method
called on a path-valued name, a ``global`` mutation, or a static ``patch(...)`` target. A reference held in a parameter
default is recorded as an injection seam, a type annotation is not a use, and ``random.Random(<seed>)`` is deterministic."""

from __future__ import annotations

import ast
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from .call_facts import dotted_name
from .effect_categories import (
    INJECTABLE_EFFECTS,
    MODULE_STATE_CATEGORIES,
    OS_BRANCH_ATTRS,
    effect_category_of,
)
from .purity_facts import local_names, module_containers, mutated_container

__all__ = ["EffectUse", "effect_uses"]


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

    @property
    def is_module_state(self) -> bool:
        """A `registry`/`cache` use (#194): shared state, not I/O, so the readers that describe a module's I/O skip it."""
        return self.category in MODULE_STATE_CATEGORIES


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


def _rebound_names(nodes: Sequence[ast.AST]) -> set[str]:
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


def _path_receiver_uses(tree: ast.AST, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> list[EffectUse]:
    """``p.read_text()``, ``p.exists()`` on a name this scope bound to a path (a ``Path(...)`` value or a ``Path`` annotation).

    Scope by scope, so a ``str`` named ``path`` in another function is not mistaken for one. A module that never
    imports ``pathlib`` has no path to find and is skipped without a second walk."""
    if not any(target == "pathlib" or target.startswith("pathlib.") for target in aliases.values()):
        return []
    uses: list[EffectUse] = []
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
                    uses.append(use)
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


def _global_effect_uses(nodes: Sequence[ast.AST]) -> list[EffectUse]:
    """A function that both declares ``global x`` and assigns ``x`` (plain, augmented or in a ``for``/``with``
    target) mutates module state from inside a call; one that only reads a global is not a use."""
    uses: list[EffectUse] = []
    for func in (n for n in nodes if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
        declared: set[str] = set()
        assigned: dict[str, int] = {}
        for node in _own_scope_nodes(func):
            if isinstance(node, ast.Global):
                declared.update(node.names)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                assigned.setdefault(node.id, node.lineno)
        uses.extend(EffectUse(assigned[name], "global", f"{func.name}.{name}") for name in sorted(declared & assigned.keys()))
    return uses


_CACHE_DECORATORS = frozenset({"functools.cache", "functools.lru_cache"})  # not `cached_property`: that cache is per instance
_CACHE_NAME = re.compile(r"cache|memo", re.IGNORECASE)


def _module_state_uses(tree: ast.AST, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> list[EffectUse]:
    """#194, module state that is not a ``global`` rebinding. A function that writes a module-level ``dict``/``list``/``set`` (item
    assignment, ``del`` or a mutating method call) is a ``registry`` use, or a ``cache`` use when the container's name says cache or
    memo; a ``functools.cache``/``lru_cache`` decorator is a ``cache`` use. A name the function (or one around it) binds itself is a
    local, not the module's container; import-time fills and reads are not uses. Recorded in every module, failing only where a ring
    lists the category in ``forbid`` (see ``MODULE_STATE_CATEGORIES``)."""
    uses = [
        EffectUse(decorator.lineno, "cache", target)
        for func in nodes if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef)
        for decorator in func.decorator_list
        if (target := dotted_name(decorator.func if isinstance(decorator, ast.Call) else decorator, aliases)) in _CACHE_DECORATORS
    ]
    containers = module_containers(tree, aliases, dotted_name) if isinstance(tree, ast.Module) else set()
    if not containers:
        return uses
    stack: list[tuple[ast.AST, tuple[tuple[str, set[str]], ...]]] = [(tree, ())]
    while stack:
        node, scopes = stack.pop()
        for child in ast.iter_child_nodes(node):
            inner = scopes
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                inner = (*scopes, (child.name, local_names(child)))
            elif scopes and (name := mutated_container(child, containers)) is not None and not any(name in bound for _, bound in scopes):
                category = "cache" if _CACHE_NAME.search(name) else "registry"
                uses.append(EffectUse(getattr(child, "lineno", 0), category, f"{scopes[-1][0]}.{name}"))
            stack.append((child, inner))
    return uses


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


def effect_uses(tree: ast.AST, nodes: Sequence[ast.AST], aliases: dict[str, str]) -> tuple[EffectUse, ...]:
    seams = _injection_defaults(nodes)
    rebound = _rebound_names(nodes)
    hints = _annotation_node_ids(nodes)
    seeded = _seeded_random_ids(nodes, aliases)
    uses: set[EffectUse] = set(_global_effect_uses(nodes))
    uses.update(_path_receiver_uses(tree, nodes, aliases))
    uses.update(_module_state_uses(tree, nodes, aliases))
    imported_os_names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            for use, bound_name in _os_branch_imports(node):
                uses.add(use)
                imported_os_names.add(bound_name)
        found = [_path_io_use(node, aliases), _patch_use(node, aliases)]
        if isinstance(node, ast.Attribute | ast.Name) and id(node) not in hints and not _is_shadowed(node, imported_os_names, rebound):
            found.append(None if id(node) in seeded else _reference_use(node, aliases, seams))
        uses.update(use for use in found if use is not None)
    return tuple(sorted(uses))


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
