"""Command-line setup flow for installing optional check tools."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

from .domain import Policy, PolicyError
from .engine import Services, pick_checker, tool_is_disabled
from .install_tools import missing_tool_requirements
from .installer_adapter import (
    InstallerCommands,
    _shell_join,
    configure_after_install,
    installer_commands,
    manual_install_command,
)


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
    installer = installer_commands(root, svc.fs, svc.runner, requirements)
    for executable, requirement in missing:
        print(f"missing: {executable} ({requirement})")
    if installer is None:
        fallback = manual_install_command(root, svc.fs, requirements)
        print(f"manual install command: {_shell_join(fallback, shlex.join(fallback))}")
        print("No supported installer found. Install the listed packages into the active Python environment.")
        return 1
    print(f"install command: {_shell_join(installer.install, shlex.join(installer.install))}")
    if not _confirm_install(args.yes):
        return 0 if getattr(args, "continue_on_decline", False) else 1
    return _run_tool_install(svc, root, installer, missing)

def _confirm_install(yes: bool) -> bool:
    if yes:
        return True
    if not _interactive_install_allowed():
        print("Not installing in a non-interactive session; rerun `arborescent install-tools` in a terminal or pass --yes.")
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
    installer: InstallerCommands,
    missing: Sequence[tuple[str, str]],
) -> int:
    installed = svc.runner.run(installer.install, root)
    if installed.returncode != 0:
        detail = installed.stderr.strip() or installed.stdout.strip() or f"installer exited {installed.returncode}"
        print(f"install-tools: installation failed: {detail}", file=sys.stderr)
        return 1
    try:
        configure_after_install(installer, root, svc.fs, svc.runner)
    except (OSError, ValueError, PolicyError) as exc:
        print(f"install-tools: could not configure the {installer.name} environment: {exc}", file=sys.stderr)
        return 1
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

def _interactive_install_allowed() -> bool:
    if os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}:
        return False
    try:
        return bool(sys.stdin.isatty()) and bool(sys.stdout.isatty())
    except (AttributeError, OSError):
        return False

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
    installer = installer_commands(root, svc.fs, svc.runner, requirements)
    command = installer.install if installer is not None else manual_install_command(root, svc.fs, requirements)
    return _shell_join(command, shlex.join(command))
