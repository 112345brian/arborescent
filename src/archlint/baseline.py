"""The shrink-only ratchet: a baseline file records today's violations; only NEW ones fail, and entries that no
longer occur must be deleted (``archlint baseline`` rewrites the file), so the debt can only go down."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .domain import PolicyError, Violation
from .ports import FileSystem

__all__ = ["Ratchet", "apply", "growth_consistent", "load", "record", "save"]

VERSION = 2


class BaselineError(PolicyError):
    """A requested baseline change violates the ratchet contract."""


@dataclass(frozen=True)
class Ratchet:
    new: list[Violation]  # not in the baseline: fail
    stale: list[str]  # in the baseline but gone: fail (shrink-only)
    tolerated: int  # in the baseline and still present

    @property
    def ok(self) -> bool:
        return not self.new and not self.stale


def load(fs: FileSystem, path: Path) -> set[str]:
    violations = document(fs, path)["violations"]
    if not isinstance(violations, list):
        raise ValueError("baseline violations must be an array")
    return {str(k) for k in violations}


def document(fs: FileSystem, path: Path) -> dict[str, object]:
    """Read a v1 or v2 baseline as a normalized document."""
    if not fs.is_file(path):
        return {"version": VERSION, "violations": [], "growth": []}
    data = fs.read_json(path)
    if not isinstance(data, dict):
        raise ValueError("baseline must be a JSON object")
    version = data.get("version", 1)
    if version not in (1, VERSION):
        raise ValueError(f"unsupported baseline version {version!r}")
    violations = data.get("violations", [])
    growth = data.get("growth", [])
    if not isinstance(violations, list) or not isinstance(growth, list):
        raise ValueError("baseline violations and growth must be arrays")
    return {"version": VERSION, "violations": [str(key) for key in violations], "growth": growth}


def save(fs: FileSystem, path: Path, violations: Iterable[Violation]) -> int:
    keys = sorted({v.key for v in violations})
    fs.write_json(path, {"version": VERSION, "violations": keys, "growth": []})
    return len(keys)


def record(
    fs: FileSystem,
    path: Path,
    violations: Iterable[Violation],
    *,
    adopt: str | None = None,
    allow_growth: bool = False,
    reason: str | None = None,
) -> int:
    """Create or shrink a baseline; growth requires a one-time adoption or an explicit reason."""
    if adopt is not None and allow_growth:
        raise BaselineError("`--adopt` and `--allow-growth` cannot be used together")
    if reason is not None and not allow_growth:
        raise BaselineError("`--reason` requires `--allow-growth`")
    if allow_growth and (reason is None or not reason.strip()):
        raise BaselineError("`--allow-growth` requires a non-empty `--reason`")
    current = {v.key for v in violations}
    if not fs.is_file(path):
        fs.write_json(path, {"version": VERSION, "violations": sorted(current), "growth": []})
        return len(current)

    old_doc = document(fs, path)
    old_violations = old_doc["violations"]
    existing_growth = old_doc["growth"]
    if not isinstance(old_violations, list) or not isinstance(existing_growth, list):
        raise ValueError("baseline violations and growth must be arrays")
    old = {str(key) for key in old_violations}
    growth = list(existing_growth)
    added = current - old
    if adopt is not None:
        family_old = {key for key in old if _in_family(key, adopt)}
        if family_old:
            raise BaselineError(f"baseline already contains {adopt!r} family entries; adoption is allowed once per family")
        other_new = {key for key in added if not _in_family(key, adopt)}
        if other_new:
            raise BaselineError(f"`--adopt {adopt}` cannot record findings outside that family: {', '.join(sorted(other_new))}")
        added = current - old
        if added:
            growth.append({"reason": f"adopt {adopt} family", "added": sorted(added)})
    elif added and not allow_growth:
        raise BaselineError(f"baseline growth refused; {len(added)} new violation(s); use `--allow-growth --reason TEXT`")
    if allow_growth and added:
        growth.append({"reason": reason.strip() if reason is not None else "", "added": sorted(added)})
    fs.write_json(path, {"version": VERSION, "violations": sorted(current), "growth": growth})
    return len(current)


def _in_family(key: str, family: str) -> bool:
    rule = key.split("|", 1)[0]
    if family == "coverage":
        return rule in {"unclassified", "noise-dir-code"}
    if family == "effects":
        return rule.startswith(("effect-", "effects-")) or rule == "dynamic-import"
    if family == "suppressions":
        return rule == "suppression"
    return rule == family or rule.startswith(f"{family}-")


def growth_consistent(current: dict[str, object], at_ref: dict[str, object]) -> bool:
    """Check that every newly added key is listed in growth entries appended since ``at_ref``."""
    current_violations = current.get("violations", [])
    ref_violations = at_ref.get("violations", [])
    if not isinstance(current_violations, list) or not isinstance(ref_violations, list):
        return False
    current_keys = {str(key) for key in current_violations}
    ref_keys = {str(key) for key in ref_violations}
    current_log = current.get("growth", [])
    ref_log = at_ref.get("growth", [])
    if not isinstance(current_log, list) or not isinstance(ref_log, list) or current_log[: len(ref_log)] != ref_log:
        return False
    logged: set[str] = set()
    for entry in current_log[len(ref_log) :]:
        if not isinstance(entry, dict) or not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            return False
        keys = entry.get("added")
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            return False
        logged.update(keys)
    return current_keys - ref_keys == logged


def apply(violations: Iterable[Violation], baseline: set[str], complete: bool = True) -> Ratchet:
    """``complete`` is False for a partial (``--changed``) run, where an absent entry proves nothing."""
    current = list(violations)
    keys = {v.key for v in current}
    new = [v for v in current if v.key not in baseline]
    stale = sorted(baseline - keys) if complete else []
    return Ratchet(new, stale, len(keys & baseline))
