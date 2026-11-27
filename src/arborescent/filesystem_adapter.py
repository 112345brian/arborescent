"""File-system adapter backed by pathlib, shutil and tempfile."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Collection, Generator, Sequence
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

    def resolve(self, path: Path) -> Path:
        return path.resolve(strict=True)

    def glob(self, root: Path, pattern: str) -> list[Path]:
        return sorted(root.glob(pattern))

    def rglob(self, root: Path, pattern: str) -> list[Path]:
        return sorted(root.rglob(pattern))

    def walk_files(self, root: Path, prune: Collection[str], *, enter_checkouts: bool = False, enter_packages: bool = False) -> list[Path]:
        found: list[Path] = []
        for folder, folders, files in os.walk(root):
            here = Path(folder)
            if not enter_checkouts and here != root and (".git" in folders or ".git" in files):  # a nested clone, linked worktree or submodule
                folders.clear()
                continue
            folders[:] = [name for name in folders if name not in prune or (enter_packages and (here / name / "__init__.py").is_file())]
            found.extend(here / name for name in files)
        return sorted(found)

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def make_executable(self, path: Path) -> None:
        path.chmod(path.stat().st_mode | 0o111)

    def remove_file(self, path: Path) -> None:
        path.unlink()

    def remove_tree(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def copy_tree(self, src: Path, dst: Path, ignore: Sequence[str] = (), *, skip_checkouts: bool = False) -> None:
        by_name = shutil.ignore_patterns(*ignore)

        def skipped(folder: str, names: list[str]) -> set[str]:
            nested = {name for name in names if skip_checkouts and (Path(folder) / name / ".git").exists()}
            return nested | set(by_name(folder, names))

        _ = shutil.copytree(src, dst, ignore=skipped)

    @contextmanager
    def temporary_directory(self, prefix: str, parent: Path | None = None) -> Generator[Path]:
        with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as tmp:
            yield Path(tmp)
