"""Arborescent's own checks, as pure functions over already-parsed modules.

Rules (ids in ``domain.Rule``): ``unclassified``, ``internal-import``, ``external-import``, ``os-branching``,
``bad-import``, ``forbidden-module`` and ``undeclared-entry-dependency``. Lazy (function-level) and ``TYPE_CHECKING`` imports count. How much of the external
and OS-branching rules applies is the policy's preset; layering and classification are never relaxed.
"""

from __future__ import annotations

import fnmatch
import re
from bisect import bisect_right
from collections.abc import Collection, Iterator, Mapping
from typing import NamedTuple

from .call_facts import absolute_callee
from .domain import (
    ALL,
    EFFECT_RULES,
    ArchitecturePolicy,
    ArchitectureScope,
    CallAllowance,
    CompanionCall,
    ConstructionSite,
    OwnedLiteral,
    Policy,
    RingRule,
    Rule,
    SeamBypass,
    Violation,
    dist_name,
    in_unit,
)
from .effect_facts import EffectUse
from .effect_import_rules import (
    EffectEdge,
    EffectImport,
    effect_at_import_findings,
    effect_via_import_violations,
    record_effect_edge,
    unsanctioned_effect_imports,
)
from .facts import Forwarder, ModuleInfo, SeamFrame, SeamUse
from .import_cycles import cyclic_components
from .import_facts import RawImport, resolve_targets
from .package_constraint_rules import placement_violations, use_violations
from .package_constraints import PackageConstraints
from .purity_rules import mixed_purity_findings, with_move_closures
from .rule_context import RuleContext

__all__ = ["RuleContext", "check_modules", "fits_ring"]

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
    """The module's recorded call sites as ``(line, absolute dotted callee)``, in line order."""
    return [(line, callee) for line, target in info.facts.calls if (callee := absolute_callee(name, info.is_package, target)) is not None]


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


def _calls_by_function(name: str, info: ModuleInfo) -> list[tuple[str, _FunctionCalls]]:
    local = {item.name for item in info.facts.top_level_defs}
    sites: list[tuple[int, str]] = []
    for line, target in info.facts.calls:
        # `Class.method` on a class of this module is recorded as written: it is not a module called `Class`
        qualified = f"{name}.{target}" if "." in target and target.split(".", 1)[0] in local else absolute_callee(name, info.is_package, target)
        if qualified is not None:
            sites.append((line, qualified))
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


def _companion_findings(context: RuleContext, name: str, info: ModuleInfo, hops: _Hops) -> list[Violation]:
    """A function that does an action and reaches no record of it (#231). Not path-sensitive: a record on any branch counts."""
    local_policy = context.policy_for(name)
    if local_policy is None or not local_policy.companion:
        return []
    ring = context.resolve_ring(name)
    entries = [item for item in local_policy.companion if item.applies_to(name, ring)]
    if not entries:
        return []
    exempt = [line for line, _ in info.facts.companion_exempt]
    functions = [
        (function, calls, any(span.start <= line <= span.end_line for line in exempt))
        for span, (function, calls) in zip(info.facts.function_spans, hops.functions(name), strict=True)
    ]
    out: list[Violation] = []
    for item in entries:
        seen: dict[str, int] = {}
        for function, calls, excused in functions:
            actions = calls.matching(item.when_calls)
            if not actions or excused or hops.reach(name, function, calls, item):
                continue
            seen[function] = seen.get(function, 0) + 1
            again = f" (definition #{seen[function]})" if seen[function] > 1 else ""
            detail = (
                f"[{name}] (ring {ring or 'none'}) {function}(){again} calls {' or '.join(item.when_calls)} but neither it nor a function"
                f" it calls calls {' or '.join(item.must_also_call)}" + (f": {item.reason}" if item.reason else "")
            )
            found = ", ".join(sorted({callee for _, callee in actions}))
            hint = f" (found: {found}; record it, or exempt the function with `# companion-exempt: <reason>`)"
            out.append(Violation(Rule.MISSING_COMPANION_CALL, name, detail, actions[0][0], hint=hint))
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
        out += _os_branching(local_policy, name, ring, rule, info)
        out += _effect_uses(context, name, ring, info, known) + effect_at_import_findings(context, ring, info, (modules, absolute_callee))
        out += _seam_bypass_findings(context, name, ring, info)
        out += (
            _abstraction_violations(name, ring, rule, info)
            + _typed_port_violations(name, ring, rule, info)
            + _reexport_findings(name, ring, rule, info, (known, tops))
        )
    out += effect_via_import_violations(context, modules, effect_graph, pending_effect_edges)
    out = with_move_closures(out, context, modules, known, tops)
    return out + mixed_purity_findings(context, modules, known, import_graph) + _finalize_checks(context, known, entry_deps, import_graph, third_party_by_scope)


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
