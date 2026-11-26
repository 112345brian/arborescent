"""AST facts for import statements and their alias bindings."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["RawImport", "import_aliases", "raw_imports"]


@dataclass(frozen=True)
class RawImport:
    """``import a.b`` -> (0, "a.b", ()); ``from ..x import y`` -> (2, "x", ("y",))."""

    level: int
    module: str
    names: tuple[str, ...]
    line: int
    lazy: bool  # inside a function body
    type_only: bool  # under ``if TYPE_CHECKING``
    end_line: int = 0  # last line of the statement when parsed from source; 0 for an import built from other facts


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _walk(node: ast.AST, lazy: bool, type_only: bool, imports: list[RawImport]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Import):
            end = child.end_lineno or child.lineno
            imports += [RawImport(0, a.name, (), child.lineno, lazy, type_only, end) for a in child.names]
        elif isinstance(child, ast.ImportFrom):
            names = tuple(a.name for a in child.names)
            end = child.end_lineno or child.lineno
            imports.append(RawImport(child.level, child.module or "", names, child.lineno, lazy, type_only, end))
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
