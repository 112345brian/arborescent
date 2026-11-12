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

__all__ = ["Followed", "ModulePurity", "assess", "assess_in_ring", "by_ring", "excluded_findings", "explain", "importer_counts", "is_allowed", "ring_scanned", "summarize"]

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
    weak: bool = False  # every effect kind is one of ``mixed_purity.weak_kinds``: reported as a note, not a finding (#195)


def ring_scanned(policy: Policy, ring: str) -> bool:
    """Whether ``mixed-purity`` looks at modules of ``ring``: not in ``exclude_rings``, in ``rings`` when that is set, and (with
    ``skip_unrestricted_rings``) in a ring whose effective ``forbid`` is not empty."""
    rule = policy.mixed_purity
    if ring in rule.exclude_rings or (rule.rings and ring not in rule.rings):
        return False
    return not (rule.skip_unrestricted_rings and not policy.forbidden_effects(ring))


def assess_in_ring(info: ModuleInfo, policy: Policy, ring: str, followed: Followed | None = None) -> ModulePurity | None:
    """``assess``, except that a module in a ring the ring filter skips (``exclude_rings``, ``rings``, ``skip_unrestricted_rings``) is never mixed."""
    return assess(info, policy, followed) if ring_scanned(policy, ring) else None


def is_allowed(policy: PurityPolicy, module: str) -> bool:
    return any(in_unit(module, entry.module) for entry in policy.allow)


class Followed:
    """The scanned modules, so a call into another module's function can be followed one hop (#200).

    A function that calls ``save_text_atomic`` from a sibling module is as effectful as the wrapper it calls, whatever ring the
    wrapper lives in. Only a call to a *function* of a scanned module is followed, and only that function's own effects (its direct
    uses and its calls to siblings in its module), never the callee's own imports: one hop, like ``effect-via-import`` is one edge.
    A class is not followed (a constructor is not its methods) and a callee that is not in the scan is unknown, not pure."""

    def __init__(self, modules: Mapping[str, ModuleInfo]) -> None:
        self.modules = modules
        self._cache: dict[tuple[str, frozenset[str]], dict[str, set[str]]] = {}

    def split(self, target: str) -> tuple[str, str] | None:
        """(module, function) when ``target`` is ``module.function`` for a scanned module, else ``None``."""
        module, _, name = target.rpartition(".")
        return (module, name) if module in self.modules else None

    def kinds(self, policy: Policy, wanted: Collection[str], module: str, name: str) -> set[str]:
        key = (module, frozenset(wanted))
        if key not in self._cache:
            info = self.modules[module]
            tally = _tally(info, policy, wanted, None)
            _close_over_local_calls(info, tally.kinds)
            functions = {u.name for u in info.facts.purity.units if not u.is_class}
            self._cache[key] = {unit: kinds for unit, kinds in tally.kinds.items() if unit in functions}
        return self._cache[key].get(name, set())


class _Tally:
    """Which effect kinds each top-level definition (and import time) uses, and the first line each does."""

    def __init__(self, info: ModuleInfo, wanted: Collection[str]) -> None:
        self.main_guard = info.facts.main_guard
        self.spans = [(u.line, u.end_line, u.name) for u in info.facts.purity.units]
        self.wanted = wanted
        self.kinds: dict[str, set[str]] = {u.name: set() for u in info.facts.purity.units}
        self.first: dict[str, int] = {}
        self.import_time: set[str] = set()
        self.evidence: dict[str, list[tuple[int, str, str]]] = {}  # owner -> (line, kind, what), for ``--explain``

    def owner(self, line: int) -> str | None:
        return next((name for start, end, name in self.spans if start <= line <= end), None)

    def add(self, line: int, kind: str, what: str = "") -> None:
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
        self.evidence.setdefault(owner, []).append((line, kind, what))


def _tally(info: ModuleInfo, policy: Policy, wanted: Collection[str], followed: Followed | None) -> _Tally:
    tally = _Tally(info, wanted)
    facts = info.facts
    for use in facts.effect_uses:
        if use.category not in _NON_PROPAGATING and policy.counts_effect_seam(use.seam):
            tally.add(use.line, use.category, use.target)
    for unit in facts.purity.units:
        for line, kind in unit.kinds:
            tally.add(line, kind, kind)
    for line, kind in facts.purity.import_time:
        tally.add(line, kind, kind)
    _add_ambient(info, policy, tally)
    if followed is not None:
        _add_followed(info, policy, wanted, followed, tally)
    return tally


def _add_ambient(info: ModuleInfo, policy: Policy, tally: _Tally) -> None:
    patterns = policy.mixed_purity.ambient
    if not patterns:
        return
    calls = list(info.facts.calls)
    # `reader.read()` on a parameter annotated `Reader`: the call a port makes, named `<module of Reader>.Reader.read` (#195)
    calls += [(line, f"{info.name}.{cls}.{method}" if local else f"{cls}.{method}") for line, cls, method, local in info.facts.purity.port_calls]
    for line, callee in calls:
        target = absolute_callee(info.name, info.is_package, callee)  # relative imports resolved: `state_root()` is usually first-party
        if target is not None and any(fnmatch.fnmatchcase(target, pattern) for pattern in patterns):
            tally.add(line, AMBIENT, target)


def _add_followed(info: ModuleInfo, policy: Policy, wanted: Collection[str], followed: Followed, tally: _Tally) -> None:
    """The effects of the other-module functions this module calls (one hop), attributed to the calling line."""
    for line, callee in info.facts.calls:
        target = absolute_callee(info.name, info.is_package, callee)
        hop = followed.split(target) if target is not None else None
        if hop is not None and hop[0] != info.name:  # a call to a sibling of this module is closed over locally
            for kind in followed.kinds(policy, wanted, *hop):
                tally.add(line, kind, f"{target} (calls into {hop[0]})")


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


def assess(info: ModuleInfo, policy: Policy, followed: Followed | None = None) -> ModulePurity | None:
    """The module's report when it is mixed under ``policy.mixed_purity``, else ``None``. Allowlisting is the caller's business.

    ``followed``: the scanned modules, so a call into another module's effectful function counts (#200). Without it only a
    module's own text is read. Every caller that has the scan passes it, or the same module is judged differently by two reports."""
    rule = policy.mixed_purity
    facts = info.facts
    if not rule.enabled or (not facts.purity.units and not facts.purity.import_time):
        return None
    tally = _tally(info, policy, set(rule.kinds) | ({AMBIENT} if rule.ambient else set()), followed)
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
    weak = bool(rule.weak_kinds) and kinds <= set(rule.weak_kinds)
    return ModulePurity(info.name, tuple(sorted(kinds)), tuple(names), pure_names, line, weak=weak)


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


def explain(info: ModuleInfo, policy: Policy, followed: Followed | None = None) -> list[str]:
    """One line per top-level definition (and import time): the effect that made it effectful, or why it does not count as pure logic.

    The advice of a ``mixed-purity`` finding lists definitions as pure or effectful; this is the evidence behind each label, so a
    wrong one is easy to report. It answers for the module whether or not the module is mixed."""
    rule = policy.mixed_purity
    wanted = set(rule.kinds) | ({AMBIENT} if rule.ambient else set())
    tally = _tally(info, policy, wanted, followed)
    _close_over_local_calls(info, tally.kinds)
    forwarding = {f.name for f in info.facts.forwarders}
    out: list[str] = []
    for unit in info.facts.purity.units:
        found = sorted(tally.evidence.get(unit.name, ()))
        inherited = sorted(c for c in unit.calls if tally.kinds.get(c, set()))
        if found or inherited:
            reasons = [f"{kind} at line {line} ({what})" if what and what != kind else f"{kind} at line {line}" for line, kind, what in found]
            reasons += [f"calls {callee} in this module ({', '.join(sorted(tally.kinds[callee]))})" for callee in inherited]
            out.append(f"{unit.name}:{unit.line}  effectful: {'; '.join(reasons)}")
        elif unit.name in forwarding or not unit.logic or unit.body_lines < rule.min_body_lines:
            out.append(f"{unit.name}:{unit.line}  no effect found (too small, a stub or a forwarder to count as pure logic)")
        else:
            out.append(f"{unit.name}:{unit.line}  no effect found (pure)")
    for line, kind, what in sorted(tally.evidence.get(IMPORT_TIME, ())):
        out.append(f"{IMPORT_TIME}:{line}  effectful: {kind}" + (f" ({what})" if what and what != kind else ""))
    return out
