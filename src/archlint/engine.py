"""``check``: generate-in-memory and diff, run the external engines in parallel, run ArchLint's own checks on one
shared parse, then apply the baseline ratchet."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import baseline as ratchet_mod
from .domain import Policy, PolicyError, Violation
from .generate import generate
from .ports import (
    Clock,
    Engine,
    FileSystem,
    ModuleScanner,
    ScanResult,
    ToolRunner,
    TypeChecker,
)
from .practices import run_practices
from .rules import check_modules

__all__ = ["CheckReport", "EngineRun", "Options", "Services", "changed_files", "drift", "run_check", "run_own", "write_generated"]


@dataclass(frozen=True)
class Services:
    scanner: ModuleScanner
    runner: ToolRunner
    checkers: Sequence[TypeChecker]  # every known adapter; the policy / --type-checker picks which run
    fs: FileSystem
    clock: Clock
    engines: Sequence[Engine] = ()  # run beside the chosen type checker (import-linter, tach); see archlint.engines


@dataclass(frozen=True)
class Options:
    changed: str | None = None  # git ref: check only files changed since it
    type_checker: str | None = None  # override the policy's first type checker
    tools: bool = True  # run import-linter, tach and the type checker
    require_tools: bool = False  # a missing engine is a failure, not a skip
    use_baseline: bool = True


@dataclass
class EngineRun:
    name: str
    status: str  # "ok" | "fail" | "skipped"
    seconds: float
    lines: list[str] = field(default_factory=lambda: [])


@dataclass
class CheckReport:
    engines: list[EngineRun] = field(default_factory=lambda: [])
    drift: list[str] = field(default_factory=lambda: [])
    violations: list[Violation] = field(default_factory=lambda: [])
    ratchet: ratchet_mod.Ratchet | None = None
    parsed: int = 0
    partial: bool = False
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        engines_ok = all(e.status != "fail" for e in self.engines)
        return engines_ok and not self.drift and (self.ratchet is None or self.ratchet.ok)


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
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps)
    if practices and only is None:
        found += run_practices(policy, root, services.runner, services.fs, scan.modules, scan.known)
    return found, scan


def write_generated(policy: Policy, root: Path, services: Services) -> list[str]:
    """Write the tool configs compiled from the policy; return the written paths (relative, sorted)."""
    _, scan = run_own(policy, root, services, practices=False)
    wanted = [c for c in services.checkers if c.name in policy.type_checkers]
    written: list[str] = []
    for rel, text in sorted(generate(policy, sorted(scan.known), wanted).items()):
        services.fs.mkdir((root / rel).parent)
        services.fs.write_text(root / rel, text)
        written.append(rel)
    return written


def drift(fs: FileSystem, policy: Policy, root: Path, names: Sequence[str], checkers: Sequence[TypeChecker]) -> list[str]:
    out: list[str] = []
    for rel, expected in sorted(generate(policy, names, checkers).items()):
        path = root / rel
        if not fs.is_file(path):
            out.append(f"{rel}: missing; run `archlint generate`")
        elif fs.read_text(path) != expected:
            out.append(f"{rel}: differs from the policy; run `archlint generate`")
    return out


def pick_checker(policy: Policy, services: Services, override: str | None = None) -> TypeChecker:
    """The type checker ``check`` runs. Every name the policy lists, and the override, must be a known adapter."""
    known = sorted(c.name for c in services.checkers)
    for name in (*policy.type_checkers, *([override] if override else [])):
        if name not in known:
            raise PolicyError(f"unknown type checker {name!r}; choose from {known}")
    chosen = override or policy.type_checkers[0]
    return next(c for c in services.checkers if c.name == chosen)


def _tool(services: Services, engine: Engine, files: Sequence[str], root: Path, opts: Options) -> EngineRun:
    name, argv, ok = engine.name, engine.command(files), engine.passed
    if not services.runner.available(argv[0]):
        return EngineRun(name, "fail" if opts.require_tools else "skipped", 0.0, [f"{argv[0]} is not installed (extra)"])
    result = services.runner.run(argv, root)
    text = (result.stdout + result.stderr).strip().splitlines()
    status = "ok" if ok(result) else "fail"
    return EngineRun(name, status, result.seconds, text[-15:] if status == "fail" else text[-1:])


def run_check(policy: Policy, root: Path, services: Services, opts: Options | None = None) -> CheckReport:
    opts = opts if opts is not None else Options()
    started = services.clock.now()
    report = CheckReport()
    only = changed_files(services.runner, root, opts.changed) if opts.changed else None
    report.partial = only is not None
    checker = pick_checker(policy, services, opts.type_checker)
    pool = ThreadPoolExecutor(max_workers=max(1, len(services.engines) + 1))
    try:
        futures = []
        if opts.tools and (only is None or only):
            files = sorted(only) if only is not None else []
            futures = [pool.submit(_tool, services, e, files, root, opts) for e in (*services.engines, checker)]
        t0 = services.clock.now()
        found, scan = run_own(policy, root, services, only)
        report.engines.append(EngineRun("archlint", "ok", services.clock.now() - t0, [f"{len(scan.modules)} modules, {scan.parsed} parsed"]))
        report.parsed = scan.parsed
        report.drift = drift(services.fs, policy, root, sorted(scan.known), [c for c in services.checkers if c.name in {*policy.type_checkers, checker.name}])
        report.violations = found
        base: set[str] = ratchet_mod.load(services.fs, root / policy.baseline) if opts.use_baseline else set()
        report.ratchet = ratchet_mod.apply(found, base, complete=only is None)
        report.engines += [f.result() for f in futures]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    report.seconds = services.clock.now() - started
    return report
