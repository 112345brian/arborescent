"""``duplicate-logic`` (#228): functions that implement the same rule a second time.

Whole-repository and pure: the caller reads the sources and binds the cache. Functions are bucketed by the digest of their
normalised body, so the cost is one pass over each file plus one pass over the buckets; no pair is ever compared.

Near copies are found by hashing too. For one changed literal, each body is bucketed once per literal under a key that leaves
that literal out. For one extra statement, the hash of a body with each deletable statement removed (from prefix hashes) is
looked up among the shorter bodies, and the local-name wiring is then checked on that candidate.

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

from .duplicate_facts import BranchShape, Decision, FunctionShape, Unit, function_shapes

__all__ = ["DuplicateCacheStore", "DuplicateFinding", "DuplicateThresholds", "References", "find_duplicates"]

References = Mapping[str, Mapping[str, str | None]]  # reference entry -> {file: its text, or None when it cannot be read}
_CACHE_VERSION = f"2.{sys.version_info[0]}.{sys.version_info[1]}"  # the AST, and so the digests, may differ between versions
_PARTNERS_SHOWN = 3
_MIN_GROUP = 2  # a repeat needs at least two
_BASE = 1_000_003
_MODULUS = (1 << 127) - 1  # a Mersenne prime: collisions of the rolling hash are not a practical concern
_TRIES = 4  # positions of one longer body checked against the same shorter one; keeps repeated statements linear
_DUPLICATES = "function `{}` duplicates logic defined elsewhere"  # one wording for exact and near copies: the key survives drift
_Shapes = tuple[FunctionShape, ...]
_Member = tuple[str, FunctionShape]  # (module or reference file, function)
_Site = tuple[str, str, int]  # (module, function, line) of a branch


class DuplicateThresholds(Protocol):
    """What the pass reads of ``policy_types.DuplicatePolicy``; structural, so this module adds no importer to the policy vocabulary."""

    @property
    def min_statements(self) -> int: ...

    @property
    def branch_skeletons(self) -> bool: ...

    @property
    def min_branch_repeats(self) -> int: ...

    @property
    def near_misses(self) -> bool: ...

    @property
    def min_near_statements(self) -> int: ...

    @property
    def branch_conditions(self) -> bool: ...


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
    if policy.near_misses:
        found += _near_findings(first, targets, policy.min_near_statements, found)
    covered: set[_Site] = set()
    if policy.branch_conditions:
        decided, covered = _decision_findings(first, reported, policy.min_branch_repeats)
        found += decided
    if policy.branch_skeletons:
        found += _branch_findings(first, reported, policy.min_branch_repeats, covered)
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
    decisions = [[item.digest, item.label, item.line, item.scoped] for item in shape.decisions]
    return [shape.qualname, shape.line, shape.statements, shape.digest, branches, [list(unit) for unit in shape.units], decisions]


def _cached(hit: object, digest: str) -> _Shapes | None:
    """The shapes of a cache record that matches ``digest``; a record of any other form is a miss, never a crash."""
    try:
        record = cast("list[Any]", hit) if isinstance(hit, list) else []
        if not record or record[0] != digest:
            return None
        return tuple(_from_json(item) for item in record[1])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def _from_json(item: list[Any]) -> FunctionShape:
    qualname, line, statements, digest, branches, units, decisions = item
    return FunctionShape(
        qualname, line, statements, digest, tuple(BranchShape(*branch) for branch in branches),
        tuple(Unit(shape, tuple(names), tuple(literals), extent) for shape, names, literals, extent in units),
        tuple(Decision(*decision) for decision in decisions),
    )


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
                module, _DUPLICATES.format(shape.qualname), shape.line,
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


# --- near copies --------------------------------------------------------------------------------------------------------


@dataclass(eq=False)
class _Body:
    """Every function with one exact body. Near matching compares bodies, so exact copies cost nothing extra."""

    first: list[_Member]  # first-party copies: the ones that can be reported
    reference: list[_Member]
    units: tuple[Unit, ...]
    partner: _Body | None = None
    difference: str = ""

    def pair(self, other: _Body, difference: str) -> None:
        """Keep the first partner found, but let a reference body replace a first-party one: it is the one to use."""
        if self.partner is None or (other.reference and not self.partner.reference):
            self.partner, self.difference = other, difference


def _near_bodies(first: Sequence[_Member], targets: Sequence[_Member], minimum: int) -> list[_Body]:
    bodies: dict[str, _Body] = {}
    for members, is_reference in ((first, False), (targets, True)):
        for member in members:
            shape = member[1]
            if shape.statements >= minimum and shape.units:
                body = bodies.setdefault(shape.digest, _Body([], [], shape.units))
                (body.reference if is_reference else body.first).append(member)
    return list(bodies.values())


def _near_findings(
    first: Sequence[_Member], targets: Sequence[_Member], minimum: int, exact: Sequence[DuplicateFinding],
) -> list[DuplicateFinding]:
    bodies = _near_bodies(first, targets, minimum)
    _pair_by_literal(bodies)
    _pair_by_statement(bodies)
    taken = {(item.subject, item.line) for item in exact}
    return [
        DuplicateFinding(module, _DUPLICATES.format(shape.qualname), shape.line, _near_hint(body.partner, body.difference))
        for body in bodies if body.partner is not None
        for module, shape in body.first if (module, shape.line) not in taken
    ]


def _near_hint(partner: _Body, difference: str) -> str:
    if partner.reference:
        path, shape = partner.reference[0]
        return f" (near copy of reference code `{path}:{shape.line} {shape.qualname}`: {difference}; use that instead of a copy)"
    module, shape = partner.first[0]
    return f" (near copy of {module}:{shape.qualname} (line {shape.line}): {difference})"


class _Rolling:
    """The hash of a sequence with a slice removed, in constant time after one pass."""

    def __init__(self, items: Sequence[str]) -> None:
        self.prefix = [0]
        self.power = [1]
        for item in items:
            number = int.from_bytes(hashlib.blake2b(item.encode(), digest_size=16).digest(), "big")
            self.prefix.append((self.prefix[-1] * _BASE + number) % _MODULUS)
            self.power.append(self.power[-1] * _BASE % _MODULUS)

    def without(self, start: int, count: int) -> int:
        """The hash of the whole sequence with ``count`` items from ``start`` removed."""
        size = len(self.prefix) - 1
        tail = size - start - count
        suffix = self.prefix[size] - self.prefix[start + count] * self.power[tail]
        return (self.prefix[start] * self.power[tail] + suffix) % _MODULUS


def _wiring(units: Sequence[Unit]) -> tuple[int, ...]:
    """Which local each use refers to, renumbered by first appearance in ``units``."""
    numbers: dict[int, int] = {}
    return tuple(numbers.setdefault(name, len(numbers)) for unit in units for name in unit.names)


def _pair_by_literal(bodies: Sequence[_Body]) -> None:
    """Bodies alike in every statement and every local, differing in exactly one literal."""
    buckets: dict[tuple[str, int, int], dict[str, list[_Body]]] = {}
    for body in bodies:
        literals = [literal for unit in body.units for literal in unit.literals]
        if not literals:
            continue
        frame = f"{len(literals)}{[unit.shape for unit in body.units]}{_wiring(body.units)}"
        frame = hashlib.blake2b(frame.encode(), digest_size=16).hexdigest()
        rolling = _Rolling(literals)
        for index, literal in enumerate(literals):
            buckets.setdefault((frame, index, rolling.without(index, 1)), {}).setdefault(literal, []).append(body)
    for values in buckets.values():
        if len(values) < _MIN_GROUP:
            continue
        # One candidate partner per value is enough, reference bodies first: any body takes one whose value is not its own.
        offered = [min(group, key=lambda body: not body.reference) for group in values.values()]
        offered.sort(key=lambda body: not body.reference)
        for group in values.values():
            own = min(group, key=lambda body: not body.reference)  # the one this value offers, if it is among the offers
            other = next(body for body in offered[:_MIN_GROUP] if body is not own)
            for body in group:
                body.pair(other, "one literal differs")


def _pair_by_statement(bodies: Sequence[_Body]) -> None:
    """A body that is another one plus a single statement (or one-line guard)."""
    hashes = [_Rolling([f"{unit.shape}{unit.literals}" for unit in body.units]) for body in bodies]
    shorter: dict[tuple[int, int], dict[tuple[int, ...], _Body]] = {}
    for body, rolling in zip(bodies, hashes, strict=True):
        _ = shorter.setdefault((len(body.units), rolling.prefix[-1]), {}).setdefault(_wiring(body.units), body)
    for body, rolling in zip(bodies, hashes, strict=True):
        tried: dict[tuple[int, int], int] = {}
        for index, unit in enumerate(body.units):
            if not unit.extent:
                continue
            key = (len(body.units) - unit.extent, rolling.without(index, unit.extent))
            wirings = shorter.get(key)
            if wirings is None or tried.get(key, 0) >= _TRIES:
                continue
            tried[key] = tried.get(key, 0) + 1
            other = wirings.get(_wiring([*body.units[:index], *body.units[index + unit.extent:]]))
            if other is not None:
                body.pair(other, "one statement more here")
                other.pair(body, "one statement fewer here")


# --- repeated branches --------------------------------------------------------------------------------------------------


def _identity(module: str, shape: FunctionShape, reported: set[str]) -> str:
    """Whole copies already reported count as one function between them."""
    return shape.digest if shape.digest in reported else f"{module}:{shape.qualname}"


def _decision_findings(first: Sequence[_Member], reported: set[str], minimum: int) -> tuple[list[DuplicateFinding], set[_Site]]:
    """One finding per function and condition that ``minimum`` functions branch on, and the branches those findings cover."""
    buckets: dict[tuple[str, str], list[tuple[str, FunctionShape, Decision]]] = {}
    for module, shape in first:
        for decision in shape.decisions:
            buckets.setdefault((decision.digest, module if decision.scoped else ""), []).append((module, shape, decision))
    found: list[DuplicateFinding] = []
    covered: set[_Site] = set()
    for members in buckets.values():
        count = len({_identity(module, shape, reported) for module, shape, _ in members})
        if count < minimum:
            continue
        for module, shape, decision in members:
            covered.add((module, shape.qualname, decision.line))
            detail = f"function `{shape.qualname}` branches on `{decision.label}` like other functions [{decision.digest[:8]}]"
            hint = f" ({count} functions pick between different calls on this; decide it in one place)"
            found.append(DuplicateFinding(module, detail, decision.line, hint))
    return found, covered


def _branch_findings(first: Sequence[_Member], reported: set[str], minimum: int, covered: set[_Site]) -> list[DuplicateFinding]:
    """One finding per function and repeated branch. Whole copies already reported count as one function between them."""
    buckets: dict[str, dict[tuple[str, str], tuple[str, BranchShape]]] = {}
    for module, shape in first:
        identity = _identity(module, shape, reported)
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
            if (module, qualname, branch.line) not in covered
        )
    return found
