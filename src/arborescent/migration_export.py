"""The use case behind ``arborescent packages export`` (#54): gather the facts, build the migration arguments."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .domain import Policy, PolicyError
from .migration_arguments import MigrationArguments, build_migration_arguments
from .package_gaps import load_package_context
from .package_pyproject import export_of
from .package_pyproject_gaps import derived_projects
from .ports import FileSystem, ScanResult

__all__ = ["migration_arguments_of"]


def migration_arguments_of(policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> MigrationArguments:
    """The arguments for the policy's ``package_catalog``; the export has nothing to say without one."""
    context = load_package_context(policy, root, fs, scan)
    if context is None:
        raise PolicyError("packages export needs package_catalog in the policy (strict preset): it names the catalog to export")
    arguments = build_migration_arguments(context.catalog, context.facts)
    if not policy.package_pyproject:
        return arguments
    derived = {project.package.id: export_of(project) for project in derived_projects(policy, scan, context.catalog, context.facts) if project.package}
    return replace(arguments, packages=tuple(replace(item, pyproject=derived.get(item.id)) for item in arguments.packages))
