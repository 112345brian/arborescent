"""Arborescent's own checks, as pure functions over already-parsed modules.

Rules (ids in ``domain.Rule``): ``unclassified``, ``internal-import``, ``external-import``, ``os-branching``,
``bad-import``, ``forbidden-module`` and ``undeclared-entry-dependency``. Lazy (function-level) and ``TYPE_CHECKING`` imports count. How much of the external
and OS-branching rules applies is the policy's preset; layering and classification are never relaxed.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from bisect import bisect_right
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from typing import NamedTuple

from .call_facts import (
    REPLACE_CALLS,
    CallShape,
    FlowNode,
    absolute_callee,
    is_private_name,
    optional_patch_lines,
    unrecorded_path,
)
from .definition_facts import (
    ClassIndex,
    NameDecl,
    Signature,
    module_attributes,
    name_declarations,
    shadowed_callees,
)
from .domain import (
    ALL,
    EFFECT_RULES,
    ArchitecturePolicy,
    ArchitectureScope,
    CallAllowance,
    CompanionCall,
    ConstructionSite,
    ModuleStatePolicy,
    NameCollisionPolicy,
    OwnedLiteral,
    Policy,
    PortBinding,
    RingRule,
    Rule,
    SeamBypass,
    StateAllow,
    Violation,
    dist_name,
    in_unit,
)
from .effect_facts import EffectUse, StateWrite
from .effect_import_rules import (
    EffectEdge,
    EffectImport,
    effect_at_import_findings,
    effect_via_import_violations,
    record_effect_edge,
    unsanctioned_effect_imports,
)
from .facts import (
    ClassMembers,
    Forwarder,
    ModuleInfo,
    SeamFrame,
    SeamUse,
    SourceFacts,
    file_module,
    patched_targets,
    unreached_bindings,
)
from .import_cycles import cyclic_components
from .import_facts import RawImport, resolve_runtime_targets, resolve_targets
from .package_constraint_rules import placement_violations, use_violations
from .package_constraints import PackageConstraints
from .purity_rules import mixed_purity_findings, with_move_closures
from .rule_context import RuleContext

__all__ = [
    "PatchSources", "RuleContext", "check_modules", "fits_ring", "name_collision_findings", "port_binding_findings",
    "shadowed_callee_findings", "unreachable_patches", "unresolved_patch_targets",
]

ThirdParty = dict[str, set[str]]  # distribution -> importing modules
NO_CONSTRAINTS = PackageConstraints()  # a policy that declares none: the constraint rules cost nothing


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
    out = _dev_tool_effects(context.policy.dev_tools_forbid, context.policy_for(name) or context.policy, name, info)
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


def _dev_tool_effects(dev_tools_forbid: Collection[str], policy: ArchitecturePolicy, name: str, info: ModuleInfo) -> list[Violation]:
    # `counts_effect_seam` is also called ad hoc in `_effect_uses`, `_patch_violation` and `_scope_effect_leaks`
    # (selftest.py's `_leaking_public_dependency` too); centralizing it (e.g. `SourceFacts.counted_effect_uses`)
    # would remove the risk of one of these call sites using the wrong scope's policy, as two of them once did.
    return [
        Violation(EFFECT_RULES[use.category], name, f"[dev tool] effect {use.category} uses {use.target}; tooling may not use it (`dev_tools_forbid`)", use.line)
        for use in info.facts.effect_uses
        if use.category in dev_tools_forbid and use.category in EFFECT_RULES and policy.counts_effect_seam(use.seam)
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


def _restricted_import(policy: ArchitecturePolicy, name: str, target: str, line: int) -> Violation | None:
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


def _result_discard_findings(context: RuleContext, name: str, info: ModuleInfo, modules: Mapping[str, ModuleInfo]) -> list[Violation]:
    """A call statement that drops the value of a result-bearing function (#141); dynamic calls are not resolved."""
    local_policy = context.policy_for(name)
    setting = local_policy.result_discard if local_policy is not None else None
    if setting is None or (setting.rings and context.resolve_ring(name) not in setting.rings):
        return []
    marked = set(setting.functions)
    seen: dict[str, int] = {}
    out: list[Violation] = []
    for line, target in info.facts.bare_calls:
        callee = absolute_callee(name, info.is_package, target)
        if callee is None:
            continue
        owner, _, function = callee.rpartition(".")
        owner_info = modules.get(owner)
        if callee not in marked and not (setting.convention and owner_info is not None and function in owner_info.facts.result_defs):
            continue
        seen[callee] = seen.get(callee, 0) + 1
        out.append(Violation(
            Rule.RESULT_DISCARD, name,
            f"[{name}] calls {callee}() as a statement and drops its result (call #{seen[callee]}); bind it, return it, test it, "
            f"or discard it on purpose with `_ = {callee.rpartition('.')[2]}(...)`",
            line,
        ))
    return out


class _Seam(NamedTuple):
    function: str  # the function to report
    owner: str  # the class that was given the seam, or "" when the function takes it
    names: tuple[str, ...]


def _enclosing(frames: tuple[SeamFrame, ...], frame: SeamFrame, below: SeamFrame) -> Iterator[tuple[SeamFrame, SeamFrame]]:
    """``(frame, the one just inside it)`` from ``frame`` outwards; the innermost pair is that frame twice."""
    yield frame, below
    if frame.parent >= 0:
        yield from _enclosing(frames, frames[frame.parent], frame)


def _seam_of(setting: SeamBypass, frames: tuple[SeamFrame, ...], use: SeamUse) -> _Seam | None:
    """The nearest seam of the use's category around it, or ``None``."""
    for frame, below in _enclosing(frames, frames[use.frame], frames[use.frame]):
        seams = sorted(p.name for p in frame.params if setting.category_of(p.name, p.annotation, p.default) == use.category)
        if not seams:
            continue
        if set(seams) & set(use.guards):
            return None  # the real one is the fallback for a seam that was not injected
        return _Seam(below.name if frame.is_class else frame.name, frame.name if frame.is_class else "", tuple(seams))
    return None


def _seam_bypass_findings(context: RuleContext, name: str, ring: str, info: ModuleInfo) -> list[Violation]:
    """A function (or a method of a class) given an injected effect that also uses the real one (#233): one per function and category."""
    local_policy = context.policy_for(name)
    setting = local_policy.effect_seam_bypassed if local_policy is not None else None
    if setting is None or (setting.rings and ring not in setting.rings):
        return []
    found: dict[tuple[str, str, str, tuple[str, ...]], list[SeamUse]] = {}
    for use in info.facts.seams.uses:
        seam = _seam_of(setting, info.facts.seams.frames, use)
        if seam is not None:
            function, owner, seams = seam
            found.setdefault((function, use.category, owner, seams), []).append(use)
    out: list[Violation] = []
    for (function, category, owner, seams), uses in found.items():
        quoted = [f"'{seam}'" for seam in seams]
        noun = f"effect seam{'s' if len(seams) > 1 else ''} {', '.join(quoted)}"
        has = f"has the {noun} injected into {owner}" if owner else f"takes the {noun}"
        detail = f"[{ring}] {function}() {has} but also uses the real {category}; use {' or '.join(quoted)} instead"
        hint = f" (it uses {', '.join(sorted({use.target for use in uses}))})"
        out.append(Violation(Rule.EFFECT_SEAM_BYPASSED, name, detail, min(use.line for use in uses), hint=hint))
    return out

def _imported_paths(name: str, info: ModuleInfo) -> list[tuple[int, str]]:
    """``(line, dotted)`` of everything a module imports by name or reaches as ``module._name``, relative forms resolved."""
    found: list[tuple[int, str]] = []
    for imp in info.facts.imports:
        if not imp.names:  # plain `import a.b`
            found.append((imp.line, imp.module))
            continue
        prefix = "." * imp.level + imp.module + ("." if imp.module else "")
        found += [(imp.line, dotted) for item in imp.names if (dotted := absolute_callee(name, info.is_package, prefix + item)) is not None]
    found += [(line, dotted) for line, target in info.facts.private_attrs if (dotted := absolute_callee(name, info.is_package, target)) is not None]
    return found


def _private_crossings(importer: str, dotted: str, known: Collection[str]) -> list[tuple[str, str]]:
    """``(owner, private target)`` for each private segment of ``dotted`` whose owner ``importer`` is outside of.

    The owner is a first-party module (or the namespace parent of a private module); an attribute of a class or object is not one.
    A top-level private module has no owner, so only the names inside it are private."""
    parts = dotted.split(".")
    out: list[tuple[str, str]] = []
    for end in range(1, len(parts)):
        owner, target = ".".join(parts[:end]), ".".join(parts[: end + 1])
        if not is_private_name(parts[end]) or (owner not in known and target not in known):
            continue
        if importer != owner and not importer.startswith(owner + "."):
            out.append((owner, target))
    return out


def _private_access_findings(context: RuleContext, name: str, info: ModuleInfo, known: Collection[str]) -> list[Violation]:
    """Another module's leading-underscore name or submodule, imported or reached as an attribute (#218); once per target."""
    local_policy = context.policy_for(name)
    setting = local_policy.private_access if local_policy is not None else None
    if setting is None or any(in_unit(name, glob) for glob in setting.exempt):
        return []
    if setting.rings and context.resolve_ring(name) not in setting.rings:
        return []
    seen: dict[str, Violation] = {}
    for line, dotted in sorted(_imported_paths(name, info)):
        for owner, target in _private_crossings(name, dotted, known):
            if target not in seen and not any(in_unit(target, glob) for glob in setting.allow):
                detail = f"[{name}] uses {target}, which is private to {owner}; make it public, or move this code inside {owner}"
                seen[target] = Violation(Rule.PRIVATE_ACCESS, name, detail, line)
    return list(seen.values())
def _docs_findings(context: RuleContext, name: str, info: ModuleInfo) -> list[Violation]:
    """A module with no docstring, or whose docstring does not say the ring the policy enforces for it (#110)."""
    local_policy = context.policy_for(name)
    if local_policy is None or not local_policy.docs.applies_to(name):
        return []
    docs = local_policy.docs
    doc = (info.facts.module_doc or "").strip()
    if not doc:
        return [Violation(Rule.MISSING_MODULE_DOCSTRING, name, f"[{name}] has no module docstring saying what it is")] if docs.module_docstring else []
    ring = None if name in context.tool_modules else context.resolve_ring(name)
    if not docs.module_names_ring or ring is None or _names_word(doc, ring):
        return []
    others = sorted(other.name for other in local_policy.rings if other.name != ring and _names_word(doc, other.name))
    said = f"names ring {others[0]!r}" if len(others) == 1 else f"names rings {others}" if others else "names no ring"
    return [Violation(Rule.DOCSTRING_RING_MISMATCH, name, f"[{name}] docstring {said}, but the module is in ring {ring!r}")]


def _names_word(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text, re.IGNORECASE) is not None


def _owner_text(item: OwnedLiteral) -> str:
    parts = [f"modules {list(item.modules)}"] if item.modules else []
    return " in ".join(parts + ([f"rings {list(item.rings)}"] if item.rings else [])) or "no module"


def _stale_owned_literals(policy: ArchitecturePolicy, modules: Collection[str]) -> list[Violation]:
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
    called = ((line, sites.get(absolute_callee(name, info.is_package, callee) or "")) for line, callee in info.facts.calls)
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


def _site_text(item: CallAllowance | ConstructionSite) -> str:
    parts = [f"modules {list(item.modules)}"] if item.modules else []
    return " in ".join(parts + ([f"rings {list(item.rings)}"] if item.rings else [])) or "no module"


class _Construction(NamedTuple):
    """One constructing call in one module: what it passes, and the class it builds."""

    module: str
    ring: str | None
    shape: CallShape
    target: str  # the canonical class
    replacing: bool


def _constructed(index: ClassIndex, name: str, shape: CallShape) -> tuple[list[str], bool]:
    """The classes a call may build: its callee, or for ``replace`` the types its first argument may have."""
    callee = index.absolute(name, shape.callee)
    replacing = callee in REPLACE_CALLS
    found = [index.absolute(name, item) for item in shape.replaced] if replacing else [callee]
    return sorted({item for item in found if item is not None}), replacing


def _argument_findings(context: RuleContext, name: str, info: ModuleInfo, index: ClassIndex) -> list[Violation]:
    """``[[construction]]`` entries and derived-field markers against every call in one module (#234)."""
    local_policy = context.policy_for(name)
    entries = local_policy.construction if local_policy is not None else ()
    if not entries and not index.has_markers:
        return []
    by_target: dict[str, list[ConstructionSite]] = {}
    for item in entries:
        by_target.setdefault(index.canonical(item.target), []).append(item)
    out: list[Violation] = []
    for shape in info.facts.call_shapes:
        targets, replacing = _constructed(index, name, shape)
        for target in targets:
            found = _Construction(name, context.resolve_ring(name), shape, target, replacing)
            rules = by_target.get(target, [])
            out += _forbidden_arguments(found, index.signature(target), rules)
            for item in rules:
                out += _missing_arguments(found, item) + _outside_only_from(found, item)
    return out


def _forbidden_arguments(found: _Construction, signature: Signature, rules: list[ConstructionSite]) -> list[Violation]:
    reasons = dict.fromkeys(signature.derived, "marked derived")
    for item in rules:
        reasons.update(dict.fromkeys(item.forbid_kwargs, item.reason or "listed in `forbid_kwargs`"))
    shape = found.shape
    by_position = () if found.replacing else signature.positional[: shape.positional]  # replace takes no field by position
    call = f"replace() on {found.target} sets" if found.replacing else f"constructing {found.target} passes"
    return [
        Violation(
            Rule.CONSTRUCTION_FORBIDDEN_ARGUMENT, found.module,
            f"[{found.module}] {call} `{field}` by {'keyword' if field in shape.keywords else 'position'};"
            f" a caller must never supply it: {reason}",
            shape.line,
        )
        for field, reason in sorted(reasons.items())
        if field in shape.keywords or field in by_position
    ]


def _missing_arguments(found: _Construction, item: ConstructionSite) -> list[Violation]:
    """``replace`` copies the other fields, and an unknown ``**value`` may carry the keyword: neither is reported."""
    if found.replacing or found.shape.open_keywords:
        return []
    return [
        Violation(
            Rule.CONSTRUCTION_MISSING_ARGUMENT, found.module,
            f"[{found.module}] constructs {item.target} without passing `{field}` by name" + (f": {item.reason}" if item.reason else ""),
            found.shape.line,
        )
        for field in item.require_kwargs
        if field not in found.shape.keywords
    ]


def _outside_only_from(found: _Construction, item: ConstructionSite) -> list[Violation]:
    if item.permits(found.module, found.ring):
        return []
    how = " through replace()" if found.replacing else ""
    return [Violation(
        Rule.CONSTRUCTION_OUTSIDE_SITE, found.module,
        f"[{found.module}] (ring {found.ring or 'none'}) constructs {item.target}{how}, which only {_site_text(item)} may construct"
        + (f": {item.reason}" if item.reason else ""),
        found.shape.line,
    )]


def _stale_construction_entries(policy: ArchitecturePolicy, modules: Collection[str], index: ClassIndex) -> list[Violation]:
    """A ``[[construction]]`` class, field or glob that no longer exists: after a rename the entry would restrict nothing."""
    out: list[Violation] = []
    for item in policy.construction:
        rule = (
            Rule.CONSTRUCTION_FORBIDDEN_ARGUMENT if item.forbid_kwargs
            else Rule.CONSTRUCTION_MISSING_ARGUMENT if item.require_kwargs
            else Rule.CONSTRUCTION_OUTSIDE_SITE
        )
        out += [Violation(rule, item.target, f"`construction` {item.target!r}: {problem}") for problem in _stale_type(item, modules, index)]
        out += [
            Violation(Rule.CONSTRUCTION_OUTSIDE_SITE, item.target, f"`construction` {item.target!r}: only_from glob {glob!r} matches no module")
            for glob in item.modules if not any(in_unit(module, glob) for module in modules)
        ]
    return out


def _stale_type(item: ConstructionSite, modules: Collection[str], index: ClassIndex) -> list[str]:
    """Only what the parsed modules can prove: a module a partial scan did not read says nothing."""
    if item.target_module not in modules:
        return [f"module {item.target_module!r} does not exist"]
    target = index.canonical(item.target)
    module, _, name = target.rpartition(".")
    if target not in index.classes:
        return [f"module {module!r} defines no class {name!r}"] if module in index.modules else []
    signature = index.signature(target)
    return [
        f"the constructor of {target} has no parameter `{field}`"
        for field in (*item.forbid_kwargs, *item.require_kwargs) if signature.lacks(field)
    ]


def _stale_construction_sites(policy: ArchitecturePolicy, modules: Collection[str]) -> list[Violation]:
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


def _call_allowlist_findings(context: RuleContext, name: str, info: ModuleInfo) -> list[Violation]:
    """A module making a call, or hand-rolling an idiom, that the policy sanctions only elsewhere (#229).

    Only calls spelled on an imported or module-level name are recorded (``docs/configuration.md`` says what is not seen).
    """
    local_policy = context.policy_for(name)
    if local_policy is None or not local_policy.call_allowlist:
        return []
    ring = context.resolve_ring(name)
    entries = [item for item in local_policy.call_allowlist if not item.permits(name, ring)]
    if not entries:
        return []
    calls = _absolute_calls(name, info)
    who = f"[{name}] (ring {ring or 'none'})"
    out: list[Violation] = []
    for item in entries:
        why = f"which only {_site_text(item)} may do" + (f": {item.reason}" if item.reason else "")
        out += _idiom_findings(name, info, item, calls, (who, why)) if item.and_calls else _restricted_calls(name, item, calls, (who, why))
    return out


def _absolute_calls(name: str, info: ModuleInfo) -> list[tuple[int, str]]:
    """The module's recorded call sites as ``(line, absolute dotted callee)``, in line order; methods on a ``Path`` included."""
    calls = [(line, callee) for line, target in info.facts.calls if (callee := absolute_callee(name, info.is_package, target)) is not None]
    return sorted({*calls, *info.facts.path_calls}) if info.facts.path_calls else calls


def _call_matches(callee: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(callee, pattern) for pattern in patterns)


def _calls_inside(info: ModuleInfo, calls: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """For each of the module's function spans (in source order, never overlapping), the call sites inside it."""
    spans = info.facts.function_spans
    starts = [span.line for span in spans]
    inside: list[list[tuple[int, str]]] = [[] for _ in spans]
    for line, callee in calls:
        slot = bisect_right(starts, line) - 1
        if slot >= 0 and line <= spans[slot].end_line:
            inside[slot].append((line, callee))
    return inside


def _restricted_calls(name: str, item: CallAllowance, calls: list[tuple[int, str]], text: tuple[str, str]) -> list[Violation]:
    """One finding per call site, numbered per callee so a further call is a new baseline entry and a moved one is not."""
    seen: dict[str, int] = {}
    out: list[Violation] = []
    for line, callee in calls:
        if _call_matches(callee, item.calls):
            seen[callee] = seen.get(callee, 0) + 1
            detail = f"{text[0]} calls {callee}() (call #{seen[callee]}), {text[1]}"
            out.append(Violation(Rule.CALL_OUTSIDE_ALLOWLIST, name, detail, line))
    return out


def _idiom_findings(
    name: str, info: ModuleInfo, item: CallAllowance, calls: list[tuple[int, str]], text: tuple[str, str],
) -> list[Violation]:
    """One finding per function holding a call to one of ``calls`` and a different call to one of ``and_calls``."""
    seen: dict[str, int] = {}
    out: list[Violation] = []
    for span, inside in zip(info.facts.function_spans, _calls_inside(info, calls), strict=True):
        first = [call for call in inside if _call_matches(call[1], item.calls)]
        second = [call for call in inside if _call_matches(call[1], item.and_calls)]
        if not any(one != other for one in first for other in second):
            continue
        seen[span.name] = seen.get(span.name, 0) + 1
        again = f" (definition #{seen[span.name]})" if seen[span.name] > 1 else ""
        detail = f"{text[0]} {span.name}(){again} calls both {' or '.join(item.calls)} and {' or '.join(item.and_calls)}, {text[1]}"
        found = ", ".join(sorted({callee for _, callee in first + second}))
        out.append(Violation(Rule.HAND_ROLLED_IDIOM, name, detail, span.line, hint=f" (found: {found})"))
    return out


def _stale_call_allowances(policy: ArchitecturePolicy, modules: Collection[str]) -> list[Violation]:
    """An owner glob that matches no module: a rename would leave the sanctioned wrapper unsanctioned, unnoticed until it is."""
    return [
        Violation(
            Rule.HAND_ROLLED_IDIOM if item.and_calls else Rule.CALL_OUTSIDE_ALLOWLIST, ", ".join(item.calls),
            f"`call_allowlist` {list(item.calls)}: modules glob {glob!r} matches no module",
        )
        for item in policy.call_allowlist for glob in item.modules
        if not any(in_unit(module, glob) for module in modules)
    ]


class _FunctionCalls(NamedTuple):
    """What one function calls: resolved ``(line, dotted callee)`` sites, and method calls on values by ``receiver.name``."""

    resolved: list[tuple[int, str]]
    methods: tuple[tuple[int, str], ...]

    def matching(self, patterns: tuple[str, ...]) -> list[tuple[int, str]]:
        """``*.name`` matches any call by its last part; a dotted pattern matches only a resolved callee."""
        names = tuple(pattern[2:] for pattern in patterns if pattern.startswith("*."))
        dotted = tuple(pattern for pattern in patterns if not pattern.startswith("*."))
        return sorted(
            {call for call in self.resolved if _call_matches(call[1], dotted)}
            | {call for call in (*self.resolved, *self.methods) if call[1].rpartition(".")[2] in names}
        )


def _qualified(name: str, info: ModuleInfo, local: Collection[str], target: str) -> str | None:
    """``Class.method`` on a class of this module is recorded as written: it is not a module called ``Class``."""
    return f"{name}.{target}" if "." in target and target.split(".", 1)[0] in local else absolute_callee(name, info.is_package, target)


def _calls_by_function(name: str, info: ModuleInfo) -> list[tuple[str, _FunctionCalls]]:
    local = {item.name for item in info.facts.top_level_defs}
    sites = [(line, found) for line, target in info.facts.calls if (found := _qualified(name, info, local, target)) is not None]
    sites = sorted({*sites, *info.facts.path_calls}) if info.facts.path_calls else sites
    return [
        (span.name, _FunctionCalls(inside, span.method_calls))
        for span, inside in zip(info.facts.function_spans, _calls_inside(info, sites), strict=True)
    ]


class _Hops:
    """One call away: the functions of this repository a function calls, and what each of them calls (#231)."""

    def __init__(self, modules: Mapping[str, ModuleInfo], known: Collection[str]) -> None:
        self.modules = modules
        self.known = known
        self.cache: dict[str, list[tuple[str, _FunctionCalls]]] = {}

    def functions(self, module: str) -> list[tuple[str, _FunctionCalls]]:
        if module not in self.cache:
            self.cache[module] = _calls_by_function(module, self.modules[module])
        return self.cache[module]

    def _targets(self, module: str, function: str, calls: _FunctionCalls, actions: tuple[str, ...]) -> list[tuple[str, str]]:
        """``(module, function name)`` of each callee that can be named: a helper, an imported function, ``self.method``.

        The action itself is not a place to look for its own record."""
        owner = function.rpartition(".")[0]
        action_calls = set(calls.matching(actions))
        out = [
            (module, f"{owner}.{text.partition('.')[2]}")
            for line, text in calls.methods if owner and text.partition(".")[0] in ("self", "cls") and (line, text) not in action_calls
        ]
        for _, callee in (call for call in calls.resolved if call not in action_calls):
            parts = callee.split(".")
            for end in range(len(parts) - 1, 0, -1):
                if ".".join(parts[:end]) in self.known:
                    out.append((".".join(parts[:end]), ".".join(parts[end:])))
                    break
        return out

    def reach(self, module: str, function: str, calls: _FunctionCalls, item: CompanionCall) -> bool:
        """Whether the function, or a function it calls, calls one of ``item.must_also_call``.

        A callee in a module this run did not parse (``--changed``) counts as reaching it: what was not read proves nothing."""
        if calls.matching(item.must_also_call):
            return True
        return any(
            target not in self.modules
            or any(name == wanted and inner.matching(item.must_also_call) for name, inner in self.functions(target))
            for target, wanted in self._targets(module, function, calls, item.when_calls)
        )


    def skipping_path(self, module: str, function: str, flow: Sequence[FlowNode], item: CompanionCall) -> tuple[int, int] | None:
        """A path of ``function`` that does the action and finishes with no record, as ``unrecorded_path`` reports it (#231).

        Each call of the flow is asked the two questions the whole function is: is it the action, and does it reach a record."""
        info = self.modules[module]
        local = {entry.name for entry in info.facts.top_level_defs}
        actions: set[tuple[str, bool]] = set()
        records: set[tuple[str, bool]] = set()
        for call in {(c.text, c.resolved): c for node in flow for c in node.calls}.values():
            name = _qualified(module, info, local, call.text) if call.resolved else call.text
            if name is None:
                continue
            one = _FunctionCalls([(call.line, name)], ()) if call.resolved else _FunctionCalls([], ((call.line, name),))
            if one.matching(item.when_calls):
                actions.add((call.text, call.resolved))
            if self.reach(module, function, one, item):
                records.add((call.text, call.resolved))
        return unrecorded_path(flow, actions, records)


def _companion_findings(context: RuleContext, name: str, info: ModuleInfo, hops: _Hops) -> list[Violation]:
    """A function that does an action and reaches no record of it, at all or on one of its paths (#231)."""
    local_policy = context.policy_for(name)
    if local_policy is None or not local_policy.companion:
        return []
    ring = context.resolve_ring(name)
    entries = [item for item in local_policy.companion if item.applies_to(name, ring)]
    if not entries:
        return []
    exempt = [line for line, _ in info.facts.companion_exempt]
    functions = [
        (function, calls, span.flow, any(span.start <= line <= span.end_line for line in exempt))
        for span, (function, calls) in zip(info.facts.function_spans, hops.functions(name), strict=True)
    ]
    out: list[Violation] = []
    for item in entries:
        seen: dict[str, int] = {}
        for function, calls, flow, excused in functions:
            actions = calls.matching(item.when_calls)
            if not actions or excused:
                continue
            line, where = actions[0][0], ""
            if hops.reach(name, function, calls, item):
                skipped = hops.skipping_path(name, function, flow, item) if flow else None
                if skipped is None:
                    continue
                line = skipped[0]
                where = f"; the path that finishes {f'at line {skipped[1]}' if skipped[1] else 'at the end of the function'} makes no record"
            seen[function] = seen.get(function, 0) + 1
            again = f" (definition #{seen[function]})" if seen[function] > 1 else ""
            detail = (
                f"[{name}] (ring {ring or 'none'}) {function}(){again} calls {' or '.join(item.when_calls)} on a path where neither it"
                f" nor a function it calls calls {' or '.join(item.must_also_call)}" + (f": {item.reason}" if item.reason else "")
            )
            found = ", ".join(sorted({callee for _, callee in actions}))
            hint = f" (found: {found}{where}; record it, or exempt the function with `# companion-exempt: <reason>`)"
            out.append(Violation(Rule.MISSING_COMPANION_CALL, name, detail, line, hint=hint))
    return out


def _stale_companions(policy: ArchitecturePolicy, modules: Collection[str]) -> list[Violation]:
    """A scope glob that matches no module: after a rename the rule would watch nothing."""
    return [
        Violation(Rule.MISSING_COMPANION_CALL, ", ".join(item.when_calls), f"`companion` {list(item.when_calls)}: modules glob {glob!r} matches no module")
        for item in policy.companion for glob in item.modules
        if not any(in_unit(module, glob) for module in modules)
    ]


def _stale_entries(policy: ArchitecturePolicy, modules: Collection[str]) -> list[Violation]:
    """Every per-module list of the policy that names something no longer there."""
    return (
        _stale_importable_by(policy, modules) + _stale_owned_literals(policy, modules)
        + _stale_construction_sites(policy, modules) + _stale_call_allowances(policy, modules) + _stale_companions(policy, modules)
    )


def _stale_importable_by(policy: ArchitecturePolicy, modules: Collection[str]) -> list[Violation]:
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


_ClassKey = tuple[str, str]  # (module, qualified class name)


class _Classes:
    """Every scanned class and its first-party ancestry. A base that is not a scanned class (third-party, builtin) ends a line."""

    def __init__(self, modules: Mapping[str, ModuleInfo]) -> None:
        self.modules = modules
        self.shapes: dict[_ClassKey, list[ClassMembers]] = {}
        for module, info in modules.items():
            for shape in info.facts.classes:
                self.shapes.setdefault((module, shape.name), []).append(shape)  # two under `if`/`else` are one class
        self._lineages: dict[_ClassKey, tuple[_ClassKey, ...]] = {}

    def find(self, dotted: str, seen: frozenset[str] = frozenset()) -> _ClassKey | None:
        """The class a dotted name means, following re-exports."""
        parts = dotted.split(".")
        for end in range(len(parts) - 1, 0, -1):
            module, rest = ".".join(parts[:end]), parts[end:]
            info = self.modules.get(module)
            if info is None:
                continue
            if (module, ".".join(rest)) in self.shapes:
                return module, ".".join(rest)
            for name, level, source, imported in info.facts.reexports:
                if name != rest[0] or dotted in seen:
                    continue
                raw = "." * level + source + (("." if source else "") + imported if imported else "")
                target = absolute_callee(module, info.is_package, raw)
                if target is not None:
                    return self.find(".".join([target, *rest[1:]]), seen | {dotted})
        return None

    def bases(self, key: _ClassKey) -> list[_ClassKey]:
        module = key[0]
        info = self.modules[module]
        names = (absolute_callee(module, info.is_package, base) for shape in self.shapes[key] for base in shape.bases)
        return [found for name in names if name is not None and (found := self.find(name)) is not None]

    def lineage(self, key: _ClassKey) -> tuple[_ClassKey, ...]:
        """The class and every scanned ancestor, each once (an inheritance cycle ends where it started)."""
        if key not in self._lineages:
            line, pending = [key], [key]
            while pending:
                new = [base for base in self.bases(pending.pop()) if base not in line]
                line += new
                pending += new
            self._lineages[key] = tuple(line)
        return self._lineages[key]

    def members(self, keys: Collection[_ClassKey]) -> set[str]:
        return {member for key in keys for shape in self.shapes[key] for member in shape.members}

    def is_protocol(self, key: _ClassKey) -> bool:
        return any(shape.protocol for shape in self.shapes[key])

    def is_abstract(self, key: _ClassKey) -> bool:
        return any(shape.abstract for shape in self.shapes[key])


def _implementors(binding: PortBinding, port: _ClassKey, classes: _Classes, tool_modules: Collection[str]) -> list[str]:
    """Concrete production classes that subclass the port or, for a Protocol, provide every member it names."""
    wanted = classes.members([key for key in classes.lineage(port) if classes.is_protocol(key)])
    found: list[str] = []
    for key in classes.shapes:
        module, name = key
        if key == port or classes.is_abstract(key) or module in tool_modules or not binding.counts(module, name):
            continue
        lineage = classes.lineage(key)
        if port in lineage or (wanted and wanted <= classes.members(lineage)):
            found.append(f"{module}.{name}")
    return sorted(found)


def _binding_problem(binding: PortBinding, classes: _Classes, known: Collection[str], tool_modules: Collection[str]) -> str | None:
    if binding.port_module not in known:
        return f"`port_binding`: module {binding.port_module!r} does not exist"
    port = classes.find(binding.port)
    if port is None:
        return f"`port_binding`: no class {binding.port!r} was found"
    if not classes.is_abstract(port):
        return f"`port_binding`: {binding.port!r} is not a Protocol or an abstract class"
    found = _implementors(binding, port, classes, tool_modules)
    if not found:
        problem = "declared port has no production implementor"
    elif len(found) > binding.max_implementors:
        problem = f"declared port has {len(found)} production implementors, at most {binding.max_implementors} expected: {', '.join(found)}"
    else:
        return None
    return problem + (f": {binding.reason}" if binding.reason else "")


def port_binding_findings(context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str]) -> list[Violation]:
    """``port_binding`` (#219). A whole-repository fact, so not part of ``check_modules``: ``modules`` must hold every scanned
    module, never a ``--changed`` subset. Implementors are counted across scopes: a port is often bound outside its own."""
    policies = [scope.policy for scope in context.policy.scopes] or [context.policy]
    bindings = {item.port: item for local_policy in policies for item in local_policy.port_bindings}
    if not bindings:
        return []
    classes = _Classes(modules)
    out: list[Violation] = []
    for port, binding in sorted(bindings.items()):
        if (problem := _binding_problem(binding, classes, known, context.tool_modules)) is not None:
            out.append(Violation(Rule.PORT_BINDING, port, problem))
        elif binding.shadow:  # only with exactly one implementation: `max` is 1 and nothing is wrong with the binding
            out += _shadow_notes(binding, classes, context.tool_modules)
    return out


def _reaches(target: str) -> bool:
    """Whether naming ``target`` can touch anything: a constant or an exception class (``subprocess.DEVNULL``) cannot."""
    last = target.rpartition(".")[2]
    return not (last.isupper() or last.endswith(("Error", "Exception", "Expired", "Warning")))


def _function_at(info: ModuleInfo, line: int) -> str:
    """The function or method holding ``line``, or "" for module-level code."""
    return next((span.name for span in info.facts.function_spans if span.start <= line <= span.end_line), "")


def _own_functions(classes: _Classes, adapter: _ClassKey) -> set[tuple[str, str]]:
    """``(module, function)`` of the implementation: the methods of the class and its scanned bases, what those call in
    their own module (transitively; a class called there brings all its methods), and that module's top-level code."""
    out: set[tuple[str, str]] = set()
    for module in {key[0] for key in classes.lineage(adapter)}:
        info = classes.modules[module]
        calls = _calls_by_function(module, info)
        local_classes = [name for owner, name in classes.lineage(adapter) if owner == module]
        pending = [f"{name}." for name in local_classes]  # "X." means every method of X
        # a class made inside a factory has no spans of its own: the factory holds its code
        pending += [name.rsplit(".", up)[0] for name in local_classes for up in range(1, name.count(".") + 1)]
        seen: set[str] = set()
        while pending:
            wanted = pending.pop()
            for name, inside in calls:
                if name in seen or not (name == wanted or (wanted.endswith(".") and name.startswith(wanted))):
                    continue
                seen.add(name)
                for _, callee in inside.resolved:
                    if callee.startswith(f"{module}."):
                        local = callee.removeprefix(f"{module}.")
                        pending += [local, f"{local}."]
        out |= {(module, name) for name in seen | {""}}
    return out


def _shadow_notes(binding: PortBinding, classes: _Classes, tool_modules: Collection[str]) -> list[Violation]:
    """``port-shadow`` (#219), an observation: a function that uses an effect category the one implementation owns."""
    port = classes.find(binding.port)
    found = _implementors(binding, port, classes, tool_modules) if port is not None else []
    adapter = classes.find(found[0]) if found else None
    if adapter is None:
        return []
    own = _own_functions(classes, adapter)
    used = {
        use.category for module in {module for module, _ in own} for use in classes.modules[module].facts.effect_uses
        if (module, _function_at(classes.modules[module], use.line)) in own
    }
    out = [
        Violation(Rule.PORT_SHADOW, binding.port, f"`shadow` lists {category}, but {found[0]} uses no {category} effect the scan can see: nothing to compare")
        for category in binding.shadow if category not in used
    ]
    watched = used.intersection(binding.shadow)
    reached: dict[tuple[str, str, str], list[EffectUse]] = {}
    for module, info in classes.modules.items():
        if module in tool_modules:
            continue
        for use in info.facts.effect_uses:  # a seam (a parameter default) is injection, not a use
            if use.category not in watched or use.seam or not _reaches(use.target):
                continue
            function = _function_at(info, use.line)
            full = f"{module}.{function}" if function else module
            if (module, function) in own or not binding.counts(module, function) or any(in_unit(full, glob) for glob in binding.shadow_allow):
                continue
            reached.setdefault((module, function, use.category), []).append(use)
    for (module, function, category), uses in reached.items():
        where = f"{function}()" if function else "module-level code"
        targets = ", ".join(sorted({use.target for use in uses}))
        detail = f"{where} uses {category} ({targets}) outside {found[0]}, the one implementation of {binding.port}"
        hint = " (go through the port, or list the module or function in this `port_binding`'s `shadow_allow`)"
        out.append(Violation(Rule.PORT_SHADOW, module, detail, min(use.line for use in uses), hint=hint))
    return sorted(out, key=lambda item: (item.subject, item.detail))


def _folded(name: str) -> str:
    """How two spellings of one word compare: ``_ratchet_spec`` and ``RatchetSpec`` are the same."""
    return name.replace("_", "").lower()


def _last_word(name: str) -> str:
    """The head noun of a CamelCase name, lowercased: ``ArchitectureScope`` -> ``scope``."""
    words = re.findall(r"[A-Z]+(?![a-z0-9])|[A-Z]?[a-z0-9]+", name)
    return str(words[-1]).lower() if words else ""


_Types = list[tuple[str, str]]  # (module, public module-level class name)


def _named_types(setting: NameCollisionPolicy, types: _Types, decl: NameDecl) -> _Types:
    """The types ``decl`` reuses the name of; none when its annotation says it holds that concept after all."""
    key = _folded(decl.name)
    if decl.name.strip("_").isupper() or key in {_folded(name) for name, _ in setting.allow}:
        return []
    words = {_folded(word) for word in setting.words}
    last = decl.name.strip("_").rpartition("_")[2].lower()
    if decl.kind == "function" and last in words:  # `_in_scope` speaks of a scope; only a declared word is read this way
        key = last
    if any(_folded(identifier).endswith(key) for identifier in decl.annotation):
        return []
    declared_word = key in words
    if decl.kind in ("function", "parameter") and not declared_word:  # a signature often repeats a type's name innocently
        return []
    return [
        (module, name) for module, name in types
        if (_folded(name) == key and (declared_word or len(key) >= setting.min_length)) or (declared_word and _last_word(name) == key)
    ]


def _module_collisions(setting: NameCollisionPolicy, types: _Types, module: str, source: str) -> list[Violation]:
    out: dict[str, Violation] = {}  # one note per name and owner: the first declaration
    for decl in name_declarations(ast.parse(source.removeprefix("\ufeff"))):
        found = _named_types(setting, types, decl)
        if not found or any(owner == module for owner, _ in found):
            continue
        names = ", ".join(sorted(f"{owner}.{name}" for owner, name in found))
        spelled = f"{decl.owner}.{decl.name}" if decl.owner else decl.name
        detail = f"{decl.kind} '{spelled}' shares its name with the type {names}"
        hint = " (rename it, or give the name a reason in `name_collisions.allow`)"
        _ = out.setdefault(detail, Violation(Rule.NAME_COLLISION, module, detail, decl.line, hint=hint))
    return list(out.values())


def name_collision_findings(context: RuleContext, modules: Mapping[str, ModuleInfo], sources: Mapping[str, str]) -> list[Violation]:
    """``name-collision`` (#221), an observation. The vocabulary is every scanned public class of the same scope, so ``modules``
    must be a whole-repository scan; ``sources`` holds the text of the modules to look at."""
    setting = context.policy.name_collisions
    if setting is None:
        return []
    vocabulary: dict[str | None, _Types] = {}
    for module, info in sorted(modules.items()):
        if module not in context.tool_modules:
            vocabulary.setdefault(context.scope_name(module), []).extend(
                (module, item.name) for item in info.facts.top_level_defs if item.is_class and not item.name.startswith("_")
            )
    return [
        found
        for module, source in sorted(sources.items())
        for found in _module_collisions(setting, vocabulary.get(context.scope_name(module), []), module, source)
    ]


def _sibling_index(known: Collection[str]) -> dict[str, list[str]]:
    """Last component -> dotted first-party modules, for naming what a bare sibling import was probably meant to be."""
    index: dict[str, list[str]] = {}
    for module in sorted(known):
        if "." in module:
            index.setdefault(module.rsplit(".", 1)[1], []).append(module)
    return index


def _external_imports(
    context: RuleContext, name: str, ring: str, line: int, tops: Collection[str], third_party: ThirdParty,
    siblings: Mapping[str, list[str]],
) -> list[Violation]:
    policy = context.require_policy(name)  # the scope's rules; the target Python (`stdlib`) is the root's
    rule = policy.ring(ring)
    out: list[Violation] = []
    for top in tops:
        is_stdlib = top in context.policy.stdlib
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


def _os_branching(policy: ArchitecturePolicy, name: str, ring: str, rule: RingRule, info: ModuleInfo) -> list[Violation]:
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
    policy = context.require_policy(name)  # the test module's scope rules; `practices` is whole-run configuration
    practices = context.policy.practices
    tests_folder = practices.get("effect-patch", practices.get("secrets-guard"))
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
    policy: ArchitecturePolicy, known: Collection[str], entry_deps: Mapping[str, tuple[str, ...] | None], third_party: ThirdParty
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


def _constraint_findings(context: RuleContext, name: str, info: ModuleInfo, imports: tuple[Collection[str], Collection[str]]) -> list[Violation]:
    """``[package.constraints]`` (#165): effect and import facts for any module, placement for a module that has a ring. Tooling is exempt."""
    constraints = context.policy.package_constraints
    local_policy = context.policy_for(name)
    if constraints == NO_CONSTRAINTS or local_policy is None or name in context.tool_modules:
        return []
    ring = context.resolve_ring(name)
    found = use_violations(constraints, local_policy, info, ring, imports)
    return found if ring is None else found + placement_violations(constraints, local_policy, info, ring)


def _untokenizable_findings(name: str, info: ModuleInfo) -> list[Violation]:
    """A file Python parses but cannot tokenize (a doubled carriage return, #181): its comment facts are unknown, so say so.

    The detail is fixed, so the baseline key survives edits; the tokenizer's line and message ride in the hint."""
    if not info.facts.untokenizable:
        return []
    line, _, message = info.facts.untokenizable.partition(": ")
    detail = (
        "the source parses but does not tokenize, so its `# noqa`, `# type: ignore` and `# deferred:` comments were not read; "
        "fix the stray carriage return or similar byte"
    )
    return [Violation(Rule.UNTOKENIZABLE_SOURCE, name, detail, int(line) if line.isdigit() else 0, hint=f" (tokenizer: {message})")]


def _module_preamble(context: RuleContext, name: str, info: ModuleInfo, known: Collection[str], tops: Collection[str]) -> list[Violation]:
    """Documentation, annotation, tooling-import and forbidden-name findings, which apply whether or not the module has a ring."""
    is_tool = name in context.tool_modules
    out: list[Violation] = _docs_findings(context, name, info) + _untokenizable_findings(name, info)
    detail = context.annotation_violation(name)
    if detail is not None:
        out.append(Violation(Rule.INVALID_ARBOR_ANNOTATION, name, detail))
    mismatch = None if is_tool else context.annotation_ring_mismatch(name)
    if mismatch is not None:
        out.append(Violation(Rule.ARBOR_RING_MISMATCH, name, mismatch))
    if not is_tool:
        out += _dev_tool_imports(context, name, info, known, tops)
    local_policy = context.policy_for(name)
    out += _constraint_findings(context, name, info, (known, tops))
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
    pending_effect_edges: list[EffectEdge] = []
    hops = _Hops(modules, known)
    for name, info in sorted(modules.items()):
        local_policy = context.policy_for(name)
        is_tool = name in context.tool_modules  # developer tooling: no ring, may import product code
        out += _module_preamble(context, name, info, known, tops)
        out += (
            _owned_literal_findings(context, name, info)
            + _construction_findings(context, name, info)
            + _call_allowlist_findings(context, name, info)
            + _companion_findings(context, name, info, hops)
            + _result_discard_findings(context, name, info, modules)
            + _private_access_findings(context, name, info, known)
        )
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
            record_effect_edge(context, effect_graph, pending_effect_edges, EffectImport(local_policy, name, ring, imp, internal))
            if error is not None:
                out.append(Violation(Rule.BAD_IMPORT, name, error, imp.line))
            out += (
                _scope_imports(context, name, imp.line, internal) + _scope_effect_leaks(context, modules, name, imp.line, internal)
                + unsanctioned_effect_imports(context, modules, EffectImport(local_policy, name, ring, imp, internal))
            )
            out += _internal_imports(context, name, ring, imp.line, internal) + _forwarded_imports(context, name, imp, _ImportEnv(modules, known, tops, import_graph))
            out += _external_imports(context, name, ring, imp.line, external, third_party, siblings)
        out += (
            _os_branching(local_policy, name, ring, rule, info)
            + _effect_uses(context, name, ring, info, known) + effect_at_import_findings(context, ring, info, (modules, absolute_callee))
            + _seam_bypass_findings(context, name, ring, info)
            + _abstraction_violations(name, ring, rule, info)
            + _typed_port_violations(name, ring, rule, info)
            + _reexport_findings(name, ring, rule, info, (known, tops))
        )
    out += effect_via_import_violations(context, modules, effect_graph, pending_effect_edges)
    out = with_move_closures(out, context, modules, known, tops)
    out += _unguarded_state_findings(context, modules, known) + mixed_purity_findings(context, modules, known, import_graph)
    out += _argument_restrictions(context, modules, known)
    return out + _finalize_checks(context, known, entry_deps, import_graph, third_party_by_scope)


def _argument_restrictions(context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str]) -> list[Violation]:
    """Every ``[[construction]]`` and derived-marker finding: each module's calls, then the entries that went stale (#234)."""
    classes = ClassIndex(modules)
    out = [violation for name, info in sorted(modules.items()) for violation in _argument_findings(context, name, info, classes)]
    policy = context.policy
    if not policy.scopes:
        return out + _stale_construction_entries(policy, known, classes)
    for scope in policy.scopes:
        out += _stale_construction_entries(scope.policy, [n for n in known if context.scope_name(n) == scope.name], classes)
    return out


def _unguarded_state_findings(context: RuleContext, modules: Mapping[str, ModuleInfo], known: Collection[str]) -> list[Violation]:
    """Module-level state written without a lock (#232): one finding per module, name and writing function, plus stale ``allow`` entries.

    A failure in ``unguarded_module_state.rings`` (every ring when unset), a note in the others."""
    setting = context.policy.module_state
    if setting is None:
        return []
    out: list[Violation] = []
    used: set[StateAllow] = set()
    for name, info in sorted(modules.items()):
        ring = context.resolve_ring(name)
        if ring is None or name in context.tool_modules:
            continue
        rule = Rule.UNGUARDED_MODULE_STATE if setting.fails_in(ring) else Rule.UNGUARDED_MODULE_STATE_NOTE
        found: dict[tuple[str, str], int] = {}
        for write in _unlocked_writes(setting, modules, known, name, info):
            allowing = setting.allowing(name, write.name)
            used.update(allowing)
            if not allowing:
                found.setdefault((write.name, write.function), write.line)  # writes are sorted by line: the first one is reported
        out.extend(
            Violation(rule, name, f"[{ring}] module-level state {state!r} is written in {function}() outside a lock", line)
            for (state, function), line in found.items()
        )
    return out + _stale_state_allow(setting, modules, known, used)


def _unlocked_writes(
    setting: ModuleStatePolicy, modules: Mapping[str, ModuleInfo], known: Collection[str], name: str, info: ModuleInfo,
) -> list[StateWrite]:
    """The module's writes that neither a configured guard nor another module's lock covers."""
    out: list[StateWrite] = []
    for write in info.facts.state_writes:
        held = tuple(filter(None, (absolute_callee(name, info.is_package, item) for item in write.held)))
        decorators = tuple(filter(None, (absolute_callee(name, info.is_package, item) for item in write.decorators)))
        if not (setting.names_guard(held, decorators) or any(_is_module_lock(item, modules, known) for item in held)):
            out.append(write)
    return out


def _is_module_lock(dotted: str, modules: Mapping[str, ModuleInfo], known: Collection[str]) -> bool:
    """Whether ``dotted`` is a lock another module defines. A known module a partial scan did not read gets the benefit of the doubt."""
    owner, _, attribute = dotted.rpartition(".")
    info = modules.get(owner)
    return owner in known if info is None else attribute in info.facts.module_locks


def _stale_state_allow(
    setting: ModuleStatePolicy, modules: Mapping[str, ModuleInfo], known: Collection[str], used: Collection[StateAllow],
) -> list[Violation]:
    out: list[Violation] = []
    for entry in setting.allow:
        matches = [module for module in known if in_unit(module, entry.module)]
        if entry in used or not all(module in modules for module in matches):  # a partial scan cannot call an entry stale
            continue
        why = "no unguarded write matches; remove the entry" if matches else "matches no module"
        out.append(Violation(Rule.UNGUARDED_MODULE_STATE, entry.module, f"`unguarded_module_state.allow` {entry.name!r}: {why}"))
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
        found += _external_imports(context, name, ring, imp.line, external, {}, {})
    found += _os_branching(policy, name, ring, policy.ring(ring), info)
    found += _effect_uses(context, name, ring, info, known)
    found += effect_at_import_findings(context, ring, info, ({name: info}, absolute_callee))  # direct and same-module only: other modules are not at hand
    found += _seam_bypass_findings(context, name, ring, info)
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
        return circular + _entry_dependencies(policy, known, entry_deps, third_party_by_scope.get(None, {})) + _stale_entries(policy, known)
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
        out.extend(_stale_entries(scope.policy, scope_modules))
    return out


def _circular_imports(graph: Mapping[str, Collection[str]]) -> list[Violation]:
    """Report each strongly connected import component, including direct self-imports.

    Import edges include lazy and type-only imports because they still create architectural dependencies.
    """
    return [
        Violation(Rule.CIRCULAR_IMPORT, ", ".join(sorted(component)), "modules form a circular import dependency")
        for component in cyclic_components(graph)
    ]


def _imported_by_test(path: str, tests: Mapping[str, SourceFacts], project: tuple[Collection[str], Collection[str]]) -> set[str]:
    """The first-party modules a test file imports itself, through its folders' ``conftest.py``, or through a helper beside the tests."""
    helpers = {item.rpartition("/")[2].removesuffix(".py"): item for item in tests}
    beside = {file_module(item)[0] for item in tests} | {""}
    folders = path.split("/")[:-1]
    pending = [path, *(conftest for end in range(len(folders) + 1) if (conftest := "/".join([*folders[:end], "conftest.py"])) in tests)]
    seen: set[str] = set()
    found: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        name, is_package = file_module(current)
        for imp in tests[current].imports:
            internal, external, _ = resolve_runtime_targets(name, is_package, imp, *project)
            found.update(internal)
            if beside.issuperset(internal):  # not a production module, so it may be a helper beside the tests
                pending += [helpers[part] for part in [*external, *imp.module.split("."), *imp.names] if part in helpers]
    return found


def _import_closure(roots: set[str], modules: Mapping[str, ModuleInfo], project: tuple[Collection[str], Collection[str]]) -> set[str]:
    """Every module that importing or running ``roots`` can load (function-level imports too, ``TYPE_CHECKING`` ones not)."""
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        info = modules.get(current)
        if current in seen or info is None:
            continue
        seen.add(current)
        for imp in info.facts.imports:
            if not imp.type_only:
                pending += resolve_runtime_targets(current, info.is_package, imp, *project)[0]
    return seen


def unreachable_patches(
    modules: Mapping[str, ModuleInfo], known: Collection[str], tests: Mapping[str, SourceFacts], exempt: Collection[str] = (),
) -> list[Violation]:
    """``unreachable-patch`` (#226): a test patches ``X.name`` and reaches a module that calls its own ``from X import name`` binding.

    A test that also patches the caller's binding, or never imports the caller (directly or transitively), is not reported."""
    project = (known, {module.split(".")[0] for module in known})
    production = {name: info for name, info in modules.items() if info.path not in tests}
    patches = patched_targets(tests)
    reached: dict[str, set[str]] = {}
    out: list[Violation] = []
    for name, origin, local, line in unreached_bindings(production, known):
        if any(in_unit(f"{name}.{local}", glob) or in_unit(name, glob) for glob in exempt):
            continue
        where: list[str] = []
        for path, targets in sorted(patches.items()):
            if origin not in targets or f"{name}.{local}" in targets:
                continue
            if path not in reached:
                reached[path] = _import_closure(_imported_by_test(path, tests, project), production, project)
            if name in reached[path]:
                where.append(f"{path}:{targets[origin]}")
        if where:
            owner = origin.rpartition(".")[0]
            detail = (
                f"[{name}] calls {origin} through its own `from {owner} import` binding {local!r}, which a test's patch of {origin} "
                f"never reaches; import {owner} and call it qualified, or patch {name}.{local}"
            )
            more = f" and {len(where) - 3} more" if len(where) > 3 else ""
            out.append(Violation(Rule.UNREACHABLE_PATCH, name, detail, line, hint=f" (patched at {', '.join(where[:3])}{more})"))
    return out


def shadowed_callee_findings(sources: Mapping[str, str]) -> list[Violation]:
    """``shadowed-callee`` (#215), from each module's source text."""
    return [
        Violation(
            Rule.SHADOWED_CALLEE, module,
            f"[{function}] assigns {name!r} from a call to {name!r}: the assignment makes the name local to the whole function, "
            "so the call raises UnboundLocalError; rename the local",
            line,
        )
        for module, source in sorted(sources.items())
        for line, function, name in shadowed_callees(source)
    ]


# `mock.patch` creates a builtin's name on a module by itself, so patching one a module does not define is on purpose.
_BUILTIN_NAMES = frozenset({
    "abs", "aiter", "all", "anext", "any", "ascii", "bin", "bool", "breakpoint", "bytearray", "bytes", "callable", "chr",
    "classmethod", "compile", "complex", "copyright", "credits", "delattr", "dict", "dir", "divmod", "enumerate", "eval",
    "exec", "exit", "filter", "float", "format", "frozenset", "getattr", "globals", "hasattr", "hash", "help", "hex", "id",
    "input", "int", "isinstance", "issubclass", "iter", "len", "license", "list", "locals", "map", "max", "memoryview",
    "min", "next", "object", "oct", "open", "ord", "pow", "print", "property", "quit", "range", "repr", "reversed", "round",
    "set", "setattr", "slice", "sorted", "staticmethod", "str", "sum", "super", "tuple", "type", "vars", "zip",
})


class PatchSources(NamedTuple):
    """What ``unresolved_patch_targets`` reads besides facts: texts and the file tree, supplied by the caller."""

    read: Callable[[str], str | None]  # the text of a file by its path relative to the root, None when unreadable
    on_disk: Callable[[str, str], bool]  # whether a package's ``__init__`` path has a submodule of this name beside it


def _missing_attribute(dotted: str, modules: Mapping[str, ModuleInfo], known: Collection[str]) -> tuple[ModuleInfo, str] | None:
    """The first-party module a patch target goes through and the next name of the target, when it is worth looking up."""
    parts = dotted.split(".")
    owner = next((name for end in range(len(parts) - 1, 0, -1) if (name := ".".join(parts[:end])) in known), None)
    if owner is None or owner not in modules or dotted in known:
        return None
    attribute = parts[owner.count(".") + 1]
    dunder = attribute.startswith("__") and attribute.endswith("__")
    return None if dunder or attribute in _BUILTIN_NAMES else (modules[owner], attribute)


class _Attributes:
    """Each patched module's attribute names, read once."""

    def __init__(self, sources: PatchSources) -> None:
        self._sources = sources
        self._found: dict[str, frozenset[str] | None] = {}

    def lacks(self, info: ModuleInfo, attribute: str) -> bool:
        """Certainly absent: not bound by the module's code, not a submodule beside it, and the module is not dynamic."""
        if info.path not in self._found:
            text = self._sources.read(info.path)
            self._found[info.path] = None if text is None else module_attributes(text)
        names = self._found[info.path]
        if names is None or attribute in names:
            return False
        return not (info.is_package and self._sources.on_disk(info.path, attribute))


def _strict_patches(path: str, facts: SourceFacts, sources: PatchSources) -> dict[str, int]:
    """Dotted target -> first line of a patch of it that needs the attribute to exist."""
    name, is_package = file_module(path)
    text = sources.read(path)
    optional = optional_patch_lines(text) if text is not None else frozenset()
    found: dict[str, int] = {}
    for line, target in facts.patches:
        if line not in optional and (dotted := absolute_callee(name, is_package, target)) is not None:
            found.setdefault(dotted, line)
    return found


def unresolved_patch_targets(
    modules: Mapping[str, ModuleInfo], known: Collection[str], tests: Mapping[str, SourceFacts], sources: PatchSources,
    exempt: Collection[str] = (),
) -> list[Violation]:
    """``unresolved-patch-target`` (#215): a test patches ``module.name`` and the first-party ``module`` has no ``name``.

    One finding per test file and target, so the list is every test to retarget. Only the first name after the longest known
    module is looked up (``module.Class.method`` checks ``Class``); builtins, dunders and patches that may create the name
    (``create=True``, ``raising=False``) are left out."""
    attributes = _Attributes(sources)
    out: list[Violation] = []
    for path, facts in sorted(tests.items()):
        if not facts.patches:
            continue
        for dotted, line in sorted(_strict_patches(path, facts, sources).items()):
            found = _missing_attribute(dotted, modules, known)
            if found is None or any(in_unit(dotted, glob) for glob in exempt) or not attributes.lacks(*found):
                continue
            info, attribute = found
            detail = (
                f"patches {dotted}, but module {info.name} has no attribute or submodule {attribute!r}; "
                "retarget the patch to where the name lives now"
            )
            out.append(Violation(Rule.UNRESOLVED_PATCH_TARGET, path, detail, line))
    return out
