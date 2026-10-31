"""``arborescent packages migrate`` (#67): supply a migration library with one package's migration arguments.

An explicit command, never a side effect of ``check`` or ``generate``. A dry run (the default) prints the exact arguments
that would be supplied; ``--yes`` supplies them. Nothing is migrated here: the library does that.
"""

from __future__ import annotations

import argparse
import shlex
from pathlib import Path

from .domain import Policy
from .driver_plan import BLOCKED, READY, DriverPlan, plan_supply, supply
from .driver_port import MigrationLibrary
from .driver_request import MigrationSite
from .engine import Services
from .installer_adapter import _shell_join, manual_install_command  # pyright: ignore[reportPrivateUsage]
from .migration_export import migration_arguments_of

__all__ = ["add_migrate_parser", "run_migrate"]


def add_migrate_parser(package_actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    migrate = package_actions.add_parser(
        "migrate", help="supply a migration library with one package's migration arguments (dry run unless --yes)"
    )
    migrate.add_argument("package_id")
    migrate.add_argument("--target", type=Path, required=True, help="the new repository to create; it must not exist")
    migrate.add_argument("--branch", default="main", help="the history to carry over (default: main)")
    migrate.add_argument("--yes", action="store_true", help="supply the library instead of printing the arguments")


def run_migrate(args: argparse.Namespace, policy: Policy, root: Path, svc: Services, library: MigrationLibrary) -> int:
    arguments = migration_arguments_of(policy, root, svc.fs, svc.scanner.scan(policy, root))
    target = args.target if args.target.is_absolute() else Path.cwd() / args.target
    plan = plan_supply(arguments, args.package_id, library, MigrationSite(root, target, args.branch))
    _print_plan(plan, library, dry_run=not args.yes)
    if plan.status == BLOCKED:
        return 1
    if plan.status != READY:
        return 0  # a workspace member: there is nothing to supply, and that is not a failure
    if not library.available():
        print(_setup_line(library, root, svc))
        return 0 if not args.yes else 1
    if not args.yes:
        return 0
    outcome = supply(plan, library)
    print(("ok: " if outcome.ok else "FAILED: ") + outcome.detail)
    return 0 if outcome.ok else 1


def _setup_line(library: MigrationLibrary, root: Path, svc: Services) -> str:
    command = manual_install_command(root, svc.fs, [library.requirement])
    return f"SETUP  missing migration library: {library.executable}; install with `{_shell_join(command, shlex.join(command))}`"


def _print_plan(plan: DriverPlan, library: MigrationLibrary, *, dry_run: bool) -> None:
    mode = "dry run: nothing is supplied; pass --yes to supply the library" if dry_run else "supplying the library"
    print(f"migrate {plan.package}: {mode}")
    if plan.status != READY:
        print(f"{plan.status}: {plan.reason}")
        for gap in plan.gaps:
            print(f"  [{gap.code}] {gap.subject}: {gap.detail}")
        return
    request = plan.request
    if request is None:
        return
    print(f"library: {library.name}")
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
        print(f"NOT APPLIED  {item}")
    print(f"arguments: {shlex.join(plan.arguments)}")
