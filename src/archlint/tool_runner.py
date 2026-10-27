"""Subprocess adapters for external checkers and package-manager discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import sysconfig
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from .domain import ToolResult


def resolve_executable(
    executable: str,
    *,
    scripts_dir: Path | None = None,
    scripts_dirs: Sequence[Path] = (),
    path_lookup: Callable[[str], str | None] = shutil.which,
    windows: bool = os.name == "nt",
) -> str | None:
    """Find a tool in the active interpreter's scripts directory, then fall back to PATH."""
    direct = Path(executable)
    if direct.is_absolute() and direct.is_file():
        return str(direct)
    suffixes = (".exe", ".cmd", ".bat", "") if windows else ("",)
    names = tuple(executable + suffix for suffix in suffixes)
    search_dirs = (*scripts_dirs, scripts_dir or Path(sysconfig.get_path("scripts")))
    for scripts in search_dirs:
        for name in names:
            candidate = scripts / name
            if not candidate.is_file():
                continue
            if windows or os.access(candidate, os.X_OK):
                return str(candidate)
    return path_lookup(executable)


def _poetry_scripts_dir(root: Path) -> Path | None:
    """Return a project's Poetry environment scripts directory when the project has one."""
    pyproject = root / "pyproject.toml"
    try:
        is_poetry_project = (root / "poetry.lock").is_file() or "poetry" in tomllib.loads(
            pyproject.read_text(encoding="utf-8")
        ).get("tool", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    poetry = shutil.which("poetry")
    if not is_poetry_project or poetry is None:
        return None
    try:
        result = subprocess.run(
            [poetry, "env", "info", "--executable"], cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    executable = result.stdout.strip()
    if result.returncode != 0 or not executable:
        return None
    return Path(executable).parent


class SubprocessToolRunner:
    """``ToolRunner`` over ``subprocess.run``; a missing executable is a result, not an exception."""

    def __init__(self, project_root: Path | None = None) -> None:
        self._project_root = project_root
        self._project_scripts = _poetry_scripts_dir(project_root) if project_root is not None else None

    def refresh_project_environment(self) -> None:
        """Refresh Poetry's environment after it may have created a venv or installed scripts."""
        if self._project_root is not None:
            self._project_scripts = _poetry_scripts_dir(self._project_root)

    def _resolve(self, executable: str) -> str | None:
        project_dirs = (self._project_scripts,) if self._project_scripts is not None else ()
        return resolve_executable(executable, scripts_dirs=project_dirs)

    def available(self, executable: str) -> bool:
        return self._resolve(executable) is not None

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        started = time.perf_counter()
        resolved = self._resolve(argv[0])
        if resolved is None:
            return ToolResult(None, "", f"{argv[0]}: not installed", time.perf_counter() - started)
        try:
            done = subprocess.run(
                [resolved, *argv[1:]], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL, check=False,
            )
        except FileNotFoundError:
            return ToolResult(None, "", f"{argv[0]}: not installed", time.perf_counter() - started)
        except subprocess.TimeoutExpired:
            return ToolResult(-1, "", f"{argv[0]}: timed out after {timeout}s", time.perf_counter() - started)
        return ToolResult(done.returncode, done.stdout, done.stderr, time.perf_counter() - started)
