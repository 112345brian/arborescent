"""``check``: generate-in-memory and diff, run the external engines in parallel, run Arborescent's own checks on one
shared parse, then apply the baseline ratchet."""

from __future__ import annotations

import sys
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import baseline as ratchet_mod
from . import ratchets as named_ratchets
from .certification import tach_multi_root_note
from .classify_audit import audit_classification
from .command_imports import (
    COMMAND_BODY_HINT,
    EagerShare,
    eager_share,
    registrations_by_module,
)
from .config import find_child_policy
from .coupling import import_edges, reach
from .coupling_policy import EagerCommands
from .domain import (
    CONSTRAINT_RULES,
    INFORMATIONAL_RULES,
    NOISE_DIRS,
    Policy,
    PolicyError,
    Rule,
    ToolResult,
    Violation,
    path_under,
)
from .duplicate_logic import find_duplicates
from .effect_import_rules import unlisted_category_findings
from .generate import generate_for_scan, moved_to_root
from .generated_config import GENERATED_MARKER
from .import_cycles import EdgeKind, resolved_internal_imports
from .lazy_imports import (
    LazyImportCount,
    lazy_import_counts,
    lazy_import_in_scope,
    lazy_import_violations,
    lazy_imports_of,
)
from .path_exclusion import nested_bare_pattern, vendor_parent
from .ports import (
    Clock,
    ContractRenderer,
    Engine,
    FileSystem,
    ModuleScanner,
    PackageChecks,
    Parallel,
    RootAwareEngine,
    ScanResult,
    ToolRunner,
    TypeChecker,
    ViolationEngine,
)
from .practices import run_practices
from .purity import excluded_findings
from .ratchet_measure import Subject
from .rules import (
    PatchSources,
    RuleContext,
    check_modules,
    name_collision_findings,
    port_binding_findings,
    shadowed_callee_findings,
    unreachable_patches,
    unresolved_patch_targets,
)
from .suggestions import ClassSuggestion, Suggestion
from .tach_visibility import invisible_import_violations
from .taint import TaintResult, compute_taint

if TYPE_CHECKING:
    from .facts import ModuleInfo, SourceFacts

__all__ = [
    "CheckReport", "EngineRun", "Options", "Services", "apply_modularity", "apply_ratchet", "blocking", "changed_files",
    "child_arbor_runs", "contract_renderers", "dev_tool_counts", "discovery_findings", "drift", "drift_report", "enforced",
    "nested_exclusions", "package_checks", "plan_engines", "ratchet_runs", "record_inventories", "run_engine",
    "run_own", "run_tool", "tool_is_disabled", "write_generated",
]


@dataclass(frozen=True)
class Services:
    scanner: ModuleScanner
    runner: ToolRunner
    checkers: Sequence[TypeChecker]  # every known adapter; the policy / --type-checker picks which run
    fs: FileSystem
    clock: Clock
    engines: Sequence[Engine]  # run beside the chosen type checker (import-linter, tach); see arborescent.engines. No default: a forgotten list used to skip them silently
    packages: PackageChecks | None = None  # the package-annotation layer; needed only when the policy sets `package_catalog`
    parallel: Parallel | None = None  # runs selftest's mutations across cores; None keeps them sequential


@dataclass(frozen=True)
class Options:
    changed: str | None = None  # git ref: check only files changed since it
    type_checker: str | None = None  # override the policy's first type checker
    tools: bool = True  # run import-linter, tach and the type checker
    require_tools: bool = False  # a missing engine is a failure, not a skip
    use_baseline: bool = True
    require_profile: bool = False
    require_visible: bool = False  # fail on every dependency tach cannot see (run-time imports) instead of only noting it
    require_modularity: bool = False  # fail unless the policy keeps `modularity = "ready"` and no package gap is open
    growth_against: str | None = None
    admitted_against: str | None = None  # git ref: fail a module new since it that was not validly admitted (#163)
    no_cache: bool = False
    rules: tuple[str, ...] = ()  # rule families to run and exit on; empty selects everything (#137)


@dataclass
class EngineRun:
    name: str
    status: str  # "ok" | "fail" | "skipped"
    seconds: float
    lines: list[str] = field(default_factory=lambda: [])
    violations: list[Violation] = field(default_factory=lambda: [])


@dataclass
class CheckReport:
    engines: list[EngineRun] = field(default_factory=lambda: [])
    drift: list[str] = field(default_factory=lambda: [])
    violations: list[Violation] = field(default_factory=lambda: [])
    notes: list[Violation] = field(default_factory=lambda: [])  # informational findings: shown, never failing
    ratchet: ratchet_mod.Ratchet | None = None
    parsed: int = 0
    excluded: dict[str, int] = field(default_factory=lambda: {})
    excluded_nested: dict[str, str] = field(default_factory=lambda: {})  # excluded folder -> the bare `exclude` name that reached it below the root
    vendor_folders: tuple[tuple[str, str], ...] = ()
    dev_tools: dict[str, int] = field(default_factory=lambda: {})  # `dev_tools` path -> modules below it
    suggestions: list[Suggestion | ClassSuggestion] = field(default_factory=list)
    suppression_counts: dict[tuple[str, str], int] = field(default_factory=lambda: {})
    discard_counts: dict[str, int] = field(default_factory=lambda: {})
    lazy_import_counts: dict[str, LazyImportCount] = field(default_factory=lambda: {})
    profile: str | None = None
    profile_failures: tuple[str, ...] = ()
    profile_warnings: tuple[str, ...] = ()
    modularity_failures: tuple[str, ...] = ()
    visibility_failures: tuple[str, ...] = ()
    modularity_certified: bool = False
    partial: bool = False
    seconds: float = 0.0
    tool_checks_enabled: bool = True
    rules: tuple[str, ...] = ()  # the `--rule` selection this report was filtered to

    def _engine_fails(self, run: EngineRun) -> bool:
        """With a selection, an engine failing only on findings outside it is not this run's failure; one with no findings (a crash) still is."""
        if run.status != "fail":
            return False
        return not self.rules or not run.violations or any(ratchet_mod.in_any_family(v.key, self.rules) for v in run.violations)

    @property
    def ok(self) -> bool:
        engines_ok = not any(self._engine_fails(e) for e in self.engines)
        return engines_ok and not self.profile_failures and not self.modularity_failures and not self.visibility_failures and (self.ratchet is None or self.ratchet.ok)

    @property
    def configured_ok(self) -> bool:
        """Whether the selected checks pass, regardless of Arborescent minimum-profile certification."""
        engines_ok = all(engine.status in {"ok", "disabled"} for engine in self.engines)
        return self.tool_checks_enabled and engines_ok and (self.ratchet is None or self.ratchet.ok)


def ratchet_runs(policy: Policy, root: Path, services: Services, scan: ScanResult, found: Sequence[Violation]) -> list[EngineRun]:
    """One run per ``[ratchet.<name>]``: it fails on a count above its baseline, or (whole-repository runs) below it."""
    started = services.clock.now()
    subject = Subject(root, services.fs, services.runner, {m: scan.paths[m] for m in scan.modules if m in scan.paths}, found)
    complete = scan.modules.keys() >= scan.paths.keys()  # every known module was parsed: not a `--changed` run
    results = named_ratchets.check(policy, subject, complete)
    seconds = (services.clock.now() - started) / max(1, len(results))
    return [EngineRun(f"ratchet {r.name}", "fail" if r.failures else "ok", seconds, r.failures or [r.summary]) for r in results]


def _tach_notes(policy: Policy, opts: Options) -> list[str]:
    note = tach_multi_root_note(policy) if opts.tools and "tach" not in policy.disabled_tools else None
    return [] if note is None else [f"tach: {note}"]


def changed_files(runner: ToolRunner, root: Path, ref: str) -> frozenset[str] | None:
    diff = runner.run(["git", "diff", "--name-only", "--relative", ref], root)
    other = runner.run(["git", "ls-files", "--others", "--exclude-standard"], root)
    if diff.returncode != 0 or other.returncode != 0:
        return None
    return frozenset(p for p in (diff.stdout + "\n" + other.stdout).split() if p.endswith(".py"))


def discovery_findings(indexes: RuleContext, scan: ScanResult) -> list[Violation]:
    """What discovery alone shows, with no module facts beyond ``__arbor__``: shared with the incremental worker."""
    policy = indexes.policy
    found = [
        Violation(Rule.INVALID_ARBOR_ANNOTATION, module, detail)
        for module, _ in scan.unclassified
        if module not in scan.modules
        if (detail := indexes.annotation_violation(module)) is not None
    ]
    found += [
        Violation(Rule.UNCLASSIFIED, module, (indexes.policy_for(module) or policy).unclassified_detail(module))
        for module, _ in scan.unclassified
    ]
    found += [
        Violation(Rule.DEV_TOOL_MISPLACED, module, f"{path} is annotated as a dev tool but lies outside every `dev_tools` path; move it or list its folder in `dev_tools`")
        for module, path in scan.misplaced_dev_tools
    ]
    found += [Violation(Rule.NOISE_DIR_CODE, path, "") for path in scan.noise_files]
    found += [Violation(Rule.VENDOR_FOLDER, folder, fingerprint) for folder, fingerprint in scan.vendor_folders]
    return found


def run_own(
    policy: Policy, root: Path, services: Services, only: frozenset[str] | None = None, practices: bool = True
) -> tuple[list[Violation], ScanResult]:
    scan = services.scanner.scan(policy, root, only)
    indexes = RuleContext(policy, scan.arbor_index, scan.scope_by_module, frozenset(scan.dev_tool_modules))
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, indexes)
    found += discovery_findings(indexes, scan)
    found += _inventory_violations(scan.modules, policy.lazy_import_rings, indexes)
    if policy.coupling.eager_commands is not None:
        found = _flag_eager_commands(
            policy.coupling.eager_commands, found, lambda path: services.fs.read_text(root / path), scan, complete=only is None
        )
    found += excluded_findings(policy, scan.excluded_modules)
    if only is None:
        found += _whole_repository_findings(indexes, root, services, scan)
    if "tach" not in policy.disabled_tools:
        found += invisible_import_violations(scan.modules.values())
    if practices and only is None:
        found += run_practices(policy, root, services.runner, services.fs, scan)
    if only is None:  # ownership is a whole-repository fact; the selftest needs it whether or not practices run
        checks = package_checks(policy, services)
        if checks is not None:
            found += checks.gap_violations(policy, root, services.fs, scan, services.scanner)
        # selftest runs without practices and still has to probe these two
        found += _unreachable_patch_findings(policy, scan)
        found += _unresolved_patch_findings(policy, root, services.fs, scan)
    if policy.practices.get("shadowed-callee"):  # per module, so a `--changed` run reports it too
        found += shadowed_callee_findings(_module_sources(root, services, scan))
    return found, scan


def _whole_repository_findings(indexes: RuleContext, root: Path, services: Services, scan: ScanResult) -> list[Violation]:
    """The checks that compare a module with its importers and peers, so a ``--changed`` run skips them."""
    policy = indexes.policy
    found = _duplicate_findings(policy, root, services, scan) if policy.duplicate_logic.enabled else []
    found += port_binding_findings(indexes, scan.modules, scan.known)
    found += unlisted_category_findings(indexes, scan.modules)
    if policy.name_collisions is not None:  # re-reads every source: only when asked for
        found += name_collision_findings(indexes, scan.modules, _module_sources(root, services, scan))
    return found + audit_classification(indexes, scan.modules, scan.known, _run_taint(policy, root, services, scan))


def _patch_practice(policy: Policy, scan: ScanResult, rule: Rule) -> tuple[dict[str, SourceFacts], list[str], list[Violation]]:
    """A patch practice's test files and exemptions, or why there is nothing to check (all empty when it is off).

    The scan reads the tests folders of every patch practice, so each one keeps only the files under its own."""
    folder = policy.practices.get(rule)
    if not folder:
        return {}, [], []
    name = str(folder).replace("\\", "/").strip("/").removeprefix("./")
    tests = {path: facts for path, facts in scan.test_facts.items() if path_under(path, name)}
    if not tests:
        return {}, [], [Violation(rule, str(folder), "no Python files under the configured tests folder, so no patch is checked")]
    exempt = policy.practices.get(f"{rule}-exempt", [])
    return tests, [str(item) for item in cast("list[object]", exempt)], []


def _unreachable_patch_findings(policy: Policy, scan: ScanResult) -> list[Violation]:
    """#226, configured as ``practices.unreachable-patch = "<tests folder>"``; a folder with no tests would check nothing silently."""
    tests, exempt, problems = _patch_practice(policy, scan, Rule.UNREACHABLE_PATCH)
    return unreachable_patches(scan.modules, scan.known, tests, exempt) if tests else problems


def _unresolved_patch_findings(policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> list[Violation]:
    """#215, configured as ``practices.unresolved-patch-target = "<tests folder>"``."""
    tests, exempt, problems = _patch_practice(policy, scan, Rule.UNRESOLVED_PATCH_TARGET)
    if not tests:
        return problems

    def on_disk(init: str, name: str) -> bool:
        folder = (root / init).parent
        return name.isidentifier() and (fs.is_dir(folder / name) or bool(fs.glob(folder, f"{name}.*")))

    sources = PatchSources(lambda path: _source_text(fs, root / path), on_disk)
    return unresolved_patch_targets(scan.modules, scan.known, tests, sources, exempt)


def nested_exclusions(excluded: Mapping[str, int], patterns: Sequence[str]) -> dict[str, str]:
    """Excluded folder -> the bare ``exclude`` name that hid it below the repository root (the entry that may have been meant as ``/name``)."""
    return {folder: pattern for folder in excluded if (pattern := nested_bare_pattern(folder, patterns)) is not None}


class _CacheFile:
    """A whole-repository pass's fact cache as one file: a missing or unreadable file is an empty cache."""

    def __init__(self, fs: FileSystem, path: Path) -> None:
        self.fs = fs
        self._path = path

    def read(self) -> str | None:
        try:
            return self.fs.read_text(self._path) if self.fs.is_file(self._path) else None
        except (OSError, UnicodeDecodeError):
            return None

    def write(self, text: str) -> None:
        self.fs.mkdir(self._path.parent)
        self.fs.write_text(self._path, text)


def _module_sources(root: Path, services: Services, scan: ScanResult) -> dict[str, str]:
    """The text of each scanned module; one that cannot be read is left out."""
    sources: dict[str, str] = {}
    for name, path in scan.paths.items():
        if name not in scan.modules:
            continue
        try:
            sources[name] = services.fs.read_text(root / path)
        except OSError:
            continue
    return sources


def _run_taint(policy: Policy, root: Path, services: Services, scan: ScanResult) -> TaintResult:
    """#98: re-reads each scanned module's source and runs the whole-repository call-graph pass, reusing the
    scanner's cache directory (a sibling ``taint.json``) so an unchanged file skips re-parsing. A module whose
    file cannot be read contributes no facts, same as one the taint pass itself failed to parse."""
    sources = _module_sources(root, services, scan)
    cache_dir = services.scanner.cache_dir
    store = _CacheFile(services.fs, cache_dir / "taint.json") if cache_dir else None
    return compute_taint(scan.modules, sources, scan.known, policy.taint_seeds, store)


def _duplicate_findings(policy: Policy, root: Path, services: Services, scan: ScanResult) -> list[Violation]:
    """#228: reads each module and reference file for the clone pass. Vendored and reference-path code is only ever a target."""
    entries = policy.duplicate_logic.reference_paths
    references = {entry: _reference_sources(root, services.fs, entry) for entry in entries}
    modules: dict[str, str] = {}
    for name in scan.modules:
        path = scan.paths[name]
        if any(path_under(path, entry) for entry in entries):
            continue
        text = _source_text(services.fs, root / path)
        if text is None:
            continue
        if vendor_parent(path, NOISE_DIRS) is None:
            modules[name] = text
        else:
            references[path] = {path: text}
    cache_dir = services.scanner.cache_dir
    store = _CacheFile(services.fs, cache_dir / "duplicates.json") if cache_dir else None
    return [
        Violation(Rule.DUPLICATE_LOGIC, item.subject, item.detail, item.line, hint=item.hint, root_cause="")  # one finding per function: no shared cause
        for item in find_duplicates(modules, references, policy.duplicate_logic, store)
    ]


def _source_text(fs: FileSystem, path: Path) -> str | None:
    try:
        return fs.read_bytes(path).decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _reference_sources(root: Path, fs: FileSystem, entry: str) -> dict[str, str | None]:
    """The Python files of one reference path (a file or a folder), whether or not the scan covers them."""
    base = root / entry
    files = [base] if fs.is_file(base) else fs.rglob(base, "*.py") if fs.is_dir(base) else []
    return {
        path.relative_to(root).as_posix(): _source_text(fs, path)
        for path in files
        if path.suffix == ".py" and fs.is_file(path) and not NOISE_DIRS.intersection(path.relative_to(base).parts)
    }


def dev_tool_counts(policy: Policy, scan: ScanResult) -> dict[str, int]:
    return {
        entry: sum(1 for module in scan.dev_tool_modules if path_under(scan.paths[module], entry))
        for entry in policy.dev_tools
    }


def blocking(found: Iterable[Violation]) -> list[Violation]:
    """The findings that can fail a check or enter the baseline (information is reported, never recorded)."""
    return [v for v in found if v.rule not in INFORMATIONAL_RULES]


def _is_package_gap(violation: Violation) -> bool:
    return str(violation.rule).startswith("package-") and violation.rule not in CONSTRAINT_RULES


def enforced(policy: Policy, found: Iterable[Violation]) -> tuple[list[Violation], list[Violation]]:
    """``(kept, demoted)``: the blocking findings, with package gaps moved to ``demoted`` under ``modularity = "report"``.
    Everything that records or compares the baseline must use ``kept``, or a baseline written under ``report`` goes stale."""
    kept = blocking(found)
    if policy.modularity != "report":
        return kept, []
    return [v for v in kept if not _is_package_gap(v)], [v for v in kept if _is_package_gap(v)]


def apply_modularity(policy: Policy, report: CheckReport, found: Sequence[Violation], opts: Options, complete: bool) -> list[Violation]:
    """The enforced findings. Records the demoted gaps as notes, the ``--require-modularity`` certificate and the ``--require-visible`` failures."""
    kept, demoted = enforced(policy, found)
    report.notes += demoted
    report.visibility_failures = _visibility_failures(found) if opts.require_visible else ()
    if opts.require_modularity:
        report.modularity_failures = _modularity_failures(policy, [v for v in blocking(found) if _is_package_gap(v)], complete)
        report.modularity_certified = not report.modularity_failures
    return kept


def _visibility_failures(found: Sequence[Violation]) -> tuple[str, ...]:
    """Why a clean tach run cannot be taken at face value; empty when every dependency is one tach can see."""
    return tuple(f"tach cannot see: {v.render()}" for v in found if v.rule == Rule.TACH_INVISIBLE_IMPORT)


def _modularity_failures(policy: Policy, gaps: Sequence[Violation], complete: bool) -> tuple[str, ...]:
    """Why the repository is not certified modularity-ready; empty when it is. A cut list is not a gap and is allowed."""
    if policy.package_catalog is None:
        return ("no `package_catalog` is set: package ownership is not declared",)
    if policy.modularity != "ready":
        return (f"`modularity` is {policy.modularity!r}: package gaps are only reported, not enforced",)
    if not complete:
        return ("package gaps are a whole-repository fact and are not evaluated in --changed mode",)
    return tuple(f"open package gap: {v.render()}" for v in gaps)


def _inventory_violations(modules: Mapping[str, ModuleInfo], lazy_rings: tuple[str, ...], context: RuleContext) -> list[Violation]:
    found: list[Violation] = []
    for module in modules.values():
        suppressions: dict[str, int] = {}
        for _, kind in module.facts.suppressions:
            suppressions[kind] = suppressions.get(kind, 0) + 1
            found.append(Violation(Rule.SUPPRESSION, module.name, f"{kind}#{suppressions[kind]}"))
        for index, _ in enumerate(module.facts.discard_calls, start=1):
            found.append(Violation(Rule.DISCARD, module.name, f"call#{index}"))
        if lazy_import_in_scope(lazy_rings, context.resolve_ring(module.name)):
            found.extend(lazy_import_violations(module))
    return found


def _eager_shares(commands: EagerCommands, read: Callable[[str], str], scan: ScanResult) -> dict[str, EagerShare]:
    lazy = {name: [imp.line for imp in lazy_imports_of(module)] for name, module in sorted(scan.modules.items())}
    candidates = {name: lines for name, lines in lazy.items() if len(lines) >= commands.min_imports}
    if not candidates:  # nothing to report: do not re-read any file
        return {}

    def text(path: str) -> str | None:
        try:
            return read(path)
        except (OSError, UnicodeDecodeError):
            return None

    modules = {name: (module.path, module.is_package) for name, module in scan.modules.items()}
    registered = registrations_by_module(modules, text, commands.decorators)  # a handler another module registers
    shares: dict[str, EagerShare] = {}
    for name, lines in candidates.items():
        source = text(scan.modules[name].path)
        if source is not None and (share := eager_share(source, lines, commands, registered.get(name, frozenset()))) is not None:
            shares[name] = share
    return shares


def _hoist_cost(module: ModuleInfo, share: EagerShare, known: frozenset[str], runtime: Mapping[str, frozenset[str]]) -> int:
    """Project modules importing ``module`` would additionally load if its command-body imports moved to module level."""
    targets = {
        target
        for _, kind, line, resolved in resolved_internal_imports({module.name: module}, known)
        if line in share.lines and kind == EdgeKind.LAZY
        for target in resolved if target
    }
    return len(reach(runtime, [module.name, *targets]) - reach(runtime, [module.name]))


def _flag_eager_commands(
    commands: EagerCommands, found: Sequence[Violation], read: Callable[[str], str], scan: ScanResult, complete: bool
) -> list[Violation]:
    """#215: one ``entrypoint-eager-commands`` note per module whose function-level imports are mostly in command bodies, and
    a hint on those ``lazy-import`` findings. Their keys do not change, so the baseline ratchet still holds each one."""
    shares = _eager_shares(commands, read, scan)
    runtime = import_edges(scan.modules, scan.known).runtime if shares and complete else None  # a partial scan has no graph
    notes: list[Violation] = []
    for name, share in shares.items():
        module = scan.modules[name]
        cost = ""
        if runtime is not None:
            cost = f"; hoisting them would add +{_hoist_cost(module, share, scan.known, runtime)} project module(s) to importing this one"
        names = ", ".join(share.commands[:5]) + (", ..." if len(share.commands) > 5 else "")
        detail = f"{share.inside} of {share.total} function-level imports are inside {len(share.commands)} command bodies ({names}){cost}"
        notes.append(Violation(Rule.ENTRYPOINT_EAGER_COMMANDS, name, detail, min(share.lines)))
    flagged = [  # only findings the rule already made, so ``[lazy_import] rings`` has been applied to them
        replace(v, hint=COMMAND_BODY_HINT) if v.rule == Rule.LAZY_IMPORT and v.subject in shares and v.line in shares[v.subject].lines else v
        for v in found
    ]
    return [*flagged, *notes]


def _inventory_counts(modules: Mapping[str, ModuleInfo]) -> tuple[dict[tuple[str, str], int], dict[str, int]]:
    suppressions: dict[tuple[str, str], int] = {}
    discards: dict[str, int] = {}
    for module in modules.values():
        for _, kind in module.facts.suppressions:
            key = (module.name, kind)
            suppressions[key] = suppressions.get(key, 0) + 1
        if module.facts.discard_calls:
            discards[module.name] = len(module.facts.discard_calls)
    return suppressions, discards


def record_inventories(report: CheckReport, policy: Policy, scan: ScanResult) -> None:
    modules = scan.modules
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module, frozenset(scan.dev_tool_modules))
    report.suppression_counts, report.discard_counts = _inventory_counts(modules)
    scoped = (module for module in modules.values() if lazy_import_in_scope(policy.lazy_import_rings, context.resolve_ring(module.name)))
    counts = ((module.name, lazy_import_counts(module)) for module in scoped)
    report.lazy_import_counts = {name: count for name, count in counts if count.total}


def write_generated(policy: Policy, root: Path, services: Services) -> list[str]:
    """Create missing optional preset files; existing project configs are never changed."""
    _, scan = run_own(policy, root, services, practices=False)
    wanted = [c for c in services.checkers if c.name in policy.type_checkers and not tool_is_disabled(policy, c)]
    written: list[str] = []
    checks = package_checks(policy, services)
    boundaries = None if checks is None else checks.boundaries(policy, root, services.fs, scan)
    renderers = contract_renderers(services)
    # never start putting a file at the root for a tool the project does not use or has configured itself
    withheld = {
        renderer.config_file for renderer, _ in moved_to_root(policy, renderers)
        if tool_is_disabled(policy, renderer) or renderer.has_own_config(services.fs, root)
    }
    for rel, text in sorted(generate_for_scan(policy, scan, wanted, renderers, boundaries).items()):
        path = root / rel
        if rel in withheld or services.fs.exists(path):
            continue
        services.fs.mkdir(path.parent)
        services.fs.write_text(path, text)
        written.append(rel)
    if checks is None:
        return written
    for rel, text in sorted(checks.new_files(policy, root, services.fs, scan).items()):  # package pyproject.toml files; never an existing one
        services.fs.mkdir((root / rel).parent)
        services.fs.write_text(root / rel, text)
        written.append(rel)
    return written


def contract_renderers(services: Services) -> list[ContractRenderer]:
    """The bound engines that are configured from the neutral contracts."""
    return [engine for engine in services.engines if isinstance(engine, ContractRenderer)]


def package_checks(policy: Policy, services: Services) -> PackageChecks | None:
    """The bound package-annotation layer, or ``None`` when the policy declares no catalog. A catalog with nothing bound is
    an error: the engine will not read a missing binding as a clean result."""
    if policy.package_catalog is None:
        return None
    if services.packages is None:
        raise PolicyError("`package_catalog` is set but no package checks are bound in Services; the entrypoint must bind them")
    return services.packages


def drift(fs: FileSystem, root: Path, expected: Mapping[str, str]) -> list[str]:
    """Generated files on disk that differ from what ``generate`` would write now. A file Arborescent did not generate is not
    drift: only one that carries the generated header is compared."""
    out: list[str] = []
    for rel, text in sorted(expected.items()):
        path = root / rel
        if fs.is_file(path):
            current = fs.read_text(path)
            if current.startswith(GENERATED_MARKER) and current != text:
                out.append(f"{rel}: differs from the policy; run `arborescent generate`")
    return out


def drift_report(policy: Policy, root: Path, services: Services, scan: ScanResult, opts: Options) -> list[str]:
    checker = pick_checker(policy, services, opts.type_checker)
    checks = package_checks(policy, services)
    boundaries = None if checks is None else checks.boundaries(policy, root, services.fs, scan)
    checkers = [
        c for c in services.checkers if c.name in {*policy.type_checkers, checker.name} and not tool_is_disabled(policy, c)
    ]
    renderers = contract_renderers(services)
    expected = generate_for_scan(policy, scan, checkers, renderers, boundaries)
    unread = [
        f"{rel}: checks nothing there (the tool reads only files below its config); delete it"
        for _, rel in moved_to_root(policy, renderers)
        if services.fs.is_file(root / rel) and services.fs.read_text(root / rel).startswith(GENERATED_MARKER)
    ]
    return _tach_notes(policy, opts) + unread + drift(services.fs, root, expected)


def pick_checker(policy: Policy, services: Services, override: str | None = None) -> TypeChecker:
    """The type checker ``check`` runs. Every name the policy lists, and the override, must be a known adapter."""
    known = sorted(c.name for c in services.checkers)
    for name in (*policy.type_checkers, *([override] if override is not None else [])):
        if name not in known:
            raise PolicyError(f"unknown type checker {name!r}; choose from {known}")
    if override in policy.disabled_tools:
        raise PolicyError(f"type checker {override!r} is disabled by `disabled_tools`")
    chosen = override or next(
        (name for name in policy.type_checkers if name not in policy.disabled_tools),
        policy.type_checkers[0],
    )
    return next(c for c in services.checkers if c.name == chosen)


def tool_is_disabled(policy: Policy, tool: object) -> bool:
    """Whether the config deliberately excludes an engine executable and its checks."""
    name = getattr(tool, "name", "")
    executable = getattr(tool, "executable", name)
    canonical = {"lint-imports": "import-linter", "tach": "tach"}.get(executable, name or executable)
    return canonical in policy.disabled_tools


def _command(engine: Engine, files: Sequence[str], root: Path, fs: FileSystem) -> list[str]:
    """The engine's command; an engine that depends on the project (``RootAwareEngine``) decides with the project in view."""
    command_in = getattr(engine, "command_in", None)
    return cast(RootAwareEngine, engine).command_in(files, root, fs) if callable(command_in) else engine.command(files)


def run_tool(services: Services, engine: Engine, files: Sequence[str], root: Path, opts: Options) -> EngineRun:
    name, argv, ok = engine.name, _command(engine, files, root, services.fs), engine.passed
    if not services.runner.available(argv[0]):
        return EngineRun(name, "fail" if opts.require_tools else "skipped", 0.0, [f"{argv[0]} is not installed (extra)"])
    result = services.runner.run(argv, root)
    text = (result.stdout + result.stderr).strip().splitlines()
    status = "ok" if ok(result) else "fail"
    findings: list[Violation] = []
    try:
        findings = _engine_violations(engine, result, root, services.fs)
    except (ValueError, TypeError, OSError) as exc:
        status = "fail"
        text.append(f"failed to collect violations: {exc}")
    return EngineRun(name, status, result.seconds, text[-15:] if status == "fail" else text[-1:], findings)


def child_arbor_command(config_path: Path, opts: Options) -> list[str]:
    argv = [sys.executable, "-m", "arborescent", "--config", str(config_path.resolve()), "check"]
    flags = (
        (not opts.tools, "--no-tools"),
        (opts.require_tools, "--require-tools"),
        (opts.require_profile, "--require-profile"),
        (opts.require_modularity, "--require-modularity"),
        (opts.require_visible, "--require-visible"),
        (not opts.use_baseline, "--no-baseline"),
        (opts.no_cache, "--no-cache"),
    )
    argv.extend(flag for enabled, flag in flags if enabled)
    if opts.type_checker is not None:
        argv.extend(("--type-checker", opts.type_checker))
    if opts.growth_against is not None:
        argv.extend(("--growth-against", opts.growth_against))
    if opts.admitted_against is not None:
        argv.extend(("--admitted-against", opts.admitted_against))
    return argv


def child_arbor_runs(policy: Policy, root: Path, services: Services, opts: Options) -> list[EngineRun]:
    if policy.child_policy_mode == "parent":
        return []
    runs: list[EngineRun] = []
    for child in policy.children:
        name = f"child arbor {child.path}"
        child_root = root / child.path
        try:
            config_path = find_child_policy(services.fs, root, child.path)
        except PolicyError as exc:
            runs.append(EngineRun(name, "fail", 0.0, [str(exc)]))
            continue
        if config_path is None:
            continue
        if not services.runner.available(sys.executable):
            runs.append(EngineRun(name, "fail", 0.0, [f"{sys.executable} is not available to run the child policy"]))
            continue

        argv = child_arbor_command(config_path, opts)
        result = services.runner.run(argv, child_root)
        output = (result.stdout + result.stderr).strip().splitlines()
        status = "ok" if result.returncode == 0 else "fail"
        lines = output[-15:] if status == "fail" else [output[-1] if output else "child checks passed"]
        runs.append(EngineRun(name, status, result.seconds, lines))
    return runs


def run_engine(services: Services, name: str, root: Path) -> EngineRun:
    """Run one named engine and collect its structured findings, failing closed if unavailable or malformed."""
    engines = (*services.engines, *services.checkers)
    engine = next((item for item in engines if item.name == name), None)
    if engine is None:
        raise PolicyError(f"unknown findings engine {name!r}; choose from {sorted(item.name for item in engines)}")
    cwd = root / getattr(engine, "working_directory", ".")
    result = run_tool(services, engine, [], cwd, Options(require_tools=True))
    if result.status == "fail":
        raise PolicyError(f"could not adopt {name} findings: {' | '.join(result.lines)}")
    return result


def _engine_violations(engine: Engine, result: ToolResult, root: Path, fs: FileSystem) -> list[Violation]:
    """Collect optional structured findings; legacy engines have no such method and return no findings."""
    collect = getattr(engine, "violations", None)
    if collect is None:
        return []
    if not callable(collect):
        raise TypeError("violations must be callable")
    findings = list(cast(ViolationEngine, engine).violations(result, root, fs))
    if any(not isinstance(item, Violation) for item in cast(Sequence[object], findings)):
        raise TypeError("violations must return Violation instances")
    return findings


def plan_engines(policy: Policy, services: Services, opts: Options, files: Sequence[str] | None) -> list[Engine | EngineRun]:
    """One entry per engine and checker: the engine itself when it should run, or an immediate skipped/disabled result.

    ``files`` is None for a whole-repository run and the changed Python files for ``--changed``."""
    checker = pick_checker(policy, services, opts.type_checker)
    futures: list[Engine | EngineRun] = []
    if opts.tools:
        active_engines = [engine for engine in services.engines if not tool_is_disabled(policy, engine)]
        active_checker = None if tool_is_disabled(policy, checker) else checker
        for engine in (*active_engines, *((active_checker,) if active_checker is not None else ())):
            if files is not None and not files:
                futures.append(EngineRun(engine.name, "skipped", 0.0, ["no changed Python files"]))
            elif files is not None and not getattr(engine, "supports_changed", True):
                futures.append(EngineRun(engine.name, "skipped", 0.0, ["graph-wide engine skipped in --changed mode"]))
            else:
                futures.append(engine)
        futures.extend(
            EngineRun(engine.name, "disabled", 0.0, ["disabled by `disabled_tools`"])
            for engine in services.engines
            if tool_is_disabled(policy, engine)
        )
        if active_checker is None:
            futures.append(EngineRun(checker.name, "disabled", 0.0, ["disabled by `disabled_tools`"]))
    else:
        futures.extend(
            EngineRun(engine.name, "skipped", 0.0, ["not run: core mode (--no-tools / --engines core)"])
            for engine in (*services.engines, checker)
            if not tool_is_disabled(policy, engine)
        )
    return futures


def apply_ratchet(
    report: CheckReport, base: set[str], rules: tuple[str, ...], complete: bool, causes: Mapping[str, Collection[str]] | None = None,
) -> ratchet_mod.Ratchet:
    """Narrow findings and the baseline to the `--rule` families first, so nothing outside them is new or stale (#137)."""
    causes = causes or {}
    if rules:
        report.rules = rules
        report.violations = [v for v in report.violations if ratchet_mod.in_any_family(v.key, rules)]
        base = {key for key in base if ratchet_mod.in_any_family(key, rules)}
        causes = {cause: members for cause, members in causes.items() if ratchet_mod.in_any_family(cause, rules)}
    return ratchet_mod.apply(report.violations, base, complete=complete, root_causes=causes)
