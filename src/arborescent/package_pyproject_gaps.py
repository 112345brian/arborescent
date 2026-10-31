"""Gather the facts for the package ``pyproject.toml`` derivation (#60), write the missing files and report drift in the others.

An existing ``pyproject.toml`` is never rewritten: ``generate`` creates only the files that are absent, and ``check`` reports a
derived field that disagrees with the annotations as ``package-pyproject-drift`` (information, like preset drift).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .domain import ALL, Policy, Rule, Violation
from .package_assignment import RepositoryFacts, owners_of
from .package_model import CatalogDocument
from .package_pyproject import (
    DerivedProject,
    ProjectFacts,
    derive_projects,
    drift_violations,
    render_template,
)
from .ports import FileSystem, ScanResult

__all__ = ["derived_projects", "existing_derived_paths", "pyproject_files", "pyproject_violations"]


def derived_projects(
    policy: Policy, scan: ScanResult, catalog: CatalogDocument, facts: RepositoryFacts
) -> tuple[DerivedProject, ...]:
    owned: dict[str, list[str]] = {package.id: [] for package in catalog.packages}
    for module, path in facts.modules.items():
        for owner in owners_of(catalog, path):
            owned[owner].append(module)
    distributions = {name: dist for local in (policy, *(scope.policy for scope in policy.scopes)) for name, dist in local.dependencies.items()}
    externals: dict[str, frozenset[str]] = {}
    for module in facts.modules:
        ring = facts.rings.get(module)
        scope = scan.scope_by_module.get(module)
        if ring is None or (policy.scopes and scope is None):
            continue
        rule = (policy.scope(scope).policy if scope is not None else policy).ring(ring)
        externals[module] = frozenset() if ALL in rule.external else rule.external
    return derive_projects(
        catalog,
        ProjectFacts(owned, facts.modules, externals, distributions, [root for local in (policy, *(s.policy for s in policy.scopes)) for root in local.source_roots], policy.python),
    )


def existing_derived_paths(fs: FileSystem, root: Path, projects: tuple[DerivedProject, ...]) -> tuple[str, ...]:
    return tuple(project.path for project in projects if fs.is_file(root / project.path))


def pyproject_files(fs: FileSystem, root: Path, projects: tuple[DerivedProject, ...]) -> dict[str, str]:
    """The text of every derived file that does not exist yet, keyed by repository-relative path."""
    return {project.path: render_template(project) for project in projects if not fs.exists(root / project.path)}


def pyproject_violations(fs: FileSystem, root: Path, projects: tuple[DerivedProject, ...]) -> list[Violation]:
    """Drift of the existing files; a file that is absent is not drift (``generate`` creates it)."""
    found: list[Violation] = []
    for project in projects:
        path = root / project.path
        if not fs.is_file(path):
            continue
        try:
            data = tomllib.loads(fs.read_text(path))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            found.append(Violation(Rule.PACKAGE_PYPROJECT_DRIFT, project.path, f"cannot be read, so no derived field was compared: {exc}"))
            continue
        found += drift_violations(project, data)
    return found
