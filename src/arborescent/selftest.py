"""``selftest``: mutation-test the policy. For each rule, inject a forbidden construct into a temp COPY of the code
and require the checks to report it; a mutation that goes unreported is a rule that does not actually bite.

Ported from the throwaway mutation script behind DeepInterview's ``tests/test_architecture.py``.
"""

from __future__ import annotations

import ast
import fnmatch
import io
import tokenize
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from .admission_probes import admission_probes
from .construct_only_probes import construct_only_mutations
from .domain import NOISE_DIRS, ArchitectureScope, Mutation, Policy, Rule
from .engine import Services, package_checks, run_own
from .facts import ModuleInfo
from .importable_by_probes import importable_by_mutations
from .module_forbid_probes import module_forbid_mutations
from .owned_literal_probes import owned_literal_mutations
from .package_constraint_probes import constraint_mutations
from .ports import PackageProbes
from .purity_probes import mixed_purity_mutations


class ScanView(Protocol):
    """The subset of ``scanner.ScanData`` this module plans mutations from.

    A structural type, not an import of ``scanner``: ``selftest`` is in the ``application`` ring, which may not
    import the ``adapters`` ring that ``scanner`` belongs to.
    """

    @property
    def paths(self) -> Mapping[str, str]: ...

    @property
    def known(self) -> frozenset[str]: ...

    @property
    def arbor_index(self) -> Mapping[str, tuple[str | None, str | None]]: ...

    @property
    def scope_by_module(self) -> Mapping[str, str]: ...

    @property
    def modules(self) -> Mapping[str, ModuleInfo]: ...


__all__ = ["NO_MUTATION", "Mutation", "SelfTestReport", "selftest"]

_SKIP_COPY = (*sorted(NOISE_DIRS), "*.pyc")

_PRACTICE = "repository practice: selftest mutates source files only and runs no practices"
# Rules with no mutation, and why. A test fails when a rule is in neither this table nor ``plan``'s output.
NO_MUTATION: dict[Rule, str] = {
    Rule.MIXED_PURITY_EXCLUDED: "informational note about files the policy excludes; the scan does not read them as modules; covered by tests/test_mixed_purity.py",
    Rule.REEXPORT_RATIO: "needs a module whose whole public surface is first-party imports, which one appended statement cannot make; covered by tests/test_reexport_ratio.py",
    Rule.FORBIDDEN_MODULE: "fires on a module name the policy forbids; selftest probes use fixed names no `forbidden_modules` pattern is written for",
    Rule.BAD_IMPORT: "a relative import above the source root is a syntax-level mistake, not a policy-dependent rule",
    Rule.GITIGNORE_SHADOW: _PRACTICE,
    Rule.SIBLING_VERSIONS: _PRACTICE,
    Rule.SECRETS_GUARD: _PRACTICE,
    Rule.DOCS_SYNC: _PRACTICE,
    Rule.TEST_REFERENCE: _PRACTICE,
    Rule.ADAPTER_EQUIVALENCE: _PRACTICE,
    Rule.ADAPTER_EQUIVALENCE_REPORT: _PRACTICE,
    Rule.NOISE_DIR_CODE: "requires a Git-tracked Python file under a noise directory; selftest works on a temporary non-Git copy",
    Rule.WEAKLY_TYPED_PORT: "needs `require_typed_ports` on a ring and a Protocol with an untyped return; covered by focused rule tests",
    Rule.TACH_INVISIBLE_IMPORT: "informational and never baselined; focused tests cover it",
    Rule.RESULT_DISCARD: "opt-in and resolved through imports; focused tests cover each call form, the convention and ring scoping",
    Rule.CLASSIFY_FACTS: "informational and never baselined; a mutation cannot make a ring wrong, focused tests cover each signal",
    Rule.VENDOR_FOLDER: "tracks whole-tree fingerprints; focused tests cover file additions, edits and removals",
    Rule.WILDCARD_RING: "a policy-level rule; selftest mutations only edit copied source files",
    Rule.EFFECT_ENV: "effect-env is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_IDENTITY: "effect-identity is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_RANDOMNESS: "effect-randomness is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_SLEEP: "effect-sleep is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_THREADS: "effect-threads is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_PROCESS: "effect-process is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.DEV_TOOL_IMPORT: "needs a `dev_tools` path in the policy; covered by focused rule tests",
    Rule.DEV_TOOL_PRODUCT_IMPORT: "needs `dev_tools` and `dev_tools_may_import` in the policy; covered by focused rule tests",
    Rule.DEV_TOOL_MISPLACED: "needs a `dev_tools` path in the policy; covered by focused rule tests",
    Rule.EFFECT_NETWORK: "effect-network is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_DATABASE: "effect-database is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_FILESYSTEM: "effect-filesystem is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_DYNAMIC_IMPORT: "effect-dynamic-import is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_PATCH: "effect-patch requires a configured tests folder and project-package target; covered by focused rule tests",
    Rule.EFFECT_CONSOLE: "effect-console is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_STATE: "effect-state is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_MONOTONIC: "effect-monotonic is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_HOST: "effect-host is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_FS_QUERY: "effect-fs-query is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_UNSANCTIONED_IMPORT: "needs `effects.sanctioned` in the policy; covered by focused rule tests",
    Rule.FORWARDED_IMPORT: "needs a forwarding module and a `module_forbid_imports` entry; covered by focused rule tests",
    Rule.SCOPE_IMPORT: "cross-scope boundaries are mutation-tested when a project configures multiple scopes",
    Rule.SCOPE_EFFECT_LEAK: "cross-scope effect leaks are mutation-tested when a project configures multiple "
    "scopes with a declared public dependency",
    Rule.CRLF: _PRACTICE,
    Rule.UNDECLARED_IMPORTS: _PRACTICE,
    Rule.STALE_CONFIG_PATH: _PRACTICE,
    Rule.SOURCE_ROOT_DRIFT: _PRACTICE,
}


@dataclass
class SelfTestReport:
    killed: list[str]
    survived: list[str]
    skipped: list[str]

    @property
    def ok(self) -> bool:
        return not self.survived


def plan(
    policy: Policy, scan: ScanView, packages: PackageProbes | None = None,
) -> tuple[list[Mutation], list[str]]:
    """The mutations for this policy, plus the ones that cannot be built (and why).

    ``packages`` (the catalog and its facts) adds the package-assignment probes; without it there are none.
    """
    paths, known, arbor_index, owners = scan.paths, scan.known, scan.arbor_index, scan.scope_by_module
    out: list[Mutation] = []
    skipped: list[str] = []
    local_policies = (
        tuple((scope.name, scope.policy, frozenset(name for name in known if owners.get(name) == scope.name)) for scope in policy.scopes)
        if policy.scopes else (("", policy, known),)
    )
    for _, local_policy, local_known in local_policies:
        local_paths = {name: paths[name] for name in local_known if name in paths}
        found, local_skipped = _local_mutations(local_policy, local_paths, local_known, arbor_index, scan.modules)
        out.extend(found)
        skipped.extend(local_skipped)
    anchor = next(iter(sorted(known)), None)
    circular_anchor = next((
        name
        for _, local_policy, local_known in local_policies
        if local_policy.forbid_circular_imports
        for name in sorted(local_known)
        if name in paths
    ), None)
    if circular_anchor is None:
        skipped.append("circular-import: disabled by policy")
    if anchor is not None:
        out.extend(_anchor_mutations(anchor, paths[anchor]))
        if _rejects_doubled_cr():
            out.append(Mutation(
                Rule.UNTOKENIZABLE_SOURCE, "a doubled carriage return Python parses but cannot tokenize", paths[anchor], "_arborescent_probe_cr = 1\r\r",
            ))
        else:
            skipped.append("untokenizable-source: this interpreter's tokenizer accepts a doubled carriage return (Python before 3.12)")
    if circular_anchor is not None:
        out.append(Mutation(
            Rule.CIRCULAR_IMPORT, "a module importing itself", paths[circular_anchor], f"import {circular_anchor}",
        ))
    import_found, import_skipped = _import_propagation_mutations(policy, scan)
    out.extend(import_found)
    skipped.extend(import_skipped)
    constraint_found, constraint_skipped = constraint_mutations(policy, paths, [(local, known) for _, local, known in local_policies], arbor_index)
    out.extend(constraint_found)
    skipped.extend(constraint_skipped)
    if policy.scopes:
        out.extend(_scope_boundary_mutations(policy, paths, known, owners))
        out.extend(_scope_effect_mutations(policy, scan))
    if packages is not None:
        package_found, package_skipped = packages.mutations(policy, scan.arbor_index, scan.scope_by_module)
        out.extend(package_found)
        skipped.extend(package_skipped)
    return out, sorted(set(skipped))


def _local_mutations(
    local_policy: Policy, local_paths: dict[str, str], local_known: frozenset[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
    modules: Mapping[str, ModuleInfo],
) -> tuple[list[Mutation], list[str]]:
    """The mutations for one policy (the whole policy, or one scope of it), plus the ones that cannot be built."""
    out: list[Mutation] = []
    skipped: list[str] = []
    steps = [
        _ring_mutations(local_policy, ring.name, local_paths, local_known, arbor_index) for ring in local_policy.rings
    ]
    steps.append(_arbor_annotation_mutations(local_policy, local_paths, local_known, arbor_index))
    steps.append(_require_annotation_plan(local_policy, local_known, local_paths, arbor_index))
    steps.append(module_forbid_mutations(local_policy, local_paths, local_known))
    steps.append(importable_by_mutations(local_policy, local_paths, local_known))
    steps.append(owned_literal_mutations(local_policy, local_paths, local_known, arbor_index))
    steps.append(construct_only_mutations(local_policy, local_paths, local_known, arbor_index))
    steps.append(mixed_purity_mutations(local_policy, local_paths, local_known, arbor_index, modules))
    steps.append(_docs_mutations(local_policy, local_paths, local_known, arbor_index))
    for found, step_skipped in steps:
        out.extend(found)
        skipped.extend(step_skipped)
    out.extend(_entry_dependency_mutations(local_policy, local_paths, local_known, arbor_index))
    return out, skipped


def _docs_mutations(
    policy: Policy, paths: dict[str, str], known: Collection[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    """Probe the documentation rules (#110) with an added module: one with no docstring, one whose docstring names another ring."""
    docs = policy.docs
    out: list[Mutation] = []
    skipped: list[str] = []
    if not (docs.module_docstring or docs.module_names_ring):
        return out, skipped
    hosts = sorted(
        module for module in known
        if module in paths and not paths[module].endswith("__init__.py") and policy.resolve_ring(arbor_index, module) is not None
    )
    host = next((module for module in hosts if docs.applies_to(module.rpartition(".")[0] + ".arborescent_probe_docs")), None)
    if host is None:
        reason = "no classified module outside `docs.exclude` to place a probe beside"
        return out, [f"{rule}: {reason}" for rule in (Rule.MISSING_MODULE_DOCSTRING, Rule.DOCSTRING_RING_MISMATCH)]
    directory = paths[host].rpartition("/")[0]
    new = (directory + "/" if directory else "") + "arborescent_probe_docs.py"
    ring = policy.resolve_ring(arbor_index, host)
    if docs.module_docstring:
        out.append(Mutation(Rule.MISSING_MODULE_DOCSTRING, "a new module with no docstring", new, "", new_file=True))
    if not docs.module_names_ring:
        return out, skipped
    other = next((rule.name for rule in policy.rings if rule.name != ring), None)
    if other is None:
        return out, [*skipped, f"{Rule.DOCSTRING_RING_MISMATCH}: policy has only one ring, nothing else to name"]
    content = f'"""A probe module that says it lives in the {other} ring."""\n__arbor__ = {{"ring": {ring!r}}}\n'
    out.append(Mutation(Rule.DOCSTRING_RING_MISMATCH, f"a new {ring} module whose docstring names the {other} ring", new, "", new_file=True, content=content))
    return out, skipped


def _entry_dependency_mutations(
    local_policy: Policy, local_paths: dict[str, str], local_known: frozenset[str], arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> list[Mutation]:
    if not local_policy.entry_scripts:
        return []
    entry_host = next((
        name for name in sorted(local_known)
        if local_policy.resolve_ring(arbor_index, name) is not None and name not in local_policy.entry_scripts
    ), None)
    if entry_host is None:
        return []
    return [Mutation(
        Rule.UNDECLARED_ENTRY_DEPENDENCY,
        "a project module imports a package the scripts don't declare",
        local_paths[entry_host], "import arborescent_probe_dist",
    )]


def _scope_boundary_mutations(
    policy: Policy, paths: Mapping[str, str], known: frozenset[str], owners: Mapping[str, str],
) -> list[Mutation]:
    mutations = []
    for scope in policy.scopes:
        hosts = sorted(name for name in known if owners.get(name) == scope.name and not paths[name].endswith("__init__.py"))
        for host in hosts:
            target = next((
                name for name in sorted(known)
                if (target_scope := owners.get(name)) is not None and target_scope != scope.name
                and (
                    target_scope not in scope.depends_on
                    or not any(fnmatch.fnmatchcase(name, pattern) for pattern in policy.scope(target_scope).public)
                )
            ), None)
            if target is not None:
                mutations.append(Mutation(
                    Rule.SCOPE_IMPORT, f"{scope.name}: forbidden cross-scope import of {target}", paths[host], f"import {target}",
                ))
                break
    return mutations


def _import_propagation_mutations(policy: Policy, scan: ScanView) -> tuple[list[Mutation], list[str]]:
    """Probe ``effect-via-import``: a new import of a sibling that directly uses a forbidden effect, within one scope."""
    paths, known, arbor_index, modules, owners = scan.paths, scan.known, scan.arbor_index, scan.modules, scan.scope_by_module
    local_policies = (
        tuple((scope.name, scope.policy, frozenset(name for name in known if owners.get(name) == scope.name)) for scope in policy.scopes)
        if policy.scopes else (("", policy, known),)
    )
    out: list[Mutation] = []
    skipped: list[str] = []
    for scope_name, local_policy, local_known in local_policies:
        mutation = _one_import_propagation_mutation(local_policy, local_known, paths, arbor_index, modules)
        if mutation is None:
            skipped.append(f"effect-via-import: {scope_name or 'policy'} has no ring/import pair to mutate")
        else:
            out.append(mutation)
    return out, skipped


def _one_import_propagation_mutation(
    policy: Policy, known: Collection[str], paths: Mapping[str, str],
    arbor_index: Mapping[str, tuple[str | None, str | None]], modules: Mapping[str, ModuleInfo],
) -> Mutation | None:
    for ring in policy.rings:
        forbidden = policy.forbidden_effects(ring.name)
        if not forbidden:
            continue
        hosts = sorted(
            name for name in known
            if name in paths and not paths[name].endswith("__init__.py") and policy.resolve_ring(arbor_index, name) == ring.name
        )
        for host in hosts:
            host_info = modules.get(host)
            if host_info is None:
                continue
            leak = _leaking_sibling(policy, forbidden, known, modules, (host, host_info))
            if leak is None:
                continue
            target, category = leak
            return Mutation(
                Rule.EFFECT_VIA_IMPORT,
                f"{ring.name}: new import of {target}, which uses the forbidden {category} effect",
                paths[host], f"import {target}",
            )
    return None


def _leaking_sibling(
    policy: Policy, forbidden: frozenset[str], known: Collection[str], modules: Mapping[str, ModuleInfo], host: tuple[str, ModuleInfo],
) -> tuple[str, str] | None:
    """A module within reach that isn't already imported by ``host`` and directly uses a forbidden effect."""
    host_name, host_info = host
    for name in sorted(known):
        if name == host_name or name not in modules or _already_imports(host_info, name):
            continue
        for use in modules[name].facts.effect_uses:
            if use.category in forbidden and policy.counts_effect_seam(use.seam):
                return name, use.category
    return None


def _scope_effect_mutations(policy: Policy, scan: ScanView) -> list[Mutation]:
    paths, known, owners, modules = scan.paths, scan.known, scan.scope_by_module, scan.modules
    mutations: list[Mutation] = []
    for scope in policy.scopes:
        hosts = sorted(
            name for name in known
            if owners.get(name) == scope.name and not paths[name].endswith("__init__.py") and name in modules
        )
        for host in hosts:
            ring = scope.policy.resolve_ring(scan.arbor_index, host)
            if ring is None:
                continue
            forbidden = scope.policy.forbidden_effects(ring)
            if not forbidden:
                continue
            leak = _leaking_public_dependency(policy, scope, forbidden, scan, modules[host])
            if leak is None:
                continue
            target, category = leak
            mutations.append(Mutation(
                Rule.SCOPE_EFFECT_LEAK,
                f"{scope.name}: new import of {target}, a public module that uses the forbidden {category} effect",
                paths[host], f"import {target}",
            ))
            break
    return mutations


def _leaking_public_dependency(
    policy: Policy, scope: ArchitectureScope, forbidden: frozenset[str], scan: ScanView, host_info: ModuleInfo,
) -> tuple[str, str] | None:
    owners, modules = scan.scope_by_module, scan.modules
    for dependency_name in scope.depends_on:
        dependency = policy.scope(dependency_name)
        for name in sorted(scan.known):
            if owners.get(name) != dependency_name:
                continue
            if not any(fnmatch.fnmatchcase(name, pattern) for pattern in dependency.public):
                continue
            info = modules.get(name)
            if info is None or _already_imports(host_info, name):
                continue
            for use in info.facts.effect_uses:
                if use.category in forbidden and dependency.policy.counts_effect_seam(use.seam):
                    return name, use.category
    return None


def _already_imports(info: ModuleInfo, target: str) -> bool:
    return any(imp.level == 0 and imp.module == target for imp in info.facts.imports)


def _ring_mutations(
    policy: Policy, name: str, paths: dict[str, str], known: frozenset[str],
    arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    mine = sorted(module for module in known if policy.resolve_ring(arbor_index, module) == name and not paths[module].endswith("__init__.py"))
    if not mine:
        return [], [f"ring {name}: no module to mutate"]
    host = mine[0]
    allowed = policy.allowed_rings(name)
    target = next((module for module in sorted(known) if (ring := policy.resolve_ring(arbor_index, module)) is not None and ring not in allowed), None)
    out: list[Mutation] = []
    skipped: list[str] = []
    for lazy in (False, True):
        out.extend(_import_mutations(name, paths[host], target, policy.ring(name).external, lazy))
        if target is None:
            skipped.append(f"ring {name}: allowed to import every ring, no forbidden internal import to inject")
    if not policy.preset.os_branching:
        skipped.append(f"ring {name}: the {policy.preset.name} preset does not enforce OS branching")
    elif not policy.ring(name).os_branching:
        out.append(Mutation(Rule.OS_BRANCHING, f"{name}: OS branching", paths[host], "import os\n_arborescent_probe = os.name"))
    out.extend(_effect_mutations(policy, name, paths[host]))
    out.extend(_abstraction_mutations(policy, name, paths[host]))
    return out, skipped


def _arbor_annotation_mutations(
    policy: Policy, paths: dict[str, str], known: Collection[str],
    arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    """Probe the annotation-hardening rules: unknown keys, conditional assignment, and ring/classify disagreement."""
    out: list[Mutation] = []
    skipped: list[str] = []
    unannotated = sorted(
        module for module in known
        if module in paths and not paths[module].endswith("__init__.py")
        and arbor_index.get(module, (None, None)) == (None, None)
        and policy.ring_of(module) is not None
    )
    if not unannotated:
        skipped.append("invalid-arbor-annotation: no unannotated classified module to mutate")
        skipped.append("arbor-ring-mismatch: no unannotated classified module to mutate")
        return out, skipped
    host = unannotated[0]
    ring = policy.ring_of(host)
    out.append(Mutation(
        Rule.INVALID_ARBOR_ANNOTATION, "an __arbor__ dictionary with an unknown key",
        paths[host], f'__arbor__ = {{"ring": {ring!r}, "bogus": 1}}',
    ))
    out.append(Mutation(
        Rule.INVALID_ARBOR_ANNOTATION, "an __arbor__ assignment nested inside conditional code",
        paths[host], f'if True:\n    __arbor__ = {{"ring": {ring!r}}}',
    ))
    other_ring = next((rule.name for rule in policy.rings if rule.name != ring), None)
    if other_ring is None:
        skipped.append("arbor-ring-mismatch: policy has only one ring, nothing to disagree with")
    else:
        out.append(Mutation(
            Rule.ARBOR_RING_MISMATCH, f"an __arbor__ ring that disagrees with classify's {ring!r}",
            paths[host], f'__arbor__ = {{"ring": {other_ring!r}}}',
        ))
    return out, skipped


def _require_annotation_plan(
    policy: Policy, known: Collection[str], paths: Mapping[str, str],
    arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> tuple[list[Mutation], list[str]]:
    if not policy.require_arbor_annotations:
        return [], ["unannotated-module: require_arbor_annotations is disabled by policy"]
    mutation = _require_annotation_mutation(policy, known, paths, arbor_index)
    if mutation is None:
        return [], ["unannotated-module: no validly annotated module to overwrite"]
    return [mutation], []


def _require_annotation_mutation(
    policy: Policy, known: Collection[str], paths: Mapping[str, str],
    arbor_index: Mapping[str, tuple[str | None, str | None]],
) -> Mutation | None:
    """Overwrite a validly annotated module's `__arbor__` with an invalid one, for `require_arbor_annotations`.

    Appending text to an existing file can only ever add an `__arbor__` assignment, never remove one — and the
    last unconditional assignment wins (`facts.py`) — so the only way to make a module *newly* lose a valid
    annotation within this append-only harness is to append a second, invalid one over a host that currently has
    a valid one. A classify-only host would already violate the rule in the baseline (nothing "new" to detect).
    """
    host = next(
        (
            name for name in sorted(known)
            if name in paths
            and (entry := arbor_index.get(name, (None, None)))[1] is None
            and entry[0] is not None
            and any(rule.name == entry[0] for rule in policy.rings)
        ),
        None,
    )
    if host is None:
        return None
    return Mutation(
        Rule.UNANNOTATED_MODULE, "a validly annotated module's __arbor__ overwritten by an invalid one",
        paths[host], '__arbor__ = {"bogus": 1}',
    )


def _import_mutations(name: str, host: str, target: str | None, external: frozenset[str], lazy: bool) -> list[Mutation]:
    how = "lazy" if lazy else "top-level"
    indent = "def _arborescent_probe() -> None:\n    " if lazy else ""
    out: list[Mutation] = []
    if target is not None:
        out.append(Mutation(Rule.INTERNAL_IMPORT, f"{name}: forbidden {how} internal import of {target}", host, f"{indent}import {target}"))
    if "*" not in external:
        out.append(Mutation(Rule.EXTERNAL_IMPORT, f"{name}: unlisted {how} external import", host, f"{indent}import arborescent_probe_pkg"))
    return out


def _rejects_doubled_cr() -> bool:
    """Whether this interpreter parses ``\\r\\r\\n`` but fails to tokenize it (the C tokenizer of 3.12+ does; 3.11 does not)."""
    probe = "x = 1\r\r\n"
    try:
        _ = ast.parse(probe)
        _ = list(tokenize.generate_tokens(io.StringIO(probe).readline))
    except tokenize.TokenError:
        return True
    except SyntaxError:
        return False
    return False


def _anchor_mutations(anchor: str, host: str) -> list[Mutation]:
    base = host.rsplit("/", 1)[0] if "/" in host else ""
    new = (base + "/" if base else "") + "arborescent_probe_unclassified.py"
    out = [Mutation(Rule.UNCLASSIFIED, "a new module matching no pattern", new, "", new_file=True)]
    out.extend((
        Mutation(Rule.SUPPRESSION, "a new noqa suppression directive", host, "# noqa"),
        Mutation(Rule.DISCARD, "a new reasoned discard call", host, 'discard(None, reason="selftest")'),
        Mutation(Rule.LAZY_IMPORT, "a new function-level import without a reason", host, "def _arborescent_probe_lazy() -> None:\n    import json"),
    ))
    return out


def _effect_mutations(policy: Policy, ring: str, host: str) -> list[Mutation]:
    forbidden = policy.forbidden_effects(ring)
    out: list[Mutation] = []
    if "clock" in forbidden:
        out.append(Mutation(
            Rule.EFFECT_CLOCK,
            f"{ring}: ambient clock use",
            host,
            "import time\n_arborescent_probe = time.time()",
        ))
    if "global" in forbidden:
        out.append(Mutation(
            Rule.EFFECT_GLOBAL,
            f"{ring}: a function rebinds a module global",
            host,
            "_arborescent_probe_value = None\n\n\ndef _arborescent_probe_global() -> None:\n    "
            "global _arborescent_probe_value\n    _arborescent_probe_value = 1",
        ))
    return out


def _abstraction_mutations(policy: Policy, ring: str, host: str) -> list[Mutation]:
    if not policy.ring(ring).require_abstract:
        return []
    return [
        Mutation(
            Rule.NON_ABSTRACT_PORT,
            f"{ring}: concrete class in a ring that requires abstract members",
            host,
            "class ArborescentProbeConcrete:\n    def probe(self) -> None:\n        pass",
        )
    ]


def selftest(policy: Policy, root: Path, services: Services) -> SelfTestReport:
    services = replace(services, scanner=services.scanner.uncached())  # mutated copies must not touch the real cache
    _, scan = run_own(policy, root, services, practices=False)
    checks = package_checks(policy, services)
    probes = None if checks is None else checks.probes(policy, root, services.fs, scan, services.scanner)
    mutations, skipped = plan(policy, scan, probes)
    fs = services.fs
    with fs.temporary_directory("arborescent-selftest-") as tmp:
        work = tmp / "copy"
        fs.copy_tree(root, work, ignore=_SKIP_COPY)
        baseline = frozenset(v.key for v in run_own(policy, work, services, practices=False)[0])
        if services.parallel is None:
            killed, survived = _run_mutations_sequential(policy, work, services, baseline, mutations)
        else:
            jobs = [(policy, root, services, baseline, m) for m in mutations]
            killed, survived = _collect(mutations, services.parallel.map(_mutate_and_check, jobs))
    admitted, admission_survived, admission_skipped = admission_probes(policy, scan.arbor_index, scan.scope_by_module, scan.known)
    return SelfTestReport(killed + admitted, survived + admission_survived, skipped + admission_skipped)


def _collect(mutations: list[Mutation], killed_flags: list[bool]) -> tuple[list[str], list[str]]:
    killed: list[str] = []
    survived: list[str] = []
    for m, was_killed in zip(mutations, killed_flags, strict=True):
        (killed if was_killed else survived).append(f"[{m.rule}] {m.label}")
    return killed, survived


def _run_mutations_sequential(
    policy: Policy, work: Path, services: Services, baseline: frozenset[str], mutations: list[Mutation],
) -> tuple[list[str], list[str]]:
    """One shared mutated copy, mutated and reverted in place: cheapest when there are few mutations to run."""
    fs = services.fs
    killed: list[str] = []
    survived: list[str] = []
    for m in mutations:
        target = work / m.file
        original = fs.read_bytes(target) if fs.exists(target) and not m.new_file else None
        if m.new_file:
            fs.write_text(target, m.content)
        else:
            fs.write_bytes(target, (original or b"") + b"\n" + m.append.encode() + b"\n")
        try:
            after = {v.key for v in run_own(policy, work, services, practices=False)[0] if v.rule == m.rule}
        finally:
            if m.new_file:
                fs.remove_file(target)
            else:
                fs.write_bytes(target, original or b"")
        (killed if after - baseline else survived).append(f"[{m.rule}] {m.label}")
    return killed, survived


def _mutate_and_check(policy: Policy, root: Path, services: Services, baseline: frozenset[str], mutation: Mutation) -> bool:
    """Runs in a worker process: copy ``root``, apply one mutation, check, and report whether it was killed."""
    fs = services.fs
    with fs.temporary_directory("arborescent-selftest-mutation-") as tmp:
        work = tmp / "copy"
        fs.copy_tree(root, work, ignore=_SKIP_COPY)
        target = work / mutation.file
        if mutation.new_file:
            fs.write_text(target, mutation.content)
        else:
            original = fs.read_bytes(target) if fs.exists(target) else b""
            fs.write_bytes(target, original + b"\n" + mutation.append.encode() + b"\n")
        after = {v.key for v in run_own(policy, work, services, practices=False)[0] if v.rule == mutation.rule}
    return bool(after - baseline)
