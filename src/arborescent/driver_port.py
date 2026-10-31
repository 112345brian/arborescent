"""The port of a dedicated migration library (#67). The library migrates; the driver only supplies its arguments."""

from __future__ import annotations

from typing import Protocol

from .driver_request import MigrationOutcome, MigrationRequest

__all__ = ["MigrationLibrary"]


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
