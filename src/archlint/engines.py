"""Engine adapters for the two non-type-checker tools ``check`` runs: import-linter and tach. Both check the whole
import graph whatever files changed, so ``command`` ignores its argument."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .domain import ToolResult
from .ports import Engine

__all__ = ["ImportLinter", "Tach", "default_engines"]


@dataclass(frozen=True)
class ImportLinter:
    name: str = "import-linter"
    executable: str = "lint-imports"
    config_file: str = "archlint-importlinter.toml"  # == generate.IMPORT_LINTER_FILE (adapters may not import it)

    def command(self, files: Sequence[str]) -> list[str]:
        return [self.executable, "--config", self.config_file]

    def passed(self, result: ToolResult) -> bool:
        return result.returncode == 0


@dataclass(frozen=True)
class Tach:
    name: str = "tach"
    executable: str = "tach"

    def command(self, files: Sequence[str]) -> list[str]:
        return [self.executable, "check"]

    def passed(self, result: ToolResult) -> bool:
        return result.returncode == 0


def default_engines() -> list[Engine]:
    return [ImportLinter(), Tach()]
