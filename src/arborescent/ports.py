"""What Arborescent needs from the world. Adapters implement these; tests substitute fakes (``arborescent.testing``)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from .domain import Policy, ToolResult, Violation
from .facts import ModuleInfo

__all__ = ["Clock", "Engine", "FileSystem", "ModuleScanner", "ScanResult", "ToolRunner", "TypeChecker", "ViolationEngine"]


class ToolRunner(Protocol):
    """Runs an external engine (lint-imports, tach, a type checker, git). Never raises for a missing tool."""

    def available(self, executable: str) -> bool: ...

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult: ...


class Clock(Protocol):
    """A monotonic stopwatch in seconds; only differences mean anything."""

    def now(self) -> float: ...


class FileSystem(Protocol):
    """Every file access the use cases make. Text is UTF-8 and newlines are kept exactly as stored (no translation);
    writes never translate ``\n``. Errors are the ``OSError`` / ``UnicodeDecodeError`` the real file system raises
    (the fake raises the closest subclass, which can differ from the OS's, e.g. ``PermissionError`` for a folder on
    Windows). Glob patterns follow the OS's case rules on the real file system; the fake is case-sensitive."""

    def read_text(self, path: Path) -> str: ...

    def read_bytes(self, path: Path) -> bytes: ...

    def write_text(self, path: Path, text: str) -> None: ...  # parent must exist; overwrites

    def write_bytes(self, path: Path, data: bytes) -> None: ...  # parent must exist; overwrites

    def read_json(self, path: Path) -> object: ...  # ValueError on empty or malformed content

    def write_json(self, path: Path, data: object) -> None: ...  # indent 2, LF, trailing newline

    def exists(self, path: Path) -> bool: ...

    def is_file(self, path: Path) -> bool: ...

    def is_dir(self, path: Path) -> bool: ...

    def glob(self, root: Path, pattern: str) -> list[Path]: ...  # sorted; ``**`` spans folders

    def rglob(self, root: Path, pattern: str) -> list[Path]: ...  # sorted; files and folders at any depth

    def mkdir(self, path: Path) -> None: ...  # with parents; existing is fine

    def remove_file(self, path: Path) -> None: ...

    def remove_tree(self, path: Path) -> None: ...  # a missing path is fine

    def copy_tree(self, src: Path, dst: Path, ignore: Sequence[str] = ()) -> None: ...  # ``ignore``: name globs to skip

    def temporary_directory(self, prefix: str, parent: Path | None = None) -> AbstractContextManager[Path]: ...  # removed with its contents on exit


class Engine(Protocol):
    """An external checker run in parallel by ``check``: import-linter, tach, and every ``TypeChecker`` (which satisfies
    this). Adding one means writing an adapter and passing it in ``Services``; ``run_check`` does not change."""

    @property
    def name(self) -> str: ...

    @property
    def executable(self) -> str: ...

    def command(self, files: Sequence[str]) -> list[str]: ...  # files: the changed ones (empty = everything)

    def passed(self, result: ToolResult) -> bool: ...


class ViolationEngine(Protocol):
    """Optional extension implemented by engines whose diagnostics feed Arborescent's violation baseline.

    It is separate from ``Engine`` so existing adapters and user-supplied engines remain source-compatible.
    """

    def violations(self, result: ToolResult, root: Path, fs: FileSystem) -> Sequence[Violation]: ...


class TypeChecker(Engine, Protocol):
    """A swappable type checker: an ``Engine`` whose strict configuration the policy renders."""

    @property
    def config_file(self) -> str: ...

    def render_config(self, policy: Policy, paths: Sequence[str]) -> str: ...


class ScanResult(Protocol):
    @property
    def known(self) -> frozenset[str]: ...  # every first-party module name in the repository

    @property
    def modules(self) -> Mapping[str, ModuleInfo]: ...  # the ones parsed (all, or only the changed ones)

    @property
    def entry_deps(self) -> Mapping[str, tuple[str, ...] | None]: ...  # entry script -> PEP 723 dependencies

    @property
    def paths(self) -> Mapping[str, str]: ...  # module name -> posix path relative to the root

    @property
    def parsed(self) -> int: ...  # files parsed this run (cache misses)

    @property
    def excluded(self) -> Mapping[str, int]: ...  # excluded folder -> number of Python files

    @property
    def noise_files(self) -> Sequence[str]: ...  # tracked Python files found below NOISE_DIRS

    @property
    def unclassified(self) -> Sequence[tuple[str, str]]: ...  # (module name, repository-relative path)

    @property
    def vendor_folders(self) -> Sequence[tuple[str, str]]: ...  # (auto-excluded folder, content fingerprint)


class ModuleScanner(Protocol):
    """Finds the policy's modules and parses each at most once (content-hash cached)."""

    def scan(self, policy: Policy, root: Path, only: frozenset[str] | None = None) -> ScanResult: ...

    def uncached(self) -> ModuleScanner: ...  # same scanner without persistent state (mutation tests)
