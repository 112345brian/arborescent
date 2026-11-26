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

    def absolute_module(self, importer: str, is_package: bool) -> str:
        """The imported module's dotted name as seen from ``importer``; dots stay when the import climbs above the source root."""
        if not self.level:
            return self.module
        parts = importer.split(".")
        package = parts if is_package else parts[:-1]
        if self.level - 1 > len(package):
            return "." * self.level + self.module
        return ".".join([*package[: len(package) - (self.level - 1)], *([self.module] if self.module else [])])


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


_Pending = tuple[ast.AST, bool, bool]  # (statement, inside a function, under ``if TYPE_CHECKING``)
_BLOCKS = (ast.stmt, ast.excepthandler, ast.match_case)  # an import is a statement: no expression is ever entered


def _import_of(node: ast.AST, lazy: bool, type_only: bool) -> list[RawImport]:
    if isinstance(node, ast.Import):
        return [RawImport(0, a.name, (), node.lineno, lazy, type_only, node.end_lineno or node.lineno) for a in node.names]
    if isinstance(node, ast.ImportFrom):
        names = tuple(a.name for a in node.names)
        return [RawImport(node.level, node.module or "", names, node.lineno, lazy, type_only, node.end_lineno or node.lineno)]
    return []


def _inner(node: ast.AST, lazy: bool, type_only: bool) -> list[_Pending]:
    """The statements directly inside ``node``, in source order, with the context each one is read in."""
    if isinstance(node, ast.If) and _is_type_checking(node.test):
        return [*((stmt, lazy, True) for stmt in node.body), *((stmt, lazy, type_only) for stmt in node.orelse)]
    lazy = lazy or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    return [(child, lazy, type_only) for child in ast.iter_child_nodes(node) if isinstance(child, _BLOCKS)]


def raw_imports(tree: ast.AST) -> tuple[RawImport, ...]:
    """Every import in ``tree``, in source order; one inside a function is ``lazy``, one under ``if TYPE_CHECKING`` is ``type_only``.

    Iterative, so a long ``elif`` ladder cannot overflow the stack."""
    imports: list[RawImport] = []
    pending = _inner(tree, False, False)[::-1]
    while pending:
        node, lazy, type_only = pending.pop()
        imports += _import_of(node, lazy, type_only)
        pending += _inner(node, lazy, type_only)[::-1]
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
