"""``arborescent packages migrate`` (#67): supply a migration library with one package's migration arguments.

An explicit command, never a side effect of ``check`` or ``generate``. A dry run (the default) prints the exact arguments
that would be supplied; ``--yes`` supplies them. Nothing is migrated here: the library does that.
"""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .domain import Policy, PolicyError
from .driver_plan import BLOCKED, READY, DriverPlan, plan_supply, supply
from .driver_port import ImportRewriter, MigrationLibrary
from .driver_request import (
    MODULE_RENAMES,
    MigrationSite,
    RewriteFinding,
    RewriteRequest,
)
from .driver_rewrite import check_source, rewrite_target
from .engine import Services
from .installer_adapter import _shell_join, manual_install_command  # pyright: ignore[reportPrivateUsage]
from .local_targets import (
    DEFAULT_CONFIG,
    ChosenTarget,
    ConfigLocation,
    choose_target,
    load_local_targets,
    locate_config,
    missing_target_message,
)
from .migration_export import migration_arguments_of
from .package_gaps import load_package_context
from .package_manifest import write_extracted_manifest
from .package_model import MANIFEST_NAME, CatalogError

__all__ = ["MigrationTools", "add_migrate_parser", "run_migrate"]


@dataclass(frozen=True)
class MigrationTools:
    """What ``migrate`` is handed by ``cli``: the two ports it drives (the library that extracts history, the import
    rewriter) and the environment its local config is found in. Only this command reads that config."""

    library: MigrationLibrary
    rewriter: ImportRewriter
    environ: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _View:
    rewriter_ready: bool
    target_source: str
    dry_run: bool


def add_migrate_parser(package_actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    """``migrate`` and ``extract``: one pipeline (a migration library, then the import rewriter); ``extract`` also writes the
    manifest that records what was extracted, which the preservation checks and ``packages validate --extracted`` read."""
    for name, summary in (
        ("migrate", "supply a migration library with one package's migration arguments (dry run unless --yes)"),
        ("extract", "migrate one package into a new tree and record it in a .arborescent-package.toml (dry run unless --yes)"),
    ):
        command = package_actions.add_parser(name, help=summary)
        command.add_argument("package_id")
        command.add_argument(
            "--target", "--output", dest="target", type=Path,
            help="the new repository to create; it must not exist (else the package's entry in the local config)",
        )
        command.add_argument("--branch", default="main", help="the history to carry over (default: main)")
        command.add_argument("--yes", action="store_true", help="supply the library instead of printing the arguments")
        if name == "extract":
            command.add_argument("--repository", help="where the extracted tree lives now (default: the package's home repository)")
            command.add_argument("--with-dependencies", action="store_true", help=argparse.SUPPRESS)  # refused in cli.main, with the reason


def run_migrate(args: argparse.Namespace, policy: Policy, root: Path, svc: Services, tools: MigrationTools) -> int:
    library, rewriter = tools.library, tools.rewriter
    arguments = migration_arguments_of(policy, root, svc.fs, svc.scanner.scan(policy, root))
    location, chosen = _resolve_target(args, root, svc, tools, tuple(item.id for item in arguments.packages))
    # A target is only needed once the plan turns out to be ready, so a blocked package is still explained without one.
    site = MigrationSite(root, root / DEFAULT_CONFIG.parent / "no-target" if chosen is None else chosen.path, args.branch)
    plan = plan_supply(arguments, args.package_id, library, site)
    if chosen is None and plan.status == READY:
        print(missing_target_message(args.package_id, location))
        return 1
    wants_rewrite = plan.status == READY and plan.rewrite is not None
    rewriter_ready = rewriter.available() if wants_rewrite else True
    findings = check_source(plan, rewriter, site) if wants_rewrite and rewriter_ready else ()
    _print_plan(plan, tools, site, _View(rewriter_ready, "" if chosen is None else chosen.source, not args.yes))
    if plan.status == BLOCKED:
        return 1
    if plan.status != READY:
        return 0  # a workspace member: there is nothing to supply, and that is not a failure
    if findings:
        _print_findings(findings)
        return 1
    missing = [_setup_line(tool, root, svc) for tool, ready in ((library, library.available()), (rewriter, rewriter_ready)) if not ready]
    for line in missing:
        print(line)
    if missing:
        return 1 if args.yes else 0  # nothing has been created: the missing tool is named before the first step runs
    if not args.yes:
        return 0
    code = _supply_and_rewrite(plan, tools, site)
    if code == 0 and args.package_action == "extract":
        return _write_manifest(args, policy, root, svc, site)
    return code


def _write_manifest(args: argparse.Namespace, policy: Policy, root: Path, svc: Services, site: MigrationSite) -> int:
    """``extract`` only: record what the library extracted, in Arborescent's own manifest, and read it back."""
    context = load_package_context(policy, root, svc.fs, svc.scanner.scan(policy, root))
    if context is None:
        raise PolicyError("packages extract needs package_catalog in the policy (strict preset)")
    try:
        write_extracted_manifest(svc.fs, context.catalog, args.package_id, site.target, args.repository)
    except CatalogError as exc:
        print(f"FAILED: the tree was extracted to {site.target} but its manifest is not valid: {exc}")
        return 1
    print(f"ok: wrote {MANIFEST_NAME} in {site.target}")
    return 0


def _supply_and_rewrite(plan: DriverPlan, tools: MigrationTools, site: MigrationSite) -> int:
    """The two explicit steps, in order: the library writes the history, then the rewriter edits the new repository only."""
    outcome = supply(plan, tools.library)
    print(("ok: " if outcome.ok else "FAILED: ") + outcome.detail)
    if not outcome.ok:
        return 1
    if plan.rewrite is None:
        return 0
    rewritten = rewrite_target(plan, tools.rewriter, site)
    if not rewritten.ok:
        print(f"FAILED: {rewritten.detail}")
        print(f"history was written to {site.target}; the import rewrite did not complete there. Delete it and rerun after fixing the cause.")
        return 1
    print(f"ok: {rewritten.detail}")
    print(f"the rewrite is an uncommitted change in {site.target}: review it with `git -C {site.target} diff`, then commit it")
    return 0


def _resolve_target(
    args: argparse.Namespace, root: Path, svc: Services, tools: MigrationTools, known: tuple[str, ...]
) -> tuple[ConfigLocation, ChosenTarget | None]:
    """Where the new repository is checked out is machine-specific: the flag, else the git-ignored local config.

    This command is the only reader of that config; the export, derived files and contracts never see it. The config is
    not read at all when the flag already names the directory.
    """
    flag = None if args.target is None else (args.target if args.target.is_absolute() else Path.cwd() / args.target)
    location = locate_config(root, tools.environ, svc.fs)
    targets = None if flag is not None else load_local_targets(svc.fs, location, known)
    return location, choose_target(args.package_id, flag, targets)


def _setup_line(library: MigrationLibrary | ImportRewriter, root: Path, svc: Services) -> str:
    command = manual_install_command(root, svc.fs, [library.requirement])
    return f"SETUP  missing migration tool: {library.executable}; install with `{_shell_join(command, shlex.join(command))}`"


def _print_findings(findings: tuple[RewriteFinding, ...]) -> None:
    print(f"blocked: {len(findings)} import statement(s) the rewriter cannot rewrite safely; nothing was created or modified")
    for item in findings:
        print(f"  [unsupported-import-form] {item.file}:{item.line}: {item.text}" if item.file else f"  [unsupported-import-form] {item.text}")


def _print_plan(plan: DriverPlan, tools: MigrationTools, site: MigrationSite, view: _View) -> None:
    mode = "dry run: nothing is supplied; pass --yes to supply the library" if view.dry_run else "supplying the library"
    print(f"migrate {plan.package}: {mode}")
    if plan.status != READY:
        print(f"{plan.status}: {plan.reason}")
        for gap in plan.gaps:
            print(f"  [{gap.code}] {gap.subject}: {gap.detail}")
        for bypass in plan.bypasses:
            print(f"  [private-import] {bypass.module} (package {bypass.package}) imports {bypass.imports}, which is not on the public surface of {plan.package}")
        return
    request = plan.request
    if request is None:
        return
    print(f"library: {tools.library.name}")
    print(f"source:  {request.source}")
    print(f"target:  {request.target}")
    print(f"target from: {view.target_source}")
    print(f"branch:  {request.branch}")
    print("path renames:")
    for rename in request.path_renames:
        print(f"  {rename.source} -> {rename.target}")
    print("module renames:")
    for module in request.module_renames:
        print(f"  {module.source} -> {module.target}")
    for item in plan.unapplied:
        if plan.rewrite is None or not item.startswith(MODULE_RENAMES):  # the rewriter takes the module renames over
            print(f"NOT APPLIED  {item}")
    print(f"arguments: {shlex.join(plan.arguments)}")
    if plan.rewrite is not None:
        _print_rewrite(plan, tools.rewriter, site, view.rewriter_ready)


def _print_rewrite(plan: DriverPlan, rewriter: ImportRewriter, site: MigrationSite, rewriter_ready: bool) -> None:
    if plan.rewrite is None:
        return
    print("import rewrite (on the new repository only, after the library ran):")
    print(f"  with: {rewriter.name}")
    for rename in plan.rewrite.renames:
        print(f"  {rename.source} -> {rename.target}")
    print("  commands:")
    for command in rewriter.commands(RewriteRequest(site.target, plan.rewrite.target_paths, plan.rewrite.renames)):
        print(f"    {shlex.join(command)}")
    if not rewriter_ready:
        print("  unsupported import forms cannot be checked until the rewriter is installed")
