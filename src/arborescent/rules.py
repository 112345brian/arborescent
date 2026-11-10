"""Arborescent's own checks, as pure functions over already-parsed modules.

Rules (ids in ``domain.Rule``): ``unclassified``, ``internal-import``, ``external-import``, ``os-branching``,
``bad-import``, ``forbidden-module`` and ``undeclared-entry-dependency``. Lazy (function-level) and ``TYPE_CHECKING`` imports count. How much of the external
and OS-branching rules applies is the policy's preset; layering and classification are never relaxed.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from typing import NamedTuple

from .domain import (
    ALL,
    EFFECT_RULES,
    ArchitectureScope,
    ConstructionSite,
    OwnedLiteral,
    Policy,
    PolicyError,
    RingRule,
    Rule,
    Violation,
    dist_name,
    in_unit,
)
from .facts import EffectUse, Forwarder, ModuleInfo, RawImport, resolve_targets
from .import_cycles import cyclic_components

__all__ = ["RuleContext", "check_modules", "fits_ring"]

ThirdParty = dict[str, set[str]]  # distribution -> importing modules


@dataclass(frozen=True)
class RuleContext:
    policy: Policy
    arbor_index: Mapping[str, tuple[str | None, str | None]]
    scope_by_module: Mapping[str, str]
    tool_modules: frozenset[str] = frozenset()  # modules below a `dev_tools` path: no ring, never imported by product code

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
        if "arbor-annotations" in self.policy.disabled_tools:
            return None
        annotation = self.arbor_index.get(name)
        return annotation[1] if annotation is not None else None

    def annotation_ring_mismatch(self, name: str) -> str | None:
        policy = self.policy_for(name)
        return policy.annotation_ring_mismatch(self.arbor_index, name) if policy is not None else None

    def annotation_required_violation(self, name: str) -> str | None:
        policy = self.policy_for(name)
        return policy.annotation_required_violation(self.arbor_index, name) if policy is not None else None


def _most_specific(targets: Collection[str]) -> list[str]:
    """`from pkg import a` resolves to both `pkg` and `pkg.a`: judge the most specific only (no other target nests under it)."""
    return [target for target in targets if not any(other.startswith(target + ".") for other in targets)]


def _dev_tool_imports(
    context: RuleContext, name: str, info: ModuleInfo, known: Collection[str], tops: Collection[str],
) -> list[Violation]:
    out: list[Violation] = []
    for imp in info.facts.imports if context.tool_modules else ():
        internal, _, _ = resolve_targets(name, info.is_package, imp, known, tops)
        hits = [target for target in internal if target in context.tool_modules]
        out.extend(
            Violation(Rule.DEV_TOOL_IMPORT, name, f"imports dev tool {target}; developer tooling may import product code, never the reverse", imp.line, target)
            for target in _most_specific(hits)
        )
    return out


class _ImportEnv(NamedTuple):
    modules: Mapping[str, ModuleInfo]
    known: Collection[str]
    tops: Collection[str]
    graph: dict[str, set[str]]


def _dev_tool_findings(context: RuleContext, name: str, info: ModuleInfo, env: _ImportEnv) -> list[Violation]:
    """Ring and external-import rules do not apply to tooling; unresolvable imports, import cycles, `dev_tools_forbid`
    effects and `dev_tools_may_import` still do."""
    out = _dev_tool_effects(context.policy_for(name) or context.policy, name, info)
    for imp in info.facts.imports:
        internal, _, error = resolve_targets(name, info.is_package, imp, env.known, env.tops)
        out += _dev_tool_product_imports(context, name, imp.line, internal)
        env.graph[name].update(
            target for target in internal
            if target in env.modules and context.scope_name(target) == context.scope_name(name)
        )
        if error is not None:
            out.append(Violation(Rule.BAD_IMPORT, name, error, imp.line))
    return out


def _dev_tool_effects(policy: Policy, name: str, info: ModuleInfo) -> list[Violation]:
    # `counts_effect_seam` is also called ad hoc in `_effect_uses`, `_patch_violation` and `_scope_effect_leaks`
    # (selftest.py's `_leaking_public_dependency` too); centralizing it (e.g. `SourceFacts.counted_effect_uses`)
    # would remove the risk of one of these call sites using the wrong scope's policy, as two of them once did.
    return [
        Violation(EFFECT_RULES[use.category], name, f"[dev tool] effect {use.category} uses {use.target}; tooling may not use it (`dev_tools_forbid`)", use.line)
        for use in info.facts.effect_uses
        if use.category in policy.dev_tools_forbid and use.category in EFFECT_RULES and policy.counts_effect_seam(use.seam)
    ]


def _dev_tool_product_imports(context: RuleContext, name: str, line: int, targets: Collection[str]) -> list[Violation]:
    policy = context.policy
    product = [target for target in targets if target not in context.tool_modules]
    return [
        Violation(Rule.DEV_TOOL_PRODUCT_IMPORT, name, f"dev tool imports product module {target}, which `dev_tools_may_import` does not allow", line, target)
        for target in _most_specific(product)
        if not policy.tool_may_import(target)
    ]


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
            out.append(Violation(Rule.INTERNAL_IMPORT, name, f"[{name}] imports forbidden module {target}; {banned!r} is forbidden by module policy", line, target))
            continue
        restriction = _restricted_import(policy, name, target, line)
        if restriction is not None:
            out.append(restriction)
            continue
        target_ring = context.resolve_ring(target)
        if target_ring is not None and target_ring not in allowed:
            detail = f"[{ring}] imports {target} [{target_ring}]; {ring} may import only {sorted(allowed)}"
            out.append(Violation(Rule.INTERNAL_IMPORT, name, detail, line, target))
    return out


def _forwarded_imports(context: RuleContext, name: str, imp: RawImport, env: _ImportEnv) -> list[Violation]:
    """`from B import f` where `f` only forwards to a module `name` may not import (`module_forbid_imports`): the same dependency, one hop away (#138)."""
    policy = context.policy_for(name)
    if policy is None:
        return []
    out: list[Violation] = []
    hosts, _, _ = resolve_targets(name, env.modules[name].is_package, imp, env.known, env.tops)
    for host in hosts:
        info = env.modules.get(host)
        for fwd in () if info is None else info.facts.forwarders:
            if fwd.name not in imp.names:
                continue
            for target in _forwarded_targets(env, host, fwd, {host}):
                banned = policy.forbids_module_import(name, target)
                if banned is not None and context.scope_name(target) == context.scope_name(name):
                    detail = f"[{name}] imports {host}.{fwd.name}, which only forwards to {target}; {banned!r} is forbidden by module policy"
                    out.append(Violation(Rule.FORWARDED_IMPORT, name, detail, imp.line, target))
    return out


def _forwarded_targets(env: _ImportEnv, host: str, fwd: Forwarder, seen: set[str]) -> list[str]:
    """The modules a forwarder reaches, following forwarders that themselves only forward (a chain, not a cycle)."""
    raw = RawImport(fwd.level, fwd.module, (fwd.imported,) if fwd.imported else (), fwd.line, False, False)
    targets, _, _ = resolve_targets(host, env.modules[host].is_package, raw, env.known, env.tops)
    out = list(targets)
    callee = (fwd.member or fwd.imported).split(".")[0]
    for target in targets:
        info = env.modules.get(target)
        if info is None or target in seen:
            continue
        for nxt in info.facts.forwarders:
            if nxt.name == callee:
                out += _forwarded_targets(env, target, nxt, seen | {target})
    return out


def _restricted_import(policy: Policy, name: str, target: str, line: int) -> Violation | None:
    allowed = policy.restricted_importers(name, target)
    if allowed is None:
        return None
    return Violation(Rule.RESTRICTED_IMPORT, name, f"[{name}] imports {target}, which is importable only by {list(allowed)}", line, target)


def _owned_literal_findings(context: RuleContext, name: str, info: ModuleInfo) -> list[Violation]:
    """A module spelling a literal that the policy gives to other modules or rings (#112)."""
    local_policy = context.policy_for(name)
    if local_policy is None or not local_policy.owned_literals:
        return []
    ring = context.resolve_ring(name)
    owned = {item.literal: item for item in local_policy.owned_literals}
    return [
        Violation(
            Rule.LITERAL_OUTSIDE_OWNER, name,
            f"[{name}] (ring {ring or 'none'}) spells {value!r}, which belongs to {_owner_text(owned[value])}"
            + (f": {owned[value].reason}" if owned[value].reason else ""),
            line,
        )
        for line, value in info.facts.string_literals
        if value in owned and not owned[value].permits(name, ring)
    ]


def _owner_text(item: OwnedLiteral) -> str:
    parts = [f"modules {list(item.modules)}"] if item.modules else []
    return " in ".join(parts + ([f"rings {list(item.rings)}"] if item.rings else [])) or "no module"


def _stale_owned_literals(policy: Policy, modules: Collection[str]) -> list[Violation]:
    """An owner glob that matches no module: after a rename the literal would be owned by nobody."""
    return [
        Violation(Rule.LITERAL_OUTSIDE_OWNER, item.literal, f"`owned_literal` {item.literal!r}: modules glob {glob!r} matches no module")
        for item in policy.owned_literals for glob in item.modules
        if not any(in_unit(module, glob) for module in modules)
    ]


def _construction_findings(context: RuleContext, name: str, info: ModuleInfo) -> list[Violation]:
    """A module calling a class or factory that the policy lets only other modules or rings build (#85)."""
    local_policy = context.policy_for(name)
    if local_policy is None or not local_policy.construct_only:
        return []
    ring = context.resolve_ring(name)
    sites = {item.target: item for item in local_policy.construct_only}
    called = ((line, sites.get(_absolute_callee(name, info.is_package, callee) or "")) for line, callee in info.facts.calls)
    return [
        Violation(
            Rule.CONSTRUCTION_OUTSIDE_SITE, name,
            f"[{name}] (ring {ring or 'none'}) calls {site.target}, which only {_site_text(site)} may construct"
            + (f": {site.reason}" if site.reason else ""),
            line,
        )
        for line, site in called
        if site is not None and not site.permits(name, ring)
    ]


def _absolute_callee(module: str, is_package: bool, callee: str) -> str | None:
    """A call recorded by ``extract`` as a dotted name: relative imports keep their dots, a bare name is defined in this module."""
    if not callee.startswith("."):
        return callee if "." in callee else f"{module}.{callee}"
    level = len(callee) - len(callee.lstrip("."))
    parts = module.split(".")
    package = parts if is_package else parts[:-1]
    if level - 1 > len(package):
        return None
    return ".".join([*package[: len(package) - (level - 1)], callee[level:]])


def _site_text(item: ConstructionSite) -> str:
    parts = [f"modules {list(item.modules)}"] if item.modules else []
    return " in ".join(parts + ([f"rings {list(item.rings)}"] if item.rings else [])) or "no module"


def _stale_construction_sites(policy: Policy, modules: Collection[str]) -> list[Violation]:
    """A target module or owner glob that matches no module: after a rename the rule would restrict, or allow, nothing."""
    out: list[Violation] = []
    for item in policy.construct_only:
        if item.target_module not in modules:
            out.append(Violation(Rule.CONSTRUCTION_OUTSIDE_SITE, item.target, f"`construct_only` {item.target!r}: module {item.target_module!r} does not exist"))
        out += [
            Violation(Rule.CONSTRUCTION_OUTSIDE_SITE, item.target, f"`construct_only` {item.target!r}: modules glob {glob!r} matches no module")
            for glob in item.modules if not any(in_unit(module, glob) for module in modules)
        ]
    return out


def _stale_importable_by(policy: Policy, modules: Collection[str]) -> list[Violation]:
    """Each `importable_by` key or importer glob that matches no module: a rename would otherwise leave the rule restricting nothing."""
    out: list[Violation] = []
    for pattern, allowed in policy.importable_by:
        if not any(in_unit(module, pattern) for module in modules):
            out.append(Violation(Rule.RESTRICTED_IMPORT, pattern, f"`importable_by` key {pattern!r} matches no module"))
        out += [
            Violation(Rule.RESTRICTED_IMPORT, pattern, f"`importable_by` importer glob {glob!r} (for {pattern!r}) matches no module")
            for glob in allowed
            if not any(in_unit(module, glob) for module in modules)
        ]
    return out


def _sibling_index(known: Collection[str]) -> dict[str, list[str]]:
    """Last component -> dotted first-party modules, for naming what a bare sibling import was probably meant to be."""
    index: dict[str, list[str]] = {}
    for module in sorted(known):
        if "." in module:
            index.setdefault(module.rsplit(".", 1)[1], []).append(module)
    return index


def _external_imports(
    policy: Policy, name: str, ring: str, line: int, tops: Collection[str], third_party: ThirdParty,
    siblings: Mapping[str, list[str]],
) -> list[Violation]:
    rule = policy.ring(ring)
    out: list[Violation] = []
    for top in tops:
        is_stdlib = top in policy.stdlib
        if not is_stdlib:
            third_party.setdefault(dist_name(policy.dependencies.get(top, top)), set()).add(name)
        if ALL not in rule.external and top not in rule.external and policy.restricts_external(top, is_stdlib):
            detail = f"[{ring}] imports external package {top!r}; {ring} may import only {sorted(rule.external)}"
            candidates = siblings.get(top)
            if candidates is not None:
                detail += (
                    f"; {top!r} looks like first-party {' or '.join(candidates)} imported bare, which only resolves when the "
                    "file runs as a script (sys.path[0]); import it by its package path"
                )
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


def _patch_violation(context: RuleContext, name: str, info: ModuleInfo, known: Collection[str], use: EffectUse) -> list[Violation]:
    policy = context.require_policy(name)
    tests_folder = policy.practices.get("effect-patch", policy.practices.get("secrets-guard"))
    package_names = {module.split(".", 1)[0] for module in known}
    if not isinstance(tests_folder, str) or not tests_folder.strip("/"):
        return []
    folder = tests_folder.replace("\\", "/").strip("/")
    if not info.path.replace("\\", "/").startswith(folder + "/") or use.target.split(".", 1)[0] not in package_names:
        return []
    target_ring = _target_ring(context, use.target, known)
    if target_ring is None or "patch" not in policy.forbidden_effects(target_ring):
        return []
    detail = f"[{target_ring}] tests patch project target {use.target}; inject a port instead"
    return [Violation(EFFECT_RULES["patch"], name, detail, use.line)]


def _effect_uses(
    context: RuleContext, name: str, ring: str, info: ModuleInfo, known: Collection[str],
) -> list[Violation]:
    policy = context.require_policy(name)
    out: list[Violation] = []
    for use in info.facts.effect_uses:
        if not policy.counts_effect_seam(use.seam):
            continue
        if use.category == "os-branching":
            continue  # preserve the original os_branching option and Rule id
        if use.category == "patch":
            out += _patch_violation(context, name, info, known, use)
            continue
        if use.category in policy.forbidden_effects(ring):
            rule = EFFECT_RULES.get(use.category)
            if rule is not None:
                detail = f"[{ring}] effect {use.category} uses {use.target}; inject a port instead"
                out.append(Violation(rule, name, detail, use.line))
    return out


def _abstraction_violations(name: str, ring: str, rule: RingRule, info: ModuleInfo) -> list[Violation]:
    if not rule.require_abstract:
        return []
    out: list[Violation] = []
    for member in info.facts.top_level_defs:
        if member.name.startswith("_") or member.is_abstract:
            continue
        kind = "class" if member.is_class else "function"
        shape = "abc.ABC, typing.Protocol or a declared ABCMeta" if member.is_class else "a stub body (`...`, `pass` or `raise NotImplementedError`)"
        detail = f"[{ring}] {kind} {member.name!r} is concrete; this ring requires {shape}"
        out.append(Violation(Rule.NON_ABSTRACT_PORT, name, detail, member.line))
    return out


def _typed_port_violations(name: str, ring: str, rule: RingRule, info: ModuleInfo) -> list[Violation]:
    if not rule.require_typed_ports:
        return []
    return [
        Violation(Rule.WEAKLY_TYPED_PORT, name, f"[{ring}] protocol method {member!r} {why}; name the result type", line)
        for line, member, why in info.facts.weak_port_returns
    ]


REEXPORT_MIN_SURFACE = 5  # a smaller public surface gives a ratio too coarse to mean anything


def _reexport_findings(name: str, ring: str, rule: RingRule, info: ModuleInfo, project: tuple[Collection[str], Collection[str]]) -> list[Violation]:
    """A module whose public surface is mostly first-party imports moves edges without changing the dependency (#136)."""
    facts = info.facts
    known, tops = project
    if rule.max_reexport_ratio is None or facts.surface_size < REEXPORT_MIN_SURFACE:
        return []
    count = sum(
        1 for _, level, module, imported in facts.reexports
        if resolve_targets(name, info.is_package, RawImport(level, module, (imported,) if imported else (), 0, False, False), known, tops)[0]
    )
    if count / facts.surface_size <= rule.max_reexport_ratio:
        return []
    detail = f"[{ring}] public surface is mostly re-exported first-party imports (limit {rule.max_reexport_ratio:.0%})"
    hint = f" ({count} of {facts.surface_size} public names are imports; importers of this module keep the same dependency)"
    return [Violation(Rule.REEXPORT_RATIO, name, detail, facts.surface_line, hint=hint)]


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


def _leak_source(context: RuleContext, source: str) -> tuple[str, str, frozenset[str]] | None:
    """``(scope, ring, forbidden effects)`` for a source module that could leak an effect; None when nothing can."""
    scope_name = context.scope_name(source)
    ring = context.resolve_ring(source) if scope_name is not None else None
    if scope_name is None or ring is None:
        return None
    forbidden = context.policy.scope(scope_name).policy.forbidden_effects(ring)
    return (scope_name, ring, forbidden) if forbidden else None


def _foreign_public_module(
    context: RuleContext, modules: Mapping[str, ModuleInfo], source_scope_name: str, target: str
) -> tuple[str, ArchitectureScope, ModuleInfo] | None:
    """The scope and facts of ``target`` when it is a declared-public module of another scope that was scanned."""
    target_scope_name = context.scope_name(target)
    if target_scope_name is None or target_scope_name == source_scope_name:
        return None
    target_scope = context.policy.scope(target_scope_name)
    info = modules.get(target)
    if info is None or not any(fnmatch.fnmatchcase(target, pattern) for pattern in target_scope.public):
        return None
    return target_scope_name, target_scope, info


def _scope_effect_leaks(
    context: RuleContext, modules: Mapping[str, ModuleInfo], source: str, line: int, targets: Collection[str],
) -> list[Violation]:
    """One-hop leak: a declared-public module in another scope uses an effect this scope's ring forbids."""
    leak_source = _leak_source(context, source)
    if leak_source is None:
        return []
    source_scope_name, source_ring, forbidden = leak_source
    out: list[Violation] = []
    for target in filter(None, targets):
        foreign = _foreign_public_module(context, modules, source_scope_name, target)
        if foreign is None:
            continue
        target_scope_name, target_scope, info = foreign
        seen: set[str] = set()
        for use in info.facts.effect_uses:
            if use.category not in forbidden or use.category in seen or not target_scope.policy.counts_effect_seam(use.seam):
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


def _module_preamble(context: RuleContext, name: str, info: ModuleInfo, known: Collection[str], tops: Collection[str]) -> list[Violation]:
    """Annotation, tooling-import and forbidden-name findings, which apply whether or not the module has a ring."""
    is_tool = name in context.tool_modules
    out: list[Violation] = []
    detail = context.annotation_violation(name)
    if detail is not None:
        out.append(Violation(Rule.INVALID_ARBOR_ANNOTATION, name, detail))
    mismatch = None if is_tool else context.annotation_ring_mismatch(name)
    if mismatch is not None:
        out.append(Violation(Rule.ARBOR_RING_MISMATCH, name, mismatch))
    if not is_tool:
        out += _dev_tool_imports(context, name, info, known, tops)
    local_policy = context.policy_for(name)
    pattern = local_policy.forbidden_module_pattern(name) if local_policy is not None else None
    if pattern is not None:
        out.append(Violation(
            Rule.FORBIDDEN_MODULE, name,
            f"module matches forbidden_modules pattern {pattern!r}; a first-party module with this name must not "
            f"exist (remove it, or drop the pattern from forbidden_modules)",
        ))
    return out


def check_modules(
    policy: Policy,
    modules: Mapping[str, ModuleInfo],
    known: Collection[str],
    entry_deps: Mapping[str, tuple[str, ...] | None],
    indexes: RuleContext | Mapping[str, tuple[str | None, str | None]],
) -> list[Violation]:
    context = indexes if isinstance(indexes, RuleContext) else RuleContext(policy, indexes, {})
    tops = {m.split(".")[0] for m in known}
    siblings = _sibling_index(known)
    out = _wildcard_findings(policy)
    third_party_by_scope: dict[str | None, ThirdParty] = {}
    import_graph: dict[str, set[str]] = {name: set() for name in modules}
    effect_graph: dict[str, set[str]] = {name: set() for name in modules}
    pending_effect_edges: list[_EffectEdge] = []
    for name, info in sorted(modules.items()):
        local_policy = context.policy_for(name)
        is_tool = name in context.tool_modules  # developer tooling: no ring, may import product code
        out += _module_preamble(context, name, info, known, tops)
        out += _owned_literal_findings(context, name, info) + _construction_findings(context, name, info)
        if is_tool:
            out += _dev_tool_findings(context, name, info, _ImportEnv(modules, known, tops, import_graph))
            continue
        ring = context.resolve_ring(name)
        if ring is None:
            out.append(Violation(Rule.UNCLASSIFIED, name, (context.policy_for(name) or context.policy).unclassified_detail(name)))
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
            _record_effect_edge(context, effect_graph, pending_effect_edges, _EffectImport(local_policy, name, ring, imp, internal))
            if error is not None:
                out.append(Violation(Rule.BAD_IMPORT, name, error, imp.line))
            out += _scope_imports(context, name, imp.line, internal)
            out += _scope_effect_leaks(context, modules, name, imp.line, internal)
            out += _internal_imports(context, name, ring, imp.line, internal) + _forwarded_imports(context, name, imp, _ImportEnv(modules, known, tops, import_graph))
            out += _external_imports(local_policy, name, ring, imp.line, external, third_party, siblings)
        out += _os_branching(local_policy, name, ring, rule, info)
        out += _effect_uses(context, name, ring, info, known)
        out += (
            _abstraction_violations(name, ring, rule, info)
            + _typed_port_violations(name, ring, rule, info)
            + _reexport_findings(name, ring, rule, info, (known, tops))
        )
    out += _effect_via_import_violations(context, modules, effect_graph, pending_effect_edges)
    out = _with_move_closures(out, context, modules, known, tops)
    return out + _finalize_checks(context, known, entry_deps, import_graph, third_party_by_scope)


def _with_move_closures(
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
            externals.update(_restricted_externals(policy, ring, external))
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


def _restricted_externals(policy: Policy, ring: str, tops: Collection[str]) -> list[str]:
    allowed = policy.ring(ring).external
    return [top for top in tops if ALL not in allowed and top not in allowed and policy.restricts_external(top, top in policy.stdlib)]


def _outside_ring(context: RuleContext, modules: Mapping[str, ModuleInfo], name: str, ring: str, targets: Collection[str]) -> list[str]:
    allowed = context.require_policy(name).allowed_rings(ring)
    return [
        t for t in targets
        if t in modules and context.scope_name(t) == context.scope_name(name) and context.resolve_ring(t) not in allowed
    ]


_NON_PROPAGATING_EFFECTS = frozenset({"os-branching", "patch"})  # not a ring effect, or meaningless once removed from its own module


class _EffectEdge(NamedTuple):
    """One import edge a forbidden effect could propagate along: ``name``'s policy forbidding it in ``ring``."""

    policy: Policy
    name: str
    ring: str
    line: int
    targets: list[str]


class _EffectImport(NamedTuple):
    policy: Policy
    name: str
    ring: str
    imp: RawImport
    internal: list[str]


def _record_effect_edge(
    context: RuleContext, effect_graph: dict[str, set[str]], pending: list[_EffectEdge], edge: _EffectImport,
) -> None:
    """A type-only import never runs, so it carries no effect; everything else, within the same scope, does."""
    if edge.imp.type_only:
        return
    targets = [
        target for target in edge.internal
        if target in effect_graph and target != edge.name and context.scope_name(target) == context.scope_name(edge.name)
    ]
    effect_graph[edge.name].update(targets)
    pending.append(_EffectEdge(edge.policy, edge.name, edge.ring, edge.imp.line, targets))


def _direct_propagating_effects(policy: Policy, info: ModuleInfo) -> frozenset[str]:
    return frozenset(
        use.category for use in info.facts.effect_uses
        if use.category not in _NON_PROPAGATING_EFFECTS and policy.counts_effect_seam(use.seam)
    )


def _effect_via_import_violations(
    context: RuleContext, modules: Mapping[str, ModuleInfo], effect_graph: Mapping[str, Collection[str]], edges: Collection[_EffectEdge],
) -> list[Violation]:
    reachable = _effect_reachable(context, modules, effect_graph)
    out: list[Violation] = []
    for edge in edges:
        out += _effect_via_import(edge, reachable)
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
            dict.fromkeys(_direct_propagating_effects(local_policy, info), (name,))
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


def _effect_via_import(edge: _EffectEdge, reachable: Mapping[str, Mapping[str, tuple[str, ...]]]) -> list[Violation]:
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


def fits_ring(context: RuleContext, name: str, info: ModuleInfo, ring: str, known: Collection[str]) -> bool:
    """Whether ``info`` would pass every per-module ring rule (imports, external packages, OS branching, effects, abstraction) as ``ring``.

    The same functions ``check_modules`` runs, asked about a ring the module is not in; an unresolvable import means no.
    """
    policy = context.policy_for(name)
    if policy is None:
        return False
    tops = {m.split(".")[0] for m in known}
    found: list[Violation] = []
    for imp in info.facts.imports:
        internal, external, error = resolve_targets(name, info.is_package, imp, known, tops)
        if error is not None:
            return False
        found += _internal_imports(context, name, ring, imp.line, internal)
        found += _external_imports(policy, name, ring, imp.line, external, {}, {})
    found += _os_branching(policy, name, ring, policy.ring(ring), info)
    found += _effect_uses(context, name, ring, info, known)
    found += _abstraction_violations(name, ring, policy.ring(ring), info)
    found += _typed_port_violations(name, ring, policy.ring(ring), info)
    return not found


def _finalize_checks(
    context: RuleContext, known: Collection[str],
    entry_deps: Mapping[str, tuple[str, ...] | None], import_graph: Mapping[str, set[str]],
    third_party_by_scope: Mapping[str | None, ThirdParty],
) -> list[Violation]:
    policy = context.policy
    if not policy.scopes:
        circular = _circular_imports(import_graph) if policy.forbid_circular_imports else []
        return circular + _entry_dependencies(policy, known, entry_deps, third_party_by_scope.get(None, {})) + _stale_importable_by(policy, known) + _stale_owned_literals(policy, known) + _stale_construction_sites(policy, known)
    out: list[Violation] = []
    for scope in policy.scopes:
        local_graph = {
            name: targets for name, targets in import_graph.items()
            if context.scope_name(name) == scope.name
        }
        if scope.policy.forbid_circular_imports:
            out.extend(_circular_imports(local_graph))
        out.extend(_entry_dependencies(scope.policy, known, entry_deps, third_party_by_scope.get(scope.name, {})))
        scope_modules = [name for name in known if context.scope_name(name) == scope.name]
        out.extend(_stale_importable_by(scope.policy, scope_modules))
        out.extend(_stale_owned_literals(scope.policy, scope_modules))
        out.extend(_stale_construction_sites(scope.policy, scope_modules))
    return out


def _circular_imports(graph: Mapping[str, Collection[str]]) -> list[Violation]:
    """Report each strongly connected import component, including direct self-imports.

    Import edges include lazy and type-only imports because they still create architectural dependencies.
    """
    return [
        Violation(Rule.CIRCULAR_IMPORT, ", ".join(sorted(component)), "modules form a circular import dependency")
        for component in cyclic_components(graph)
    ]
