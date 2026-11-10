"""Named ratchets (#147): per-module counters kept in one baseline format and only ever lowered.

A baseline file holds ``{"version": 1, "ratchets": {name: {module: count}}, "growth": [...]}``. ``check`` fails a module whose
count rose (a regression) or fell (stale: lower the baseline so the gain cannot be spent again). ``record`` creates or lowers
entries and refuses a rise unless it is attributed. A growth entry has the shape the numeric ratchet files use:
``{"key": "name|module", "reason": ..., "approved-by": ..., "from": n, "to": m}``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .baseline import BaselineError
from .domain import Policy, RatchetSpec
from .ports import FileSystem, ToolRunner
from .ratchet_measure import MeasureError, Subject, measure

__all__ = ["RatchetResult", "check", "growth_against", "growth_problems", "record"]

VERSION = 1
ADOPTED_BY = "arborescent ratchet"
Document = dict[str, dict[str, dict[str, int]] | list[object]]


@dataclass(frozen=True)
class RatchetResult:
    name: str
    failures: list[str]
    summary: str


def check(policy: Policy, subject: Subject, complete: bool = True) -> list[RatchetResult]:
    """Measure each ratchet and compare it with its baseline. ``complete`` is False for a partial (``--changed``) run,
    where a lower count proves nothing."""
    return [_check_one(spec, subject, complete) for spec in policy.ratchets]


def _check_one(spec: RatchetSpec, subject: Subject, complete: bool) -> RatchetResult:
    try:
        current = measure(spec, subject)
        recorded = _recorded(subject.fs, subject.root / spec.baseline).get(spec.name)
    except (MeasureError, ValueError) as exc:
        return RatchetResult(spec.name, [f"{spec.name}: {exc}"], "")
    summary = f"{spec.name}: {sum(current.values())} in {len(current)} module(s)"
    if recorded is None:
        return RatchetResult(spec.name, [f"{spec.name}: no baseline in {spec.baseline}; run `arborescent ratchet --name {spec.name}`"], summary)
    return RatchetResult(spec.name, _differences(spec.name, current, recorded, complete), summary)


def _differences(name: str, current: Mapping[str, int], recorded: Mapping[str, int], complete: bool) -> list[str]:
    lines = [f"{name}: {module}: {count} > baseline {recorded.get(module, 0)}" for module, count in current.items() if count > recorded.get(module, 0)]
    if complete:
        lines += [
            f"{name}: {module}: {current.get(module, 0)} < baseline {count}; run `arborescent ratchet --name {name}` to lower it"
            for module, count in recorded.items()
            if current.get(module, 0) < count
        ]
    return lines


def record(
    specs: Sequence[RatchetSpec], subject: Subject, *, allow_growth: bool = False, reason: str | None = None, approved_by: str | None = None
) -> list[str]:
    """Create or lower each ratchet's baseline. A rise needs ``allow_growth`` with a reason and an approver; the first
    recording of a ratchet is an adoption and logs itself. A file is written only after every ratchet in it is accepted."""
    _validate_options(allow_growth, reason, approved_by)
    attribution = ((reason or "").strip(), (approved_by or "").strip())
    summary: list[str] = []
    for path in sorted({spec.baseline for spec in specs}):
        document = _document(subject.fs, subject.root / path)
        ratchets, growth = _tables(document), list(_log(document))
        for spec in (s for s in specs if s.baseline == path):
            current = measure(spec, subject)
            growth += _entries(spec.name, ratchets.get(spec.name), current, attribution, allow_growth)
            ratchets[spec.name] = current
            summary.append(f"ratchet {spec.name}: {sum(current.values())} in {len(current)} module(s) recorded in {path}")
        subject.fs.write_json(subject.root / path, {"version": VERSION, "ratchets": dict(sorted(ratchets.items())), "growth": growth})
    return summary


def _validate_options(allow_growth: bool, reason: str | None, approved_by: str | None) -> None:
    if (reason is not None or approved_by is not None) and not allow_growth:
        raise BaselineError("`--reason` and `--approved-by` require `--allow-growth`")
    if allow_growth and not ((reason or "").strip() and (approved_by or "").strip()):
        raise BaselineError("`--allow-growth` requires a non-empty `--reason` and `--approved-by`")


def _entries(
    name: str, recorded: Mapping[str, int] | None, current: Mapping[str, int], attribution: tuple[str, str], allow_growth: bool
) -> list[dict[str, object]]:
    """The growth-log entries recording this ratchet adds; a refusal when it rose without permission."""
    if recorded is None:
        return [_entry(name, module, 0, count, (f"adopt ratchet {name}", ADOPTED_BY)) for module, count in current.items()]
    raised = {module: (recorded.get(module, 0), count) for module, count in current.items() if count > recorded.get(module, 0)}
    if raised and not allow_growth:
        listing = ", ".join(f"{module} {old}->{new}" for module, (old, new) in raised.items())
        raise BaselineError(f"ratchet {name} would rise ({listing}); fix the code, or use `--allow-growth --reason TEXT --approved-by WHO`")
    return [_entry(name, module, old, new, attribution) for module, (old, new) in raised.items()]


def _entry(name: str, module: str, old: int, new: int, attribution: tuple[str, str]) -> dict[str, object]:
    return {"key": f"{name}|{module}", "reason": attribution[0], "approved-by": attribution[1], "from": old, "to": new}


def _document(fs: FileSystem, path: Path) -> Document:
    """A ratchet baseline file as ``{"ratchets": ..., "growth": ...}``; a missing file is an empty one."""
    return _normalize(fs.read_json(path)) if fs.is_file(path) else {"ratchets": {}, "growth": []}


def _recorded(fs: FileSystem, path: Path) -> dict[str, dict[str, int]]:
    return _tables(_document(fs, path))


def _tables(document: Document) -> dict[str, dict[str, int]]:
    ratchets = document["ratchets"]
    if not isinstance(ratchets, dict):
        raise ValueError("`ratchets` must be an object")
    return dict(ratchets)


def _log(document: Document) -> list[object]:
    growth = document["growth"]
    if not isinstance(growth, list):
        raise ValueError("`growth` must be an array")
    return growth


def _normalize(data: object) -> Document:
    if not isinstance(data, dict):
        raise ValueError(f"a ratchet baseline must be a JSON object of version {VERSION}")
    raw = cast("dict[str, object]", data)
    if raw.get("version", VERSION) != VERSION:
        raise ValueError(f"a ratchet baseline must be a JSON object of version {VERSION}")
    ratchets, growth = raw.get("ratchets", {}), raw.get("growth", [])
    if not isinstance(ratchets, dict) or not isinstance(growth, list):
        raise ValueError("`ratchets` must be an object and `growth` an array")
    tables = cast("dict[str, object]", ratchets)
    return {"ratchets": {name: _counts(name, table) for name, table in tables.items()}, "growth": cast("list[object]", growth)}


def _counts(name: str, table: object) -> dict[str, int]:
    counts = cast("dict[str, object]", table) if isinstance(table, dict) else {}
    if not isinstance(table, dict) or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in counts.values()):
        raise ValueError(f"ratchet {name!r} must map modules to positive whole numbers")
    return {module: cast("int", n) for module, n in counts.items()}


def growth_against(ref: str, policy: Policy, root: Path, fs: FileSystem, runner: ToolRunner) -> list[str]:
    """Problems with how the ratchet baselines changed since git ``ref``: every added or raised count needs an
    attributed growth entry appended since. Empty when there is nothing to report."""
    problems: list[str] = []
    for path in sorted({spec.baseline for spec in policy.ratchets}):
        try:
            problems += [f"{path}: {p}" for p in growth_problems(_document(fs, root / path), _document_at(ref, path, root, runner))]
        except ValueError as exc:
            problems.append(f"{path}: {exc}")
    return problems


def _document_at(ref: str, path: str, root: Path, runner: ToolRunner) -> Document:
    listing = runner.run(["git", "ls-tree", "-r", "--name-only", ref, "--", path], root)
    if listing.returncode != 0:
        raise ValueError(f"could not read {ref}: {(listing.stderr or listing.stdout).strip()}")
    if not listing.stdout.strip():  # the file is new since ref: every count in it is an addition
        return {"ratchets": {}, "growth": []}
    shown = runner.run(["git", "show", f"{ref}:./{path}"], root)
    if shown.returncode != 0:
        raise ValueError(f"could not read {path} at {ref}: {(shown.stderr or shown.stdout).strip()}")
    return _normalize(json.loads(shown.stdout))


def growth_problems(current: Document, at_ref: Document) -> list[str]:
    """The ``name|module`` counts in ``current`` that rose or appeared since ``at_ref`` with no attributed entry appended since."""
    values, before = _flatten(current), _flatten(at_ref)
    log, ref_log = _log(current), _log(at_ref)
    if log[: len(ref_log)] != ref_log:
        return ["existing growth entries were rewritten or removed"]
    logged = _logged_keys(log[len(ref_log) :])
    return [
        f"{key}: added or raised ({before.get(key, 0)} -> {count}) without a growth entry with a reason and approved-by"
        for key, count in sorted(values.items())
        if count > before.get(key, 0) and key not in logged
    ]


def _logged_keys(entries: Sequence[object]) -> set[str]:
    """The ``key`` of each growth entry that names its reason and approver."""
    fields = [cast("dict[str, object]", entry) for entry in entries if isinstance(entry, dict)]
    return {str(entry["key"]) for entry in fields if isinstance(entry.get("key"), str) and _attributed(entry)}


def _flatten(document: Document) -> dict[str, int]:
    return {f"{name}|{module}": n for name, table in _tables(document).items() for module, n in table.items()}


def _attributed(entry: Mapping[str, object]) -> bool:
    return all(isinstance(entry.get(field), str) and str(entry[field]).strip() for field in ("reason", "approved-by"))
