"""What Arborescent needs from the world. Adapters implement these; tests substitute fakes (``arborescent.testing``)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, runtime_checkable

from .domain import (
    Allowance,
    ArchitectureContracts,
    Contract,
    Mutation,
    Policy,
    ToolResult,
    Violation,
)
from .facts import ModuleInfo

__all__ = [
    "Clock", "ContractRenderer", "Engine", "FileSystem", "ModuleScanner", "PackageBoundaries", "PackageChecks", "PackageProbes", "RootAwareEngine",
    "ScanResult", "ToolRunner", "TypeChecker", "ViolationEngine",
]


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

    def resolve(self, path: Path) -> Path: ...  # strict canonical path, following symlinks

    def glob(self, root: Path, pattern: str) -> list[Path]: ...  # sorted; ``**`` spans folders

    def rglob(self, root: Path, pattern: str) -> list[Path]: ...  # sorted; files and folders at any depth

    def mkdir(self, path: Path) -> None: ...  # with parents; existing is fine

    def make_executable(self, path: Path) -> None: ...  # FileNotFoundError if missing; a no-op where files have no execute bit

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


class RootAwareEngine(Protocol):
    """Optional extension for an engine whose command depends on the project it runs in.

    import-linter does not discover the file ``generate`` writes for it; this lets the adapter add ``--config`` only when
    that file is Arborescent's and the project has no import-linter configuration of its own. Separate from ``Engine`` so
    existing and user-supplied adapters are unaffected.
    """

    def command_in(self, files: Sequence[str], root: Path, fs: FileSystem) -> list[str]: ...


@runtime_checkable
class ContractRenderer(Protocol):
    """An engine that is configured from the neutral contracts: it writes its own file format and nothing else does."""

    @property
    def config_file(self) -> str: ...  # relative to the generated-files directory

    def render_contracts(self, contracts: ArchitectureContracts) -> str: ...


class PackageBoundaries(Protocol):
    """What the engine configs are compiled from when packages are declared: who may import whom, as contracts."""

    def allows(self, importer: str, imported: str) -> bool: ...

    def contracts(self, module_leaves: Sequence[str], allowance: Callable[[str], Allowance | None]) -> list[Contract]: ...


class PackageProbes(Protocol):
    """The selftest mutations the package rules contribute, built over the catalog that was loaded for this run."""

    def mutations(
        self, policy: Policy, arbor_index: Mapping[str, tuple[str | None, str | None]], scope_by_module: Mapping[str, str]
    ) -> tuple[list[Mutation], list[str]]: ...  # (mutations, why the rest cannot be built)


class PackageChecks(Protocol):
    """Everything the engine asks of the package-annotation layer, and nothing else.

    The engine owns this interface and the annotation layer implements it, so the core never imports the annotation layer:
    a package that depends on the core is not also depended on by it. Bound in ``Services``; with a ``package_catalog`` in
    the policy and nothing bound the engine refuses to run, so a missing binding cannot read as a clean result.
    """

    def gap_violations(
        self, policy: Policy, root: Path, fs: FileSystem, scan: ScanResult, scanner: ModuleScanner
    ) -> list[Violation]: ...

    def boundaries(self, policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> PackageBoundaries | None: ...

    def new_files(self, policy: Policy, root: Path, fs: FileSystem, scan: ScanResult) -> dict[str, str]: ...  # path -> text

    def probes(
        self, policy: Policy, root: Path, fs: FileSystem, scan: ScanResult, scanner: ModuleScanner
    ) -> PackageProbes | None: ...


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
    def arbor_index(self) -> Mapping[str, tuple[str | None, str | None]]: ...  # module -> ring annotation / error

    @property
    def scope_by_module(self) -> Mapping[str, str]: ...  # module -> owning architecture scope

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
    def dev_tool_modules(self) -> frozenset[str]: ...  # modules below a `dev_tools` path

    @property
    def misplaced_dev_tools(self) -> Sequence[tuple[str, str]]: ...  # (module, path) annotated as dev tooling outside `dev_tools`

    @property
    def vendor_folders(self) -> Sequence[tuple[str, str]]: ...  # (auto-excluded folder, content fingerprint)


class ModuleScanner(Protocol):
    """Finds the policy's modules and parses each at most once (content-hash cached)."""

    def scan(self, policy: Policy, root: Path, only: frozenset[str] | None = None) -> ScanResult: ...

    def uncached(self) -> ModuleScanner: ...  # same scanner without persistent state (mutation tests)
