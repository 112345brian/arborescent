"""``effect-via-import``: a module inherits a forbidden effect from the modules it imports (#103).

The direct ``effect-*`` rules see only a module's own uses. A ring declared free of an effect can still reach it through a
helper that lives in a ring allowed to use it. This pass follows runtime import edges (not ``if TYPE_CHECKING`` ones; importing ``pkg.sub`` also runs ``pkg/__init__``) from each
module to the nearest module that uses the effect directly and reports the chain on the importer.

Limits, on purpose: module granularity (a helper that mixes pure and effectful functions taints every importer), and a module
whose ring forbids the effect is a barrier. It is either clean, or already reported on its own, so nothing is reported twice.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import NamedTuple

from .domain import EFFECT_RULES, Policy, Rule, Violation
from .facts import ModuleInfo, resolve_runtime_targets

__all__ = ["Lookup", "effect_via_import"]

_NOT_PROPAGATED = frozenset({"patch", "os-branching"})  # about tests and platform branches, not about what a module does at run time


@dataclass(frozen=True)
class _Reach:
    """The pieces `effect_via_import` needs, resolved once."""

    modules: Mapping[str, ModuleInfo]
    edges: Mapping[str, Mapping[str, int]]  # importer -> imported module -> line of the import
    direct: Mapping[str, Mapping[str, tuple[int, str]]]  # module -> category -> (line, target) of its first counted direct use
    forbidden: Mapping[str, frozenset[str]]  # module -> effect categories its ring forbids


class Lookup(NamedTuple):
    """How `effect_via_import` asks the caller about a module."""

    ring_of: Callable[[str], str | None]
    policy_of: Callable[[str], Policy | None]
    scope_of: Callable[[str], str | None]
    skip: Collection[str] = ()  # developer tooling: no ring, never a source or a barrier


def effect_via_import(modules: Mapping[str, ModuleInfo], known: Collection[str], lookup: Lookup) -> list[Violation]:
    """One finding per (module, category) that reaches a forbidden effect through its imports, naming the import chain."""
    skipped = frozenset(lookup.skip)
    reach = _Reach(
        modules,
        _edges(modules, known, lookup.scope_of, skipped),
        _direct(modules, lookup.policy_of),
        _forbidden(modules, lookup.ring_of, lookup.policy_of, skipped),
    )
    out: list[Violation] = []
    for name in sorted(modules):
        for category in sorted(reach.forbidden.get(name, frozenset())):
            if category in reach.direct.get(name, {}) or category in _NOT_PROPAGATED or category not in EFFECT_RULES:
                continue
            finding = _finding(reach, name, category, lookup.ring_of(name) or "")
            if finding is not None:
                out.append(finding)
    return out


def _edges(modules: Mapping[str, ModuleInfo], known: Collection[str], scope_of: Callable[[str], str | None], skipped: frozenset[str]) -> dict[str, dict[str, int]]:
    tops = {module.split(".")[0] for module in known}
    edges: dict[str, dict[str, int]] = {name: {} for name in modules}
    for name, info in modules.items():
        for imp in info.facts.imports:
            if imp.type_only:
                continue
            internal, _external, error = resolve_runtime_targets(name, info.is_package, imp, known, tops)
            if error is not None:
                continue
            for target in internal:
                if target in modules and target != name and target not in skipped and scope_of(target) == scope_of(name):
                    edges[name].setdefault(target, imp.line)
    return edges


def _direct(modules: Mapping[str, ModuleInfo], policy_of: Callable[[str], Policy | None]) -> dict[str, dict[str, tuple[int, str]]]:
    found: dict[str, dict[str, tuple[int, str]]] = {}
    for name, info in modules.items():
        policy = policy_of(name)
        if policy is None:
            continue
        for use in info.facts.effect_uses:
            if policy.counts_effect_seam(use.seam):
                found.setdefault(name, {}).setdefault(use.category, (use.line, use.target))
    return found


def _forbidden(
    modules: Mapping[str, ModuleInfo], ring_of: Callable[[str], str | None], policy_of: Callable[[str], Policy | None], skipped: frozenset[str],
) -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    for name in modules:
        ring, policy = ring_of(name), policy_of(name)
        if name not in skipped and ring is not None and policy is not None:
            out[name] = policy.forbidden_effects(ring)
    return out


def _finding(reach: _Reach, start: str, category: str, ring: str) -> Violation | None:
    chain = _nearest_chain(reach, start, category)
    if chain is None:
        return None
    source = chain[-1]
    line, target = reach.direct[source][category]
    path = " -> ".join(chain)
    detail = (
        f"[{ring}] effect {category} reached through imports: {path}; {source} uses {target} (line {line}). "
        f"Move the effect behind a port, or let ring {ring!r} use {category}"
    )
    return Violation(Rule.EFFECT_VIA_IMPORT, start, detail, reach.edges[start][chain[1]])


def _nearest_chain(reach: _Reach, start: str, category: str) -> list[str] | None:
    """Shortest import chain from ``start`` to a module that uses ``category`` directly, not passing through a ring that forbids it."""
    parents: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        for target in sorted(reach.edges.get(current, {})):
            if target in parents or category in reach.forbidden.get(target, frozenset()):
                continue  # a ring that forbids the effect is a barrier: clean, or reported on its own
            parents[target] = current
            if category in reach.direct.get(target, {}):
                return _walk_back(parents, target)
            queue.append(target)
    return None


def _walk_back(parents: Mapping[str, str | None], end: str) -> list[str]:
    chain: list[str] = []
    node: str | None = end
    while node is not None:
        chain.append(node)
        node = parents[node]
    return chain[::-1]
