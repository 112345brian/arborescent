"""``duplicate-logic`` (#228): functions that implement the same rule a second time.

Whole-repository and pure: the caller reads the sources and binds the cache. Functions are bucketed by the digest of their
normalised body, so the cost is one pass over each file plus one pass over the buckets; no pair is ever compared.

A finding is per function, not per group, and its detail names neither the partners nor the digest, so the baseline stays
shrink-only: removing a copy leaves one stale entry (two when the group disappears), editing both copies alike changes no
key, and only a function that was not a duplicate before is new. The partners are in the hint.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

from .duplicate_facts import BranchShape, FunctionShape, function_shapes

__all__ = ["DuplicateCacheStore", "DuplicateFinding", "DuplicateThresholds", "References", "find_duplicates"]

References = Mapping[str, Mapping[str, str | None]]  # reference entry -> {file: its text, or None when it cannot be read}
_CACHE_VERSION = f"1.{sys.version_info[0]}.{sys.version_info[1]}"  # the AST, and so the digests, may differ between versions
_PARTNERS_SHOWN = 3
_MIN_GROUP = 2  # a repeat needs at least two
_Shapes = tuple[FunctionShape, ...]
_Member = tuple[str, FunctionShape]  # (module or reference file, function)


class DuplicateThresholds(Protocol):
    """What the pass reads of ``policy_types.DuplicatePolicy``; structural, so this module adds no importer to the policy vocabulary."""

    @property
    def min_statements(self) -> int: ...

    @property
    def branch_skeletons(self) -> bool: ...

    @property
    def min_branch_repeats(self) -> int: ...


class DuplicateCacheStore(Protocol):
    """Where the per-file shapes are kept between runs; a missing or unreadable store is an empty cache."""

    def read(self) -> str | None: ...

    def write(self, text: str) -> None: ...


@dataclass(frozen=True)
class DuplicateFinding:
    subject: str  # a module, or a reference path that cannot be compared against
    detail: str
    line: int = 0
    hint: str = ""


def find_duplicates(
    modules: Mapping[str, str], references: References, policy: DuplicateThresholds, cache: DuplicateCacheStore | None = None,
) -> list[DuplicateFinding]:
    """``modules`` maps a first-party module to its source. Reference functions are targets only and never findings."""
    parser = _Parser(cache)
    first: list[_Member] = [
        (name, shape) for name, text in sorted(modules.items()) for shape in parser.shapes(f"m:{name}", text) or ()
    ]
    targets, unreadable = _reference_members(references, parser)
    parser.save()
    bodies = _by_body(first, policy.min_statements)
    reference_bodies = _by_body(targets, policy.min_statements)
    found, reported = _body_findings(bodies, reference_bodies)
    if policy.branch_skeletons:
        found += _branch_findings(first, reported, policy.min_branch_repeats)
    return sorted(found + unreadable, key=lambda item: (item.subject, item.line, item.detail))


class _Parser:
    """``function_shapes`` per file, reused while the file's content digest still matches."""

    def __init__(self, cache: DuplicateCacheStore | None) -> None:
        self._cache = cache
        self._stored = _load(cache)
        self._fresh: dict[str, Any] = {}

    def shapes(self, key: str, text: str) -> _Shapes | None:
        """``None`` when Python cannot parse the file."""
        digest = hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()
        shapes = _cached(self._stored.get(key), digest)
        if shapes is None:
            try:
                shapes = function_shapes(text)
            except (SyntaxError, ValueError, RecursionError):
                return None
        self._fresh[key] = [digest, [_to_json(shape) for shape in shapes]]
        return shapes

    def save(self) -> None:
        if self._cache is not None and self._fresh != self._stored:
            self._cache.write(json.dumps({"v": _CACHE_VERSION, "files": self._fresh}))


def _load(cache: DuplicateCacheStore | None) -> dict[str, object]:
    text = cache.read() if cache is not None else None
    if text is None:
        return {}
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}
    document = cast("dict[str, object]", loaded) if isinstance(loaded, dict) else {}
    files = document.get("files")
    return cast("dict[str, object]", files) if document.get("v") == _CACHE_VERSION and isinstance(files, dict) else {}


def _to_json(shape: FunctionShape) -> list[Any]:
    branches = [[branch.digest, branch.label, branch.line] for branch in shape.branches]
    return [shape.qualname, shape.line, shape.statements, shape.digest, branches]


def _cached(hit: object, digest: str) -> _Shapes | None:
    """The shapes of a cache record that matches ``digest``; a record of any other form is a miss, never a crash."""
    try:
        record = cast("list[Any]", hit) if isinstance(hit, list) else []
        if not record or record[0] != digest:
            return None
        return tuple(
            FunctionShape(item[0], item[1], item[2], item[3], tuple(BranchShape(*branch) for branch in item[4]))
            for item in record[1]
        )
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _reference_members(references: References, parser: _Parser) -> tuple[list[_Member], list[DuplicateFinding]]:
    """The reference functions, plus a finding for each entry or file that cannot be compared against: never a silent pass."""
    members: list[_Member] = []
    problems: list[DuplicateFinding] = []
    for entry, files in sorted(references.items()):
        parsed = {path: None if text is None else parser.shapes(f"r:{path}", text) for path, text in sorted(files.items())}
        members.extend((path, shape) for path, shapes in parsed.items() for shape in shapes or ())
        failed = [path for path, shapes in parsed.items() if shapes is None]
        if len(failed) == len(parsed):
            problems.append(DuplicateFinding(entry, "`duplicate_logic.reference_paths` entry has no readable Python file"))
        else:
            problems.extend(DuplicateFinding(path, "reference file cannot be parsed, so copies of it are not seen") for path in failed)
    return members, problems


def _by_body(members: Sequence[_Member], minimum: int) -> dict[str, list[_Member]]:
    buckets: dict[str, list[_Member]] = {}
    for member in members:
        if member[1].statements >= minimum:
            buckets.setdefault(member[1].digest, []).append(member)
    return buckets


def _body_findings(
    bodies: Mapping[str, list[_Member]], references: Mapping[str, list[_Member]],
) -> tuple[list[DuplicateFinding], set[str]]:
    """The findings, and the body digests they cover."""
    found: list[DuplicateFinding] = []
    reported: set[str] = set()
    for digest, members in bodies.items():
        targets = references.get(digest, [])
        if not targets and len(members) < _MIN_GROUP:
            continue
        reported.add(digest)
        found.extend(
            DuplicateFinding(
                module, f"function `{shape.qualname}` duplicates logic defined elsewhere", shape.line,
                _reference_hint(targets) if targets else _partner_hint(members, (module, shape.qualname)),
            )
            for module, shape in members
        )
    return found, reported


def _reference_hint(targets: Sequence[_Member]) -> str:
    shown = ", ".join(f"`{path}:{shape.line} {shape.qualname}`" for path, shape in targets[:_PARTNERS_SHOWN])
    return f" (same body as reference code {shown}{_more(len(targets) - _PARTNERS_SHOWN)}; use that instead of a copy)"


def _partner_hint(members: Sequence[_Member], own: tuple[str, str]) -> str:
    # Only the head of the group is read, so a group of any size costs the same per member.
    others = [item for item in members[: _PARTNERS_SHOWN + 1] if (item[0], item[1].qualname) != own][:_PARTNERS_SHOWN]
    shown = ", ".join(f"{module}:{shape.qualname} (line {shape.line})" for module, shape in others)
    return f" (same body as {shown}{_more(len(members) - 1 - len(others))})"


def _more(count: int) -> str:
    return f" and {count} more" if count > 0 else ""


def _branch_findings(first: Sequence[_Member], reported: set[str], minimum: int) -> list[DuplicateFinding]:
    """One finding per function and repeated branch. Whole copies already reported count as one function between them."""
    buckets: dict[str, dict[tuple[str, str], tuple[str, BranchShape]]] = {}
    for module, shape in first:
        identity = shape.digest if shape.digest in reported else f"{module}:{shape.qualname}"
        for branch in shape.branches:
            _ = buckets.setdefault(branch.digest, {}).setdefault((module, shape.qualname), (identity, branch))
    found: list[DuplicateFinding] = []
    for digest, members in buckets.items():
        count = len({identity for identity, _ in members.values()})
        if count < minimum:
            continue
        found.extend(
            DuplicateFinding(
                module, f"function `{qualname}` repeats a branch {branch.label} [{digest[:8]}]", branch.line,
                f" ({count} functions branch this way; decide it in one place)",
            )
            for (module, qualname), (_, branch) in members.items()
        )
    return found
