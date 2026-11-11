"""Adapter equivalence (#113): modules that implement one port need a test function that calls into every one of them.

Pure over plain data. ``practices`` computes the per-test-function call sets with ``function_references`` and passes the
modules each test function reached. The check cannot know the behaviour; it forces a differential test to exist.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from typing import cast

from .domain import Rule, Violation

__all__ = ["equivalence_findings", "parse_groups"]

_SHAPE = "adapter-equivalence must be a list of groups, each a list of module names, e.g. [['pkg.fact_queries', 'pkg.modes_store']]"


def parse_groups(value: object) -> tuple[list[tuple[str, ...]], list[str]]:
    """The declared groups, and every way the value is malformed (a malformed value must never switch the check off)."""
    if not isinstance(value, list) or not all(isinstance(group, list) for group in cast("list[object]", value)):
        return [], [_SHAPE]
    groups: list[tuple[str, ...]] = []
    problems: list[str] = []
    for raw in cast("list[list[object]]", value):
        members = tuple(member for member in raw if isinstance(member, str))
        if len(members) != len(raw):
            problems.append(f"{_SHAPE}; got {raw!r}")
            continue
        groups.append(members)
    return groups, problems


def _suggestion(name: str, modules: Collection[str]) -> str:
    matches = sorted(m for m in modules if m.endswith(f".{name}"))
    return f"; did you mean {matches[0]!r}?" if len(matches) == 1 else ""


def _group_problems(groups: Sequence[tuple[str, ...]], modules: Collection[str]) -> list[Violation]:
    out: list[Violation] = []
    owner: dict[str, int] = {}
    for number, group in enumerate(groups, 1):
        subject = ", ".join(group) or f"group {number}"
        if len(set(group)) < 2:
            out.append(Violation(Rule.ADAPTER_EQUIVALENCE, subject, f"group {number} needs at least two distinct modules, got {list(group)!r}"))
        for member in dict.fromkeys(group):
            if member not in modules:
                out.append(Violation(Rule.ADAPTER_EQUIVALENCE, subject, f"{member!r} is not a scanned module{_suggestion(member, modules)}"))
            elif owner.setdefault(member, number) != number:
                out.append(Violation(Rule.ADAPTER_EQUIVALENCE, member, f"{member!r} is in more than one group (groups {owner[member]} and {number})"))
    return out


def equivalence_findings(
    value: object, modules: Collection[str], tests: Iterable[frozenset[str]], *, report: bool = False,
) -> list[Violation]:
    """Problems with the declared groups (always failures), then each group no single test function reaches in full.

    ``tests`` holds, for each test function, the modules it calls into. ``report`` demotes only the missing-test finding.
    """
    groups, shape = parse_groups(value)
    out = [Violation(Rule.ADAPTER_EQUIVALENCE, "adapter-equivalence", problem) for problem in shape]
    out += _group_problems(groups, modules)
    reached = list(tests)
    rule = Rule.ADAPTER_EQUIVALENCE_REPORT if report else Rule.ADAPTER_EQUIVALENCE
    for group in groups:
        members = set(group)
        if len(members) < 2 or not members <= set(modules) or any(members <= seen for seen in reached):
            continue  # a malformed group is already reported above; do not also claim its test is missing
        subject = ", ".join(group)
        out.append(Violation(rule, subject, "no single test function calls a function of every module in this group (a differential test of the adapters is missing)"))
    return out
