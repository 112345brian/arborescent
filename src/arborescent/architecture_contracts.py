"""The policy compiled into the neutral contracts every graph-analysis engine is configured from.

The decisions live here once: which ring may import which, what a ring may not import from outside, what a scope exposes,
what a package boundary adds. How a particular tool spells them is the engine adapter's business (``engines``).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Mapping, Sequence

from .domain import ALL, Allowance, ArchitectureContracts, Contract, ModuleLayer, Policy
from .ports import PackageBoundaries
from .rules import RuleContext

__all__ = ["compile_contracts", "leaves", "tops_of"]

Index = Mapping[str, tuple[str | None, str | None]]


def leaves(names: Collection[str]) -> list[str]:
    """Modules that are not the parent package of another module: the tools' module paths cover descendants,
    so a package that has children cannot be targeted on its own (its ``__init__`` ring is checked by Arborescent)."""
    every = set(names)
    return sorted(n for n in every if not any(o.startswith(n + ".") for o in every))


def tops_of(names: Collection[str]) -> list[str]:
    """Importable packages only: import-linter rejects a single-file module (an entry script) as a root package."""
    return sorted({n.split(".")[0] for n in names if "." in n})


def scope_layer(scope_name: str, ring: str) -> str:
    return f"{scope_name}__{ring}"


def compile_contracts(
    policy: Policy, names: Sequence[str], arbor_index: Index,
    scope_by_module: Mapping[str, str], packages: PackageBoundaries | None,
) -> ArchitectureContracts:
    context = RuleContext(policy, arbor_index, scope_by_module)
    module_leaves = leaves(names)
    forbidden = _scoped_forbidden(policy, module_leaves, context) if policy.scopes else _forbidden(policy, names, module_leaves, arbor_index)
    forbidden += _package_forbidden(policy, module_leaves, context, packages)
    layers, modules = _scoped_modules(policy, module_leaves, context, packages) if policy.scopes else _modules(policy, module_leaves, arbor_index, packages)
    explicit = bool(policy.scopes) or packages is not None or not policy.expressible_by_layers()
    scopes = [scope.policy for scope in policy.scopes] or [policy]
    return ArchitectureContracts(
        root_packages=tuple(tops_of(names)),
        forbidden=tuple(forbidden),
        layers=tuple(layers),
        modules=tuple(modules),
        explicit_boundaries=explicit,
        source_roots=tuple(policy.source_roots),
        config_dir="" if policy.output_dir in ("", ".") else policy.output_dir.rstrip("/"),
        distributions=tuple(sorted({(module, distribution) for local in scopes for module, distribution in local.dependencies.items()})),
        external_exclude=tuple(sorted({name for local in scopes for name in local.tach_external_exclude})),
    )


def _ring_forbidden(
    label: str, local: Policy, ring_modules: Mapping[str, list[str]],
) -> list[Contract]:
    out: list[Contract] = []
    prefix = f"{label} " if label else ""
    for rule in local.rings:
        mine = ring_modules[rule.name]
        if not mine:
            continue
        allowed = local.allowed_rings(rule.name)
        outer = [m for r in local.rings if r.name not in allowed for m in ring_modules[r.name]]
        if outer:
            out.append((f"{prefix}{rule.name} ring imports only {sorted(allowed)}", mine, outer))
        banned = sorted({*(local.io_modules if local.preset.bans_io else ()), *sorted(local.dependencies)} - rule.external)
        if ALL not in rule.external and banned:
            out.append((f"{prefix}{rule.name} ring imports no I/O or third-party package it does not list", mine, banned))
    return out


def _forbidden(policy: Policy, names: Sequence[str], module_leaves: Sequence[str], arbor_index: Index) -> list[Contract]:
    by_ring: dict[str, list[str]] = {r.name: [] for r in policy.rings}
    graph = set(tops_of(names))
    for n in module_leaves:
        ring = policy.resolve_ring(arbor_index, n) if n.split(".")[0] in graph else None
        if ring is not None:
            by_ring[ring].append(n)
    out = _ring_forbidden("", policy, by_ring)
    if policy.forbid_everywhere:
        out.append(("no module imports a forbidden package", tops_of(names), list(policy.forbid_everywhere)))
    return out


def _scoped_forbidden(policy: Policy, module_leaves: Sequence[str], context: RuleContext) -> list[Contract]:
    scope_by_module = context.scope_by_module
    by_scope: dict[str, dict[str, list[str]]] = {s.name: {r.name: [] for r in s.policy.rings} for s in policy.scopes}
    for name in module_leaves:
        scope_name = scope_by_module.get(name)
        ring = context.resolve_ring(name) if scope_name is not None else None
        if scope_name is not None and ring is not None:
            by_scope[scope_name][ring].append(name)
    out: list[Contract] = []
    for scope in policy.scopes:
        out += _ring_forbidden(scope.name, scope.policy, by_scope[scope.name])
        own = [m for m in module_leaves if scope_by_module.get(m) == scope.name]
        if own and scope.policy.forbid_everywhere:
            out.append((f"{scope.name} scope imports no forbidden package", own, list(scope.policy.forbid_everywhere)))
        hidden = []
        for module in module_leaves:
            target = scope_by_module.get(module)
            if target is None or target == scope.name:
                continue
            public = any(fnmatch.fnmatchcase(module, pattern) for pattern in policy.scope(target).public)
            if target not in scope.depends_on or not public:
                hidden.append(module)
        if own and hidden:
            out.append((f"{scope.name} scope imports only declared public modules", own, hidden))
    return out


def _package_forbidden(
    policy: Policy, module_leaves: Sequence[str], context: RuleContext, packages: PackageBoundaries | None,
) -> list[Contract]:
    """The package assignment as contracts: the engine's own broken-contract output is the cut list."""
    if packages is None:
        return []

    def allowance(module: str) -> Allowance | None:
        ring, local = context.resolve_ring(module), context.policy_for(module) or policy
        if ring is None:
            return None
        universe = {*(local.io_modules if local.preset.bans_io else ()), *local.dependencies}
        return frozenset(universe), local.ring(ring).external

    return list(packages.contracts(module_leaves, allowance))


def _modules(
    policy: Policy, module_leaves: Sequence[str], arbor_index: Index, packages: PackageBoundaries | None,
) -> tuple[list[str], list[ModuleLayer]]:
    explicit = packages is not None or not policy.expressible_by_layers()
    classified = [(n, policy.resolve_ring(arbor_index, n)) for n in module_leaves]
    out: list[ModuleLayer] = []
    for name, ring in classified:
        if ring is None:
            continue
        deps = None
        if explicit:
            allowed = policy.allowed_rings(ring)
            deps = tuple(other for other, r in classified if r in allowed and other != name and (packages is None or packages.allows(name, other)))
        out.append(ModuleLayer(name, ring, deps))
    return [r.name for r in reversed(policy.rings)], out


def _scoped_modules(
    policy: Policy, module_leaves: Sequence[str], context: RuleContext, packages: PackageBoundaries | None,
) -> tuple[list[str], list[ModuleLayer]]:
    layers = [scope_layer(scope.name, ring.name) for scope in policy.scopes for ring in reversed(scope.policy.rings)]
    classified = []
    for name in module_leaves:
        scope_name = context.scope_by_module.get(name)
        ring = context.resolve_ring(name)
        if scope_name is not None and ring is not None:
            classified.append((name, scope_name, ring))
    out: list[ModuleLayer] = []
    for name, scope_name, ring in classified:
        scope = policy.scope(scope_name)
        allowed = scope.policy.allowed_rings(ring)
        deps = []
        for other, other_scope, other_ring in classified:
            if other == name:
                continue
            same_scope_inner = other_scope == scope_name and other_ring in allowed
            public_dependency = other_scope in scope.depends_on and any(
                fnmatch.fnmatchcase(other, pattern) for pattern in policy.scope(other_scope).public
            )
            if (same_scope_inner or public_dependency) and (packages is None or packages.allows(name, other)):
                deps.append(other)
        out.append(ModuleLayer(name, scope_layer(scope_name, ring), tuple(sorted(deps))))
    return layers, out
