"""Count what a named ratchet (#147) counts: per module, by rule id, source pattern, call name or an external command."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .domain import RatchetSpec, Violation, in_unit
from .ports import FileSystem, ToolRunner

__all__ = ["MeasureError", "Subject", "measure"]


class MeasureError(ValueError):
    """A ratchet could not be measured. A measure that cannot run fails the check; it never reads as zero."""


@dataclass(frozen=True)
class Subject:
    """What is measured: the repository root, the modules to read (module -> repository-relative path) and the findings so far."""

    root: Path
    fs: FileSystem
    runner: ToolRunner
    paths: Mapping[str, str]
    found: Sequence[Violation]


def measure(spec: RatchetSpec, subject: Subject) -> dict[str, int]:
    """The count per module that has a non-zero count, restricted to the ratchet's ``scope``."""
    if spec.measure == "rule":
        counts = _by_rule(spec.argument[0], subject.found)
    elif spec.measure == "command":
        counts = _by_command(spec, subject)
    else:
        counts = _by_source(spec, subject)
    return {module: count for module, count in sorted(counts.items()) if count > 0 and _in_scope(spec, module)}


def _in_scope(spec: RatchetSpec, module: str) -> bool:
    return not spec.scope or any(in_unit(module, glob) for glob in spec.scope)


def _by_rule(rule: str, found: Sequence[Violation]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for violation in found:
        if violation.rule == rule:
            counts[violation.subject] = counts.get(violation.subject, 0) + 1
    return counts


def _by_source(spec: RatchetSpec, subject: Subject) -> dict[str, int]:
    counter = _regex_count if spec.measure == "regex" else _call_count
    counts: dict[str, int] = {}
    for module, path in subject.paths.items():
        if not _in_scope(spec, module):
            continue
        try:
            counts[module] = counter(spec.argument[0], subject.fs.read_text(subject.root / path))
        except (OSError, ValueError, SyntaxError) as exc:  # unreadable, not UTF-8, or not Python: a ratchet cannot call that zero
            raise MeasureError(f"{spec.name}: could not read {path}: {exc}") from exc
    return counts


def _regex_count(pattern: str, text: str) -> int:
    return len(re.findall(pattern, text, re.MULTILINE))


def _call_count(callee: str, text: str) -> int:
    return sum(1 for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Call) and _dotted(node.func) == callee)


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` for a chain of attribute accesses ending in a plain name; ``None`` for anything else (a call result, a subscript)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return None if head is None else f"{head}.{node.attr}"
    return None


def _by_command(spec: RatchetSpec, subject: Subject) -> dict[str, int]:
    """Run the command and read ``module count`` lines. A failed run, an odd line or a repeated module is an error."""
    argv = list(spec.argument)
    if not subject.runner.available(argv[0]):
        raise MeasureError(f"{spec.name}: {argv[0]} is not installed")
    result = subject.runner.run(argv, subject.root)
    if result.returncode != 0:
        raise MeasureError(f"{spec.name}: {' '.join(argv)} exited {result.returncode}: {(result.stderr or result.stdout).strip()[-300:]}")
    counts: dict[str, int] = {}
    for number, line in enumerate(result.stdout.splitlines(), start=1):
        if not line.strip():
            continue
        module, _, raw = line.strip().rpartition(" ")
        if not module.strip() or not raw.isdecimal():
            raise MeasureError(f"{spec.name}: output line {number} is not `module count`: {line.strip()!r}")
        if module.strip() in counts:
            raise MeasureError(f"{spec.name}: output names {module.strip()!r} twice")
        counts[module.strip()] = int(raw)
    return counts
