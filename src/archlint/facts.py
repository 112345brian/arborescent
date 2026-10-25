"""One AST pass per file: every import (lazy and type-only ones included), OS-branching uses, PEP 723 metadata.

The result is plain data (``SourceFacts``) so it can be cached by content hash and reused by every check.
"""

from __future__ import annotations

import ast
import re
import tomllib
from collections.abc import Collection, Iterable
from dataclasses import dataclass

__all__ = ["OS_BRANCH_ATTRS", "ModuleInfo", "RawImport", "SourceFacts", "extract", "resolve_targets"]

OS_BRANCH_ATTRS = frozenset({"os.name", "sys.platform", "platform.system"})
_PEP723 = re.compile(r"^# /// script\n((?:#.*\n)+?)# ///$", re.MULTILINE)


@dataclass(frozen=True)
class RawImport:
    """``import a.b`` -> (0, "a.b", ()); ``from ..x import y`` -> (2, "x", ("y",))."""

    level: int
    module: str
    names: tuple[str, ...]
    line: int
    lazy: bool  # inside a function body
    type_only: bool  # under ``if TYPE_CHECKING``


@dataclass(frozen=True)
class SourceFacts:
    imports: tuple[RawImport, ...]
    os_uses: tuple[tuple[int, str], ...]
    script_deps: tuple[str, ...] | None  # PEP 723 `dependencies`, None when the file has no block


@dataclass(frozen=True)
class ModuleInfo:
    name: str
    path: str  # posix, relative to the policy root
    is_package: bool
    facts: SourceFacts


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _walk(node: ast.AST, lazy: bool, type_only: bool, imports: list[RawImport], os_uses: list[tuple[int, str]]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Import):
            imports += [RawImport(0, a.name, (), child.lineno, lazy, type_only) for a in child.names]
        elif isinstance(child, ast.ImportFrom):
            names = tuple(a.name for a in child.names)
            imports.append(RawImport(child.level, child.module or "", names, child.lineno, lazy, type_only))
            if child.level == 0 and child.module:
                os_uses += [
                    (child.lineno, f"{child.module}.{n}") for n in names if f"{child.module}.{n}" in OS_BRANCH_ATTRS
                ]
        elif isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name):
            dotted = f"{child.value.id}.{child.attr}"
            if dotted in OS_BRANCH_ATTRS:
                os_uses.append((child.lineno, dotted))
        if isinstance(child, ast.If) and _is_type_checking(child.test):
            _walk(child.test, lazy, type_only, imports, os_uses)
            for stmt in child.body:
                _walk_stmt(stmt, lazy, True, imports, os_uses)
            for stmt in child.orelse:
                _walk_stmt(stmt, lazy, type_only, imports, os_uses)
            continue
        _walk(child, lazy or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)), type_only, imports, os_uses)


def _walk_stmt(stmt: ast.stmt, lazy: bool, type_only: bool, imports: list[RawImport], os_uses: list[tuple[int, str]]) -> None:
    holder = ast.Module(body=[stmt], type_ignores=[])
    _walk(holder, lazy, type_only, imports, os_uses)


def _script_deps(source: str) -> tuple[str, ...] | None:
    if "# /// script" not in source:
        return None
    block = _PEP723.search(source.replace("\r\n", "\n"))
    if not block:
        return None
    text = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in block.group(1).splitlines())
    try:
        deps = tomllib.loads(text).get("dependencies", [])
    except tomllib.TOMLDecodeError:
        return None
    return tuple(re.split(r"[<>=!~\[ ;]", d, maxsplit=1)[0] for d in deps)


def extract(source: str, filename: str = "<source>") -> SourceFacts:
    tree = ast.parse(source, filename=filename)
    imports: list[RawImport] = []
    os_uses: list[tuple[int, str]] = []
    _walk(tree, False, False, imports, os_uses)
    return SourceFacts(tuple(imports), tuple(sorted(set(os_uses))), _script_deps(source))


def resolve_targets(
    module: str, is_package: bool, imp: RawImport, known: Collection[str], tops: Collection[str]
) -> tuple[list[str], list[str], str | None]:
    """Split one import into (internal module names, external top-level names, error).

    Internal names are dotted modules from ``known``; ``from . import a, b`` yields the submodules ``a`` and ``b``
    when they are modules, and ``from .m import x`` yields ``m``.
    """
    parts = module.split(".")
    pkg = parts if is_package else parts[:-1]
    if imp.level:
        if imp.level - 1 > len(pkg):
            return [], [], f"relative import climbs above the source root ({'.' * imp.level}{imp.module})"
        base = pkg[: len(pkg) - (imp.level - 1)] + (imp.module.split(".") if imp.module else [])
    else:
        base = imp.module.split(".")
        if base[0] not in tops:
            return [], [base[0]] if base[0] else [], None
    if not imp.names:  # plain `import a.b.c`
        return [_longest_known(base, known)], [], None
    subs = [".".join([*base, n]) for n in imp.names if n != "*" and ".".join([*base, n]) in known]
    head = [_longest_known(base, known)] if base else []
    return (subs if not imp.module and subs else [*head, *subs]), [], None


def _longest_known(parts: Iterable[str], known: Collection[str]) -> str:
    items = list(parts)
    while items and ".".join(items) not in known:
        items.pop()
    return ".".join(items)
