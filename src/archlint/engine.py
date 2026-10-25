"""``check``: generate-in-memory and diff, run the external engines in parallel, run ArchLint's own checks on one
shared parse, then apply the baseline ratchet."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import baseline as ratchet_mod
from .domain import Policy, ToolResult, Violation
from .generate import IMPORT_LINTER_FILE, generate
from .ports import FileSystem, ModuleScanner, ScanResult, ToolRunner, TypeChecker
from .practices import run_practices
from .rules import check_modules

__all__ = ["CheckReport", "EngineRun", "Options", "Services", "changed_files", "drift", "run_check", "run_own"]


@dataclass(frozen=True)
class Services:
    scanner: ModuleScanner
    runner: ToolRunner
    checkers: Sequence[TypeChecker]  # every known adapter; the policy / --type-checker picks which run
    fs: FileSystem


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


def drift(fs: FileSystem, policy: Policy, root: Path, names: Sequence[str], checkers: Sequence[TypeChecker]) -> list[str]:
    out: list[str] = []
    for rel, expected in sorted(generate(policy, names, checkers).items()):
        path = root / rel
        if not fs.is_file(path):
            out.append(f"{rel}: missing; run `archlint generate`")
        elif fs.read_text(path) != expected:
            out.append(f"{rel}: differs from the policy; run `archlint generate`")
    return out


def _tool(services: Services, name: str, argv: list[str], root: Path, opts: Options, ok: Callable[[ToolResult], bool]) -> EngineRun:
    if not services.runner.available(argv[0]):
        return EngineRun(name, "fail" if opts.require_tools else "skipped", 0.0, [f"{argv[0]} is not installed (extra)"])
    result = services.runner.run(argv, root)
    text = (result.stdout + result.stderr).strip().splitlines()
    status = "ok" if ok(result) else "fail"
    return EngineRun(name, status, result.seconds, text[-15:] if status == "fail" else text[-1:])


def run_check(policy: Policy, root: Path, services: Services, opts: Options | None = None) -> CheckReport:
    opts = opts if opts is not None else Options()
    started = time.perf_counter()
    report = CheckReport()
    only = changed_files(services.runner, root, opts.changed) if opts.changed else None
    report.partial = only is not None
    chosen = opts.type_checker or policy.type_checkers[0]
    checker = next((c for c in services.checkers if c.name == chosen), None)
    if checker is None:
        raise ValueError(f"unknown type checker {chosen!r}")
    pool = ThreadPoolExecutor(max_workers=3)
    futures = []
    if opts.tools and (only is None or only):
        files = sorted(only) if only is not None else []
        futures = [
            pool.submit(_tool, services, "import-linter", ["lint-imports", "--config", IMPORT_LINTER_FILE], root, opts, lambda r: r.returncode == 0),
            pool.submit(_tool, services, "tach", ["tach", "check"], root, opts, lambda r: r.returncode == 0),
            pool.submit(_tool, services, checker.name, checker.command(files), root, opts, checker.passed),
        ]  # fmt: skip
    t0 = time.perf_counter()
    found, scan = run_own(policy, root, services, only)
    report.engines.append(EngineRun("archlint", "ok", time.perf_counter() - t0, [f"{len(scan.modules)} modules, {scan.parsed} parsed"]))
    report.parsed = scan.parsed
    report.drift = drift(services.fs, policy, root, sorted(scan.known), [c for c in services.checkers if c.name in {*policy.type_checkers, checker.name}])
    report.violations = found
    base: set[str] = ratchet_mod.load(services.fs, root / policy.baseline) if opts.use_baseline else set()
    report.ratchet = ratchet_mod.apply(found, base, complete=only is None)
    report.engines += [f.result() for f in futures]
    pool.shutdown()
    report.seconds = time.perf_counter() - started
    return report
