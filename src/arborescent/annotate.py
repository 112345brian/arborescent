"""``arborescent annotate``: migrate existing ``classify`` glob matches into explicit ``__arbor__`` annotations.

Reuses the scanner's ``arbor_index`` (does a file already carry an annotation, valid or not) and ``classify``
resolution (what ring it would get without one) so the insertion logic never re-implements either.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

from .domain import Policy
from .ports import FileSystem, ScanResult

__all__ = ["AnnotatePlan", "apply_annotations", "insert_annotation", "plan_annotations"]


@dataclass(frozen=True)
class AnnotatePlan:
    to_annotate: tuple[tuple[str, str, str], ...] = ()  # (module, path, ring)
    already_annotated: tuple[str, ...] = ()  # paths that already carry an __arbor__ (valid or not)
    unmatched: tuple[str, ...] = ()  # paths with no classify match and no annotation


def plan_annotations(policy: Policy, scan: ScanResult) -> AnnotatePlan:
    to_annotate: list[tuple[str, str, str]] = []
    already_annotated: list[str] = []
    unmatched: list[str] = []
    for module, rel in sorted(scan.paths.items(), key=lambda item: item[1]):
        if not rel.endswith(".py"):
            continue
        ring, error = scan.arbor_index.get(module, (None, None))
        if ring is not None or error is not None:
            already_annotated.append(rel)
            continue
        classified = policy.ring_of(module)
        if classified is None:
            unmatched.append(rel)
            continue
        to_annotate.append((module, rel, classified))
    return AnnotatePlan(tuple(to_annotate), tuple(already_annotated), tuple(unmatched))


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _is_future_import(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__"


def insert_annotation(source: str, ring: str) -> str:
    """Insert ``__arbor__ = {"ring": ...}`` right after the module docstring and a following
    ``from __future__ import annotations``, when present; otherwise at the top of the file."""
    body = ast.parse(source).body
    lines = source.splitlines(keepends=True)
    insert_at = 0
    index = 0
    if body and _is_docstring(body[0]):
        insert_at = body[0].end_lineno or 0
        index = 1
    if index < len(body) and _is_future_import(body[index]):
        insert_at = body[index].end_lineno or insert_at
    prefix = lines[:insert_at]
    suffix = lines[insert_at:]
    block = ["\n"] if prefix and prefix[-1].strip() else []
    block.append(f"__arbor__ = {json.dumps({'ring': ring})}\n")
    if suffix and suffix[0].strip():
        block.append("\n")
    return "".join(prefix) + "".join(block) + "".join(suffix)


def apply_annotations(fs: FileSystem, root: Path, plan: AnnotatePlan) -> int:
    for _, rel, ring in plan.to_annotate:
        path = root / rel
        fs.write_text(path, insert_annotation(fs.read_text(path), ring))
    return len(plan.to_annotate)
