"""The port of a dedicated migration library (#67). The library migrates; the driver only supplies its arguments."""

from __future__ import annotations

from typing import Protocol

from .driver_request import (
    MigrationOutcome,
    MigrationRequest,
    RewriteFinding,
    RewriteRequest,
)

__all__ = ["ImportRewriter", "MigrationLibrary"]


class MigrationLibrary(Protocol):
    """A history-preserving migration library. Adapters implement it; tests substitute ``testing.FakeMigrationLibrary``.

    ``supply`` is the only method with a side effect. ``arguments`` renders exactly what ``supply`` would pass the
    library, so a dry run can show it.
    """

    @property
    def name(self) -> str: ...

    @property
    def executable(self) -> str: ...  # what has to be installed

    @property
    def requirement(self) -> str: ...  # the pip requirement that provides it

    def available(self) -> bool: ...

    def unsupported(self, request: MigrationRequest) -> tuple[str, ...]: ...  # arguments the library cannot apply

    def arguments(self, request: MigrationRequest) -> tuple[str, ...]: ...

    def supply(self, request: MigrationRequest) -> MigrationOutcome: ...


class ImportRewriter(Protocol):
    """Applies a module-name map to the import statements of a tree. Separate from ``MigrationLibrary``: a library that
    extracts history does not rewrite imports, and one that rewrites imports does not extract history.

    ``unsupported`` reads and changes nothing. ``apply`` must call it first and, when it finds anything, refuse before
    touching a file.
    """

    @property
    def name(self) -> str: ...

    @property
    def executable(self) -> str: ...

    @property
    def requirement(self) -> str: ...

    def available(self) -> bool: ...

    def unsupported(self, request: RewriteRequest) -> tuple[RewriteFinding, ...]: ...  # statements it cannot rewrite safely

    def commands(self, request: RewriteRequest) -> tuple[tuple[str, ...], ...]: ...  # the exact invocations, in order

    def apply(self, request: RewriteRequest) -> MigrationOutcome: ...
