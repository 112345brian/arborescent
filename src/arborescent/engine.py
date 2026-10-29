"""``check``: generate-in-memory and diff, run the external engines in parallel, run Arborescent's own checks on one
shared parse, then apply the baseline ratchet."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from . import baseline as ratchet_mod
from .certification import PROFILE, certify
from .config import find_child_policy
from .domain import Policy, PolicyError, Rule, ToolResult, Violation
from .generate import generate
from .ports import (
    Clock,
    Engine,
    FileSystem,
    ModuleScanner,
    ScanResult,
    ToolRunner,
    TypeChecker,
    ViolationEngine,
)
from .practices import run_practices
from .rules import RuleContext, check_modules
from .suggestions import Suggestion, file_size_suggestions

if TYPE_CHECKING:
    from .facts import ModuleInfo

__all__ = ["CheckReport", "EngineRun", "Options", "Services", "changed_files", "drift", "run_check", "run_engine", "run_own", "tool_is_disabled", "write_generated"]


@dataclass(frozen=True)
class Services:
    scanner: ModuleScanner
    runner: ToolRunner
    checkers: Sequence[TypeChecker]  # every known adapter; the policy / --type-checker picks which run
    fs: FileSystem
    clock: Clock
    engines: Sequence[Engine]  # run beside the chosen type checker (import-linter, tach); see arborescent.engines. No default: a forgotten list used to skip them silently


@dataclass(frozen=True)
class Options:
    changed: str | None = None  # git ref: check only files changed since it
    type_checker: str | None = None  # override the policy's first type checker
    tools: bool = True  # run import-linter, tach and the type checker
    require_tools: bool = False  # a missing engine is a failure, not a skip
    use_baseline: bool = True
    require_profile: bool = False
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
    ratchet: ratchet_mod.Ratchet | None = None
    parsed: int = 0
    excluded: dict[str, int] = field(default_factory=lambda: {})
    vendor_folders: tuple[tuple[str, str], ...] = ()
    suggestions: list[Suggestion] = field(default_factory=list)
    suppression_counts: dict[tuple[str, str], int] = field(default_factory=lambda: {})
    discard_counts: dict[str, int] = field(default_factory=lambda: {})
    profile: str | None = None
    profile_failures: tuple[str, ...] = ()
    partial: bool = False
    seconds: float = 0.0
    tool_checks_enabled: bool = True

    @property
    def ok(self) -> bool:
        engines_ok = all(e.status != "fail" for e in self.engines)
        return engines_ok and not self.profile_failures and (self.ratchet is None or self.ratchet.ok)

    @property
    def configured_ok(self) -> bool:
        """Whether the selected checks pass, regardless of Arborescent minimum-profile certification."""
        engines_ok = all(engine.status in {"ok", "disabled"} for engine in self.engines)
        return self.tool_checks_enabled and engines_ok and (self.ratchet is None or self.ratchet.ok)


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
    indexes = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, indexes)
    found += [
        Violation(Rule.INVALID_ARBOR_ANNOTATION, module, detail)
        for module, _ in scan.unclassified
        if module not in scan.modules
        if (detail := _annotation_violation(policy, scan.arbor_index, scan.scope_by_module, module)) is not None
    ]
    found += [
        Violation(Rule.UNCLASSIFIED, module, "module matches no [classify] pattern; every module needs a ring")
        for module, _ in scan.unclassified
    ]
    found += [Violation(Rule.NOISE_DIR_CODE, path, "") for path in scan.noise_files]
    found += [Violation(Rule.VENDOR_FOLDER, folder, fingerprint) for folder, fingerprint in scan.vendor_folders]
    found += _inventory_violations(scan.modules)
    if practices and only is None:
        found += run_practices(policy, root, services.runner, services.fs, scan)
    return found, scan


def _annotation_violation(
    policy: Policy, arbor_index: Mapping[str, tuple[str | None, str | None]],
    scope_by_module: Mapping[str, str], module: str,
) -> str | None:
    if not policy.scopes:
        return policy.annotation_violation(arbor_index, module)
    scope_name = scope_by_module.get(module)
    if scope_name is not None:
        return policy.scope_annotation_violation(scope_name, arbor_index, module)
    annotation = arbor_index.get(module)
    return annotation[1] if annotation is not None else None


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
    generated = sorted(generate(policy, sorted(scan.known), wanted, scan.arbor_index, scan.scope_by_module).items())
    for rel, text in generated:
        path = root / rel
        if services.fs.exists(path):
            continue
        services.fs.mkdir(path.parent)
        services.fs.write_text(path, text)
        written.append(rel)
    return written


def drift(
    fs: FileSystem, policy: Policy, root: Path, scan: ScanResult, checkers: Sequence[TypeChecker],
) -> list[str]:
    out: list[str] = []
    for rel, expected in sorted(generate(policy, sorted(scan.known), checkers, scan.arbor_index, scan.scope_by_module).items()):
        path = root / rel
        if fs.is_file(path):
            current = fs.read_text(path)
            if current.startswith("# Generated by `arborescent generate`") and current != expected:
                out.append(f"{rel}: differs from the policy; run `arborescent generate`")
    return out


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


def _tool(services: Services, engine: Engine, files: Sequence[str], root: Path, opts: Options) -> EngineRun:
    name, argv, ok = engine.name, engine.command(files), engine.passed
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
    runs: list[EngineRun] = []
    for child in policy.children:
        name = f"child arbor {child.path}"
        child_root = root / child.path
        try:
            config_path = find_child_policy(services.fs, root, child.path)
        except PolicyError as exc:
            runs.append(EngineRun(name, "fail", 0.0, [str(exc)]))
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


def run_check(policy: Policy, root: Path, services: Services, opts: Options | None = None) -> CheckReport:
    opts = opts if opts is not None else Options()
    if opts.require_profile and not opts.require_tools:
        opts = replace(opts, require_tools=True)
    started = services.clock.now()
    report = CheckReport(tool_checks_enabled=opts.tools)
    only = changed_files(services.runner, root, opts.changed) if opts.changed is not None else None
    report.partial = only is not None
    checker = pick_checker(policy, services, opts.type_checker)
    pool = ThreadPoolExecutor(max_workers=max(1, len(services.engines) + 1))
    try:
        files = sorted(path for path in only if services.fs.is_file(root / path)) if only is not None else []
        futures: list[Future[EngineRun] | EngineRun] = []
        if opts.tools:
            active_engines = [engine for engine in services.engines if not tool_is_disabled(policy, engine)]
            active_checker = None if tool_is_disabled(policy, checker) else checker
            for engine in (*active_engines, *((active_checker,) if active_checker is not None else ())):
                if only is not None and not files:
                    futures.append(EngineRun(engine.name, "skipped", 0.0, ["no changed Python files"]))
                elif only is not None and not getattr(engine, "supports_changed", True):
                    futures.append(EngineRun(engine.name, "skipped", 0.0, ["graph-wide engine skipped in --changed mode"]))
                else:
                    futures.append(pool.submit(_tool, services, engine, files, root / getattr(engine, "working_directory", "."), opts))
            futures.extend(
                EngineRun(engine.name, "disabled", 0.0, ["disabled by `disabled_tools`"])
                for engine in services.engines
                if tool_is_disabled(policy, engine)
            )
            if active_checker is None:
                futures.append(EngineRun(checker.name, "disabled", 0.0, ["disabled by `disabled_tools`"]))
        t0 = services.clock.now()
        found, scan = run_own(policy, root, services, only)
        report.engines.append(EngineRun("arborescent", "ok", services.clock.now() - t0, [f"{len(scan.modules)} modules, {scan.parsed} parsed"]))
        report.parsed = scan.parsed
        report.suppression_counts, report.discard_counts = _inventory_counts(scan.modules)
        report.excluded = dict(scan.excluded)
        report.vendor_folders = tuple(scan.vendor_folders)
        report.suggestions = file_size_suggestions(
            services.fs,
            root,
            {module.name: module.path for module in scan.modules.values()},
            threshold=policy.suggestion_threshold,
        )
        report.drift = drift(
            services.fs,
            policy,
            root,
            scan,
            [
                c
                for c in services.checkers
                if c.name in {*policy.type_checkers, checker.name} and not tool_is_disabled(policy, c)
            ],
        )
        engine_runs = [f.result() if isinstance(f, Future) else f for f in futures]
        report.engines += engine_runs
        report.violations = found + [v for run in engine_runs for v in run.violations]
        base: set[str] = ratchet_mod.load(services.fs, root / policy.baseline) if opts.use_baseline else set()
        ruff_skipped = any(run.name == "ruff" and run.status in {"skipped", "disabled"} for run in engine_runs)
        if not opts.tools or ruff_skipped:
            # Ruff is the only external engine whose findings enter the baseline; absent output cannot prove them stale.
            base = {key for key in base if not key.split("|", 1)[0].startswith("ruff-")}
        report.ratchet = ratchet_mod.apply(report.violations, base, complete=only is None)
        if opts.require_profile:
            report.profile = PROFILE
            report.profile_failures = certify(root, policy, scan, services.runner, services.fs)
        report.engines.extend(_child_arbor_runs(policy, root, services, opts))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    report.seconds = services.clock.now() - started
    return report
