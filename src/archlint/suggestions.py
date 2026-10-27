"""Report-only source hints. Suggestions never enter the violation baseline."""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .ports import FileSystem

__all__ = ["Suggestion", "file_size_suggestions", "logical_code_lines"]

_DATA_LITERAL_MIN_LINES = 10
_IGNORED_TOKEN_TYPES = {
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
    tokenize.COMMENT,
}


@dataclass(frozen=True)
class Suggestion:
    """A non-blocking hint about a source file."""

    path: str
    logical_lines: int
    threshold: int

    @property
    def message(self) -> str:
        return f"{self.path}: {self.logical_lines} logical code lines (suggestion threshold: {self.threshold})"


def logical_code_lines(source: str) -> int:
    """Count lines with code tokens, excluding comments, docstrings and large literal data tables."""
    tree = ast.parse(source)
    ignored: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if body and _is_docstring(body[0]):
                ignored.append((body[0].lineno, body[0].end_lineno or body[0].lineno))
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            value = node.value
            if value is not None and _is_data_literal(value) and _span(value) >= _DATA_LITERAL_MIN_LINES:
                end = getattr(value, "end_lineno", None)
                ignored.append((value.lineno, end if isinstance(end, int) else value.lineno))

    code_lines = {
        token.start[0]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in _IGNORED_TOKEN_TYPES
        and not _in_spans(token.start[0], ignored)
    }
    return len(code_lines)


def file_size_suggestions(
    fs: FileSystem,
    root: Path,
    modules: Mapping[str, str],
    *,
    threshold: int,
) -> list[Suggestion]:
    """Make suggestions from parsed first-party module paths (relative path by module name)."""
    return [
        Suggestion(path, count, threshold)
        for _, path in sorted(modules.items())
        if (count := logical_code_lines(fs.read_text(root / path))) > threshold
    ]


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _span(node: ast.AST) -> int:
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if not isinstance(start, int):
        return 0
    return (end if isinstance(end, int) else start) - start + 1


def _is_data_literal(node: ast.expr) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_data_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is None or _is_data_literal(key) for key in node.keys) and all(
            _is_data_literal(value) for value in node.values
        )
    return False


def _in_spans(line: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in spans)
