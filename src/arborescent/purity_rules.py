"""The ``mixed-purity`` rule (#173) and the move-closure hint on ``internal-import`` findings (#133).

Split out of ``rules`` as its own cohesive concern: everything here is either about judging a module's purity mix
against the policy's allowlist, or about explaining what moving a misplaced module would drag along with it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import replace

from .domain import ALL, ArchitecturePolicy, Rule, Violation, in_unit
from .facts import ModuleInfo
from .import_facts import resolve_targets
from .purity import (
    Followed,
    ModulePurity,
    assess_in_ring,
    importer_counts,
    is_allowed,
    summarize,
)
from .rule_context import RuleContext

__all__ = ["mixed_purity_findings", "with_move_closures"]


def mixed_purity_findings(
    context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str], import_graph: Mapping[str, Collection[str]],
) -> list[Violation]:
    """A module holding substantial pure logic next to effectful code, in any ring (#173), plus stale allowlist entries.

    The detail carries no names or counts, so the baseline key stays stable as functions come and go; the effect kinds,
    the definitions on each side and the importer count (the blast radius) are in the hint."""
    importers = importer_counts(import_graph)
    followed = Followed(modules)
    out: list[Violation] = []
    mixed: set[str] = set()
    for name, info in sorted(modules.items()):
        local_policy, ring = context.policy_for(name), context.resolve_ring(name)
        report = assess_in_ring(info, local_policy, context.policy.mixed_purity, ring, followed) if local_policy is not None and ring is not None and name not in context.tool_modules else None
        if local_policy is None or ring is None or report is None:
            continue
        mixed.add(name)
        if not is_allowed(context.policy.mixed_purity, name):
            found = replace(report, importers=importers.get(name, 0))
            out.append(_purity_violation(ring, found))
    return out + _stale_purity_allow(context, modules, known, mixed)


def _purity_violation(ring: str, report: ModulePurity) -> Violation:
    if report.weak:
        detail = f"[{ring}] mixes substantial pure logic with code whose only effects are weak kinds"
        return Violation(Rule.MIXED_PURITY_WEAK, report.module, detail, report.line, hint=summarize(report))
    detail = f"[{ring}] mixes substantial pure logic with effectful code"
    return Violation(Rule.MIXED_PURITY, report.module, detail, report.line, hint=summarize(report))


def _stale_purity_allow(
    context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str], mixed: Collection[str],
) -> list[Violation]:
    """An allowlist entry that matches no module, or only modules that are not mixed: it would hide nothing, or hide a future regression."""
    out: list[Violation] = []
    rule = context.policy.mixed_purity  # root-only: every scope is judged by the same allowlist
    if not rule.enabled:
        return out
    groups = [[n for n in known if context.scope_name(n) == scope.name] for scope in context.policy.scopes] or [list(known)]
    for scope_known in groups:
        for entry in rule.allow:
            matches = [name for name in scope_known if in_unit(name, entry.module)]
            if not matches:
                out.append(Violation(Rule.MIXED_PURITY, entry.module, f"`mixed_purity.allow` {entry.module!r}: matches no module"))
            elif all(name in modules for name in matches) and not any(name in mixed for name in matches):
                out.append(Violation(
                    Rule.MIXED_PURITY, entry.module, f"`mixed_purity.allow` {entry.module!r}: no module it matches is mixed any more; remove the entry",
                ))
    return out


def with_move_closures(
    found: list[Violation], context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str], tops: Collection[str],
) -> list[Violation]:
    """Ring-mismatch ``internal-import`` findings gain the whole closure of what a move would drag along (#133)."""
    return [
        replace(v, hint=hint) if v.rule == Rule.INTERNAL_IMPORT and v.target and (hint := _move_closure(context, modules, known, tops, v)) else v
        for v in found
    ]


def _move_closure(context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str], tops: Collection[str], violation: Violation) -> str:
    name = violation.subject
    ring, policy = context.resolve_ring(name), context.policy_for(name)
    if ring is None or policy is None or " may import only " not in violation.detail:
        return ""
    move: list[str] = []
    externals: set[str] = set()
    seen, queue = {name}, [name]
    while queue:
        current = queue.pop()
        for imp in modules[current].facts.imports if current in modules else ():
            internal, external, _ = resolve_targets(current, modules[current].is_package, imp, known, tops)
            externals.update(_restricted_externals(policy, context.policy.stdlib, ring, external))
            for target in _outside_ring(context, modules, name, ring, internal):
                if target not in seen:
                    seen.add(target)
                    move.append(target)
                    queue.append(target)
    return _closure_text(name, ring, move, externals)


def _closure_text(name: str, ring: str, move: list[str], externals: set[str]) -> str:
    if not move and not externals:
        return ""
    parts = [f"to make {name} live in {ring}, also move: {sorted(move) or 'nothing'}"]
    if externals:
        parts.append(f"and allow externals: {sorted(externals)}")
    return " (" + "; ".join(parts) + ")"


def _restricted_externals(policy: ArchitecturePolicy, stdlib: Collection[str], ring: str, tops: Collection[str]) -> list[str]:
    allowed = policy.ring(ring).external
    return [top for top in tops if ALL not in allowed and top not in allowed and policy.restricts_external(top, top in stdlib)]


def _outside_ring(context: RuleContext, modules: Mapping[str, ModuleInfo], name: str, ring: str, targets: Collection[str]) -> list[str]:
    allowed = context.require_policy(name).allowed_rings(ring)
    return [
        t for t in targets
        if t in modules and context.scope_name(t) == context.scope_name(name) and context.resolve_ring(t) not in allowed
    ]
