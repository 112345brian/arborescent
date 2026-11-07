"""``check``: generate-in-memory and diff, run the external engines in parallel, run Arborescent's own checks on one
shared parse, then apply the baseline ratchet."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import baseline as ratchet_mod
from .certification import PROFILE, certify, tach_multi_root_note, version_warnings
from .classify_audit import audit_classification
from .config import find_child_policy
from .domain import (
    INFORMATIONAL_RULES,
    Policy,
    PolicyError,
    Rule,
    ToolResult,
    Violation,
    path_under,
)
from .generate import generate_for_scan
from .generated_config import GENERATED_MARKER
from .ports import (
    Clock,
    ContractRenderer,
    Engine,
    FileSystem,
    ModuleScanner,
    PackageChecks,
    RootAwareEngine,
    ScanResult,
    ToolRunner,
    TypeChecker,
    ViolationEngine,
)
from .practices import run_practices
from .rules import RuleContext, check_modules
from .suggestions import Suggestion, file_size_suggestions
from .tach_visibility import invisible_import_violations
from .taint import TaintResult, compute_taint

if TYPE_CHECKING:
    from .facts import ModuleInfo

__all__ = ["CheckReport", "EngineRun", "Options", "Services", "blocking", "changed_files", "contract_renderers", "drift", "enforced", "package_checks", "run_check", "run_engine", "run_own", "tool_is_disabled", "write_generated"]


@dataclass(frozen=True)
class Services:
    scanner: ModuleScanner
    runner: ToolRunner
    checkers: Sequence[TypeChecker]  # every known adapter; the policy / --type-checker picks which run
    fs: FileSystem
    clock: Clock
    engines: Sequence[Engine]  # run beside the chosen type checker (import-linter, tach); see arborescent.engines. No default: a forgotten list used to skip them silently
    packages: PackageChecks | None = None  # the package-annotation layer; needed only when the policy sets `package_catalog`


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
    no_cache: bool = False


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
    vendor_folders: tuple[tuple[str, str], ...] = ()
    dev_tools: dict[str, int] = field(default_factory=lambda: {})  # `dev_tools` path -> modules below it
    suggestions: list[Suggestion] = field(default_factory=list)
    suppression_counts: dict[tuple[str, str], int] = field(default_factory=lambda: {})
    discard_counts: dict[str, int] = field(default_factory=lambda: {})
    profile: str | None = None
    profile_failures: tuple[str, ...] = ()
    profile_warnings: tuple[str, ...] = ()
    modularity_failures: tuple[str, ...] = ()
    visibility_failures: tuple[str, ...] = ()
    modularity_certified: bool = False
    partial: bool = False
    seconds: float = 0.0
    tool_checks_enabled: bool = True

    @property
    def ok(self) -> bool:
        engines_ok = all(e.status != "fail" for e in self.engines)
        return engines_ok and not self.profile_failures and not self.modularity_failures and not self.visibility_failures and (self.ratchet is None or self.ratchet.ok)

    @property
    def configured_ok(self) -> bool:
        """Whether the selected checks pass, regardless of Arborescent minimum-profile certification."""
        engines_ok = all(engine.status in {"ok", "disabled"} for engine in self.engines)
        return self.tool_checks_enabled and engines_ok and (self.ratchet is None or self.ratchet.ok)


def _tach_notes(policy: Policy, opts: Options) -> list[str]:
    note = tach_multi_root_note(policy) if opts.tools and "tach" not in policy.disabled_tools else None
    return [] if note is None else [f"tach: {note}"]


def changed_files(runner: ToolRunner, root: Path, ref: str) -> frozenset[str] | None:
    diff = runner.run(["git", "diff", "--name-only", "--relative", ref], root)
    other = runner.run(["git", "ls-files", "--others", "--exclude-standard"], root)
    if diff.returncode != 0 or other.returncode != 0:
        return None
    return frozenset(p for p in (diff.stdout + "\n" + other.stdout).split() if p.endswith(".py"))


def run_own(
    policy: Policy, root: Path, services: Services, only: frozenset[str] | None = None, practices: bool = True
) -> tuple[list[Violation], ScanResult]:
    scan = services.scanner.scan(policy, root, only)
    indexes = RuleContext(policy, scan.arbor_index, scan.scope_by_module, frozenset(scan.dev_tool_modules))
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, indexes)
    found += [
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
    found += _inventory_violations(scan.modules)
    if only is None:  # the signals compare a module with its importers and peers: a whole-repository fact
        found += audit_classification(indexes, scan.modules, scan.known, _run_taint(policy, root, services, scan))
    if "tach" not in policy.disabled_tools:
        found += invisible_import_violations(scan.modules.values())
    if practices and only is None:
        found += run_practices(policy, root, services.runner, services.fs, scan)
    if only is None:  # ownership is a whole-repository fact; the selftest needs it whether or not practices run
        checks = package_checks(policy, services)
        if checks is not None:
            found += checks.gap_violations(policy, root, services.fs, scan, services.scanner)
    return found, scan


def _run_taint(policy: Policy, root: Path, services: Services, scan: ScanResult) -> TaintResult:
    """#98: re-reads each scanned module's source (not cached: see ``taint``'s own module docstring) and runs the
    whole-repository call-graph pass. A module whose file cannot be read contributes no facts, same as one the
    taint pass itself failed to parse."""
    sources: dict[str, str] = {}
    for name, path in scan.paths.items():
        if name not in scan.modules:
            continue
        try:
            sources[name] = services.fs.read_text(root / path)
        except OSError:
            continue
    return compute_taint(scan.modules, sources, scan.known, policy.taint_seeds)


def _dev_tool_counts(policy: Policy, scan: ScanResult) -> dict[str, int]:
    return {
        entry: sum(1 for module in scan.dev_tool_modules if path_under(scan.paths[module], entry))
        for entry in policy.dev_tools
    }


def blocking(found: Iterable[Violation]) -> list[Violation]:
    """The findings that can fail a check or enter the baseline (information is reported, never recorded)."""
    return [v for v in found if v.rule not in INFORMATIONAL_RULES]


def _is_package_gap(violation: Violation) -> bool:
    return str(violation.rule).startswith("package-")


def enforced(policy: Policy, found: Iterable[Violation]) -> tuple[list[Violation], list[Violation]]:
    """``(kept, demoted)``: the blocking findings, with package gaps moved to ``demoted`` under ``modularity = "report"``.
    Everything that records or compares the baseline must use ``kept``, or a baseline written under ``report`` goes stale."""
    kept = blocking(found)
    if policy.modularity != "report":
        return kept, []
    return [v for v in kept if not _is_package_gap(v)], [v for v in kept if _is_package_gap(v)]


def _apply_modularity(policy: Policy, report: CheckReport, found: Sequence[Violation], opts: Options, complete: bool) -> list[Violation]:
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


def _inventory_violations(modules: Mapping[str, ModuleInfo]) -> list[Violation]:
    found: list[Violation] = []
    for module in modules.values():
        suppressions: dict[str, int] = {}
        for _, kind in module.facts.suppressions:
            suppressions[kind] = suppressions.get(kind, 0) + 1
            found.append(Violation(Rule.SUPPRESSION, module.name, f"{kind}#{suppressions[kind]}"))
        for index, _ in enumerate(module.facts.discard_calls, start=1):
            found.append(Violation(Rule.DISCARD, module.name, f"call#{index}"))
    return found


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


def write_generated(policy: Policy, root: Path, services: Services) -> list[str]:
    """Create missing optional preset files; existing project configs are never changed."""
    _, scan = run_own(policy, root, services, practices=False)
    wanted = [c for c in services.checkers if c.name in policy.type_checkers and not tool_is_disabled(policy, c)]
    written: list[str] = []
    checks = package_checks(policy, services)
    boundaries = None if checks is None else checks.boundaries(policy, root, services.fs, scan)
    generated = sorted(generate_for_scan(policy, scan, wanted, contract_renderers(services), boundaries).items())
    for rel, text in generated:
        path = root / rel
        if services.fs.exists(path):
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


def _drift_report(policy: Policy, root: Path, services: Services, scan: ScanResult, opts: Options) -> list[str]:
    checker = pick_checker(policy, services, opts.type_checker)
    checks = package_checks(policy, services)
    boundaries = None if checks is None else checks.boundaries(policy, root, services.fs, scan)
    checkers = [
        c for c in services.checkers if c.name in {*policy.type_checkers, checker.name} and not tool_is_disabled(policy, c)
    ]
    expected = generate_for_scan(policy, scan, checkers, contract_renderers(services), boundaries)
    return _tach_notes(policy, opts) + drift(services.fs, root, expected)


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


def _tool(services: Services, engine: Engine, files: Sequence[str], root: Path, opts: Options) -> EngineRun:
    name, argv, ok = engine.name, _command(engine, files, root, services.fs), engine.passed
    if not services.runner.available(argv[0]):
        return EngineRun(name, "fail" if opts.require_tools else "skipped", 0.0, [f"{argv[0]} is not installed (extra)"])
    result = services.runner.run(argv, root)
    text = (result.stdout + result.stderr).strip().splitlines()
    status = "ok" if ok(result) else "fail"
    findings: list[Violation] = []
    try:
        findings = _engine_violations(engine, result, root, services.fs)
    except Exception as exc:
        status = "fail"
        text.append(f"failed to collect violations: {exc}")
    return EngineRun(name, status, result.seconds, text[-15:] if status == "fail" else text[-1:], findings)


def _child_arbor_command(config_path: Path, opts: Options) -> list[str]:
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
    return argv


def _child_arbor_runs(policy: Policy, root: Path, services: Services, opts: Options) -> list[EngineRun]:
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

        argv = _child_arbor_command(config_path, opts)
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
    result = _tool(services, engine, [], cwd, Options(require_tools=True))
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


def _plan_engines(policy: Policy, services: Services, opts: Options, files: Sequence[str] | None) -> list[Engine | EngineRun]:
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


def run_check(policy: Policy, root: Path, services: Services, opts: Options | None = None) -> CheckReport:
    opts = opts if opts is not None else Options()
    if opts.require_profile and not opts.require_tools:
        opts = replace(opts, require_tools=True)
    started = services.clock.now()
    report = CheckReport(tool_checks_enabled=opts.tools)
    only = changed_files(services.runner, root, opts.changed) if opts.changed is not None else None
    report.partial = only is not None
    pool = ThreadPoolExecutor(max_workers=max(1, len(services.engines) + 1))
    try:
        files = sorted(path for path in only if services.fs.is_file(root / path)) if only is not None else []
        planned = _plan_engines(policy, services, opts, files if only is not None else None)
        futures: list[Future[EngineRun] | EngineRun] = [
            item if isinstance(item, EngineRun)
            else pool.submit(_tool, services, item, files, root / getattr(item, "working_directory", "."), opts)
            for item in planned
        ]
        t0 = services.clock.now()
        found, scan = run_own(policy, root, services, only)
        report.engines.append(EngineRun("arborescent", "ok", services.clock.now() - t0, [f"{len(scan.modules)} modules, {scan.parsed} parsed"]))
        report.parsed = scan.parsed
        report.suppression_counts, report.discard_counts = _inventory_counts(scan.modules)
        report.excluded = dict(scan.excluded)
        report.vendor_folders = tuple(scan.vendor_folders)
        report.dev_tools = _dev_tool_counts(policy, scan)
        report.suggestions = file_size_suggestions(
            services.fs,
            root,
            {module.name: module.path for module in scan.modules.values()},
            threshold=policy.suggestion_threshold,
        )
        report.drift = _drift_report(policy, root, services, scan, opts)
        engine_runs = [f.result() if isinstance(f, Future) else f for f in futures]
        report.engines += engine_runs
        report.notes = [v for v in found if v.rule in INFORMATIONAL_RULES]
        report.violations = _apply_modularity(policy, report, found, opts, only is None) + [v for run in engine_runs for v in run.violations]
        base: set[str] = ratchet_mod.load(services.fs, root / policy.baseline) if opts.use_baseline else set()
        ruff_skipped = any(run.name == "ruff" and run.status in {"skipped", "disabled"} for run in engine_runs)
        if not opts.tools or ruff_skipped:
            # Ruff is the only external engine whose findings enter the baseline; absent output cannot prove them stale.
            base = {key for key in base if not key.split("|", 1)[0].startswith("ruff-")}
        report.ratchet = ratchet_mod.apply(report.violations, base, complete=only is None)
        if opts.require_profile:
            report.profile = PROFILE
            report.profile_failures = certify(root, policy, scan, services.runner, services.fs)
            report.profile_warnings = version_warnings(root, services.runner, policy.disabled_tools)
        report.engines.extend(_child_arbor_runs(policy, root, services, opts))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    report.seconds = services.clock.now() - started
    return report
