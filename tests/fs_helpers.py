"""One file-system fixture, two implementations: every test that takes ``fs_root`` runs on the real disk and in memory."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from archlint.adapters import LocalFileSystem
from archlint.ports import FileSystem
from archlint.testing import FakeFileSystem

KINDS = ("local", "fake")


def make_fs(kind: str, tmp_path: Path) -> tuple[FileSystem, Path]:
    return (LocalFileSystem(), tmp_path) if kind == "local" else (FakeFileSystem(), Path("/work"))


def put(fs: FileSystem, root: Path, files: Mapping[str, str | bytes]) -> None:
    """Create files (and their folders) through the port itself."""
    for rel, content in files.items():
        path = root / rel
        fs.mkdir(path.parent)
        fs.write_bytes(path, content if isinstance(content, bytes) else content.encode("utf-8"))
