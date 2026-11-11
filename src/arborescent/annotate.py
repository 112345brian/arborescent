"""``arborescent annotate``: migrate existing ``classify`` glob matches into explicit ``__arbor__`` annotations.

A module new since a reference never gets a ring from here: it goes through ``arborescent admit``. What remains is the
legacy migration of rings a human already wrote into ``classify``, behind ``--legacy``; it records policy, not a
validated placement.

Reuses the scanner's ``arbor_index`` (does a file already carry an annotation, valid or not) and ``classify``
resolution (what ring it would get without one) so the insertion logic never re-implements either.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

from .domain import Policy
from .ports import FileSystem, ScanResult, ToolRunner
from .rules import RuleContext

__all__ = ["AnnotatePlan", "apply_annotations", "insert_annotation", "new_since", "plan_annotations"]


@dataclass(frozen=True)
class AnnotatePlan:
    to_annotate: tuple[tuple[str, str, str], ...] = ()  # (module, path, ring)
    already_annotated: tuple[str, ...] = ()  # paths that already carry an __arbor__ (valid or not)
    unmatched: tuple[str, ...] = ()  # paths with no classify match and no annotation
    new_modules: tuple[tuple[str, str], ...] = ()  # (module, path) new since the reference: refused, routed to `admit`


def new_since(runner: ToolRunner, root: Path, ref: str) -> frozenset[str]:
    """Repository-relative paths of the ``.py`` files added since the merge base of ``ref`` and HEAD (a rename is not an addition).

    Raises ``ValueError`` when git or the reference cannot be read: a refusal that cannot see what is new must not pass."""
    if not runner.available("git"):
        raise ValueError("git is not available, so the modules new since the reference cannot be listed")
    base = runner.run(["git", "merge-base", ref, "HEAD"], root)
    if base.returncode != 0 or not base.stdout.strip():
        raise ValueError(f"could not resolve {ref!r} against HEAD: {(base.stderr or base.stdout).strip() or 'no merge base'}")
    diff = runner.run(["git", "diff", "--name-status", "-M", "--diff-filter=A", "-z", base.stdout.strip(), "HEAD"], root)
    if diff.returncode != 0:
        raise ValueError(f"could not list files added since {ref!r}: {(diff.stderr or diff.stdout).strip()}")
    parts = diff.stdout.split("\0")
    return frozenset(path for status, path in zip(parts[0::2], parts[1::2], strict=False) if status == "A" and path.endswith(".py"))


def plan_annotations(policy: Policy, scan: ScanResult, new_paths: frozenset[str] = frozenset()) -> AnnotatePlan:
    indexes = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    to_annotate: list[tuple[str, str, str]] = []
    already_annotated: list[str] = []
    unmatched: list[str] = []
    new_modules: list[tuple[str, str]] = []
    for module, rel in sorted(scan.paths.items(), key=lambda item: item[1]):
        if not rel.endswith(".py"):
            continue
        ring, error = scan.arbor_index.get(module, (None, None))
        if ring is not None or error is not None:
            already_annotated.append(rel)
            continue
        classified = indexes.resolve_ring(module)
        if classified is None:
            unmatched.append(rel)
            continue
        if rel in new_paths:
            new_modules.append((module, rel))
            continue
        to_annotate.append((module, rel, classified))
    return AnnotatePlan(tuple(to_annotate), tuple(already_annotated), tuple(unmatched), tuple(new_modules))


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _is_future_import(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__"


def insert_annotation(source: str, ring: str, reason: str | None = None) -> str:
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
    annotation = {"ring": ring} if reason is None else {"ring": ring, "ring_reason": reason}
    block.append(f"__arbor__ = {json.dumps(annotation)}\n")
    if suffix and suffix[0].strip():
        block.append("\n")
    return "".join(prefix) + "".join(block) + "".join(suffix)


def apply_annotations(fs: FileSystem, root: Path, plan: AnnotatePlan) -> int:
    for _, rel, ring in plan.to_annotate:
        path = root / rel
        fs.write_text(path, insert_annotation(fs.read_text(path), ring))
    return len(plan.to_annotate)
