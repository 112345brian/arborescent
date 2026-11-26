"""Report-only source hints. Suggestions never enter the violation baseline."""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ClassSuggestion", "Suggestion", "class_size_suggestions", "file_size_suggestions", "logical_code_lines", "size_suggestions"]

_DATA_LITERAL_MIN_LINES = 10
_ENUM_BASE_SUFFIXES = ("Enum", "Flag", "Choices")  # their class-level names are members, not fields
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


@dataclass(frozen=True)
class ClassSuggestion:
    """A non-blocking hint about one class with many fields or methods (#220). A threshold of 0 is off."""

    path: str
    line: int
    name: str
    fields: int
    methods: int
    field_threshold: int
    method_threshold: int

    @property
    def message(self) -> str:
        counted = (("fields", self.fields, self.field_threshold), ("methods", self.methods, self.method_threshold))
        over = " and ".join(f"{n} {what} (suggestion threshold: {limit})" for what, n, limit in counted if _exceeds(n, limit))
        return f"{self.path}:{self.line}: class {self.name} has {over}"


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

    try:
        code_lines = {
            token.start[0]
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in _IGNORED_TOKEN_TYPES
            and not _in_spans(token.start[0], ignored)
        }
    except (tokenize.TokenError, SyntaxError):
        # ast accepted the source but tokenize did not (#181; reported as `untokenizable-source`): count statement start lines.
        code_lines = {
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.stmt) and not _in_spans(node.lineno, ignored)
        }
    return len(code_lines)


def file_size_suggestions(
    read_text: Callable[[Path], str],
    root: Path,
    modules: Mapping[str, str],
    *,
    threshold: int,
) -> list[Suggestion]:
    """Make suggestions from parsed first-party module paths (relative path by module name)."""
    return [
        Suggestion(path, count, threshold)
        for _, path in sorted(modules.items())
        if (count := logical_code_lines(read_text(root / path).removeprefix("\ufeff"))) > threshold
    ]


def class_size_suggestions(
    read_text: Callable[[Path], str],
    root: Path,
    modules: Mapping[str, str],
    *,
    max_fields: int,
    max_methods: int,
) -> list[ClassSuggestion]:
    """One suggestion per class (nested ones on their own) with more than ``max_fields`` fields or ``max_methods`` methods."""
    if not (max_fields or max_methods):
        return []
    out: list[ClassSuggestion] = []
    for _, path in sorted(modules.items()):
        source = read_text(root / path).removeprefix("\ufeff")
        if "class" not in source:
            continue
        classes = sorted((n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ClassDef)), key=lambda n: n.lineno)
        for node in classes:
            fields, methods = _members(node)
            if _exceeds(fields, max_fields) or _exceeds(methods, max_methods):
                out.append(ClassSuggestion(path, node.lineno, node.name, fields, methods, max_fields, max_methods))
    return out


def size_suggestions(
    read_text: Callable[[Path], str], root: Path, modules: Mapping[str, str], limits: tuple[int, int, int]
) -> list[Suggestion | ClassSuggestion]:
    """The report-only size hints, large files then large classes; ``limits`` is (logical lines, fields, methods)."""
    lines, fields, methods = limits
    return [
        *file_size_suggestions(read_text, root, modules, threshold=lines),
        *class_size_suggestions(read_text, root, modules, max_fields=fields, max_methods=methods),
    ]


def _exceeds(count: int, limit: int) -> bool:
    return 0 < limit < count


def _members(node: ast.ClassDef) -> tuple[int, int]:
    """(fields, methods) the class itself declares: distinct names, inherited ones not counted.

    A field is a class-level name (not ``ClassVar``, a dunder or an ``ALL_CAPS`` constant) or an attribute a method assigns on
    ``self``; a method is any non-dunder ``def``, so a property with its setter is one and no field."""
    declared: set[str] = set()
    assigned: set[str] = set()
    methods: set[str] = set()
    for stmt in _class_body(node.body):
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            methods.add(stmt.name)
            assigned.update(_self_attributes(stmt))
        else:
            declared.update(_declared_names(stmt))
    if _is_enum(node):
        declared = set()
    fields = {name for name in declared if not name.isupper()} | assigned
    return (
        sum(1 for name in fields - methods if not _is_dunder(name)),
        sum(1 for name in methods if not _is_dunder(name)),
    )


def _class_body(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """The statements of a class body, looking inside ``if``/``try``/``with`` blocks."""
    for stmt in body:
        if isinstance(stmt, ast.If):
            yield from _class_body([*stmt.body, *stmt.orelse])
        elif isinstance(stmt, ast.Try):
            yield from _class_body([*stmt.body, *(s for h in stmt.handlers for s in h.body), *stmt.orelse, *stmt.finalbody])
        elif isinstance(stmt, ast.With):
            yield from _class_body(stmt.body)
        else:
            yield stmt


def _declared_names(stmt: ast.stmt) -> Iterator[str]:
    if isinstance(stmt, ast.AnnAssign):
        if "ClassVar" in ast.unparse(stmt.annotation):
            return
        targets = [stmt.target]
    elif isinstance(stmt, ast.Assign):
        targets = stmt.targets
    else:
        return
    for target in targets:
        yield from (n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))


def _self_attributes(method: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[str]:
    """Names assigned on the method's first parameter; static and class methods have no ``self``."""
    decorators = {d.id for d in method.decorator_list if isinstance(d, ast.Name)}
    positional = [*method.args.posonlyargs, *method.args.args]
    if not positional or decorators & {"staticmethod", "classmethod"}:
        return
    receiver = positional[0].arg
    pending: list[ast.AST] = list(method.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.ClassDef):
            continue  # a class defined inside the method has its own `self`
        if (
            isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name) and node.value.id == receiver
        ):
            yield node.attr
        pending.extend(ast.iter_child_nodes(node))


def _is_enum(node: ast.ClassDef) -> bool:
    return any(ast.unparse(base).endswith(_ENUM_BASE_SUFFIXES) for base in node.bases)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


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
