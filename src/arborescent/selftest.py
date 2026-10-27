"""``selftest``: mutation-test the policy. For each rule, inject a forbidden construct into a temp COPY of the code
and require the checks to report it; a mutation that goes unreported is a rule that does not actually bite.

Ported from the throwaway mutation script behind DeepInterview's ``tests/test_architecture.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .domain import NOISE_DIRS, Policy, Rule
from .engine import Services, run_own

__all__ = ["NO_MUTATION", "Mutation", "SelfTestReport", "selftest"]

_SKIP_COPY = (*sorted(NOISE_DIRS), "*.pyc")

_PRACTICE = "repository practice: selftest mutates source files only and runs no practices"
# Rules with no mutation, and why. A test fails when a rule is in neither this table nor ``plan``'s output.
NO_MUTATION: dict[Rule, str] = {
    Rule.BAD_IMPORT: "a relative import above the source root is a syntax-level mistake, not a policy-dependent rule",
    Rule.GITIGNORE_SHADOW: _PRACTICE,
    Rule.SIBLING_VERSIONS: _PRACTICE,
    Rule.SECRETS_GUARD: _PRACTICE,
    Rule.DOCS_SYNC: _PRACTICE,
    Rule.NOISE_DIR_CODE: "requires a Git-tracked Python file under a noise directory; selftest works on a temporary non-Git copy",
    Rule.VENDOR_FOLDER: "tracks whole-tree fingerprints; focused tests cover file additions, edits and removals",
    Rule.WILDCARD_RING: "a policy-level rule; selftest mutations only edit copied source files",
    Rule.EFFECT_ENV: "effect-env is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_IDENTITY: "effect-identity is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_RANDOMNESS: "effect-randomness is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_SLEEP: "effect-sleep is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_THREADS: "effect-threads is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_PROCESS: "effect-process is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_DYNAMIC_IMPORT: "effect-dynamic-import is exercised by focused rule tests; the selftest probes the clock effect family",
    Rule.EFFECT_PATCH: "effect-patch requires a configured tests folder and project-package target; covered by focused rule tests",
    Rule.CRLF: _PRACTICE,
    Rule.UNDECLARED_IMPORTS: _PRACTICE,
}


@dataclass(frozen=True)
class Mutation:
    rule: Rule  # the rule id that must newly fire
    label: str
    file: str  # posix path relative to the root
    append: str  # text appended to the file ("" for an added file: see ``new_file``)
    new_file: bool = False


@dataclass
class SelfTestReport:
    killed: list[str]
    survived: list[str]
    skipped: list[str]

    @property
    def ok(self) -> bool:
        return not self.survived


def plan(policy: Policy, paths: dict[str, str], known: frozenset[str]) -> tuple[list[Mutation], list[str]]:
    """The mutations for this policy, plus the ones that cannot be built (and why)."""
    out: list[Mutation] = []
    skipped: list[str] = []
    for ring in policy.rings:
        ring_mutations, ring_skipped = _ring_mutations(policy, ring.name, ring.external, paths, known)
        out.extend(ring_mutations)
        skipped.extend(ring_skipped)
    anchor = next(iter(sorted(known)), None)
    if not policy.forbid_circular_imports:
        skipped.append("circular-import: disabled by policy")
    if anchor is not None:
        out.extend(_anchor_mutations(anchor, paths[anchor], policy.forbid_circular_imports))
    if policy.entry_scripts:
        entry_host = next((m for m in sorted(known) if policy.ring_of(m) is not None and m not in policy.entry_scripts), None)
        if entry_host is not None:
            out.append(Mutation(Rule.UNDECLARED_ENTRY_DEPENDENCY, "a project module imports a package the scripts don't declare", paths[entry_host], "import arborescent_probe_dist"))
    return out, sorted(set(skipped))


def _ring_mutations(
    policy: Policy, name: str, external: frozenset[str], paths: dict[str, str], known: frozenset[str]
) -> tuple[list[Mutation], list[str]]:
    mine = sorted(module for module in known if policy.ring_of(module) == name and not paths[module].endswith("__init__.py"))
    if not mine:
        return [], [f"ring {name}: no module to mutate"]
    host = mine[0]
    allowed = policy.allowed_rings(name)
    target = next((module for module in sorted(known) if (ring := policy.ring_of(module)) is not None and ring not in allowed), None)
    out: list[Mutation] = []
    skipped: list[str] = []
    for lazy in (False, True):
        out.extend(_import_mutations(name, paths[host], target, external, lazy))
        if target is None:
            skipped.append(f"ring {name}: allowed to import every ring, no forbidden internal import to inject")
    if not policy.preset.os_branching:
        skipped.append(f"ring {name}: the {policy.preset.name} preset does not enforce OS branching")
    elif not policy.ring(name).os_branching:
        out.append(Mutation(Rule.OS_BRANCHING, f"{name}: OS branching", paths[host], "import os\n_arborescent_probe = os.name"))
    out.extend(_effect_mutations(policy, name, paths[host]))
    return out, skipped


def _import_mutations(name: str, host: str, target: str | None, external: frozenset[str], lazy: bool) -> list[Mutation]:
    how = "lazy" if lazy else "top-level"
    indent = "def _arborescent_probe() -> None:\n    " if lazy else ""
    out: list[Mutation] = []
    if target is not None:
        out.append(Mutation(Rule.INTERNAL_IMPORT, f"{name}: forbidden {how} internal import of {target}", host, f"{indent}import {target}"))
    if "*" not in external:
        out.append(Mutation(Rule.EXTERNAL_IMPORT, f"{name}: unlisted {how} external import", host, f"{indent}import arborescent_probe_pkg"))
    return out


def _anchor_mutations(anchor: str, host: str, forbid_circular_imports: bool) -> list[Mutation]:
    base = host.rsplit("/", 1)[0] if "/" in host else ""
    new = (base + "/" if base else "") + "arborescent_probe_unclassified.py"
    out = [Mutation(Rule.UNCLASSIFIED, "a new module matching no pattern", new, "", new_file=True)]
    if forbid_circular_imports:
        out.append(Mutation(Rule.CIRCULAR_IMPORT, "a module importing itself", host, f"import {anchor}"))
    out.extend((
        Mutation(Rule.SUPPRESSION, "a new noqa suppression directive", host, "# noqa"),
        Mutation(Rule.DISCARD, "a new reasoned discard call", host, 'discard(None, reason="selftest")'),
    ))
    return out


def _effect_mutations(policy: Policy, ring: str, host: str) -> list[Mutation]:
    if "clock" not in policy.forbidden_effects(ring):
        return []
    return [
        Mutation(
            Rule.EFFECT_CLOCK,
            f"{ring}: ambient clock use",
            host,
            "import time\n_arborescent_probe = time.time()",
        )
    ]


def selftest(policy: Policy, root: Path, services: Services) -> SelfTestReport:
    services = replace(services, scanner=services.scanner.uncached())  # mutated copies must not touch the real cache
    _, scan = run_own(policy, root, services, practices=False)
    mutations, skipped = plan(policy, dict(scan.paths), scan.known)
    report = SelfTestReport([], [], skipped)
    fs = services.fs
    with fs.temporary_directory("arborescent-selftest-") as tmp:
        work = tmp / "copy"
        fs.copy_tree(root, work, ignore=_SKIP_COPY)
        baseline = {v.key for v in run_own(policy, work, services, practices=False)[0]}
        for m in mutations:
            target = work / m.file
            original = fs.read_bytes(target) if fs.exists(target) and not m.new_file else None
            if m.new_file:
                fs.write_text(target, "X = 1\n")
            else:
                fs.write_bytes(target, (original or b"") + b"\n" + m.append.encode() + b"\n")
            try:
                after = {v.key for v in run_own(policy, work, services, practices=False)[0] if v.rule == m.rule}
            finally:
                if m.new_file:
                    fs.remove_file(target)
                else:
                    fs.write_bytes(target, original or b"")
            (report.killed if after - baseline else report.survived).append(f"[{m.rule}] {m.label}")
    return report
