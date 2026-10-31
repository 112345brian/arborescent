"""In-memory fakes for the ports, so tests (and your own tests of an Arborescent integration) need no subprocess or disk."""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .domain import ToolResult
from .driver_request import (
    MigrationOutcome,
    MigrationRequest,
    RewriteFinding,
    RewriteRequest,
)

__all__ = ["FakeClock", "FakeFileSystem", "FakeImportRewriter", "FakeMigrationLibrary", "FakeToolRunner"]


@dataclass
class FakeToolRunner:
    """Answers by executable name; records every call. ``installed`` is the set of tools that 'exist'."""

    installed: set[str] = field(default_factory=lambda: set())
    results: dict[str, ToolResult] = field(default_factory=lambda: {})
    calls: list[list[str]] = field(default_factory=lambda: [])

    def available(self, executable: str) -> bool:
        return executable in self.installed

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        self.calls.append(list(argv))
        if argv[0] not in self.installed:
            return ToolResult(None, "", f"{argv[0]}: not installed")
        return self.results.get(argv[0], ToolResult(0, "ok", ""))


@dataclass
class FakeMigrationLibrary:
    """A ``MigrationLibrary`` that records the requests it is supplied with and migrates nothing."""

    name: str = "fake library"
    executable: str = "fake-migrate"
    requirement: str = "fake-library>=1"
    installed: bool = True
    unapplied: tuple[str, ...] = ()
    outcome: MigrationOutcome = field(default_factory=lambda: MigrationOutcome(True, "supplied"))
    supplied: list[MigrationRequest] = field(default_factory=lambda: [])
    log: list[str] = field(default_factory=lambda: [])  # share one list with a FakeImportRewriter to see the order

    def available(self) -> bool:
        return self.installed

    def unsupported(self, request: MigrationRequest) -> tuple[str, ...]:
        return self.unapplied

    def arguments(self, request: MigrationRequest) -> tuple[str, ...]:
        return (self.executable, request.package, str(request.target))

    def supply(self, request: MigrationRequest) -> MigrationOutcome:
        self.supplied.append(request)
        self.log.append("supply")
        return self.outcome


@dataclass
class FakeImportRewriter:
    """An ``ImportRewriter`` that records the requests it is applied to and rewrites nothing."""

    name: str = "fake rewriter"
    executable: str = "fake-rewrite"
    requirement: str = "fake-rewriter>=1"
    installed: bool = True
    findings: tuple[RewriteFinding, ...] = ()
    outcome: MigrationOutcome = field(default_factory=lambda: MigrationOutcome(True, "rewritten"))
    applied: list[RewriteRequest] = field(default_factory=lambda: [])
    log: list[str] = field(default_factory=lambda: [])

    def available(self) -> bool:
        return self.installed

    def unsupported(self, request: RewriteRequest) -> tuple[RewriteFinding, ...]:
        return self.findings

    def commands(self, request: RewriteRequest) -> tuple[tuple[str, ...], ...]:
        return tuple((self.executable, rename.source, rename.target) for rename in request.renames)

    def apply(self, request: RewriteRequest) -> MigrationOutcome:
        self.applied.append(request)
        self.log.append("rewrite")
        return self.outcome


@dataclass
class FakeClock:
    """Every reading advances the time by ``step`` seconds, so durations are exact and repeatable."""

    step: float = 0.5
    t: float = 0.0

    def now(self) -> float:
        self.t += self.step
        return self.t


def _segments_match(pattern: Sequence[str], parts: Sequence[str]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_segments_match(pattern[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and _segments_match(pattern[1:], parts[1:])


@dataclass
class FakeFileSystem:
    """A ``FileSystem`` held in a dict. Same contract as ``LocalFileSystem``: bytes are stored as written (no newline
    translation), a missing file raises ``FileNotFoundError``, ``write_*`` needs the parent folder, listings are sorted.
    Paths are compared by their posix form, so ``Path("/r/a.txt")`` and ``Path("/r") / "a.txt"`` are the same file."""

    files: dict[str, bytes] = field(default_factory=lambda: {})
    dirs: set[str] = field(default_factory=lambda: {"/"})
    _temp_count: int = 0

    @staticmethod
    def _key(path: Path) -> str:
        key = path.as_posix().rstrip("/")
        return key or "/"

    @staticmethod
    def _parent(key: str) -> str:
        return key.rsplit("/", 1)[0] or "/"

    def _all_dirs(self) -> set[str]:
        out = set(self.dirs)
        for start in [*self.files, *self.dirs]:
            key = start
            while key not in ("", "/") and self._parent(key) != key:
                key = self._parent(key)
                out.add(key)
        return out

    def _under(self, root: str) -> list[str]:
        prefix = root.rstrip("/") + "/"
        return sorted((k for k in {*self.files, *self._all_dirs()} if k.startswith(prefix)), key=lambda k: k.split("/"))

    def add(self, path: Path, text: str | bytes) -> None:
        """Test setup: create a file and its parent folders in one go."""
        key = self._key(path)
        self.mkdir(Path(self._parent(key)))
        self.files[key] = text if isinstance(text, bytes) else text.encode("utf-8")

    def read_bytes(self, path: Path) -> bytes:
        key = self._key(path)
        if key not in self.files:
            raise IsADirectoryError(key) if key in self._all_dirs() else FileNotFoundError(key)
        return self.files[key]

    def read_text(self, path: Path) -> str:
        return self.read_bytes(path).decode("utf-8")

    def write_bytes(self, path: Path, data: bytes) -> None:
        key = self._key(path)
        if key in self._all_dirs():
            raise IsADirectoryError(key)
        if self._parent(key) not in self._all_dirs():
            raise FileNotFoundError(key)
        self.files[key] = data

    def write_text(self, path: Path, text: str) -> None:
        self.write_bytes(path, text.encode("utf-8"))

    def read_json(self, path: Path) -> object:
        return json.loads(self.read_text(path))

    def write_json(self, path: Path, data: object) -> None:
        self.write_text(path, json.dumps(data, indent=2) + "\n")

    def exists(self, path: Path) -> bool:
        key = self._key(path)
        return key in self.files or key in self._all_dirs()

    def is_file(self, path: Path) -> bool:
        return self._key(path) in self.files

    def is_dir(self, path: Path) -> bool:
        return self._key(path) in self._all_dirs()

    def resolve(self, path: Path) -> Path:
        return Path(self._key(path))

    def glob(self, root: Path, pattern: str) -> list[Path]:
        base = self._key(root).rstrip("/")
        want = pattern.split("/")
        return [Path(k) for k in self._under(base) if _segments_match(want, k[len(base) + 1 :].split("/"))]

    def rglob(self, root: Path, pattern: str) -> list[Path]:
        return self.glob(root, "**/" + pattern)

    def mkdir(self, path: Path) -> None:
        key = self._key(path)
        if key in self.files:
            raise FileExistsError(key)
        self.dirs.add(key)

    def remove_file(self, path: Path) -> None:
        key = self._key(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        del self.files[key]

    def remove_tree(self, path: Path) -> None:
        key = self._key(path)
        for k in self._under(key):
            _ = self.files.pop(k, None)
            self.dirs.discard(k)
        self.dirs.discard(key)

    def copy_tree(self, src: Path, dst: Path, ignore: Sequence[str] = ()) -> None:
        s, d = self._key(src), self._key(dst)
        if s not in self._all_dirs():
            raise FileNotFoundError(s)
        if d in self._all_dirs() or d in self.files:
            raise FileExistsError(d)
        self.dirs.add(d)
        skipped: list[str] = []
        for k in self._under(s):
            name = k.rsplit("/", 1)[-1]
            if any(fnmatch.fnmatchcase(name, p) for p in ignore) or any(k.startswith(x + "/") for x in skipped):
                skipped.append(k)
                continue
            target = d.rstrip("/") + k[len(s.rstrip("/")) :]
            if k in self.files:
                self.files[target] = self.files[k]
            else:
                self.dirs.add(target)

    @contextmanager
    def temporary_directory(self, prefix: str, parent: Path | None = None) -> Generator[Path]:
        self._temp_count += 1
        tmp = (parent or Path("/arborescent-test-temp")) / f"{prefix}{self._temp_count}"
        self.mkdir(tmp)
        try:
            yield tmp
        finally:
            self.remove_tree(tmp)
