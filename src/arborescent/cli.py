"""The composition root: wires the adapters to the use cases. ``arborescent generate | check | baseline | selftest | bench``."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from . import baseline as ratchet_mod
from .adapters import (
    FsScanner,
    LocalFileSystem,
    ProcessPoolParallel,
    SubprocessToolRunner,
    SystemClock,
)
from .annotate import apply_annotations, plan_annotations
from .bench import run_bench
from .classify_audit import suggest_ring
from .cli_baseline_report import add_report_parser, run_report
from .cli_coupling import add_coupling_parser, run_coupling
from .cli_cycles import run_cycles
from .cli_install import _install_tools, _setup_install_command
from .cli_migrate import MigrationTools, add_migrate_parser, run_migrate
from .cli_ratchet import add_ratchet_parser, growth_run, run_ratchet
from .cli_report import _print_excluded, _print_report, _print_rule_label
from .config import find_policy, load_policy
from .domain import Policy, PolicyError, Rule, Violation, path_under
from .driver_filter_repo import FilterRepo
from .driver_libcst import LibCstRename
from .driver_port import ImportRewriter, MigrationLibrary
from .engine import (
    EngineRun,
    Options,
    Services,
    enforced,
    pick_checker,
    run_check,
    run_engine,
    run_own,
    write_generated,
)
from .engines import default_engines
from .git_hook import HookOutcome, ensure_git_hook
from .migration_export import migration_arguments_of
from .migration_json import export_document, render_export, render_schema
from .package_checks import AnnotationPackageChecks
from .packages import PackageCatalog, load_extracted_package
from .ports import FileSystem, ScanResult
from .rules import RuleContext
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
        AnnotationPackageChecks(),
        ProcessPoolParallel(),
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arborescent",
        description="One architecture policy -> import-linter, tach, a type checker.",
    )
    p.add_argument(
        "--config",
        type=Path,
        help="arborescent.toml or pyproject.toml (default: search upwards)",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("generate", help="create missing optional tool config presets")
    install = sub.add_parser(
        "install-tools",
        help="install missing check tools into the active Python environment",
    )
    install.add_argument(
        "--type-checker",
        choices=sorted(all_checkers()),
        help="install this type checker instead of the policy default",
    )
    install.add_argument(
        "--yes",
        action="store_true",
        help="install without prompting (explicitly mutates this environment)",
    )
    _add_check_parser(sub.add_parser)
    _add_maintenance_parsers(sub.add_parser)
    _add_packages_parser(sub.add_parser)
    _add_bench_parser(sub.add_parser)
    return p


def _add_check_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    c = add_parser(
        "check", help="drift, engines (parallel), own checks, baseline ratchet"
    )
    c.add_argument(
        "--changed", metavar="REF", help="check only files changed since this git ref"
    )
    c.add_argument(
        "--type-checker",
        choices=sorted(all_checkers()),
        help="override the policy's type checker",
    )
    c.add_argument(
        "--no-tools",
        action="store_true",
        help="skip import-linter, tach and the type checker",
    )
    c.add_argument(
        "--engines",
        choices=("all", "core"),
        default="all",
        help="`core` runs only Arborescent's own checks (the same as --no-tools): no import-linter, tach, ruff or type checker is invoked or installed",
    )
    c.add_argument(
        "--list-dev-tools",
        action="store_true",
        help="print every `dev_tools` path with the modules below it, and annotated dev tools outside them, then exit",
    )
    c.add_argument(
        "--require-tools",
        action="store_true",
        help="a missing engine fails instead of skipping",
    )
    c.add_argument(
        "--install-tools",
        action="store_true",
        help="prompt to install missing check tools before checking",
    )
    c.add_argument(
        "--yes",
        action="store_true",
        help="install missing check tools without prompting (requires --install-tools)",
    )
    c.add_argument(
        "--require-profile",
        action="store_true",
        help="require Arborescent minimum profile v1",
    )
    c.add_argument(
        "--require-modularity",
        action="store_true",
        help="fail unless `modularity` is \"ready\" with a package_catalog and no package gap is open",
    )
    c.add_argument(
        "--require-visible",
        action="store_true",
        help="fail on every run-time import (importlib, __import__) that tach cannot follow",
    )
    c.add_argument(
        "--no-baseline",
        action="store_true",
        help="ignore the baseline: report every violation",
    )
    c.add_argument(
        "--growth-against",
        metavar="REF",
        help="verify baseline growth entries against a git ref",
    )
    c.add_argument(
        "--rule",
        action="append",
        default=[],
        metavar="FAMILY",
        help="run and exit on this rule family only (repeatable; the families `baseline --adopt` accepts, or any rule id)",
    )
    c.add_argument("--no-cache", action="store_true")


def _add_maintenance_parsers(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    b = add_parser(
        "baseline", help="create or shrink the baseline; growth must be explicit"
    )
    b.add_argument("--adopt", metavar="FAMILY", help="adopt one new rule family once")
    b.add_argument(
        "--allow-growth",
        action="store_true",
        help="allow baseline growth with a required reason",
    )
    b.add_argument("--reason", help="explain why new baseline keys are being recorded")
    b.add_argument("--approved-by", metavar="WHO", help="who approved the growth: a name or issue URL (required with --allow-growth)")
    b.add_argument("--prune", action="store_true", help="only remove stale entries; never adds, works while the tree has new violations")
    add_report_parser(b)
    discover = add_parser(
        "discover",
        help="list in-scope Python files that still need a ring classification",
    )
    discover.add_argument("--suggest", action="store_true", help="per module, print the tightest ring its imports and effects permit, with the evidence")
    annotate = add_parser(
        "annotate",
        help="migrate classify matches into explicit __arbor__ annotations (dry-run by default)",
    )
    annotate.add_argument(
        "--yes",
        action="store_true",
        help="write the annotations instead of printing what would change",
    )
    add_parser("selftest", help="mutation-test the rules")
    add_coupling_parser(add_parser)
    add_parser("cycles", help="report import cycles, the edges to cut and the function-level imports that only break a cycle")
    add_ratchet_parser(add_parser)


def _add_packages_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    packages = add_parser(
        "packages", help="validate, list or extract declared package metadata"
    )
    package_actions = packages.add_subparsers(dest="package_action", required=True)
    for action in ("validate", "list"):
        command = package_actions.add_parser(action)
        command.add_argument(
            "--catalog",
            default="packages.toml",
            help="package metadata file relative to repository root",
        )
    _ = package_actions.choices["validate"].add_argument(
        "--extracted",
        type=Path,
        help="validate an extracted package directory instead of the repository catalog",
    )
    export = package_actions.add_parser(
        "export", help="write the read-only migration export (JSON) for the policy's package_catalog"
    )
    export.add_argument("--output", type=Path, help="file to write (default: standard output)")
    export.add_argument(
        "--require-ready",
        action="store_true",
        help="exit 1 when the export still has an annotation gap of severity error",
    )
    _ = package_actions.add_parser("schema", help="print the JSON Schema the export validates against")
    add_migrate_parser(package_actions.add_parser)


def _add_bench_parser(add_parser: Callable[..., argparse.ArgumentParser]) -> None:
    b = add_parser(
        "bench", help="time every engine, cold and warm, on prepared codebases"
    )
    b.add_argument(
        "dirs",
        nargs="+",
        type=Path,
        help="directories that each contain an arborescent.toml",
    )
    b.add_argument(
        "--checkers",
        nargs="*",
        choices=sorted(all_checkers()),
        default=sorted(all_checkers()),
    )


def main(
    argv: Sequence[str] | None = None, services: Services | None = None, library: MigrationLibrary | None = None,
    rewriter: ImportRewriter | None = None, environ: Mapping[str, str] | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "check" and args.yes and not args.install_tools:
        parser.error("--yes requires --install-tools")
    if getattr(args, "with_dependencies", False):
        parser.error("--with-dependencies was removed: a dependency is a separate package, so extract each one on its own")
    try:
        if args.command == "bench":
            return run_bench(args.dirs, args.checkers, default_services)
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
                else _find_catalog_root(fs, Path.cwd(), args.catalog)
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


def _find_catalog_root(fs: FileSystem, start: Path, catalog_path: str) -> Path:
    candidate = Path(catalog_path)
    if candidate.is_absolute():
        if not fs.is_file(candidate):
            raise PolicyError(f"package catalog does not exist: {candidate}")
        return candidate.parent
    for folder in (start, *start.parents):
        if fs.is_file(folder / candidate):
            return folder
    raise PolicyError(f"no {catalog_path!r} found from {start}")


def _dispatch(
    args: argparse.Namespace, policy: Policy, root: Path, svc: Services, tools: MigrationTools
) -> int:
    if args.command == "packages":
        return _dispatch_packages(args, policy, root, svc, tools)
    if args.command == "coupling":
        return run_coupling(args, policy, root, svc)
    handlers: dict[str, Callable[[], int]] = {
        "generate": lambda: _generate(policy, root, svc),
        "install-tools": lambda: _install_tools(args, policy, root, svc),
        "baseline": lambda: run_report(args, policy, root, svc) if args.baseline_action == "report" else _baseline(args, policy, root, svc),
        "discover": lambda: _discover(policy, root, svc, args.suggest),
        "annotate": lambda: _annotate(args, policy, root, svc),
        "selftest": lambda: _selftest(policy, root, svc),
        "cycles": lambda: run_cycles(policy, root, svc),
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


def _stale_engine_findings(svc: Services, root: Path, policy: Policy) -> list[Violation]:
    """``--prune`` judges every baseline entry, so each engine that owns a baselined rule family must be re-run, or its
    still-valid entries would look stale and be dropped."""
    rules = {key.split("|", 1)[0] for key in ratchet_mod.load(svc.fs, root / policy.baseline)}
    names = {item.name for item in (*svc.engines, *svc.checkers)} - {"ruff"}  # `_baseline` already re-ran ruff for its entries
    extra: list[Violation] = []
    for name in sorted(names):
        if any(r == name or r.startswith(f"{name}-") for r in rules):
            extra += run_engine(svc, name, root).violations
    return extra


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
        found = [*found, *_stale_engine_findings(svc, root, policy)]
        if args.adopt is not None or args.allow_growth or args.reason is not None or args.approved_by is not None:
            raise PolicyError("`--prune` cannot be combined with `--adopt`, `--allow-growth`, `--reason` or `--approved-by`")
        removed = ratchet_mod.prune(svc.fs, root / policy.baseline, found)
        print(f"baseline: pruned {removed} stale entr{'y' if removed == 1 else 'ies'} from {policy.baseline}")
        return 0
    approved_by = _growth_approver(args)
    path = root / policy.baseline
    logged = ratchet_mod.growth_count(svc.fs, path)
    count = ratchet_mod.record(
        svc.fs,
        root / policy.baseline,
        found,
        adopt=args.adopt,
        allow_growth=args.allow_growth,
        reason=args.reason,
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
    _, scan = run_own(policy, root, svc, practices=False)
    plan = plan_annotations(policy, scan)
    if args.yes:
        written = apply_annotations(svc.fs, root, plan)
        for _, rel, ring in plan.to_annotate:
            print(f"annotated {rel} (ring: {ring})")
    else:
        written = 0
        for _, rel, ring in plan.to_annotate:
            print(f"would annotate {rel} (ring: {ring})")
    for rel in plan.unmatched:
        print(f"no classify match: {rel}")
    print(
        f"annotate: {written if args.yes else len(plan.to_annotate)} "
        f"{'annotated' if args.yes else 'would be annotated'}, "
        f"{len(plan.already_annotated)} already annotated, {len(plan.unmatched)} unmatched"
    )
    return 0


def _selftest(policy: Policy, root: Path, svc: Services) -> int:
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


def _core_mode_conflict(args: argparse.Namespace) -> str | None:
    """Core mode runs no external engine, so an option that demands one is rejected rather than silently passing."""
    if not (args.no_tools or args.engines == "core"):
        return None
    demanded = [flag for flag, on in (("--require-tools", args.require_tools), ("--require-profile", args.require_profile), ("--install-tools", args.install_tools)) if on]
    if not demanded:
        return None
    return f"{', '.join(demanded)} cannot be combined with core mode (--no-tools / --engines core): no external engine runs"


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


def _validate_rule_selection(rules: list[str], svc: Services) -> None:
    """A typo in `--rule` must fail, not select nothing and report green."""
    known = {rule.value for rule in Rule} | {item.name for item in (*svc.engines, *svc.checkers)} | {"coverage", "effects", "suppressions", "discard"}
    unknown = sorted(r for r in rules if r not in known and not r.startswith("ruff-") and not any(k.startswith(f"{r}-") for k in known))
    if unknown:
        raise PolicyError(f"unknown rule family {unknown[0]!r}; choose from {sorted(known)}")


def _check(args: argparse.Namespace, policy: Policy, root: Path, svc: Services) -> int:
    _validate_rule_selection(args.rule, svc)
    conflict = _core_mode_conflict(args)
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
        no_cache=args.no_cache,
        rules=tuple(args.rule),
    )
    report = run_check(policy, root, svc, opts)
    _print_rule_label(policy)
    _print_excluded(report)
    if args.growth_against:
        report.engines.append(_growth_check(args.growth_against, policy, root, svc))
        if policy.ratchets:
            report.engines.append(growth_run(args.growth_against, policy, root, svc))
    setup_command = (
        None
        if args.no_tools
        else _setup_install_command(policy, root, svc, args.type_checker)
    )
    _print_report(args, report, setup_command)
    return 1 if install_status else (0 if report.ok else 1)


def _growth_approver(args: argparse.Namespace) -> str | None:
    """Who is on record as approving growth: required for ``--allow-growth``; an adoption is attributed to itself."""
    raw = args.approved_by
    approver = raw if isinstance(raw, str) else None
    if approver is not None and not (approver.strip() and args.allow_growth):
        raise PolicyError("`--approved-by` needs a non-blank value and `--allow-growth`")
    if args.allow_growth and approver is None:
        raise PolicyError("`--allow-growth` requires `--approved-by` (a name or issue URL)")
    if args.adopt is not None:
        return f"arborescent baseline --adopt {args.adopt}"
    return approver


def _json_at_ref(ref: str, path: str, root: Path, svc: Services) -> dict[str, object]:
    """The JSON object stored at ``path`` in git ``ref``; ValueError when it cannot be read."""
    git = svc.runner.run(["git", "show", f"{ref}:{path}"], root)
    if git.returncode != 0:
        raise ValueError(f"could not read {path} at {ref}: {git.stderr.strip() or git.stdout.strip()}")
    with svc.fs.temporary_directory("arborescent-growth-") as temp:
        copy = temp / "ratchet.json"
        svc.fs.write_text(copy, git.stdout)
        data = svc.fs.read_json(copy)
    if not isinstance(data, dict):
        raise ValueError(f"{path} at {ref} is not a JSON object")
    return {str(key): value for key, value in data.items()}


def _growth_check(ref: str, policy: Policy, root: Path, svc: Services) -> EngineRun:
    started = svc.clock.now()
    lines: list[str] = []
    try:
        current = ratchet_mod.document(svc.fs, root / policy.baseline)
        if not ratchet_mod.growth_consistent(current, _json_at_ref(ref, policy.baseline, root, svc)):
            lines.append(f"baseline changes since {ref} are not fully accounted for by new growth log entries with a reason and approved-by")
        for name in policy.ratchet_files:
            current_values = svc.fs.read_json(root / name)
            if not isinstance(current_values, dict):
                raise ValueError(f"{name} is not a JSON object")
            lines.extend(f"{name}: {p}" for p in ratchet_mod.values_growth_problems(current_values, _json_at_ref(ref, name, root, svc)))
    except (ValueError, TypeError, OSError) as exc:
        lines.append(f"could not verify ratchet growth: {exc}")
    status = "fail" if lines else "ok"
    return EngineRun("baseline growth log", status, svc.clock.now() - started, lines or [f"growth logs match ratchet changes since {ref}"])


if __name__ == "__main__":
    raise SystemExit(main())
