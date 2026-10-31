"""What the driver hands a migration library (#67): one package's migration arguments, and what came back.

Pure value types. The request holds only arguments that already exist in ``migration_arguments``; the driver adds no
fact of its own. It names no library: each adapter decides how to spell these for its tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .migration_arguments import PathRename
from .package_model import ModuleRename

__all__ = ["MODULE_RENAMES", "ImportRewrite", "MigrationOutcome", "MigrationRequest", "MigrationSite", "RewriteFinding", "RewriteRequest"]

MODULE_RENAMES = "module renames"  # the name under which a library reports it cannot apply them


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


@dataclass(frozen=True, slots=True)
class ImportRewrite:
    """What a plan carries for the import-rewriting step, independent of which tree it is applied to."""

    renames: tuple[ModuleRename, ...]  # old dotted module name -> new, one per owned module whose name changes, longest first
    source_paths: tuple[str, ...]  # the owned paths in the monorepo (read only: used to check, never to rewrite)
    target_paths: tuple[str, ...]  # the same paths in the new repository ("." is its root)


@dataclass(frozen=True, slots=True)
class RewriteRequest:
    root: Path
    paths: tuple[str, ...]  # below ``root``
    renames: tuple[ModuleRename, ...]


@dataclass(frozen=True, slots=True)
class RewriteFinding:
    """An import statement the rewriter cannot handle safely, or one it left behind."""

    file: str  # relative to the request's root, posix
    line: int
    text: str
