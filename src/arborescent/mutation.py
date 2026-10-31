"""A selftest mutation: text appended to one file (or a new file) that must make one rule newly fire."""

from __future__ import annotations

from dataclasses import dataclass

from .domain import Rule

__all__ = ["Mutation"]


@dataclass(frozen=True)
class Mutation:
    rule: Rule  # the rule id that must newly fire
    label: str
    file: str  # posix path relative to the root
    append: str  # text appended to the file ("" for an added file: see ``new_file``)
    new_file: bool = False
