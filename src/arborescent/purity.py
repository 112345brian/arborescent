"""``mixed-purity`` (#173): a module that holds substantial pure logic next to effectful code is two modules.

A pure function of a pure module and the same function inside a module that also reads the clock, walks the
filesystem and logs are different things to an importer: the second drags every effect along. This module classifies a
module's top-level definitions from facts already extracted (``EffectUse`` for the existing effect categories, ``PurityFacts``
for logging, memo and module state, ``FunctionCalls`` for configured ambient lookups) and reports a module as mixed when
both sides are non-trivial. It is a pure function of facts and a policy: no second detector, no I/O.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from .domain import Policy, Rule, Violation, in_unit
from .facts import ModuleInfo, absolute_callee
from .purity_policy import AMBIENT, PurityPolicy

__all__ = ["ModulePurity", "assess", "by_ring", "excluded_findings", "importer_counts", "is_allowed", "summarize"]

_NON_PROPAGATING = frozenset({"os-branching", "patch"})  # a portability fact and a test-only use: neither is an effect of the module
IMPORT_TIME = "<import time>"


@dataclass(frozen=True)
class ModulePurity:
    """The per-module report: what the effects are, which definitions carry them, and which stay pure."""

    module: str
    kinds: tuple[str, ...]
    effectful: tuple[str, ...]  # definitions that use an effect directly or call one that does; ``<import time>`` for a module-level effect
    pure: tuple[str, ...]  # substantial pure definitions (the ones that count towards the threshold)
    line: int  # the first effectful line, where the finding is anchored
    importers: int = 0  # blast radius: modules in the scanned tree that import this one


def is_allowed(policy: PurityPolicy, module: str) -> bool:
    return any(in_unit(module, entry.module) for entry in policy.allow)


class _Tally:
    """Which effect kinds each top-level definition (and import time) uses, and the first line each does."""

    def __init__(self, info: ModuleInfo, wanted: Collection[str]) -> None:
        self.main_guard = info.facts.main_guard
        self.spans = [(u.line, u.end_line, u.name) for u in info.facts.purity.units]
        self.wanted = wanted
        self.kinds: dict[str, set[str]] = {u.name: set() for u in info.facts.purity.units}
        self.first: dict[str, int] = {}
        self.import_time: set[str] = set()

    def owner(self, line: int) -> str | None:
        return next((name for start, end, name in self.spans if start <= line <= end), None)

    def add(self, line: int, kind: str) -> None:
        if kind not in self.wanted:
            return
        owner = self.owner(line)
        if owner is None:
            if self.main_guard and line >= self.main_guard:
                return  # the `if __name__ == "__main__":` block runs as a program, not when the module is imported
            owner = IMPORT_TIME
            self.import_time.add(kind)
        else:
            self.kinds[owner].add(kind)
        self.first[owner] = min(self.first.get(owner, line), line)


def _tally(info: ModuleInfo, policy: Policy, wanted: Collection[str]) -> _Tally:
    tally = _Tally(info, wanted)
    facts = info.facts
    for use in facts.effect_uses:
        if use.category not in _NON_PROPAGATING and policy.counts_effect_seam(use.seam):
            tally.add(use.line, use.category)
    for unit in facts.purity.units:
        for line, kind in unit.kinds:
            tally.add(line, kind)
    for line, kind in facts.purity.import_time:
        tally.add(line, kind)
    for line, callee in facts.calls if policy.mixed_purity.ambient else ():
        target = absolute_callee(info.name, info.is_package, callee)  # relative imports resolved: `state_root()` is usually first-party
        if target is not None and any(fnmatch.fnmatchcase(target, pattern) for pattern in policy.mixed_purity.ambient):
            tally.add(line, AMBIENT)
    return tally


def _close_over_local_calls(info: ModuleInfo, kinds: dict[str, set[str]]) -> None:
    """A unit that calls an effectful sibling by name is effectful: it inherits that sibling's kinds."""
    calls = {u.name: u.calls for u in info.facts.purity.units}
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            for callee in callees:
                missing = kinds.get(callee, set()) - kinds[name]
                if missing:
                    kinds[name] |= missing
                    changed = True


def assess(info: ModuleInfo, policy: Policy) -> ModulePurity | None:
    """The module's report when it is mixed under ``policy.mixed_purity``, else ``None``. Allowlisting is the caller's business."""
    rule = policy.mixed_purity
    facts = info.facts
    if not rule.enabled or (not facts.purity.units and not facts.purity.import_time):
        return None
    tally = _tally(info, policy, set(rule.kinds) | ({AMBIENT} if rule.ambient else set()))
    _close_over_local_calls(info, tally.kinds)
    forwarding = {f.name for f in facts.forwarders}
    effectful = [u for u in facts.purity.units if tally.kinds[u.name]]
    pure = [
        u for u in facts.purity.units
        if not tally.kinds[u.name] and u.logic and u.body_lines >= rule.min_body_lines and u.name not in forwarding
    ]
    names = list(dict.fromkeys(u.name for u in effectful)) + ([IMPORT_TIME] if tally.import_time else [])  # a name defined twice (a fallback) is one definition
    pure_names = tuple(dict.fromkeys(u.name for u in pure))  # a fallback defined twice is one pure definition, not two
    if not names or len(pure_names) < rule.min_pure_units:
        return None
    kinds = tally.import_time.union(*(tally.kinds[u.name] for u in effectful))
    line = min((tally.first[name] for name in names if name in tally.first), default=0)
    return ModulePurity(info.name, tuple(sorted(kinds)), tuple(names), pure_names, line)


def by_ring(rows: Collection[tuple[ModulePurity, str, bool]]) -> list[tuple[str, list[tuple[ModulePurity, str, bool]]]]:
    """Ranked (report, ring, allowed) rows grouped by ring: rings in the order of their best-ranked row, rows keeping their rank."""
    groups: dict[str, list[tuple[ModulePurity, str, bool]]] = {}
    for row in rows:
        groups.setdefault(row[1], []).append(row)
    return list(groups.items())


def importer_counts(graph: Mapping[str, Collection[str]]) -> dict[str, int]:
    """module -> number of modules whose import graph edge points at it."""
    counts: dict[str, int] = {}
    for importer, targets in graph.items():
        for target in targets:
            if target != importer:
                counts[target] = counts.get(target, 0) + 1
    return counts


def summarize(report: ModulePurity, limit: int = 6) -> str:
    """The advice appended to a finding: everything a reader needs to rank it, kept out of the baseline key."""

    def names(items: tuple[str, ...]) -> str:
        shown = ", ".join(items[:limit])
        return shown + (f" (+{len(items) - limit} more)" if len(items) > limit else "")

    return (
        f" (effects: {', '.join(report.kinds)}; effectful: {names(report.effectful)}; pure: {names(report.pure)}; "
        f"imported by {report.importers} module{'s' if report.importers != 1 else ''}; "
        "split the pure logic into its own module, or allow it with a reason under `mixed_purity.allow`)"
    )



def excluded_findings(policy: Policy, modules: Mapping[str, ModuleInfo]) -> list[Violation]:
    """Mixed modules under an excluded path: reported as notes, never failures and never baselined, so the exclusion hides nothing silently."""
    found: list[Violation] = []
    for name, info in sorted(modules.items()):
        report = assess(info, policy)
        if report is not None:
            detail = f"{info.path} is excluded from the scan and mixes substantial pure logic with effectful code"
            found.append(Violation(Rule.MIXED_PURITY_EXCLUDED, name, detail, report.line, hint=summarize(report)))
    return found
