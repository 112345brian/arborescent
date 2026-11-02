"""Engine adapters for import-linter, Tach, and Ruff."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .domain import TACH_EXCLUDED_FOLDERS, ArchitectureContracts, ToolResult, Violation
from .generated_config import (
    HEADER,
    IMPORT_LINTER_FILE,
    TACH_FILE,
    has_own_importlinter_config,
    is_generated,
    toml_list,
    toml_quote,
)
from .ports import Engine, FileSystem

__all__ = ["ImportLinter", "Ruff", "Tach", "TachExternal", "default_engines"]


@dataclass(frozen=True)
class ImportLinter:
    name: str = "import-linter"
    executable: str = "lint-imports"
    config_file: str = IMPORT_LINTER_FILE  # what `generate` writes; import-linter does not discover it by itself
    supports_changed: bool = False

    def command(self, files: Sequence[str]) -> list[str]:
        return [self.executable]

    def command_in(self, files: Sequence[str], root: Path, fs: FileSystem) -> list[str]:
        """Read the generated contracts, but never over a configuration the project wrote itself."""
        if has_own_importlinter_config(fs, root) or not is_generated(fs, root / self.config_file):
            return self.command(files)
        return [self.executable, "--config", self.config_file]

    def passed(self, result: ToolResult) -> bool:
        return result.returncode == 0

    def render_contracts(self, contracts: ArchitectureContracts) -> str:
        """Every neutral contract that forbids an import becomes a ``forbidden`` contract; layers and per-module lists are tach's."""
        lines = [
            HEADER, "[tool.importlinter]", f"root_packages = {toml_list(contracts.root_packages)}",
            "include_external_packages = true", "",
        ]
        for name, sources, forbidden in contracts.forbidden:
            lines += [
                "[[tool.importlinter.contracts]]", f"name = {toml_quote(name)}", 'type = "forbidden"',
                f"source_modules = {toml_list(sources)}", f"forbidden_modules = {toml_list(forbidden)}",
                "allow_indirect_imports = true", "",
            ]
        return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class Tach:
    name: str = "tach"
    executable: str = "tach"
    config_file: str = TACH_FILE
    working_directory: str = "."
    supports_changed: bool = False

    def command(self, files: Sequence[str]) -> list[str]:
        return [self.executable, "check", "--dependencies"]

    def passed(self, result: ToolResult) -> bool:
        return result.returncode == 0

    def render_contracts(self, contracts: ArchitectureContracts) -> str:
        """Layers plus, when the boundaries are listed per module, each module's ``depends_on``. Paths are relative to this file."""
        depth = len([part for part in contracts.config_dir.split("/") if part and part != "."])
        relative = "../" * depth
        source_roots = [f"{relative}{root}" for root in contracts.source_roots]
        excludes = [
            f"{relative}**/{name}" if name == "__pycache__" else f"{relative}{name}" for name in TACH_EXCLUDED_FOLDERS
        ]
        explicit = contracts.explicit_boundaries
        lines = [
            HEADER,
            f"source_roots = {toml_list(source_roots)}",
            f"exclude = {toml_list(excludes)}",
            f"layers = {toml_list(contracts.layers)}",
            'root_module = "ignore"',
        ]
        if explicit:
            lines += [
                "forbid_circular_dependencies = false  # per-module depends_on lists encode the layer, scope and package boundaries",
                "layers_explicit_depends_on = true",
            ]
        else:
            lines.append("forbid_circular_dependencies = true")
        lines.append("")
        renames = [f"{module}:{distribution}" for module, distribution in contracts.distributions]
        lines += ["[external]", f"exclude = {toml_list(contracts.external_exclude)}", f"rename = {toml_list(renames)}", ""]
        for module in contracts.modules:
            lines += ["[[modules]]", f"path = {toml_quote(module.path)}", f"layer = {toml_quote(module.layer)}"]
            if module.may_import is not None:
                lines.append("depends_on = [" + ", ".join("{ path = " + toml_quote(other) + " }" for other in module.may_import) + "]")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


@dataclass(frozen=True)
class TachExternal:
    name: str = "tach-external"
    executable: str = "tach"
    working_directory: str = "."
    supports_changed: bool = False

    def command(self, files: Sequence[str]) -> list[str]:
        return [self.executable, "check-external"]

    def passed(self, result: ToolResult) -> bool:
        return result.returncode == 0


@dataclass(frozen=True)
class Ruff:
    name: str = "ruff"
    executable: str = "ruff"
    config_file: str = "ruff.toml"  # retained for optional preset generation
    supports_changed: bool = True

    def command(self, files: Sequence[str]) -> list[str]:
        return [
            self.executable,
            "check",
            "--ignore-noqa",
            "--exit-zero",
            "--output-format",
            "json",
            *files,
        ]

    def passed(self, result: ToolResult) -> bool:
        return result.returncode == 0

    def violations(self, result: ToolResult, root: Path, fs: FileSystem) -> list[Violation]:
        if result.returncode != 0:
            raise ValueError(f"ruff exited with status {result.returncode}")
        try:
            diagnostics = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"ruff output is not complete JSON: {exc}") from exc
        if not isinstance(diagnostics, list):
            raise ValueError("ruff JSON output must be a list")

        counts: dict[tuple[str, str, str, str], int] = {}
        found: list[Violation] = []
        for item in diagnostics:
            if not isinstance(item, dict):
                raise ValueError("ruff diagnostic must be an object")
            code = item.get("code")
            if not isinstance(code, str) or not code:
                raise ValueError("ruff diagnostic has no rule code")
            if code == "E902":
                raise ValueError("ruff E902 file read errors cannot be baselined")
            filename = item.get("filename")
            message = item.get("message")
            location = item.get("location")
            row = location.get("row") if isinstance(location, dict) else None
            if not isinstance(filename, str) or not isinstance(message, str) or not isinstance(row, int) or row < 1:
                raise ValueError("ruff diagnostic is missing filename, message, or source location")
            path = Path(filename)
            if path.is_absolute():
                try:
                    path = path.relative_to(root)
                except ValueError as exc:
                    raise ValueError(f"ruff reported a file outside the project root: {filename}") from exc
            relative = path.as_posix().removeprefix("./")
            lines = fs.read_text(root / path).splitlines()
            if row > len(lines):
                raise ValueError(f"ruff diagnostic row {row} is outside {relative}")
            normalized = "".join(lines[row - 1].split()).encode("utf-8")
            fingerprint = hashlib.blake2b(normalized, digest_size=4).hexdigest()
            count_key = (code, relative, message, fingerprint)
            counts[count_key] = counts.get(count_key, 0) + 1
            suffix = "" if counts[count_key] == 1 else f"#{counts[count_key]}"
            found.append(Violation(f"ruff-{code}", relative, f"{message}|{fingerprint}{suffix}"))
        return found


def default_engines(output_dir: str = ".") -> list[Engine]:
    _ = output_dir  # output_dir controls optional generated presets, never normal tool discovery
    return [
        ImportLinter(),
        Tach(),
        TachExternal(),
        Ruff(),
    ]
