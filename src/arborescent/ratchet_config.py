"""``[ratchet.<name>]`` (#147): translate the raw table into ``RatchetSpec`` values, refusing anything ambiguous."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from .domain import PolicyError, RatchetSpec, Rule

__all__ = ["DEFAULT_BASELINE", "ratchet_specs"]

DEFAULT_BASELINE = "arborescent-ratchets.json"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")  # no "|": a growth key is `name|module`
_MEASURES = ("rule", "regex", "ast_call", "command")
_KEYS = frozenset({*_MEASURES, "scope", "baseline"})


def ratchet_specs(raw: object) -> tuple[RatchetSpec, ...]:
    """One spec per ``[ratchet.<name>]`` table, sorted by name. Each table sets exactly one measure."""
    if not isinstance(raw, Mapping):
        raise PolicyError("`ratchet` must be a table of `[ratchet.<name>]` tables")
    return tuple(_spec(str(name), table) for name, table in sorted(cast("Mapping[str, Any]", raw).items()))


def _spec(name: str, table: object) -> RatchetSpec:
    if not _NAME.fullmatch(name):
        raise PolicyError(f"ratchet name {name!r} must be letters, digits, `_`, `.` or `-`")
    if not isinstance(table, Mapping):
        raise PolicyError(f"[ratchet.{name}] must be a table")
    fields = cast("Mapping[str, Any]", table)
    unknown = set(fields) - _KEYS
    if unknown:
        raise PolicyError(f"[ratchet.{name}] has unknown setting(s) {sorted(unknown)}; allowed: {sorted(_KEYS)}")
    chosen = [measure for measure in _MEASURES if measure in fields]
    if len(chosen) != 1:
        raise PolicyError(f"[ratchet.{name}] must set exactly one measure of {list(_MEASURES)}; found {chosen}")
    measure = chosen[0]
    argument = _argument(name, measure, fields[measure])
    scope = fields.get("scope", [])
    if not isinstance(scope, list) or not all(isinstance(item, str) and item for item in cast("list[object]", scope)):
        raise PolicyError(f"[ratchet.{name}] `scope` must be a list of non-empty module globs")
    baseline = fields.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline.strip():
        raise PolicyError(f"[ratchet.{name}] `baseline` must be a non-empty path")
    return RatchetSpec(name, measure, argument, tuple(cast("list[str]", scope)), baseline)


def _argument(name: str, measure: str, value: object) -> tuple[str, ...]:
    if measure == "command":
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in cast("list[object]", value)):
            raise PolicyError(f"[ratchet.{name}] `command` must be a non-empty argument list, e.g. [\"python\", \"scan.py\"]")
        return tuple(cast("list[str]", value))
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"[ratchet.{name}] `{measure}` must be a non-empty string")
    if measure == "rule" and value not in {rule.value for rule in Rule}:
        raise PolicyError(f"[ratchet.{name}] unknown rule id {value!r}")
    if measure == "regex":
        try:
            re.compile(value)
        except re.error as exc:
            raise PolicyError(f"[ratchet.{name}] `regex` does not compile: {exc}") from exc
    return (value,)
