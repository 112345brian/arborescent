"""``arborescent packages migrate`` (#67): supply a migration library with one package's migration arguments.

An explicit command, never a side effect of ``check`` or ``generate``. A dry run (the default) prints the exact arguments
that would be supplied; ``--yes`` supplies them. Nothing is migrated here: the library does that.
"""

from __future__ import annotations

import argparse
import shlex
from dataclasses import dataclass
from pathlib import Path

from .domain import Policy
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
from .migration_export import migration_arguments_of

__all__ = ["MigrationTools", "add_migrate_parser", "run_migrate"]


@dataclass(frozen=True)
class MigrationTools:
    """The two ports ``migrate`` drives, bound by ``cli``: the library that extracts history and the import rewriter."""

    library: MigrationLibrary
    rewriter: ImportRewriter


def add_migrate_parser(package_actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    migrate = package_actions.add_parser(
        "migrate", help="supply a migration library with one package's migration arguments (dry run unless --yes)"
    )
    migrate.add_argument("package_id")
    migrate.add_argument("--target", type=Path, required=True, help="the new repository to create; it must not exist")
    migrate.add_argument("--branch", default="main", help="the history to carry over (default: main)")
    migrate.add_argument("--yes", action="store_true", help="supply the library instead of printing the arguments")


def run_migrate(args: argparse.Namespace, policy: Policy, root: Path, svc: Services, tools: MigrationTools) -> int:
    library, rewriter = tools.library, tools.rewriter
    arguments = migration_arguments_of(policy, root, svc.fs, svc.scanner.scan(policy, root))
    site = MigrationSite(root, args.target if args.target.is_absolute() else Path.cwd() / args.target, args.branch)
    plan = plan_supply(arguments, args.package_id, library, site)
    wants_rewrite = plan.status == READY and plan.rewrite is not None
    rewriter_ready = rewriter.available() if wants_rewrite else True
    findings = check_source(plan, rewriter, site) if wants_rewrite and rewriter_ready else ()
    _print_plan(plan, tools, site, rewriter_ready, dry_run=not args.yes)
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
    outcome = supply(plan, library)
    print(("ok: " if outcome.ok else "FAILED: ") + outcome.detail)
    if not outcome.ok:
        return 1
    if plan.rewrite is None:
        return 0
    rewritten = rewrite_target(plan, rewriter, site)
    if not rewritten.ok:
        print(f"FAILED: {rewritten.detail}")
        print(f"history was written to {site.target}; the import rewrite did not complete there. Delete it and rerun after fixing the cause.")
        return 1
    print(f"ok: {rewritten.detail}")
    print(f"the rewrite is an uncommitted change in {site.target}: review it with `git -C {site.target} diff`, then commit it")
    return 0


def _setup_line(library: MigrationLibrary | ImportRewriter, root: Path, svc: Services) -> str:
    command = manual_install_command(root, svc.fs, [library.requirement])
    return f"SETUP  missing migration tool: {library.executable}; install with `{_shell_join(command, shlex.join(command))}`"


def _print_findings(findings: tuple[RewriteFinding, ...]) -> None:
    print(f"blocked: {len(findings)} import statement(s) the rewriter cannot rewrite safely; nothing was created or modified")
    for item in findings:
        print(f"  [unsupported-import-form] {item.file}:{item.line}: {item.text}" if item.file else f"  [unsupported-import-form] {item.text}")


def _print_plan(plan: DriverPlan, tools: MigrationTools, site: MigrationSite, rewriter_ready: bool, *, dry_run: bool) -> None:
    mode = "dry run: nothing is supplied; pass --yes to supply the library" if dry_run else "supplying the library"
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
        _print_rewrite(plan, tools.rewriter, site, rewriter_ready)


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
