"""The composition root: wires the adapters to the use cases. ``archlint generate | check | baseline | selftest | bench``."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from . import baseline as ratchet_mod
from .adapters import FsScanner, LocalFileSystem, SubprocessToolRunner, SystemClock
from .config import find_policy, load_policy
from .domain import Policy, PolicyError
from .engine import (
    CheckReport,
    EngineRun,
    Options,
    Services,
    pick_checker,
    run_check,
    run_engine,
    run_own,
    tool_is_disabled,
    write_generated,
)
from .engines import default_engines
from .install_tools import (
    include_poetry_archlint_group_in_dev,
    is_poetry_project,
    manual_install_command,
    missing_tool_requirements,
)
from .selftest import selftest
from .typecheckers import all_checkers

__all__ = ["default_services", "main"]


@dataclass(frozen=True)
class _InstallerCommands:
    install: list[str]
    check: list[str]
    name: str


def default_services(root: Path, cache: bool = True, output_dir: str = ".") -> Services:
    runner = SubprocessToolRunner(root)
    scanner = FsScanner(root / ".archlint" if cache else None, runner)
    return Services(
        scanner,
        runner,
        list(all_checkers(output_dir).values()),
        LocalFileSystem(),
        SystemClock(),
        default_engines(output_dir),
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="archlint", description="One architecture policy -> import-linter, tach, a type checker.")
    p.add_argument("--config", type=Path, help="archlint.toml or pyproject.toml (default: search upwards)")
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
    c.add_argument("--require-profile", action="store_true", help="require ArchLint minimum profile v1")
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
    b.add_argument("dirs", nargs="+", type=Path, help="directories that each contain an archlint.toml")
    b.add_argument("--checkers", nargs="*", choices=sorted(all_checkers()), default=sorted(all_checkers()))
    return p


def main(argv: Sequence[str] | None = None, services: Services | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "check" and args.yes and not args.install_tools:
        parser.error("--yes requires --install-tools")
    try:
        if args.command == "bench":
            from .bench import run_bench  # imported lazily: only this command needs it

            return run_bench(args.dirs, args.checkers, default_services)
        fs = services.fs if services else LocalFileSystem()
        policy, root = load_policy(fs, args.config or find_policy(fs, Path.cwd()))
        svc = services or default_services(root, cache=not getattr(args, "no_cache", False), output_dir=policy.output_dir)
        return _dispatch(args, policy, root, svc)
    except PolicyError as exc:
        print(f"archlint: {exc}", file=sys.stderr)
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


def _install_tools(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    checker = pick_checker(policy, svc, args.type_checker)
    selected = [engine for engine in (*svc.engines, checker) if not tool_is_disabled(policy, engine)]
    if not selected:
        print("install-tools: no check tools selected (all disabled by policy)")
        return 0
    try:
        missing = missing_tool_requirements(selected, svc.runner)
    except ValueError as exc:
        print(f"install-tools: {exc}", file=sys.stderr)
        return 2
    if not missing:
        print("install-tools: all selected check tools are available")
        return 0

    requirements = [requirement for _, requirement in missing]
    installer = _installer_commands(svc, root, requirements)
    for executable, requirement in missing:
        print(f"missing: {executable} ({requirement})")
    if installer is None:
        fallback = manual_install_command(root, svc.fs, requirements)
        print(f"manual install command: {shlex.join(fallback)}")
        print("No supported installer found. Install the listed packages into the active Python environment.")
        return 1
    print(f"install command: {shlex.join(installer.install)}")
    if not _confirm_install(args.yes):
        return 0 if getattr(args, "continue_on_decline", False) else 1
    return _run_tool_install(svc, root, installer, missing)


def _confirm_install(yes: bool) -> bool:
    if yes:
        return True
    if not _interactive_install_allowed():
        print("Not installing in a non-interactive session; rerun `archlint install-tools` in a terminal or pass --yes.")
        return False
    try:
        answer = input("Install these check tools into the active Python environment? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() in {"y", "yes"}:
        return True
    print("install-tools: cancelled; environment unchanged")
    return False


def _run_tool_install(
    svc: Services,
    root: Path,
    installer: _InstallerCommands,
    missing: Sequence[tuple[str, str]],
) -> int:
    installed = svc.runner.run(installer.install, root)
    if installed.returncode != 0:
        detail = installed.stderr.strip() or installed.stdout.strip() or f"installer exited {installed.returncode}"
        print(f"install-tools: installation failed: {detail}", file=sys.stderr)
        return 1
    if installer.name == "poetry":
        try:
            include_poetry_archlint_group_in_dev(root, svc.fs)
        except (OSError, ValueError, PolicyError) as exc:
            print(f"install-tools: could not configure the Poetry dev group: {exc}", file=sys.stderr)
            return 1
        refresh = getattr(svc.runner, "refresh_project_environment", None)
        if callable(refresh):
            refresh()
    checked = svc.runner.run(installer.check, root)
    if checked.returncode != 0:
        detail = checked.stderr.strip() or checked.stdout.strip() or "dependency check failed"
        print(f"install-tools: {installer.name} dependency check failed: {detail}", file=sys.stderr)
        return 1
    still_missing = [tool for tool, _ in missing if not svc.runner.available(tool)]
    if still_missing:
        print(
            f"install-tools: installation completed but executables were not found in the active environment: {', '.join(still_missing)}",
            file=sys.stderr,
        )
        return 1
    print(f"install-tools: installed and verified {len(missing)} check tool(s)")
    return 0


def _installer_commands(
    svc: Services, root: Path, requirements: Sequence[str]
) -> _InstallerCommands | None:
    if is_poetry_project(root, svc.fs):
        if not svc.runner.available("poetry"):
            return None
        return _InstallerCommands(
            ["poetry", "add", "--group", "archlint", *requirements],
            ["poetry", "check", "--lock"],
            "poetry",
        )
    python = sys.executable
    pip_check = svc.runner.run([python, "-m", "pip", "--version"], root)
    if pip_check.returncode == 0:
        return _InstallerCommands(
            [python, "-m", "pip", "install", *requirements],
            [python, "-m", "pip", "check"],
            "pip",
        )
    if svc.runner.available("uv"):
        return _InstallerCommands(
            ["uv", "pip", "install", "--python", python, *requirements],
            ["uv", "pip", "check", "--python", python],
            "uv",
        )
    return None


def _interactive_install_allowed() -> bool:
    if os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}:
        return False
    try:
        return bool(sys.stdin.isatty()) and bool(sys.stdout.isatty())
    except (AttributeError, OSError):
        return False


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


def _setup_install_command(policy: Policy, root: Path, svc: Services, type_checker: str | None) -> str | None:
    try:
        checker = pick_checker(policy, svc, type_checker)
        selected = [engine for engine in (*svc.engines, checker) if not tool_is_disabled(policy, engine)]
        missing = missing_tool_requirements(selected, svc.runner)
    except (PolicyError, ValueError):
        return None
    if not missing:
        return None
    requirements = [requirement for _, requirement in missing]
    installer = _installer_commands(svc, root, requirements)
    command = installer.install if installer is not None else manual_install_command(root, svc.fs, requirements)
    return shlex.join(command)


def _print_rule_label(policy: Policy) -> None:
    if policy.customization_labels:
        print(f"customised rules: {', '.join(policy.customization_labels)}")
    else:
        print(f"standard rules (baseline v{ratchet_mod.VERSION})")


def _print_excluded(report: CheckReport) -> None:
    vendor_folders = report.vendor_folders
    excluded = report.excluded
    print("auto-excluded vendor folders:")
    if vendor_folders:
        for folder, _ in vendor_folders:
            print(f"  {folder}/")
    else:
        print("  none")
    print("excluded folders:")
    if not excluded:
        print("  none")
    else:
        for folder, count in sorted(excluded.items()):
            print(f"  {folder}/ ({count} file(s))")


def _print_report(args: argparse.Namespace, report: CheckReport, setup_command: str | None = None) -> None:
    for failure in report.profile_failures:
        print(f"PROFILE FAIL {failure}")
    _print_consistency(report)
    if report.profile is not None and report.ok:
        print(f"CERTIFIED meets ArchLint minimum profile {report.profile} (Ruff 0.17.2, Tach 0.35.3)")
    for line in report.drift:
        print(f"DRIFT  {line}")
    for suggestion in report.suggestions:
        print(f"SUGGEST {suggestion.message}")
    _print_inventories(report)
    shown = report.ratchet.new if report.ratchet and not args.no_baseline else report.violations
    for v in shown:
        print(f"FAIL   {v.render()}")
    if report.ratchet:
        for key in report.ratchet.stale:
            print(f"STALE  baseline entry no longer occurs (delete it: `archlint baseline`): {key}")
    for e in report.engines:
        print(f"{e.status.upper():7}{e.name} ({e.seconds:.2f}s) {' | '.join(e.lines[-1:]) if e.status != 'fail' else ''}")
        if e.status == "fail":
            print("\n".join("        " + ln for ln in e.lines))
    _print_setup_hint(report, setup_command)
    _print_summary(report)


def _print_consistency(report: CheckReport) -> None:
    if report.configured_ok:
        print("CONSISTENT configured ArchLint checks pass")


def _print_inventories(report: CheckReport) -> None:
    print("suppression counts by module:")
    grouped: dict[str, dict[str, int]] = {}
    for (module, kind), count in report.suppression_counts.items():
        grouped.setdefault(module, {})[kind] = count
    if grouped:
        for module, kinds in sorted(grouped.items()):
            detail = ", ".join(f"{kind}={count}" for kind, count in sorted(kinds.items()))
            print(f"  {module}: {detail}")
    else:
        print("  none")
    print("discard calls by module:")
    if report.discard_counts:
        for module, count in sorted(report.discard_counts.items()):
            print(f"  {module}: {count}")
    else:
        print("  none")


def _print_setup_hint(report: CheckReport, setup_command: str | None = None) -> None:
    missing = sorted({
        run.name
        for run in report.engines
        if run.status in {"skipped", "fail"} and any("is not installed" in line for line in run.lines)
    })
    if missing:
        command = setup_command or "archlint install-tools"
        hint = f"SETUP  missing checker(s): {', '.join(missing)}; install with `{command}`"
        if setup_command is not None:
            hint += " or run `archlint check --install-tools` for guided setup"
        print(hint)


def _print_summary(report: CheckReport) -> None:
    tolerated = report.ratchet.tolerated if report.ratchet else 0
    print(
        f"archlint check: {'OK' if report.ok else 'FAILED'} in {report.seconds:.2f}s "
        f"({len(report.violations)} violation(s), {tolerated} baselined, {report.parsed} file(s) parsed"
        f"{', partial: --changed' if report.partial else ''})"
    )


def _growth_check(ref: str, policy: Policy, root: Path, svc: Services) -> EngineRun:
    started = svc.clock.now()
    git = svc.runner.run(["git", "show", f"{ref}:{policy.baseline}"], root)
    if git.returncode != 0:
        lines = [f"could not read {policy.baseline} at {ref}: {git.stderr.strip() or git.stdout.strip()}"]
        return EngineRun("baseline growth log", "fail", svc.clock.now() - started, lines)
    try:
        current = ratchet_mod.document(svc.fs, root / policy.baseline)
        with svc.fs.temporary_directory("archlint-growth-") as temp:
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
