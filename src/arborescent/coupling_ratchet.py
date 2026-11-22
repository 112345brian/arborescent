"""The optional coupling ratchet (#148): hub fan-in and entrypoint closure sizes may shrink, never grow unrecorded.

The baseline is ``{name: {key: count}}`` plus a ``growth`` log, the shape named ratchets share. ``fan-in`` holds the modules
at or above the policy's ``hub_fan_in`` (a module not listed must stay below it), ``closure`` holds every declared entrypoint.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import cast

from .coupling import Closure, FanIn
from .coupling_policy import CouplingPolicy

FAN_IN = "fan-in"
CLOSURE = "closure"
GROWTH = "growth"
Counts = dict[str, dict[str, int]]


def measured(policy: CouplingPolicy, rows: Iterable[FanIn], closures: Iterable[Closure]) -> dict[str, dict[str, int]]:
    return {
        FAN_IN: {row.module: row.direct for row in rows if row.direct >= policy.hub_fan_in},
        CLOSURE: {closure.entrypoint: len(closure.modules) for closure in closures},
    }


def regressions(current: Counts, baseline: Counts) -> list[str]:
    """Every count above its baseline, and every hub or entrypoint the baseline has no entry for."""
    found: list[str] = []
    for name, counts in current.items():
        recorded = baseline.get(name, {})
        for key, count in sorted(counts.items()):
            allowed = recorded.get(key)
            if allowed is None:
                found.append(f"{name}: {key} is not in the baseline ({count})")
            elif count > allowed:
                found.append(f"{name}: {key} grew from {allowed} to {count}")
    return found


def lowerable(current: Counts, baseline: Counts) -> list[str]:
    """Baseline entries that are now smaller or gone: `arborescent coupling baseline` lowers them."""
    return [
        f"{name}: {key} is {current.get(name, {}).get(key, 0)}, baseline {allowed}"
        for name, recorded in baseline.items()
        for key, allowed in sorted(recorded.items())
        if current.get(name, {}).get(key, 0) < allowed
    ]


def _table(document: Mapping[str, object], name: str) -> dict[str, int]:
    table = document.get(name, {})
    if not isinstance(table, dict):
        raise ValueError(f"coupling baseline `{name}` must map module names to counts")
    counts: dict[str, int] = {}
    for key, value in cast("dict[object, object]", table).items():
        if not isinstance(key, str) or not isinstance(value, int):
            raise ValueError(f"coupling baseline `{name}` must map module names to counts")
        counts[key] = value
    return counts


def parse(document: object) -> tuple[dict[str, dict[str, int]], list[object]]:
    """The counts and growth log of a baseline file; ``ValueError`` when the file is not that shape."""
    if not isinstance(document, dict):
        raise ValueError("coupling baseline must be a JSON object")
    fields = cast("dict[str, object]", document)
    log = fields.get(GROWTH, [])
    if not isinstance(log, list):
        raise ValueError("coupling baseline `growth` must be a list")
    return {name: _table(fields, name) for name in (FAN_IN, CLOSURE)}, cast("list[object]", log)


def next_document(
    existing: tuple[Counts, list[object]] | None, current: Counts, allow_growth: bool, reason: str | None
) -> dict[str, object]:
    """The baseline to write: every count lowered to its current value, entries that are gone dropped.

    A count that would rise, or an entry the baseline lacks, needs ``allow_growth`` and a ``reason``, which is logged.
    The first baseline (``existing`` is None) records the current state without a log entry.
    """
    nothing: Counts = {FAN_IN: {}, CLOSURE: {}}
    previous = nothing if existing is None else existing[0]
    log: list[object] = [] if existing is None else existing[1]
    added = [] if existing is None else regressions(current, previous)
    if added and not allow_growth:
        raise ValueError("coupling baseline would grow (use --allow-growth --reason): " + "; ".join(added))
    if added and (reason is None or not reason.strip()):
        raise ValueError("--allow-growth needs --reason")
    document: dict[str, object] = {name: dict(sorted(counts.items())) for name, counts in current.items()}
    document[GROWTH] = [*log, {"reason": (reason or "").strip(), "added": added}] if added else log
    return document
