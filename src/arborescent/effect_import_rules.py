"""The ``effect-unsanctioned-import``, ``effect-via-import`` and ``effect-at-import`` rules: whether a forbidden effect
reaches a module through what it imports, directly or transitively, whether an import of a provider-ring module is through
a listed seam, and whether importing a module runs an effect its ring bans at import.

Split out of ``rules`` as its own cohesive concern. ``effect-at-import`` (#230) takes its direct uses from the effect facts
(``EffectUse.at_import``) and follows a call made at import one hop, to what that function runs itself, never what it calls
in turn.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import NamedTuple

from .domain import ArchitecturePolicy, Rule, Violation, in_unit
from .effects_policy import EffectsPolicy
from .facts import ImportCall, ModuleInfo
from .import_syntax import RawImport
from .package_constraint_rules import is_exempt_path, reach_violations
from .rule_context import RuleContext

__all__ = [
    "EffectEdge", "EffectImport", "direct_propagating_effects", "effect_at_import_findings", "effect_via_import_violations", "is_sanctioned",
    "record_effect_edge", "unsanctioned_effect_imports",
]

_NON_PROPAGATING_EFFECTS = frozenset({"os-branching", "patch"})  # not a ring effect, or meaningless once removed from its own module


class EffectEdge(NamedTuple):
    """One import edge a forbidden effect could propagate along: ``name``'s policy forbidding it in ``ring``."""

    policy: ArchitecturePolicy
    name: str
    ring: str
    line: int
    targets: list[str]


class EffectImport(NamedTuple):
    policy: ArchitecturePolicy
    name: str
    ring: str
    imp: RawImport
    internal: list[str]


def record_effect_edge(
    context: RuleContext, effect_graph: dict[str, set[str]], pending: list[EffectEdge], edge: EffectImport,
) -> None:
    """A type-only import never runs, so it carries no effect; everything else, within the same scope, does."""
    if edge.imp.type_only:
        return
    targets = [
        target for target in edge.internal
        if target in effect_graph and target != edge.name and context.scope_name(target) == context.scope_name(edge.name)
    ]
    effect_graph[edge.name].update(targets)
    pending.append(EffectEdge(edge.policy, edge.name, edge.ring, edge.imp.line, targets))


def is_sanctioned(effects: EffectsPolicy, module: str) -> bool:
    """Whether ``effects.sanctioned`` names this module as a capability seam: its own direct uses stop at it."""
    return any(in_unit(module, pattern) for pattern in effects.sanctioned)


def unsanctioned_effect_imports(context: RuleContext, modules: Mapping[str, ModuleInfo], edge: EffectImport) -> list[Violation]:
    """The guard that makes ``effects.sanctioned`` safe: a ring that forbids effects imports a provider-ring module only if it is a listed seam.

    Off unless the policy sets ``effects.sanctioned``. Type-only imports never run and are not checked. A target with no measured
    direct effects of its own (a pure module that ``admit`` steers into the effects ring) is not a seam and needs no listing (#202);
    what it imports still reaches the importer through ``effect-via-import``."""
    effects = context.policy.effects
    if not effects.configured or edge.imp.type_only or not edge.policy.forbidden_effects(edge.ring):
        return []
    out: list[Violation] = []
    for target in sorted(set(edge.internal)):
        target_ring = context.resolve_ring(target)
        if target_ring not in effects.rings or context.scope_name(target) != context.scope_name(edge.name) or is_sanctioned(effects, target):
            continue
        target_policy = context.policy_for(target)
        if target in modules and target_policy is not None and not direct_propagating_effects(effects, target_policy, modules[target]):
            continue
        detail = (
            f"[{edge.ring}] imports {target!r}, a module of the {target_ring!r} ring that is not listed in `effects.sanctioned`; "
            "reach the effect through a sanctioned seam, or list the module if it is one"
        )
        out.append(Violation(Rule.EFFECT_UNSANCTIONED_IMPORT, edge.name, detail, edge.imp.line))
    return out


def direct_propagating_effects(effects: EffectsPolicy, policy: ArchitecturePolicy, info: ModuleInfo) -> frozenset[str]:
    if is_sanctioned(effects, info.name):
        return frozenset[str]()
    return frozenset(
        use.category for use in info.facts.effect_uses
        if use.category not in _NON_PROPAGATING_EFFECTS and policy.counts_effect_seam(use.seam)
    )


def effect_via_import_violations(
    context: RuleContext, modules: Mapping[str, ModuleInfo], effect_graph: Mapping[str, Collection[str]], edges: Collection[EffectEdge],
) -> list[Violation]:
    reachable = _effect_reachable(context, modules, effect_graph)
    out: list[Violation] = []
    constraints = context.policy.package_constraints
    for edge in edges:
        out += _effect_via_import(edge, reachable)
        if constraints.must_be_pure and not is_exempt_path(modules[edge.name].path):
            out += reach_violations(constraints, edge.name, edge.line, edge.targets, reachable)
    return out


def _effect_reachable(
    context: RuleContext, modules: Mapping[str, ModuleInfo], effect_graph: Mapping[str, Collection[str]],
) -> dict[str, dict[str, tuple[str, ...]]]:
    """module -> {effect category: the chain of modules, starting with itself, down to the one that directly uses it}.

    Only edges in ``effect_graph`` (first-party, non-type-only, same scope) propagate. A cycle stops recursion rather
    than looping; the modules on it still get whatever their own direct effects and acyclic imports contribute."""
    memo: dict[str, dict[str, tuple[str, ...]]] = {}
    visiting: set[str] = set()

    def compute(name: str) -> dict[str, tuple[str, ...]]:
        if name in memo:
            return memo[name]
        if name in visiting:
            return {}
        visiting.add(name)
        info = modules.get(name)
        local_policy = context.policy_for(name)
        result: dict[str, tuple[str, ...]] = (
            dict.fromkeys(direct_propagating_effects(context.policy.effects, local_policy, info), (name,))
            if local_policy is not None and info is not None else {}
        )
        for target in sorted(effect_graph.get(name, ())):
            for category, chain in compute(target).items():
                if category not in result:
                    result[category] = (name, *chain)
        visiting.discard(name)
        memo[name] = result
        return result

    return {name: compute(name) for name in modules}


def _effect_via_import(edge: EffectEdge, reachable: Mapping[str, Mapping[str, tuple[str, ...]]]) -> list[Violation]:
    forbidden = edge.policy.forbidden_effects(edge.ring)
    if not forbidden:
        return []
    out: list[Violation] = []
    for target in edge.targets:
        seen: set[str] = set()
        for category, chain in reachable.get(target, {}).items():
            if category not in forbidden or category in seen:
                continue
            seen.add(category)
            detail = f"[{edge.ring}] imports {target!r}; effect {category} reaches it via {' -> '.join(chain)}; inject a port instead"
            out.append(Violation(Rule.EFFECT_VIA_IMPORT, edge.name, detail, edge.line))
    return out


# Calling a class runs these.
_CONSTRUCTORS = ("__new__", "__init__", "__post_init__")
_REEXPORT_DEPTH = 5  # a chain of package re-exports is not a function hop, but it has to end

# What a one-hop call may be followed into, and `call_facts.absolute_callee`, handed in by `rules`, which already imports it.
Followable = tuple[Mapping[str, ModuleInfo], Callable[[str, bool, str], str | None]]
_Reached = tuple[str, list[tuple[str, str]]]  # (dotted callee, the (category, target) it runs itself)


def effect_at_import_findings(context: RuleContext, ring: str, info: ModuleInfo, followable: Followable) -> list[Violation]:
    """``effect-at-import`` (#230): a category ``ring`` lists in ``forbid_at_import`` runs while ``info`` is imported."""
    name = info.name
    forbidden = context.require_policy(name).ring(ring).forbid_at_import
    if not forbidden:
        return []
    out = [
        Violation(Rule.EFFECT_AT_IMPORT, name, f"[{ring}] effect {use.category} uses {use.target} at import time; move it into a function", use.line)
        for use in info.facts.effect_uses
        if use.at_import and use.category in forbidden
    ]
    for call in info.facts.import_time.calls:
        found = _resolve_call(info, call, followable)
        if found is None:
            continue
        callee, effects = found
        for category in sorted({category for category, _ in effects if category in forbidden}):
            targets = ", ".join(sorted({target for kind, target in effects if kind == category}))
            detail = f"[{ring}] import-time call of {callee} runs effect {category}; move it into a function"
            out.append(Violation(Rule.EFFECT_AT_IMPORT, name, detail, call.line, hint=f" (it uses {targets})"))
    return out


def _resolve_call(info: ModuleInfo, call: ImportCall, followable: Followable) -> _Reached | None:
    """What a call into a scanned module runs; ``None`` when it runs nothing we can see."""
    if call.local:
        effects = _own_effects(info, call.callee)
        return (f"{info.name}.{call.callee}", effects) if effects else None
    dotted = followable[1](info.name, info.is_package, call.callee)
    return None if dotted is None else _follow(dotted, followable, _REEXPORT_DEPTH)


def _follow(dotted: str, followable: Followable, depth: int) -> _Reached | None:
    modules, absolute = followable
    parts = dotted.split(".")
    cut = next((end for end in range(len(parts) - 1, 0, -1) if ".".join(parts[:end]) in modules), None)
    if cut is None:
        return None
    module = modules[".".join(parts[:cut])]
    head, rest = parts[cut], parts[cut + 1:]
    effects = _own_effects(module, ".".join(parts[cut:]))
    if effects:
        return dotted, effects
    if depth == 0 or any(member.name == head for member in module.facts.top_level_defs):
        return None
    for surface, level, source, imported in module.facts.reexports:
        if surface == head:
            origin = "." * level + ".".join(part for part in (source, imported, *rest) if part)
            target = absolute(module.name, module.is_package, origin)
            return None if target is None else _follow(target, followable, depth - 1)
    return None


def _own_effects(info: ModuleInfo, qualname: str) -> list[tuple[str, str]]:
    names = {qualname}
    if "." not in qualname:
        names.update(f"{qualname}.{method}" for method in _CONSTRUCTORS)
    return [(category, target) for function, category, target in info.facts.import_time.function_effects if function in names]
