"""Import facts: every import statement a module holds (lazy and type-only ones included), the alias table
that resolves a local name to its canonical dotted target, dynamic-import targets, and how one import resolves to
first-party module edges (``resolve_targets`` for the ring question, ``resolve_runtime_targets`` for what runs)."""

from __future__ import annotations

import ast
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "RawImport", "dotted_name", "dynamic_imports", "import_aliases", "raw_imports", "resolve_runtime_targets", "resolve_targets",
]


@dataclass(frozen=True)
class RawImport:
    """``import a.b`` -> (0, "a.b", ()); ``from ..x import y`` -> (2, "x", ("y",))."""

    level: int
    module: str
    names: tuple[str, ...]
    line: int
    lazy: bool  # inside a function body
    type_only: bool  # under ``if TYPE_CHECKING``


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _walk(node: ast.AST, lazy: bool, type_only: bool, imports: list[RawImport]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Import):
            imports += [RawImport(0, a.name, (), child.lineno, lazy, type_only) for a in child.names]
        elif isinstance(child, ast.ImportFrom):
            names = tuple(a.name for a in child.names)
            imports.append(RawImport(child.level, child.module or "", names, child.lineno, lazy, type_only))
        if isinstance(child, ast.If) and _is_type_checking(child.test):
            _walk(child.test, lazy, type_only, imports)
            for stmt in child.body:
                _walk_stmt(stmt, lazy, True, imports)
            for stmt in child.orelse:
                _walk_stmt(stmt, lazy, type_only, imports)
            continue
        _walk(child, lazy or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)), type_only, imports)


def _walk_stmt(stmt: ast.stmt, lazy: bool, type_only: bool, imports: list[RawImport]) -> None:
    holder = ast.Module(body=[stmt], type_ignores=[])
    _walk(holder, lazy, type_only, imports)


def raw_imports(tree: ast.AST) -> tuple[RawImport, ...]:
    """Every import in ``tree``, in source order; one inside a function is ``lazy``, one under ``if TYPE_CHECKING`` is ``type_only``."""
    imports: list[RawImport] = []
    _walk(tree, False, False, imports)
    return tuple(imports)


def import_aliases(nodes: Sequence[ast.AST]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname if item.asname is not None else item.name.split(".")[0]] = item.name if item.asname is not None else item.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"
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


def resolve_targets(
    module: str, is_package: bool, imp: RawImport, known: Collection[str], tops: Collection[str]
) -> tuple[list[str], list[str], str | None]:
    """Split one import into (internal module names, external top-level names, error).

    Internal names are dotted modules from ``known``; ``from . import a, b`` yields the submodules ``a`` and ``b``
    when they are modules, and ``from .m import x`` yields ``m``.

    ``from pkg import sub`` is an edge to ``pkg.sub`` alone, as ``from pkg.sub import x`` is; ``pkg`` is added only when some
    imported name is not a submodule (it is read off the package) or is ``*``. That is the question a ring check asks.
    """
    base, external, error = _import_base(module, is_package, imp, tops)
    return ([], external, error) if base is None else (_import_edges(base, imp, known, runtime_init=False), [], None)


def resolve_runtime_targets(
    module: str, is_package: bool, imp: RawImport, known: Collection[str], tops: Collection[str]
) -> tuple[list[str], list[str], str | None]:
    """Like ``resolve_targets``, but for what actually runs: importing ``pkg.sub`` also runs ``pkg/__init__``.

    Package splitting must carry that file with the modules that import its submodules, so ``from pkg import sub`` keeps the
    edge to ``pkg`` next to the one to ``pkg.sub``."""
    base, external, error = _import_base(module, is_package, imp, tops)
    return ([], external, error) if base is None else (_import_edges(base, imp, known, runtime_init=True), [], None)


def _import_base(module: str, is_package: bool, imp: RawImport, tops: Collection[str]) -> tuple[list[str] | None, list[str], str | None]:
    """The dotted path an import names, or (None, external names, error) when it is not first-party or cannot be resolved."""
    parts = module.split(".")
    pkg = parts if is_package else parts[:-1]
    if imp.level:
        if imp.level - 1 > len(pkg):
            return None, [], f"relative import climbs above the source root ({'.' * imp.level}{imp.module})"
        return pkg[: len(pkg) - (imp.level - 1)] + (imp.module.split(".") if imp.module else []), [], None
    base = imp.module.split(".")
    if base[0] not in tops:
        return None, [base[0]] if base[0] else [], None
    return base, [], None


def _import_edges(base: list[str], imp: RawImport, known: Collection[str], *, runtime_init: bool) -> list[str]:
    if not imp.names:  # plain `import a.b.c`
        return [_longest_known(base, known)]
    subs = [".".join([*base, n]) for n in imp.names if n != "*" and ".".join([*base, n]) in known]
    head = [_longest_known(base, known)] if base else []
    if runtime_init:
        return subs if not imp.module and subs else [*head, *subs]
    all_submodules = bool(subs) and len(subs) == len(imp.names)  # every name is a submodule: nothing is read off the package itself
    return ([] if all_submodules else head) + subs


def _longest_known(parts: Iterable[str], known: Collection[str]) -> str:
    items = list(parts)
    while items and ".".join(items) not in known:
        items.pop()
    return ".".join(items)
