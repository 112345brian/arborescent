"""The composition root: wires the adapters to the use cases. ``arborescent generate | check | baseline | selftest | bench``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import baseline as ratchet_mod
from .adapters import FsScanner, LocalFileSystem, SubprocessToolRunner, SystemClock
from .bench import run_bench
from .cli_install import _install_tools, _setup_install_command
from .cli_report import _print_excluded, _print_report, _print_rule_label
from .config import find_policy, load_policy
from .domain import Policy, PolicyError
from .engine import (
    EngineRun,
    Options,
    Services,
    pick_checker,
    run_check,
    run_engine,
    run_own,
    write_generated,
)
from .engines import default_engines
from .ports import ScanResult
from .selftest import selftest
from .typecheckers import all_checkers

__all__ = ["default_services", "main"]


def default_services(root: Path, cache: bool = True, output_dir: str = ".") -> Services:
    runner = SubprocessToolRunner(root)
    scanner = FsScanner(root / ".arborescent" if cache else None, runner)
    return Services(
        scanner,
        runner,
        list(all_checkers(output_dir).values()),
        LocalFileSystem(),
        SystemClock(),
        default_engines(output_dir),
    )

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="arborescent", description="One architecture policy -> import-linter, tach, a type checker.")
    p.add_argument("--config", type=Path, help="arborescent.toml or pyproject.toml (default: search upwards)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("generate", help="create missing optional tool config presets")
    install = sub.add_parser("install-tools", help="install missing check tools into the active Python environment")
    install.add_argument("--type-checker", choices=sorted(all_checkers()), help="install this type checker instead of the policy default")
    install.add_argument("--yes", action="store_true", help="install without prompting (explicitly mutates this environment)")
    c = sub.add_parser("check", help="drift, engines (parallel), own checks, baseline ratchet")
    c.add_argument("--changed", metavar="REF", help="check only files changed since this git ref")
    c.add_argument("--type-checker", choices=sorted(all_checkers()), help="override the policy's type checker")
    c.add_argument("--no-tools", action="store_true", help="skip import-linter, tach and the type checker")
    c.add_argument("--require-tools", action="store_true", help="a missing engine fails instead of skipping")
    c.add_argument("--install-tools", action="store_true", help="prompt to install missing check tools before checking")
    c.add_argument("--yes", action="store_true", help="install missing check tools without prompting (requires --install-tools)")
    c.add_argument("--require-profile", action="store_true", help="require Arborescent minimum profile v1")
    c.add_argument("--no-baseline", action="store_true", help="ignore the baseline: report every violation")
    c.add_argument("--growth-against", metavar="REF", help="verify baseline growth entries against a git ref")
    c.add_argument("--no-cache", action="store_true")
    b = sub.add_parser("baseline", help="create or shrink the baseline; growth must be explicit")
    b.add_argument("--adopt", metavar="FAMILY", help="adopt one new rule family once")
    b.add_argument("--allow-growth", action="store_true", help="allow baseline growth with a required reason")
    b.add_argument("--reason", help="explain why new baseline keys are being recorded")
    sub.add_parser("discover", help="list in-scope Python files that still need a ring classification")
    sub.add_parser("selftest", help="mutation-test the rules")
    b = sub.add_parser("bench", help="time every engine, cold and warm, on prepared codebases")
    b.add_argument("dirs", nargs="+", type=Path, help="directories that each contain an arborescent.toml")
    b.add_argument("--checkers", nargs="*", choices=sorted(all_checkers()), default=sorted(all_checkers()))
    return p

def main(argv: Sequence[str] | None = None, services: Services | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "check" and args.yes and not args.install_tools:
        parser.error("--yes requires --install-tools")
    try:
        if args.command == "bench":
            return run_bench(args.dirs, args.checkers, default_services)
        fs = services.fs if services else LocalFileSystem()
        policy, root = load_policy(fs, args.config or find_policy(fs, Path.cwd()))
        svc = services or default_services(root, cache=not getattr(args, "no_cache", False), output_dir=policy.output_dir)
        return _dispatch(args, policy, root, svc)
    except PolicyError as exc:
        print(f"arborescent: {exc}", file=sys.stderr)
        return 2

def _dispatch(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    if args.command == "generate":
        return _generate(policy, root, svc)
    if args.command == "install-tools":
        return _install_tools(args, policy, root, svc)
    if args.command == "baseline":
        return _baseline(args, policy, root, svc)
    if args.command == "discover":
        return _discover(policy, root, svc)
    if args.command == "selftest":
        return _selftest(policy, root, svc)
    return _check(args, policy, root, svc)

def _generate(policy: Policy, root: Path, svc: Services) -> int:
    _ = pick_checker(policy, svc)
    for rel in write_generated(policy, root, svc):
        print(f"wrote {rel}")
    return 0

def _baseline(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    found, _ = run_own(policy, root, svc)
    engine_names = {item.name for item in (*svc.engines, *svc.checkers)}
    if args.adopt in engine_names:
        found += run_engine(svc, args.adopt, root).violations
    elif args.adopt is None and any(
        key.split("|", 1)[0].startswith("ruff-") for key in ratchet_mod.load(svc.fs, root / policy.baseline)
    ):
        found += run_engine(svc, "ruff", root).violations
    elif args.adopt is not None and args.adopt not in {"coverage", "effects", "suppressions", "discard"}:
        families = engine_names | {"coverage", "effects", "suppressions", "discard"}
        raise PolicyError(f"unknown adoption family {args.adopt!r}; choose from {sorted(families)}")
    count = ratchet_mod.record(
        svc.fs,
        root / policy.baseline,
        found,
        adopt=args.adopt,
        allow_growth=args.allow_growth,
        reason=args.reason,
    )
    print(f"baseline: recorded {count} violation(s) in {policy.baseline}")
    return 0


def _discover(policy: Policy, root: Path, svc: Services) -> int:
    _, scan = run_own(policy, root, svc, practices=False)
    if policy.scopes:
        return _discover_scoped(policy, scan)
    by_folder: dict[str, list[str]] = {}
    for _, path in scan.unclassified:
        folder = Path(path).parent.as_posix()
        by_folder.setdefault(folder, []).append(path)
    suggested = policy.rings[-1].name
    if not by_folder:
        print("discover: no unclassified Python files")
        return 0
    print("Unclassified Python files:")
    for folder, paths in sorted(by_folder.items()):
        print(f"{folder}/ ({len(paths)} file(s)); suggested ring: {suggested}")
        for path in sorted(paths):
            print(f"  {path}")
    return 0


def _discover_scoped(policy: Policy, scan: ScanResult) -> int:
    by_scope_folder: dict[tuple[str, str], list[str]] = {}
    candidates = dict(scan.unclassified)
    for module, path in scan.paths.items():
        scope_name = scan.scope_by_module.get(module)
        if scope_name is not None and policy.resolve_scope_ring(scope_name, scan.arbor_index, module) is None:
            candidates[module] = path
    for module, path in candidates.items():
        scope_name = scan.scope_by_module.get(module) or "unowned"
        folder = Path(path).parent.as_posix()
        by_scope_folder.setdefault((scope_name, folder), []).append(path)
    if not by_scope_folder:
        print("discover: no unclassified Python files")
        return 0
    print("Unclassified Python files:")
    for (scope_name, folder), paths in sorted(by_scope_folder.items()):
        suggestion = policy.scope(scope_name).policy.rings[-1].name if scope_name != "unowned" else "none (unowned)"
        print(f"{scope_name}: {folder}/ ({len(paths)} file(s)); suggested ring: {suggestion}")
        for path in sorted(paths):
            print(f"  {path}")
    return 0


def _selftest(policy: Policy, root: Path, svc: Services) -> int:
    rep = selftest(policy, root, svc)
    for line in rep.killed:
        print(f"killed    {line}")
    for line in rep.survived:
        print(f"SURVIVED  {line}")
    for line in rep.skipped:
        print(f"skipped   {line}")
    print(f"selftest: {len(rep.killed)} killed, {len(rep.survived)} survived, {len(rep.skipped)} skipped")
    return 0 if rep.ok else 1


def _check(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    install_status = 0
    if args.install_tools:
        install_status = _install_tools(
            argparse.Namespace(type_checker=args.type_checker, yes=args.yes, continue_on_decline=True),
            policy,
            root,
            svc,
        )
    opts = Options(args.changed, args.type_checker, not args.no_tools, args.require_tools, not args.no_baseline, args.require_profile)
    report = run_check(policy, root, svc, opts)
    _print_rule_label(policy)
    _print_excluded(report)
    if args.growth_against:
        report.engines.append(_growth_check(args.growth_against, policy, root, svc))
    setup_command = None if args.no_tools else _setup_install_command(policy, root, svc, args.type_checker)
    _print_report(args, report, setup_command)
    return 1 if install_status else (0 if report.ok else 1)


def _growth_check(ref: str, policy: Policy, root: Path, svc: Services) -> EngineRun:
    started = svc.clock.now()
    git = svc.runner.run(["git", "show", f"{ref}:{policy.baseline}"], root)
    if git.returncode != 0:
        lines = [f"could not read {policy.baseline} at {ref}: {git.stderr.strip() or git.stdout.strip()}"]
        return EngineRun("baseline growth log", "fail", svc.clock.now() - started, lines)
    try:
        current = ratchet_mod.document(svc.fs, root / policy.baseline)
        with svc.fs.temporary_directory("arborescent-growth-") as temp:
            baseline_at_ref = temp / "baseline.json"
            svc.fs.write_text(baseline_at_ref, git.stdout)
            at_ref = svc.fs.read_json(baseline_at_ref)
        if not isinstance(at_ref, dict):
            raise ValueError("baseline at ref is not a JSON object")
        if not ratchet_mod.growth_consistent(current, at_ref):
            lines = [f"baseline changes since {ref} are not fully accounted for by new growth log entries"]
            status = "fail"
        else:
            lines, status = [f"growth log matches baseline changes since {ref}"], "ok"
    except (ValueError, TypeError) as exc:
        lines, status = [f"could not verify baseline growth: {exc}"], "fail"
    return EngineRun("baseline growth log", status, svc.clock.now() - started, lines)


if __name__ == "__main__":
    raise SystemExit(main())
