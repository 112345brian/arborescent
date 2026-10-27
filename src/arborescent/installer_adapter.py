"""Package-manager adapters for Arborescent's optional check tools."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import FileSystem, ToolRunner


@dataclass(frozen=True)
class InstallerCommands:
    install: list[str]
    check: list[str]
    name: str


def installer_commands(root: Path, fs: FileSystem, runner: ToolRunner, requirements: Sequence[str]) -> InstallerCommands | None:
    """Select the package-manager commands that target the current project environment."""
    if is_poetry_project(root, fs):
        if not runner.available("poetry"):
            return None
        return InstallerCommands(
            ["poetry", "add", "--group", "arborescent", *requirements],
            ["poetry", "check", "--lock"],
            "poetry",
        )
    python = sys.executable
    pip_check = runner.run([python, "-m", "pip", "--version"], root)
    if pip_check.returncode == 0:
        return InstallerCommands(
            [python, "-m", "pip", "install", *requirements],
            [python, "-m", "pip", "check"],
            "pip",
        )
    if runner.available("uv"):
        return InstallerCommands(
            ["uv", "pip", "install", "--python", python, *requirements],
            ["uv", "pip", "check", "--python", python],
            "uv",
        )
    return None


def configure_after_install(installer: InstallerCommands, root: Path, fs: FileSystem, runner: ToolRunner) -> None:
    """Apply package-manager metadata and refresh the runner after installation."""
    if installer.name == "poetry":
        include_poetry_arborescent_group_in_dev(root, fs)
        refresh = getattr(runner, "refresh_project_environment", None)
        if callable(refresh):
            refresh()

def is_poetry_project(root: Path, fs: FileSystem) -> bool:
    """Detect Poetry through its lockfile or project configuration without reading outside the file-system port."""
    if fs.is_file(root / "poetry.lock"):
        return True
    try:
        project = tomllib.loads(fs.read_text(root / "pyproject.toml"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    return "poetry" in project.get("tool", {})


def manual_install_command(root: Path, fs: FileSystem, requirements: Sequence[str]) -> list[str]:
    """Return a shell-ready command when Arborescent cannot invoke a supported installer itself."""
    if is_poetry_project(root, fs):
        return ["poetry", "add", "--group", "arborescent", *requirements]
    return [sys.executable, "-m", "pip", "install", *requirements]


def include_poetry_arborescent_group_in_dev(root: Path, fs: FileSystem) -> None:
    """Keep checkers isolated in an optional Poetry group included by the project's dev group."""
    path = root / "pyproject.toml"
    text = fs.read_text(path)
    project = tomllib.loads(text)
    poetry = project.get("tool", {}).get("poetry", {})
    groups = poetry.get("group", {})
    if "dependency-groups" in project:
        dev_items = project["dependency-groups"].get("dev", [])
        if not any(isinstance(item, dict) and item.get("include-group") == "arborescent" for item in dev_items):
            text = _include_pep735_group(text, "dev", "arborescent")
    else:
        dev = groups.get("dev", {})
        includes = dev.get("include-groups", [])
        if "arborescent" not in includes:
            text = _set_poetry_group_option(text, "dev", "include-groups", [*includes, "arborescent"])
    arborescent = groups.get("arborescent", {})
    if not arborescent.get("optional", False):
        text = _set_poetry_group_option(text, "arborescent", "optional", True)
    fs.write_text(path, text)


def _set_poetry_group_option(text: str, group: str, key: str, value: object) -> str:
    """Set one scalar/list in a Poetry group while preserving the rest of the TOML source."""
    header = f"[tool.poetry.group.{group}]"
    child_header = f"[tool.poetry.group.{group}.dependencies]"
    if isinstance(value, list):
        rendered = json.dumps(value)
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        raise ValueError(f"unsupported Poetry group option value: {value!r}")
    lines = text.splitlines(keepends=True)
    headers = [index for index, line in enumerate(lines) if line.strip() == header]
    if headers:
        start = headers[0]
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("[")),
            len(lines),
        )
        section = "".join(lines[start:end])
        pattern = re.compile(
            rf"(?m)^([ \t]*{re.escape(key)}[ \t]*=[ \t]*)(?:\[[^\]]*\]|true|false)([ \t]*(?:#.*)?)(?=\r?$)"
        )
        replacement = rf"\g<1>{rendered}\g<2>"
        updated, count = pattern.subn(replacement, section)
        if count:
            lines[start:end] = [updated]
        else:
            lines.insert(start + 1, replacement + "\n")
        return "".join(lines)
    child_index = next((index for index, line in enumerate(lines) if line.strip() == child_header), None)
    section = f"{header}\n{key} = {rendered}\n\n"
    if child_index is not None:
        lines.insert(child_index, section)
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("\n" + section)
    return "".join(lines)


def _include_pep735_group(text: str, parent: str, child: str) -> str:
    """Append a PEP 735 group inclusion to one array without rewriting neighboring TOML."""
    section = re.search(r"(?m)^\[dependency-groups\]\s*$", text)
    if section is None:
        ending = "" if not text or text.endswith("\n") else "\n"
        return f"{text}{ending}\n[dependency-groups]\n{parent} = [{{ include-group = \"{child}\" }}]\n"
    section_end = re.search(r"(?m)^\[[^\]]+\]\s*$", text[section.end() :])
    end = section.end() + section_end.start() if section_end is not None else len(text)
    body = text[section.end() : end]
    assignment = re.search(rf"(?m)^\s*{re.escape(parent)}\s*=\s*\[", body)
    if assignment is None:
        insertion = f"\n{parent} = [{{ include-group = \"{child}\" }}]\n"
        return text[:end].rstrip() + insertion + text[end:]
    array_open = section.end() + assignment.end() - 1
    _toml_array_close(text, array_open)  # Validate before inserting, preserving original comments and commas.
    addition = f"\n    {{ include-group = \"{child}\" }},"
    return text[: array_open + 1] + addition + text[array_open + 1 :]


def _toml_array_close(text: str, array_open: int) -> int:
    depth = 0
    index = array_open
    while index < len(text):
        char = text[index]
        if char in {"'", '"'}:
            index = _toml_string_end(text, index)
            continue
        if char == "#":
            index = _toml_comment_end(text, index)
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("unterminated dependency-groups array in pyproject.toml")


def _toml_string_end(text: str, start: int) -> int:
    quote = text[start]
    triple = text.startswith(quote * 3, start)
    delimiter = quote * (3 if triple else 1)
    index = start + len(delimiter)
    while index < len(text):
        if text.startswith(delimiter, index):
            return index + len(delimiter)
        if quote == '"' and text[index] == "\\":
            index += 2
        else:
            index += 1
    return len(text)


def _toml_comment_end(text: str, start: int) -> int:
    newline = text.find("\n", start)
    return len(text) if newline < 0 else newline + 1
