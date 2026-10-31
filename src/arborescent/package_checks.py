"""The package-annotation layer's implementation of what the engine asks of it (``ports.PackageChecks``).

The engine owns that interface and never imports this module: the entrypoint binds an instance in ``Services``. This is the
only place the engine's package questions meet the package modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .domain import Mutation, Policy, Violation
from .package_gaps import (
    PackageContext,
    load_package_context,
    package_gap_violations,
    package_layout,
)
from .package_probes import package_mutations
from .package_pyproject_gaps import derived_projects, pyproject_files
from .ports import (
    FileSystem,
    ModuleScanner,
    PackageBoundaries,
    PackageProbes,
    ScanResult,
)

__all__ = ["AnnotationPackageChecks"]


@dataclass(frozen=True)
class _Probes:
    context: PackageContext

    def mutations(
        self, policy: Policy, arbor_index: Mapping[str, tuple[str | None, str | None]], scope_by_module: Mapping[str, str]
    ) -> tuple[list[Mutation], list[str]]:
        return package_mutations(policy, arbor_index, scope_by_module, self.context)


@dataclass(frozen=True)
class AnnotationPackageChecks:
    def gap_violations(
        self, policy: Policy, root: Path, fs: FileSystem, scan: ScanResult, scanner: ModuleScanner
    ) -> list[Violation]:
        return package_gap_violations(policy, root, fs, scan, scanner)

    def boundaries(self, policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> PackageBoundaries | None:
        return package_layout(policy, root, fs, scan)

    def new_files(self, policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> dict[str, str]:
        """The package ``pyproject.toml`` files that do not exist yet, derived from the annotations."""
        context = load_package_context(policy, root, fs, scan)
        if context is None or not policy.package_pyproject:
            return {}
        return dict(pyproject_files(fs, root, derived_projects(policy, scan, context.catalog, context.facts)))

    def probes(
        self, policy: Policy, root: Path, fs: FileSystem, scan: ScanResult, scanner: ModuleScanner
    ) -> PackageProbes | None:
        context = load_package_context(policy, root, fs, scan, scanner)
        return None if context is None else _Probes(context)
