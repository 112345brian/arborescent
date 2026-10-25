"""ArchLint's own checks, as pure functions over already-parsed modules.

Rules (ids in ``domain.Rule``): ``unclassified``, ``internal-import``, ``external-import``, ``os-branching``,
``bad-import`` and ``undeclared-entry-dependency``. Lazy (function-level) and ``TYPE_CHECKING`` imports count. How much of the external
and OS-branching rules applies is the policy's preset; layering and classification are never relaxed.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from .domain import ALL, Policy, RingRule, Rule, Violation, dist_name
from .facts import ModuleInfo, resolve_targets

__all__ = ["check_modules"]

ThirdParty = dict[str, set[str]]  # distribution -> importing modules


def _internal_imports(policy: Policy, name: str, ring: str, line: int, targets: Collection[str]) -> list[Violation]:
    allowed = policy.allowed_rings(ring)
    out: list[Violation] = []
    for target in filter(None, targets):
        target_ring = policy.ring_of(target)
        if target_ring is not None and target_ring not in allowed:
            detail = f"[{ring}] imports {target} [{target_ring}]; {ring} may import only {sorted(allowed)}"
            out.append(Violation(Rule.INTERNAL_IMPORT, name, detail, line))
    return out


def _external_imports(
    policy: Policy, name: str, ring: str, rule: RingRule, line: int, tops: Collection[str], third_party: ThirdParty
) -> list[Violation]:
    out: list[Violation] = []
    for top in tops:
        is_stdlib = top in policy.stdlib
        if not is_stdlib:
            third_party.setdefault(dist_name(policy.dependencies.get(top, top)), set()).add(name)
        if ALL not in rule.external and top not in rule.external and policy.restricts_external(top, is_stdlib):
            detail = f"[{ring}] imports external package {top!r}; {ring} may import only {sorted(rule.external)}"
            out.append(Violation(Rule.EXTERNAL_IMPORT, name, detail, line))
    return out


def _os_branching(policy: Policy, name: str, ring: str, rule: RingRule, info: ModuleInfo) -> list[Violation]:
    if not policy.preset.os_branching or rule.os_branching:
        return []
    return [
        Violation(Rule.OS_BRANCHING, name, f"[{ring}] references {attr}; only permitted rings may branch on the OS", line)
        for line, attr in info.facts.os_uses
    ]


def _entry_dependencies(
    policy: Policy, known: Collection[str], entry_deps: Mapping[str, tuple[str, ...] | None], third_party: ThirdParty
) -> list[Violation]:
    out: list[Violation] = []
    for script in policy.entry_scripts:
        declared = entry_deps.get(script)
        if declared is None:
            if script in known:
                out.append(Violation(Rule.UNDECLARED_ENTRY_DEPENDENCY, script, "entry script has no `# /// script` block"))
            continue
        have = {dist_name(d) for d in declared}
        for dist in sorted(set(third_party) - have):
            out.append(Violation(Rule.UNDECLARED_ENTRY_DEPENDENCY, script, f"does not declare {dist!r} (imported by project code)"))
    return out


def check_modules(
    policy: Policy,
    modules: Mapping[str, ModuleInfo],
    known: Collection[str],
    entry_deps: Mapping[str, tuple[str, ...] | None],
) -> list[Violation]:
    tops = {m.split(".")[0] for m in known}
    out: list[Violation] = []
    third_party: ThirdParty = {}
    for name, info in sorted(modules.items()):
        ring = policy.ring_of(name)
        if ring is None:
            out.append(Violation(Rule.UNCLASSIFIED, name, "module matches no [classify] pattern; every module needs a ring"))
            continue
        rule = policy.ring(ring)
        for imp in info.facts.imports:
            internal, external, error = resolve_targets(name, info.is_package, imp, known, tops)
            if error:
                out.append(Violation(Rule.BAD_IMPORT, name, error, imp.line))
            out += _internal_imports(policy, name, ring, imp.line, internal)
            out += _external_imports(policy, name, ring, rule, imp.line, external, third_party)
        out += _os_branching(policy, name, ring, rule, info)
    return out + _entry_dependencies(policy, known, entry_deps, third_party)
