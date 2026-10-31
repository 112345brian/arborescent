"""Arborescent's own checks, as pure functions over already-parsed modules.

Rules (ids in ``domain.Rule``): ``unclassified``, ``internal-import``, ``external-import``, ``os-branching``,
``bad-import`` and ``undeclared-entry-dependency``. Lazy (function-level) and ``TYPE_CHECKING`` imports count. How much of the external
and OS-branching rules applies is the policy's preset; layering and classification are never relaxed.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass

from .domain import (
    ALL,
    EFFECT_RULES,
    Policy,
    PolicyError,
    RingRule,
    Rule,
    Violation,
    dist_name,
)
from .facts import ModuleInfo, resolve_targets

__all__ = ["check_modules"]

ThirdParty = dict[str, set[str]]  # distribution -> importing modules


@dataclass(frozen=True)
class RuleContext:
    policy: Policy
    arbor_index: Mapping[str, tuple[str | None, str | None]]
    scope_by_module: Mapping[str, str]

    def scope_name(self, name: str) -> str | None:
        return self.scope_by_module.get(name) if self.policy.scopes else None

    def policy_for(self, name: str) -> Policy | None:
        if not self.policy.scopes:
            return self.policy
        scope_name = self.scope_name(name)
        return self.policy.scope(scope_name).policy if scope_name is not None else None

    def require_policy(self, name: str) -> Policy:
        policy = self.policy_for(name)
        if policy is None:
            raise PolicyError(f"module {name!r} has no owning architecture scope")
        return policy

    def resolve_ring(self, name: str) -> str | None:
        policy = self.policy_for(name)
        return policy.resolve_ring(self.arbor_index, name) if policy is not None else None

    def annotation_violation(self, name: str) -> str | None:
        policy = self.policy_for(name)
        if policy is not None:
            return policy.annotation_violation(self.arbor_index, name)
        annotation = self.arbor_index.get(name)
        return annotation[1] if annotation is not None else None

    def annotation_ring_mismatch(self, name: str) -> str | None:
        policy = self.policy_for(name)
        return policy.annotation_ring_mismatch(self.arbor_index, name) if policy is not None else None

    def annotation_required_violation(self, name: str) -> str | None:
        policy = self.policy_for(name)
        return policy.annotation_required_violation(self.arbor_index, name) if policy is not None else None


def _internal_imports(
    context: RuleContext, name: str, ring: str, line: int, targets: Collection[str],
) -> list[Violation]:
    policy = context.policy_for(name)
    if policy is None:
        return []
    allowed = policy.allowed_rings(ring)
    out: list[Violation] = []
    source_scope = context.scope_name(name)
    for target in filter(None, targets):
        if context.scope_name(target) != source_scope:
            continue
        banned = policy.forbids_module_import(name, target)
        if banned is not None:
            out.append(Violation(Rule.INTERNAL_IMPORT, name, f"[{name}] imports forbidden module {target}; {banned!r} is forbidden by module policy", line))
            continue
        target_ring = context.resolve_ring(target)
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


def _target_ring(context: RuleContext, target: str, known: Collection[str]) -> str | None:
    parts = target.split(".")
    for end in range(len(parts), 0, -1):
        module = ".".join(parts[:end])
        if module in known:
            return context.resolve_ring(module)
    return None


def _effect_uses(
    context: RuleContext, name: str, ring: str, info: ModuleInfo, known: Collection[str],
) -> list[Violation]:
    policy = context.require_policy(name)
    out: list[Violation] = []
    tests_folder = policy.practices.get("secrets-guard")
    package_names = {module.split(".", 1)[0] for module in known}
    for use in info.facts.effect_uses:
        if use.category == "os-branching":
            continue  # preserve the original os_branching option and Rule id
        if use.category == "patch":
            if not isinstance(tests_folder, str) or not tests_folder.strip("/"):
                continue
            folder = tests_folder.replace("\\", "/").strip("/")
            path = info.path.replace("\\", "/")
            if not path.startswith(folder + "/"):
                continue
            if use.target.split(".", 1)[0] not in package_names:
                continue
            target_ring = _target_ring(context, use.target, known)
            if target_ring is None or "patch" not in policy.forbidden_effects(target_ring):
                continue
            detail = f"[{target_ring}] tests patch project target {use.target}; inject a port instead"
            out.append(Violation(EFFECT_RULES["patch"], name, detail, use.line))
            continue
        if use.category in policy.forbidden_effects(ring):
            rule = EFFECT_RULES.get(use.category)
            if rule is not None:
                detail = f"[{ring}] effect {use.category} uses {use.target}; inject a port instead"
                out.append(Violation(rule, name, detail, use.line))
    return out


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
        out.extend(
            Violation(Rule.UNDECLARED_ENTRY_DEPENDENCY, script, f"does not declare {dist!r} (imported by project code)")
            for dist in sorted(set(third_party) - have)
        )
    return out


def _scope_imports(context: RuleContext, source: str, line: int, targets: Collection[str]) -> list[Violation]:
    source_scope_name = context.scope_name(source)
    if source_scope_name is None:
        return []
    source_scope = context.policy.scope(source_scope_name)
    out: list[Violation] = []
    for target in filter(None, targets):
        target_scope_name = context.scope_name(target)
        if target_scope_name is None or target_scope_name == source_scope_name:
            continue
        target_scope = context.policy.scope(target_scope_name)
        reasons = []
        if (forbidden := source_scope.policy.forbids_module_import(source, target)) is not None:
            reasons.append(f"module {source!r} forbids importing {target!r} (matches {forbidden!r})")
        if target_scope_name not in source_scope.depends_on:
            reasons.append(f"scope {source_scope_name!r} does not declare dependency on {target_scope_name!r}")
        if not any(fnmatch.fnmatchcase(target, pattern) for pattern in target_scope.public):
            reasons.append(f"module {target!r} is not public in scope {target_scope_name!r}")
        if reasons:
            out.append(Violation(Rule.SCOPE_IMPORT, source, "; ".join(reasons), line))
    return out


def _scope_effect_leaks(
    context: RuleContext, modules: Mapping[str, ModuleInfo], source: str, line: int, targets: Collection[str],
) -> list[Violation]:
    """One-hop leak: a declared-public module in another scope uses an effect this scope's ring forbids."""
    source_scope_name = context.scope_name(source)
    if source_scope_name is None:
        return []
    source_ring = context.resolve_ring(source)
    if source_ring is None:
        return []
    forbidden = context.policy.scope(source_scope_name).policy.forbidden_effects(source_ring)
    if not forbidden:
        return []
    out: list[Violation] = []
    for target in filter(None, targets):
        target_scope_name = context.scope_name(target)
        if target_scope_name is None or target_scope_name == source_scope_name:
            continue
        target_scope = context.policy.scope(target_scope_name)
        if not any(fnmatch.fnmatchcase(target, pattern) for pattern in target_scope.public):
            continue
        info = modules.get(target)
        if info is None:
            continue
        seen: set[str] = set()
        for use in info.facts.effect_uses:
            if use.category not in forbidden or use.category in seen:
                continue
            seen.add(use.category)
            detail = (
                f"[{source_ring}] imports public module {target!r} from scope {target_scope_name!r}; "
                f"{target!r} uses effect {use.category} via {use.target}, forbidden for ring {source_ring!r}"
            )
            out.append(Violation(Rule.SCOPE_EFFECT_LEAK, source, detail, line))
    return out


def _wildcard_findings(policy: Policy) -> list[Violation]:
    scoped = ((scope.name, scope.policy) for scope in policy.scopes)
    local_policies = scoped if policy.scopes else (("", policy),)
    return [
        Violation(Rule.WILDCARD_RING, f"{scope_name}:{ring.name}" if scope_name else ring.name, axis)
        for scope_name, local_policy in local_policies
        for ring in local_policy.rings[:-1]
        for axis, names in (("external", ring.external), ("may_import", ring.may_import))
        if ALL in names
    ]


def check_modules(
    policy: Policy,
    modules: Mapping[str, ModuleInfo],
    known: Collection[str],
    entry_deps: Mapping[str, tuple[str, ...] | None],
    indexes: RuleContext | Mapping[str, tuple[str | None, str | None]],
) -> list[Violation]:
    context = indexes if isinstance(indexes, RuleContext) else RuleContext(policy, indexes, {})
    tops = {m.split(".")[0] for m in known}
    out = _wildcard_findings(policy)
    third_party_by_scope: dict[str | None, ThirdParty] = {}
    import_graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, info in sorted(modules.items()):
        local_policy = context.policy_for(name)
        detail = context.annotation_violation(name)
        if detail is not None:
            out.append(Violation(Rule.INVALID_ARBOR_ANNOTATION, name, detail))
        mismatch = context.annotation_ring_mismatch(name)
        if mismatch is not None:
            out.append(Violation(Rule.ARBOR_RING_MISMATCH, name, mismatch))
        ring = context.resolve_ring(name)
        if ring is None:
            out.append(Violation(Rule.UNCLASSIFIED, name, "module matches no [classify] pattern; every module needs a ring"))
            continue
        required = context.annotation_required_violation(name)
        if required is not None:
            out.append(Violation(Rule.UNANNOTATED_MODULE, name, required))
        if local_policy is None:
            continue
        rule = local_policy.ring(ring)
        third_party = third_party_by_scope.setdefault(context.scope_name(name), {})
        for imp in info.facts.imports:
            internal, external, error = resolve_targets(name, info.is_package, imp, known, tops)
            import_graph[name].update(
                target for target in internal
                if target in modules and context.scope_name(target) == context.scope_name(name)
            )
            if error is not None:
                out.append(Violation(Rule.BAD_IMPORT, name, error, imp.line))
            out += _scope_imports(context, name, imp.line, internal)
            out += _scope_effect_leaks(context, modules, name, imp.line, internal)
            out += _internal_imports(context, name, ring, imp.line, internal)
            out += _external_imports(local_policy, name, ring, rule, imp.line, external, third_party)
        out += _os_branching(local_policy, name, ring, rule, info)
        out += _effect_uses(context, name, ring, info, known)
    return out + _finalize_checks(context, known, entry_deps, import_graph, third_party_by_scope)


def _finalize_checks(
    context: RuleContext, known: Collection[str],
    entry_deps: Mapping[str, tuple[str, ...] | None], import_graph: Mapping[str, set[str]],
    third_party_by_scope: Mapping[str | None, ThirdParty],
) -> list[Violation]:
    policy = context.policy
    if not policy.scopes:
        circular = _circular_imports(import_graph) if policy.forbid_circular_imports else []
        return circular + _entry_dependencies(policy, known, entry_deps, third_party_by_scope.get(None, {}))
    out: list[Violation] = []
    for scope in policy.scopes:
        local_graph = {
            name: targets for name, targets in import_graph.items()
            if context.scope_name(name) == scope.name
        }
        if scope.policy.forbid_circular_imports:
            out.extend(_circular_imports(local_graph))
        out.extend(_entry_dependencies(scope.policy, known, entry_deps, third_party_by_scope.get(scope.name, {})))
    return out


def _circular_imports(graph: Mapping[str, Collection[str]]) -> list[Violation]:
    """Report each strongly connected import component, including direct self-imports.

    Iterative Kosaraju traversal avoids recursion limits on large repositories. Import edges include
    lazy and type-only imports because they still create architectural dependencies.
    """
    nodes = set(graph)
    edges = {node: set(targets) & nodes for node, targets in graph.items()}
    reverse: dict[str, set[str]] = {node: set() for node in nodes}
    for source, targets in edges.items():
        for target in targets:
            reverse[target].add(source)

    order = _finish_order(edges)
    components = _strong_components(order, reverse)
    return [
        Violation(Rule.CIRCULAR_IMPORT, ", ".join(sorted(component)), "modules form a circular import dependency")
        for component in components
        if len(component) > 1 or next(iter(component)) in edges[next(iter(component))]
    ]


def _finish_order(edges: Mapping[str, set[str]]) -> list[str]:
    """Return DFS finish order without relying on Python's recursion limit."""
    visited: set[str] = set()
    order: list[str] = []
    for start in sorted(edges):
        if start in visited:
            continue
        visited.add(start)
        pending: list[tuple[str, Iterator[str]]] = [(start, iter(sorted(edges[start])))]
        while pending:
            node, targets = pending[-1]
            try:
                target = next(targets)
            except StopIteration:
                pending.pop()
                order.append(node)
                continue
            if target not in visited:
                visited.add(target)
                pending.append((target, iter(sorted(edges[target]))))
    return order


def _strong_components(order: list[str], reverse: Mapping[str, set[str]]) -> list[set[str]]:
    """Collect the strongly connected components in reverse-graph finish order."""
    assigned: set[str] = set()
    components: list[set[str]] = []
    for start in reversed(order):
        if start in assigned:
            continue
        component: set[str] = set()
        pending = [start]
        assigned.add(start)
        while pending:
            node = pending.pop()
            component.add(node)
            for source in reverse[node]:
                if source not in assigned:
                    assigned.add(source)
                    pending.append(source)
        components.append(component)
    return components
