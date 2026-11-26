"""The composition root: wires the adapters to the use cases. ``arborescent generate | check | baseline | selftest | bench``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import baseline as ratchet_mod
from .annotate import apply_annotations, new_since, plan_annotations
from .baseline_report_view import FileReport, ReportOptions, file_report
from .classify_audit import suggest_ring
from .cli_admit import run_admit
from .cli_arguments import (
    build_parser,
    catalog_root,
    core_mode_conflict,
    growth_approver,
    usage_error,
    validate_rule_selection,
)
from .cli_baseline_report import run_report
from .cli_coupling import run_coupling
from .cli_install import _install_tools, _setup_install_command
from .cli_migrate import MigrationTools, run_migrate
from .cli_purity import Listing, run_explain, run_purity
from .cli_ratchet import growth_run, run_ratchet
from .cli_report import _print_excluded, _print_report, _print_rule_label
from .config import find_policy, load_policy
from .coupling_measure import Measure
from .domain import Policy, PolicyError, path_under
from .driver_filter_repo import FilterRepo
from .driver_libcst import LibCstRename
from .driver_port import ImportRewriter, MigrationLibrary
from .engine import (
    Options,
    Services,
    enforced,
    pick_checker,
    run_engine,
    run_own,
    write_generated,
)
from .engines import default_engines
from .git_hook import HookOutcome, ensure_git_hook
from .migration_export import migration_arguments_of
from .migration_json import export_document, render_export, render_schema
from .packages import PackageCatalog, load_extracted_package
from .ports import FileSystem, ScanResult
from .rule_context import RuleContext
from .typecheckers import all_checkers

__all__ = ["default_services", "main"]


def default_services(root: Path, cache: bool = True, output_dir: str = ".") -> Services:
    # deferred: construct concrete adapters only when a command needs default services
    from .adapters import (
        FsScanner,
        LocalFileSystem,
        ProcessPoolParallel,
        SubprocessToolRunner,
        SystemClock,
    )

    # deferred: default package checks are a service adapter
    from .package_checks import AnnotationPackageChecks

    runner = SubprocessToolRunner(root)
    scanner = FsScanner(root / ".arborescent" if cache else None, runner)
    return Services(
        scanner,
        runner,
        list(all_checkers(output_dir).values()),
        LocalFileSystem(),
        SystemClock(),
        default_engines(output_dir),
        AnnotationPackageChecks(),
        ProcessPoolParallel(),
    )


def _run_bench(args: argparse.Namespace) -> int:
    # deferred: only the benchmark command needs the full check harness
    from .bench import run_bench

    def report_progress(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    for block in run_bench(args.dirs, args.checkers, default_services, report_progress):
        print(block)
    return 0


def main(
    argv: Sequence[str] | None = None, services: Services | None = None, library: MigrationLibrary | None = None,
    rewriter: ImportRewriter | None = None, environ: Mapping[str, str] | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (message := usage_error(args)) is not None:
        parser.error(message)
    try:
        if args.command == "bench":
            return _run_bench(args)
        # deferred: argument help needs no filesystem adapter
        from .filesystem_adapter import LocalFileSystem

        fs = services.fs if services else LocalFileSystem()
        if args.command == "packages" and args.package_action == "schema":
            sys.stdout.write(render_schema())
            return 0
        if args.command == "packages" and getattr(args, "extracted", None) is not None:
            extracted = load_extracted_package(fs, args.extracted)
            print(f"valid: extracted {extracted.semantics.id} ({len(extracted.paths)} paths)")
            return 0
        if args.command == "packages" and args.package_action not in ("export", "migrate", "extract"):
            root = (
                args.config.parent
                if args.config
                else catalog_root(fs.is_file, Path.cwd(), args.catalog)
            )
            return _packages(args, root, fs)
        policy, root = load_policy(fs, args.config or find_policy(fs, Path.cwd()))
        svc = services or default_services(
            root,
            cache=not getattr(args, "no_cache", False),
            output_dir=policy.output_dir,
        )
        tools = MigrationTools(
            library or FilterRepo(svc.runner, svc.fs), rewriter or LibCstRename(svc.runner), os.environ if environ is None else environ
        )
        return _dispatch(args, policy, root, svc, tools)
    except PolicyError as exc:
        print(f"arborescent: {exc}", file=sys.stderr)
        return 2


def _dispatch(
    args: argparse.Namespace, policy: Policy, root: Path, svc: Services, tools: MigrationTools
) -> int:
    if args.command == "packages":
        return _dispatch_packages(args, policy, root, svc, tools)
    if args.command == "coupling":
        return run_coupling(args, Measure(policy, svc.scanner.scan(policy, root)), root, svc.fs)
    if args.command == "baseline" and args.baseline_action == "report":
        return _report_baselines(args, policy, root, svc)
    if args.command == "cycles":
        # deferred: load the cycles reporter for the cycles command
        from .cli_cycles import run_cycles

        return run_cycles(policy, root, svc)
    handlers: dict[str, Callable[[], int]] = {
        "generate": lambda: _generate(policy, root, svc),
        "install-tools": lambda: _install_tools(args, policy, root, svc),
        "baseline": lambda: _baseline(args, policy, root, svc),
        "discover": lambda: _discover(policy, root, svc, args.suggest),
        "annotate": lambda: _annotate(args, policy, root, svc),
        "admit": lambda: run_admit(args, policy, root, svc),
        "selftest": lambda: _selftest(policy, root, svc),
        "purity": lambda: run_explain(policy, root, svc, args.explain) if args.explain else run_purity(
            policy, root, svc, args.json, Listing(args.top, tuple(args.path), tuple(args.module), Path.cwd())
        ),
        "ratchet": lambda: _ratchet(args, policy, root, svc),
    }
    return handlers.get(args.command, lambda: _check(args, policy, root, svc))()


def _dispatch_packages(
    args: argparse.Namespace, policy: Policy, root: Path, svc: Services, tools: MigrationTools
) -> int:
    if args.package_action in ("migrate", "extract"):
        return run_migrate(args, policy, root, svc, tools)
    return _export(args, policy, root, svc)


def _packages(args: argparse.Namespace, root: Path, fs: FileSystem) -> int:
    catalog = PackageCatalog.load(fs, root, args.catalog)
    if args.package_action == "validate":
        print(f"valid: {len(catalog.packages)} packages")
        return 0
    if args.package_action == "list":
        for package in catalog.packages:
            print(f"{package.id}\t{package.root}\t{package.extraction_status}")
        return 0
    return 0


def _export(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    arguments = migration_arguments_of(policy, root, svc.fs, svc.scanner.scan(policy, root), svc.scanner)
    text = render_export(export_document(arguments, policy.package_catalog or "packages.toml"))
    if args.output is None:
        sys.stdout.write(text)
    else:
        destination = args.output if args.output.is_absolute() else root / args.output
        svc.fs.mkdir(destination.parent)
        svc.fs.write_text(destination, text)
        print(f"wrote {destination}")
    return 1 if args.require_ready and not arguments.ready else 0


def _generate(policy: Policy, root: Path, svc: Services) -> int:
    _ = pick_checker(policy, svc)
    for rel in write_generated(policy, root, svc):
        print(f"wrote {rel}")
    _report_git_hook(ensure_git_hook(policy, root, svc.fs, svc.runner))
    return 0


def _report_git_hook(outcome: HookOutcome) -> None:
    """Say what the pre-push hook did; a hook that is already right, not wanted or not possible is silent."""
    if outcome.status in ("installed", "updated"):
        print(f"{outcome.status} git hook {outcome.path} (git_hook: blocks a push that would fail `arborescent check`)")
    elif outcome.status == "removed":
        print(f"removed git hook {outcome.path} (git_hook is now off)")
    elif outcome.status == "foreign":
        print(
            f"arborescent: {outcome.path} exists and is not Arborescent's, so it was left alone; "
            "git_hook cannot block a failing push until it runs `arborescent check --require-tools --require-profile`",
            file=sys.stderr,
        )


def _report_baselines(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    def load(name: str, options: ReportOptions) -> FileReport:
        return file_report(name, options, root, svc.fs, svc.runner)

    return run_report(args, policy.baseline, load)


def _baseline(
    args: argparse.Namespace, policy: Policy, root: Path, svc: Services
) -> int:
    found, _ = run_own(policy, root, svc)
    found, _ = enforced(policy, found)
    engine_names = {item.name for item in (*svc.engines, *svc.checkers)}
    if args.adopt in engine_names:
        found += run_engine(svc, args.adopt, root).violations
    elif args.adopt is None and any(
        key.split("|", 1)[0].startswith("ruff-")
        for key in ratchet_mod.load(svc.fs, root / policy.baseline)
    ):
        found += run_engine(svc, "ruff", root).violations
    elif args.adopt is not None and args.adopt not in {
        "coverage",
        "effects",
        "suppressions",
        "discard",
    }:
        families = engine_names | {"coverage", "effects", "suppressions", "discard"}
        raise PolicyError(
            f"unknown adoption family {args.adopt!r}; choose from {sorted(families)}"
        )
    if args.prune:
        # deferred: only pruning needs to repeat engines whose findings may be stale
        from .check_gates import stale_engine_findings

        found = [*found, *stale_engine_findings(svc, root, policy)]
        if args.adopt is not None or args.allow_growth or args.reason is not None or args.approved_by is not None:
            raise PolicyError("`--prune` cannot be combined with `--adopt`, `--allow-growth`, `--reason` or `--approved-by`")
        removed = ratchet_mod.prune(svc.fs, root / policy.baseline, found)
        print(f"baseline: pruned {removed} stale entr{'y' if removed == 1 else 'ies'} from {policy.baseline}")
        return 0
    approved_by = growth_approver(args)
    path = root / policy.baseline
    logged = ratchet_mod.growth_count(svc.fs, path)
    count = ratchet_mod.record(
        svc.fs,
        root / policy.baseline,
        found,
        ratchet_mod.RecordOptions(adopt=args.adopt, allow_growth=args.allow_growth, reason=args.reason),
    )
    if approved_by is not None:
        ratchet_mod.stamp_approval(svc.fs, path, approved_by, logged)
    print(f"baseline: recorded {count} violation(s) in {policy.baseline}")
    summary = ratchet_mod.internal_import_summary(found)
    if summary is not None:
        print(summary)
    return 0


def _discover(policy: Policy, root: Path, svc: Services, suggest: bool = False) -> int:
    _, scan = run_own(policy, root, svc, practices=False)
    if policy.scopes:
        return _discover_scoped(policy, scan, suggest)
    by_folder: dict[str, list[str]] = {}
    for _, path in scan.unclassified:
        folder = Path(path).parent.as_posix()
        by_folder.setdefault(folder, []).append(path)
    suggested = policy.rings[-1].name
    if by_folder:
        print("Unclassified Python files:")
    else:
        print("discover: no unclassified Python files")
    for folder, paths in sorted(by_folder.items()):
        print(f"{folder}/ ({len(paths)} file(s)); suggested ring: {suggested}")
        for path in sorted(paths):
            print(f"  {path}")
    if suggest:
        _print_ring_suggestions(policy, scan, dict(scan.unclassified))
    return 0


def _print_ring_suggestions(policy: Policy, scan: ScanResult, candidates: dict[str, str]) -> None:
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    # modules inside `sources` that no classify rule or annotation reaches are only reported by `check`, not listed above
    candidates = candidates | {
        module: scan.paths[module] for module in scan.modules
        if module in scan.paths and context.policy_for(module) is not None and context.resolve_ring(module) is None
    }
    if not candidates:
        return
    print("Ring suggestions (from imports and effects; a prompt to look, not a verdict):")
    for module, path in sorted(candidates.items(), key=lambda item: item[1]):
        info = scan.modules.get(module)
        print(f"  {path}: {suggest_ring(context, module, info, scan.known) if info is not None else 'not parsed (outside the configured sources)'}")


def _discover_scoped(policy: Policy, scan: ScanResult, suggest: bool = False) -> int:
    by_scope_folder: dict[tuple[str, str], list[str]] = {}
    candidates = dict(scan.unclassified)
    indexes = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    # Scope-owned modules whose scope assigns no ring; orphans are already in `scan.unclassified`.
    candidates.update({
        module: path
        for module, path in scan.paths.items()
        if indexes.scope_name(module) is not None and indexes.resolve_ring(module) is None
    })
    for module, path in candidates.items():
        scope_name = scan.scope_by_module.get(module) or "unowned"
        folder = Path(path).parent.as_posix()
        by_scope_folder.setdefault((scope_name, folder), []).append(path)
    if not by_scope_folder:
        print("discover: no unclassified Python files")
        return 0
    print("Unclassified Python files:")
    for (scope_name, folder), paths in sorted(by_scope_folder.items()):
        suggestion = (
            policy.scope(scope_name).policy.rings[-1].name
            if scope_name != "unowned"
            else "none (unowned)"
        )
        print(
            f"{scope_name}: {folder}/ ({len(paths)} file(s)); suggested ring: {suggestion}"
        )
        for path in sorted(paths):
            print(f"  {path}")
    if suggest:
        _print_ring_suggestions(policy, scan, candidates)
    return 0


def _ratchet(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    print("\n".join(run_ratchet(args, policy, root, svc)))
    return 0


def _annotate(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    if not args.legacy:
        print("annotate: it no longer writes rings onto modules by itself. Place a new module with `arborescent admit MODULE --ring R`;")
        print("annotate: migrate rings that `classify` already holds with `arborescent annotate --legacy --against REF`.")
        return 1
    if not args.against:
        print("annotate: --legacy needs --against REF, so modules new since REF are refused and routed to `arborescent admit`")
        return 1
    try:
        new_paths = new_since(svc.runner, root, args.against)
    except ValueError as exc:
        print(f"annotate: {exc}")
        return 1
    _, scan = run_own(policy, root, svc, practices=False)
    plan = plan_annotations(policy, scan, new_paths)
    print("annotate: --legacy records the policy's placement, not a validated one; `classify` is human-written policy and nothing here checked it against the module's facts")
    for module, rel in plan.new_modules:
        print(f"new since {args.against}, not annotated: {rel}; run `arborescent admit {module} --ring R`")
    written = apply_annotations(svc.fs, root, plan) if args.yes else 0
    for _, rel, ring in plan.to_annotate:
        print(f"{'annotated' if args.yes else 'would annotate'} {rel} (ring: {ring})")
    for rel in plan.unmatched:
        print(f"no classify match: {rel}")
    print(
        f"annotate: {written if args.yes else len(plan.to_annotate)} "
        f"{'annotated' if args.yes else 'would be annotated'}, "
        f"{len(plan.already_annotated)} already annotated, {len(plan.unmatched)} unmatched, {len(plan.new_modules)} new (use admit)"
    )
    return 0


def _selftest(policy: Policy, root: Path, svc: Services) -> int:
    # deferred: load mutation machinery only for selftest
    from .selftest import selftest

    rep = selftest(policy, root, svc)
    for line in rep.killed:
        print(f"killed    {line}")
    for line in rep.survived:
        print(f"SURVIVED  {line}")
    for line in rep.skipped:
        print(f"skipped   {line}")
    print(
        f"selftest: {len(rep.killed)} killed, {len(rep.survived)} survived, {len(rep.skipped)} skipped"
    )
    return 0 if rep.ok else 1


def _list_dev_tools(policy: Policy, root: Path, svc: Services) -> int:
    scan = svc.scanner.scan(policy, root)
    if not policy.dev_tools:
        print("no `dev_tools` paths configured")
    for entry in policy.dev_tools:
        below = sorted(module for module in scan.dev_tool_modules if path_under(scan.paths[module], entry))
        print(f"{entry}: {len(below)} module(s)")
        for module in below:
            print(f"  {module}  ({scan.paths[module]})")
    for module, path in scan.misplaced_dev_tools:
        print(f"MISPLACED {module} ({path}): annotated as a dev tool but outside every `dev_tools` path")
    return 1 if scan.misplaced_dev_tools else 0


def _check(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    validate_rule_selection(args.rule, {item.name for item in (*svc.engines, *svc.checkers)})
    conflict = core_mode_conflict(args)
    if conflict is not None:
        print(f"arborescent check: {conflict}", file=sys.stderr)
        return 2
    if args.list_dev_tools:
        return _list_dev_tools(policy, root, svc)
    args.no_tools = args.no_tools or args.engines == "core"
    _report_git_hook(ensure_git_hook(policy, root, svc.fs, svc.runner))
    install_status = 0
    if args.install_tools:
        install_status = _install_tools(
            argparse.Namespace(
                type_checker=args.type_checker, yes=args.yes, continue_on_decline=True
            ),
            policy,
            root,
            svc,
        )
    opts = Options(
        changed=args.changed,
        type_checker=args.type_checker,
        tools=not args.no_tools,
        require_tools=args.require_tools,
        use_baseline=not args.no_baseline,
        require_profile=args.require_profile,
        require_modularity=args.require_modularity,
        require_visible=args.require_visible,
        growth_against=args.growth_against,
        admitted_against=args.admitted_against,
        no_cache=args.no_cache,
        rules=tuple(args.rule),
    )
    # deferred: only the check command needs the full check runner
    from .check_runner import run_check

    report = run_check(policy, root, svc, opts)
    _print_rule_label(policy)
    _print_excluded(report)
    if args.growth_against:
        # deferred: compare baseline growth only when a reference was requested
        from .check_gates import growth_check

        report.engines.append(growth_check(args.growth_against, policy, root, svc))
        if policy.ratchets:
            report.engines.append(growth_run(args.growth_against, policy, root, svc))
    if args.admitted_against:
        # deferred: compare admission only when a reference was requested
        from .check_gates import admission_check

        report.engines.append(admission_check(args.admitted_against, policy, root, svc))
    setup_command = (
        None
        if args.no_tools
        else _setup_install_command(policy, root, svc, args.type_checker)
    )
    _print_report(args, report, setup_command)
    return 1 if install_status else (0 if report.ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
