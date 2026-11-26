"""Subprocess adapters for external checkers and package-manager discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import sysconfig
import tempfile
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from .domain import ToolResult
from .generated_config import unread_tach_roots, windows_tach_config

# Besides Python files, what `tach check` and `tach check-external` read from the tree.
_TACH_INPUTS = frozenset(
    {"tach.toml", "tach.domain.toml", "pyproject.toml", "requirements.txt", "setup.py", ".gitignore", ".ignore"}
)


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


def _stop(error: OSError) -> None:
    raise error


def _copy_for_tach(project: Path, target: Path) -> None:
    """Copy what tach reads, keeping relative paths; installed environments, symlinks and other files stay behind."""
    for folder, folders, files in os.walk(project, onerror=_stop):
        here = Path(folder)
        folders[:] = [
            name for name in folders
            if name not in {".git", "node_modules", "__pycache__"} and not (here / name / "pyvenv.cfg").is_file()
        ]
        copy = target / here.relative_to(project)
        copy.mkdir(exist_ok=True)
        for name in files:
            source = here / name
            if (name.endswith((".py", ".pyi")) or name in _TACH_INPUTS) and not source.is_symlink() and source.is_file():
                _ = shutil.copyfile(source, copy / name)
    if (project / ".git").exists():  # tach applies .gitignore only inside a repository
        (target / ".git" / "info").mkdir(parents=True)
        exclude = project / ".git" / "info" / "exclude"
        if exclude.is_file():
            _ = shutil.copyfile(exclude, target / ".git" / "info" / "exclude")


def _unread_roots_message(cwd: Path) -> str:
    """Why tach must not run in ``cwd``: it would pass with a source root unchecked. Empty when it may run.

    ``tach.toml`` wins over a ``[tool.tach]`` table. On Windows ``windows_tach_config`` normalises the same spellings in a copy instead."""
    for name, table in (("tach.toml", ()), ("pyproject.toml", ("tool", "tach"))):
        config = cwd / name
        if not config.is_file():
            continue
        try:
            unread = unread_tach_roots(config.read_text(encoding="utf-8-sig"), table)
        except (OSError, UnicodeDecodeError):
            return ""  # tach reports a config it cannot read
        if not unread:
            return ""
        spelled = "; ".join(f"write {written!r} as {canonical!r}" for written, canonical in unread)
        return f"tach: not run, it would leave source_roots in {name} unchecked as spelled: {spelled}"
    return ""


class SubprocessToolRunner:
    """``ToolRunner`` over ``subprocess.run``; a missing executable is a result, not an exception."""

    def __init__(self, project_root: Path | None = None, *, windows: bool = os.name == "nt") -> None:
        self._windows = windows
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
        command = [resolved, *argv[1:]]
        if argv[0] == "tach" and list(argv[1:2]) in (["check"], ["check-external"]):
            if self._windows:
                return self._run_tach(command, cwd, timeout, started)
            if refusal := _unread_roots_message(cwd):
                return ToolResult(-1, "", refusal, time.perf_counter() - started)
        return self._execute(command, argv[0], cwd, timeout, started)

    def _run_tach(self, command: list[str], cwd: Path, timeout: float, started: float) -> ToolResult:
        """Run in ``cwd``, or in a temporary copy of it when Windows tach cannot read its config (``windows_tach_config``).

        The project is never written to. A copy that cannot be made is a failed run: tach in place would skip files."""
        try:
            config = cwd / "tach.toml"
            rewritten = windows_tach_config(config.read_text(encoding="utf-8-sig")) if config.is_file() else None
            if rewritten is not None:
                with tempfile.TemporaryDirectory(prefix="arborescent-tach-", ignore_cleanup_errors=True) as temp:
                    copy = Path(temp).resolve()
                    _copy_for_tach(cwd, copy)
                    _ = (copy / "tach.toml").write_text(rewritten, encoding="utf-8")
                    return self._execute(command, "tach", copy, timeout, started)
        except (OSError, UnicodeDecodeError) as exc:
            message = f"tach: could not run on a copy with backslash source_roots: {exc}"
            return ToolResult(-1, "", message, time.perf_counter() - started)
        return self._execute(command, "tach", cwd, timeout, started)

    def _execute(self, command: list[str], name: str, cwd: Path, timeout: float, started: float) -> ToolResult:
        try:
            done = subprocess.run(
                command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL, check=False,
            )
        except FileNotFoundError:
            return ToolResult(None, "", f"{name}: not installed", time.perf_counter() - started)
        except subprocess.TimeoutExpired:
            return ToolResult(-1, "", f"{name}: timed out after {timeout}s", time.perf_counter() - started)
        return ToolResult(done.returncode, done.stdout, done.stderr, time.perf_counter() - started)
