"""The ``[package.constraints]`` table (#164): what a package promises *not* to do. Schema and validation only; nothing is enforced yet.

Constraints are separate from ``__arbor__`` annotations and from ``packages.toml`` metadata, and carry no position: the host places modules
in rings, the package only states intent. Pure.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from .presets import EFFECT_CATEGORIES

CONSTRAINT_KEYS = ("must_be_pure", "forbid_effects")


class ConstraintError(ValueError):
    """An invalid ``[package]`` table; ``config`` re-raises it as a policy error."""


@dataclass(frozen=True)
class PackageConstraints:
    """``must_be_pure``: no effect use at all. ``forbid_effects``: scanner effect categories the package never uses."""

    must_be_pure: bool = False
    forbid_effects: tuple[str, ...] = ()


def parse_package_constraints(raw: Mapping[str, Any]) -> PackageConstraints:
    """Read ``[package.constraints]`` from a decoded policy file. Absent means no constraints; anything unrecognised is an error."""
    if "package" not in raw:
        return PackageConstraints()
    package = _table(raw["package"], "`package`")
    unknown = sorted(set(package) - {"constraints"})
    if unknown:
        raise ConstraintError(f"unknown key in `package`: {unknown}; allowed: ['constraints']")
    table = _table(package.get("constraints", {}), "`package.constraints`")
    unknown = sorted(set(table) - set(CONSTRAINT_KEYS))
    if unknown:
        raise ConstraintError(f"unknown key in `package.constraints`: {unknown}; allowed: {list(CONSTRAINT_KEYS)}")
    return PackageConstraints(_must_be_pure(table), _forbid_effects(table))


def _table(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConstraintError(f"{name} must be a table")
    return cast("Mapping[str, Any]", value)


def _must_be_pure(table: Mapping[str, Any]) -> bool:
    value = table.get("must_be_pure", False)
    if not isinstance(value, bool):
        raise ConstraintError("`package.constraints.must_be_pure` must be a boolean")
    return value


def _forbid_effects(table: Mapping[str, Any]) -> tuple[str, ...]:
    value: object = table.get("forbid_effects", [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in cast("list[object]", value)):
        raise ConstraintError("`package.constraints.forbid_effects` must be a list of effect category names")
    names = cast("list[str]", value)
    unknown = sorted(set(names) - EFFECT_CATEGORIES)
    if unknown:
        raise ConstraintError(f"unknown effect category in `package.constraints.forbid_effects`: {unknown}; known: {sorted(EFFECT_CATEGORIES)}")
    return tuple(dict.fromkeys(names))
