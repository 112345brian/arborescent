"""The shrink-only ratchet: a baseline file records today's violations; only NEW ones fail, and entries that no
longer occur must be deleted (``arborescent baseline`` rewrites the file), so the debt can only go down."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .domain import PolicyError, Rule, Violation
from .ports import FileSystem

__all__ = [
    "Ratchet", "apply", "cause_count", "growth_consistent", "growth_count", "internal_import_summary", "load", "load_root_causes",
    "prune", "record", "recorded_rules", "root_cause_groups", "save", "stamp_approval", "values_growth_problems",
]

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


def load_root_causes(fs: FileSystem, path: Path) -> dict[str, frozenset[str]]:
    """The per-root-cause entries (#201): root cause -> the fan-out members (importing modules, say) recorded under it."""
    return _root_causes(document(fs, path))


def recorded_rules(fs: FileSystem, path: Path) -> set[str]:
    """Every rule id the baseline holds an entry for, whether recorded per finding or per root cause."""
    return {key.split("|", 1)[0] for key in (*load(fs, path), *load_root_causes(fs, path))}


def _root_causes(doc: Mapping[str, object]) -> dict[str, frozenset[str]]:
    raw = doc.get("root_causes", {})
    if not isinstance(raw, dict):
        raise ValueError("baseline root_causes must be an object")
    out: dict[str, frozenset[str]] = {}
    for cause, members in raw.items():
        if not isinstance(members, list):
            raise ValueError(f"baseline root cause {cause!r} must list its members")
        out[str(cause)] = frozenset(str(member) for member in members)
    return out


def _member_id(cause: str, subject: str) -> str:
    """The growth-log name of one fan-out member of a root cause (it starts with the rule id, like a key)."""
    return f"{cause}|{subject}"


def _ids(doc: Mapping[str, object]) -> set[str]:
    """Everything a baseline records, as the names a growth log uses: plain keys and ``<root cause>|<member>``."""
    keys = doc.get("violations", [])
    plain = {str(key) for key in keys} if isinstance(keys, list) else set[str]()
    return plain | {_member_id(cause, str(member)) for cause, members in _root_causes(doc).items() for member in members}


def document(fs: FileSystem, path: Path) -> dict[str, object]:
    """Read a v1 or v2 baseline as a normalized document; ``root_causes`` appears only when the file has it."""
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
    doc: dict[str, object] = {"version": VERSION, "violations": [str(key) for key in violations], "growth": growth}
    if "root_causes" in data:
        _root_causes(data)  # reject a malformed section here, not later in apply
        doc["root_causes"] = data["root_causes"]
    return doc


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
    by_root_cause: bool = False,
) -> int:
    """Create or shrink a baseline; growth requires a one-time adoption or an explicit reason.

    ``by_root_cause`` records a finding that names a root cause as a member of that cause's entry instead of as its own key (#201).
    The layout is sticky: a finding already recorded under its own key stays there, and a cause already in the file keeps grouping."""
    _validate_record_options(adopt, allow_growth, reason)
    found = list({v.key: v for v in violations}.values())
    old_doc = document(fs, path)
    old_plain = {str(key) for key in _list(old_doc["violations"])}
    old_causes = _root_causes(old_doc)
    plain, grouped = _layout(found, old_plain, old_causes, by_root_cause)
    entries = _entries(plain, grouped)
    if not fs.is_file(path):
        fs.write_json(path, {"version": VERSION, "violations": entries["violations"], "growth": [], **_causes_part(entries)})
        return len(found)
    growth = [*_list(old_doc["growth"]), *_growth_entries(_ids(old_doc), _ids(entries), adopt, allow_growth, reason)]
    fs.write_json(path, {"version": VERSION, "violations": entries["violations"], "growth": growth, **_causes_part(entries)})
    return len(found)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("baseline violations and growth must be arrays")
    return list[object](value)


def _layout(
    found: Iterable[Violation], old_plain: Collection[str], old_causes: Mapping[str, Collection[str]], by_root_cause: bool,
) -> tuple[set[str], dict[str, set[str]]]:
    """Split findings into own-key entries and per-root-cause members (cause -> subjects)."""
    plain: set[str] = set()
    grouped: dict[str, set[str]] = {}
    for item in found:
        if item.root_cause and item.key not in old_plain and (by_root_cause or item.root_cause in old_causes):
            grouped.setdefault(item.root_cause, set()).add(item.subject)
        else:
            plain.add(item.key)
    return plain, grouped


def _entries(plain: Collection[str], grouped: Mapping[str, Collection[str]]) -> dict[str, object]:
    entries: dict[str, object] = {"violations": sorted(plain)}
    if grouped:
        entries["root_causes"] = {cause: sorted(members) for cause, members in sorted(grouped.items())}
    return entries


def _causes_part(entries: Mapping[str, object]) -> dict[str, object]:
    return {"root_causes": entries["root_causes"]} if "root_causes" in entries else {}


def prune(fs: FileSystem, path: Path, violations: Iterable[Violation]) -> int:
    """Drop baseline entries that no longer occur; never add one, never touch the growth log. Returns how many were removed.

    A root-cause entry loses the members that no longer occur, and goes when none is left."""
    if not fs.is_file(path):
        return 0
    found = list(violations)
    doc = document(fs, path)
    old = {str(key) for key in _list(doc["violations"])}
    kept = old & {v.key for v in found}
    causes = _root_causes(doc)
    now: dict[str, set[str]] = {}
    for item in found:
        if item.root_cause:
            now.setdefault(item.root_cause, set()).add(item.subject)
    kept_causes = {cause: members & now.get(cause, set()) for cause, members in causes.items()}
    kept_causes = {cause: members for cause, members in kept_causes.items() if members}
    removed = len(old - kept) + sum(len(members) for members in causes.values()) - sum(len(members) for members in kept_causes.values())
    if removed:
        pruned: dict[str, object] = {**doc, "violations": sorted(kept)}
        if "root_causes" in doc:
            pruned["root_causes"] = {cause: sorted(members) for cause, members in sorted(kept_causes.items())}
        fs.write_json(path, pruned)
    return removed


def _validate_record_options(adopt: str | None, allow_growth: bool, reason: str | None) -> None:
    if adopt is not None and allow_growth:
        raise BaselineError("`--adopt` and `--allow-growth` cannot be used together")
    if reason is not None and not allow_growth:
        raise BaselineError("`--reason` requires `--allow-growth`")
    if allow_growth and (reason is None or not reason.strip()):
        raise BaselineError("`--allow-growth` requires a non-empty `--reason`")


def _growth_entries(old: set[str], current: set[str], adopt: str | None, allow_growth: bool, reason: str | None) -> list[dict[str, object]]:
    """The growth-log entries a recording adds: none when the baseline only shrinks, or a refusal when growth is not allowed."""
    added = current - old
    if adopt is not None:
        return _adoption_entries(old, added, adopt)
    if added and not allow_growth:
        raise BaselineError(f"baseline growth refused; {len(added)} new violation(s); use `--allow-growth --reason TEXT`")
    if allow_growth and added:
        return [{"reason": reason.strip() if reason is not None else "", "added": sorted(added)}]
    return []


def _adoption_entries(old: set[str], added: set[str], adopt: str) -> list[dict[str, object]]:
    if any(in_family(key, adopt) for key in old):
        raise BaselineError(f"baseline already contains {adopt!r} family entries; adoption is allowed once per family")
    other_new = {key for key in added if not in_family(key, adopt)}
    if other_new:
        raise BaselineError(f"`--adopt {adopt}` cannot record findings outside that family: {', '.join(sorted(other_new))}")
    return [{"reason": f"adopt {adopt} family", "added": sorted(added)}] if added else []


def in_family(key: str, family: str) -> bool:
    rule = key.split("|", 1)[0]
    if family == "coverage":
        return rule in {"unclassified", "noise-dir-code"}
    if family == "effects":
        return rule.startswith(("effect-", "effects-")) or rule == "dynamic-import"
    if family == "suppressions":
        return rule == "suppression"
    return rule == family or rule.startswith(f"{family}-")


def growth_consistent(current: dict[str, object], at_ref: dict[str, object]) -> bool:
    """Check that every newly added key (or root-cause member) is listed in growth entries appended since ``at_ref``."""
    if not isinstance(current.get("violations", []), list) or not isinstance(at_ref.get("violations", []), list):
        return False
    try:
        current_keys, ref_keys = _ids(current), _ids(at_ref)  # plain keys and root-cause members (#201)
    except ValueError:
        return False
    current_log = current.get("growth", [])
    ref_log = at_ref.get("growth", [])
    if not isinstance(current_log, list) or not isinstance(ref_log, list) or current_log[: len(ref_log)] != ref_log:
        return False
    logged: set[str] = set()
    for entry in current_log[len(ref_log) :]:
        if not isinstance(entry, dict) or not _attributed(entry):
            return False
        keys = entry.get("added")
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            return False
        logged.update(keys)
    return current_keys - ref_keys == logged


def _attributed(entry: dict[str, object]) -> bool:
    """A growth entry needs a non-blank reason and a non-blank ``approved-by``."""
    return all(isinstance(entry.get(field), str) and str(entry[field]).strip() for field in ("reason", "approved-by"))


def growth_count(fs: FileSystem, path: Path) -> int:
    growth = document(fs, path)["growth"]
    return len(growth) if isinstance(growth, list) else 0


def stamp_approval(fs: FileSystem, path: Path, approved_by: str, since: int) -> None:
    """Record who approved the growth entries appended after the first ``since`` entries."""
    doc = document(fs, path)
    growth = doc["growth"]
    if not isinstance(growth, list):
        raise ValueError("baseline growth must be an array")
    stamped = [{**entry, "approved-by": approved_by.strip()} if i >= since and isinstance(entry, dict) else entry for i, entry in enumerate(growth)]
    fs.write_json(path, {**doc, "growth": stamped})


def values_growth_problems(current: dict[str, object], at_ref: dict[str, object]) -> list[str]:
    """Names in a numeric ratchet file (``{"values": {name: number}, "growth": [...]}``) that were added or raised
    since ``at_ref`` without an attributed growth entry for that name appended since."""
    values, before = current.get("values", {}), at_ref.get("values", {})
    log, ref_log = current.get("growth", []), at_ref.get("growth", [])
    if not isinstance(values, dict) or not isinstance(before, dict) or not isinstance(log, list) or not isinstance(ref_log, list):
        return ["file must hold a `values` object and a `growth` array"]
    if log[: len(ref_log)] != ref_log:
        return ["existing growth entries were rewritten or removed"]
    logged = {str(e["key"]) for e in log[len(ref_log) :] if isinstance(e, dict) and isinstance(e.get("key"), str) and _attributed(e)}
    problems: list[str] = []
    for name, value in sorted(values.items()):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            problems.append(f"{name}: value is not a number")
        elif name not in logged and (name not in before or not isinstance(before[name], (int, float)) or value > before[name]):
            problems.append(f"{name}: added or raised without a growth entry with a reason and approved-by")
    return problems


def in_any_family(key: str, families: Iterable[str]) -> bool:
    return any(in_family(key, family) for family in families)


def root_cause_groups(violations: Iterable[Violation]) -> list[tuple[str, list[Violation]]]:
    """Findings that name a root cause, grouped by it, widest fan-out first (ties by cause name). Others are not grouped."""
    groups: dict[str, list[Violation]] = {}
    for item in violations:
        if item.root_cause:
            groups.setdefault(item.root_cause, []).append(item)
    return sorted(groups.items(), key=lambda pair: (-len({v.subject for v in pair[1]}), pair[0]))


def cause_count(violations: Iterable[Violation]) -> int:
    """How many distinct root causes the findings come from; a finding naming none is its own cause."""
    return len({item.cause for item in violations})


def rule_summary(violations: Iterable[Violation], new: Iterable[Violation]) -> str | None:
    """One line splitting findings by rule id, with how many of each are not covered by the baseline (#137).

    A rule whose findings share root causes also says how many causes there are: ``effect-via-import 713 / 5 root causes``."""
    items = list(violations)
    totals = Counter(item.rule for item in items)
    if not totals:
        return None
    fresh = Counter(item.rule for item in new)
    causes = {rule: cause_count(item for item in items if item.rule == rule) for rule in totals}

    def label(rule: str) -> str:
        shared = f" / {causes[rule]} root cause{'' if causes[rule] == 1 else 's'}" if causes[rule] != totals[rule] else ""
        return f"{rule} {totals[rule]}{shared} ({fresh[rule]} new)"

    return "by rule: " + ", ".join(label(rule) for rule in sorted(totals))


def internal_import_summary(baselined: Iterable[Violation]) -> str | None:
    """One line sizing a shrink-only import cap in modules, not edges: ``from pkg import mod`` records two entries."""
    entries = {v.key: v for v in baselined if v.rule == Rule.INTERNAL_IMPORT and v.target}
    if not entries:
        return None
    importers = {v.subject for v in entries.values()}
    targets = {v.target for v in entries.values()}

    def count(n: int, noun: str, plural: str) -> str:
        return f"{n} {noun if n == 1 else plural}"

    return (
        f"baselined internal-import: {count(len(entries), 'entry', 'entries')}, "
        f"{count(len(importers), 'importing module', 'importing modules')}, {count(len(targets), 'distinct target', 'distinct targets')}"
    )


def apply(
    violations: Iterable[Violation], baseline: set[str], complete: bool = True, root_causes: Mapping[str, Collection[str]] | None = None,
) -> Ratchet:
    """``complete`` is False for a partial (``--changed``) run, where an absent entry proves nothing.

    ``root_causes`` maps a recorded root cause to its fan-out members (#201). A finding under a recorded cause is covered
    while its subject is among them; a subject the cause gained since is new, like any finding the baseline lacks."""
    current = list(violations)
    causes = root_causes or {}

    def covered(item: Violation) -> bool:
        return item.key in baseline or (bool(item.root_cause) and item.subject in causes.get(item.root_cause, ()))

    new = [v for v in current if not covered(v)]
    keys = {v.key for v in current}
    stale = sorted(baseline - keys) if complete else []
    if complete:
        stale = sorted([*stale, *_stale_causes(current, causes)])
    return Ratchet(new, stale, len({v.key for v in current if covered(v)}))


def _stale_causes(current: list[Violation], causes: Mapping[str, Collection[str]]) -> list[str]:
    """One line per recorded root cause with members that no longer occur (the whole cause when none does)."""
    now: dict[str, set[str]] = {}
    for item in current:
        if item.root_cause:
            now.setdefault(item.root_cause, set()).add(item.subject)
    out: list[str] = []
    for cause, members in causes.items():
        gone = sorted(set(members) - now.get(cause, set()))
        if gone:
            shown = ", ".join(gone[:3]) + (", ..." if len(gone) > 3 else "")
            out.append(f"{cause} ({'all ' if len(gone) == len(members) else ''}{len(gone)} of {len(members)} recorded fan-out member(s) gone: {shown})")
    return out
