"""File-system adapter backed by pathlib, shutil and tempfile."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path


class LocalFileSystem:
    """``FileSystem`` over local files. Bytes in and out: no newline translation."""

    def read_text(self, path: Path) -> str:
        return path.read_bytes().decode("utf-8")

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def write_text(self, path: Path, text: str) -> None:
        _ = path.write_bytes(text.encode("utf-8"))

    def write_bytes(self, path: Path, data: bytes) -> None:
        _ = path.write_bytes(data)

    def read_json(self, path: Path) -> object:
        return json.loads(self.read_text(path))

    def write_json(self, path: Path, data: object) -> None:
        self.write_text(path, json.dumps(data, indent=2) + "\n")

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def glob(self, root: Path, pattern: str) -> list[Path]:
        return sorted(root.glob(pattern))

    def rglob(self, root: Path, pattern: str) -> list[Path]:
        return sorted(root.rglob(pattern))

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def remove_file(self, path: Path) -> None:
        path.unlink()

    def remove_tree(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def copy_tree(self, src: Path, dst: Path, ignore: Sequence[str] = ()) -> None:
        _ = shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*ignore))

    @contextmanager
    def temporary_directory(self, prefix: str, parent: Path | None = None) -> Generator[Path]:
        with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as tmp:
            yield Path(tmp)
