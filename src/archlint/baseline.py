"""The shrink-only ratchet: a baseline file records today's violations; only NEW ones fail, and entries that no
longer occur must be deleted (``archlint baseline`` rewrites the file), so the debt can only go down."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .domain import Violation

__all__ = ["Ratchet", "apply", "load", "save"]


@dataclass(frozen=True)
class Ratchet:
    new: list[Violation]  # not in the baseline: fail
    stale: list[str]  # in the baseline but gone: fail (shrink-only)
    tolerated: int  # in the baseline and still present

    @property
    def ok(self) -> bool:
        return not self.new and not self.stale


def load(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k) for k in data.get("violations", [])}


def save(path: Path, violations: Iterable[Violation]) -> int:
    keys = sorted({v.key for v in violations})
    path.write_text(json.dumps({"version": 1, "violations": keys}, indent=2) + "\n", encoding="utf-8", newline="\n")
    return len(keys)


def apply(violations: Iterable[Violation], baseline: set[str], complete: bool = True) -> Ratchet:
    """``complete`` is False for a partial (``--changed``) run, where an absent entry proves nothing."""
    current = list(violations)
    keys = {v.key for v in current}
    new = [v for v in current if v.key not in baseline]
    stale = sorted(baseline - keys) if complete else []
    return Ratchet(new, stale, len(keys & baseline))
