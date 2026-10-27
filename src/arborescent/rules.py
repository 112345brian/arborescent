"""Arborescent's own checks, as pure functions over already-parsed modules.

Rules (ids in ``domain.Rule``): ``unclassified``, ``internal-import``, ``external-import``, ``os-branching``,
``bad-import`` and ``undeclared-entry-dependency``. Lazy (function-level) and ``TYPE_CHECKING`` imports count. How much of the external
and OS-branching rules applies is the policy's preset; layering and classification are never relaxed.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping

from .domain import ALL, EFFECT_RULES, Policy, RingRule, Rule, Violation, dist_name
from .facts import ModuleInfo, resolve_targets

__all__ = ["check_modules"]

ThirdParty = dict[str, set[str]]  # distribution -> importing modules


def _internal_imports(policy: Policy, name: str, ring: str, line: int, targets: Collection[str]) -> list[Violation]:
    allowed = policy.allowed_rings(ring)
    out: list[Violation] = []
    for target in filter(None, targets):
        banned = policy.forbids_module_import(name, target)
        if banned is not None:
            out.append(Violation(Rule.INTERNAL_IMPORT, name, f"[{name}] imports forbidden module {target}; {banned!r} is forbidden by module policy", line))
            continue
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


def _target_ring(policy: Policy, target: str) -> str | None:
    parts = target.split(".")
    for end in range(len(parts), 0, -1):
        ring = policy.ring_of(".".join(parts[:end]))
        if ring is not None:
            return ring
    return None


def _effect_uses(policy: Policy, name: str, ring: str, info: ModuleInfo, known: Collection[str]) -> list[Violation]:
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
            target_ring = _target_ring(policy, use.target)
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


def check_modules(
    policy: Policy,
    modules: Mapping[str, ModuleInfo],
    known: Collection[str],
    entry_deps: Mapping[str, tuple[str, ...] | None],
) -> list[Violation]:
    tops = {m.split(".")[0] for m in known}
    out = [
        Violation(Rule.WILDCARD_RING, ring.name, axis)
        for ring in policy.rings[:-1]
        for axis, names in (("external", ring.external), ("may_import", ring.may_import))
        if ALL in names
    ]
    third_party: ThirdParty = {}
    import_graph: dict[str, set[str]] = {name: set() for name in modules}
    for name, info in sorted(modules.items()):
        ring = policy.ring_of(name)
        if ring is None:
            out.append(Violation(Rule.UNCLASSIFIED, name, "module matches no [classify] pattern; every module needs a ring"))
            continue
        rule = policy.ring(ring)
        for imp in info.facts.imports:
            internal, external, error = resolve_targets(name, info.is_package, imp, known, tops)
            import_graph[name].update(target for target in internal if target in modules)
            if error is not None:
                out.append(Violation(Rule.BAD_IMPORT, name, error, imp.line))
            out += _internal_imports(policy, name, ring, imp.line, internal)
            out += _external_imports(policy, name, ring, rule, imp.line, external, third_party)
        out += _os_branching(policy, name, ring, rule, info)
        out += _effect_uses(policy, name, ring, info, known)
    if policy.forbid_circular_imports:
        out.extend(_circular_imports(import_graph))
    return out + _entry_dependencies(policy, known, entry_deps, third_party)


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
