"""What the driver hands a migration library (#67): one package's migration arguments, and what came back.

Pure value types. The request holds only arguments that already exist in ``migration_arguments``; the driver adds no
fact of its own. It names no library: each adapter decides how to spell these for its tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .migration_arguments import PathRename
from .package_model import ModuleRename

__all__ = ["MigrationOutcome", "MigrationRequest", "MigrationSite"]


@dataclass(frozen=True, slots=True)
class MigrationSite:
    """Where a package's history is read from and written to."""

    source: Path  # the monorepo; never modified
    target: Path  # the new repository, which must not exist yet
    branch: str  # the history to carry over


@dataclass(frozen=True, slots=True)
class MigrationRequest:
    package: str
    source: Path  # the monorepo the history is read from; never modified
    target: Path  # the new repository, which must not exist yet
    branch: str  # the history to carry over
    path_renames: tuple[PathRename, ...]  # owned monorepo path -> path in the new repository ("." is its root)
    module_renames: tuple[ModuleRename, ...]  # dotted-module prefix -> new prefix, for import rewriting


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    ok: bool
    detail: str
