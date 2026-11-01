"""Symlink safety, generated-output detection and shipped-policy checks for package trees."""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

from .config import load_policy
from .domain import NOISE_DIRS, Policy, PolicyError
from .package_model import CatalogError
from .ports import FileSystem

# Directories and files below a declared package path that are local build output, not distributable source.
GENERATED_DIRS = NOISE_DIRS | {"build", "dist", ".pytest_cache"}
GENERATED_FILES = ("*.pyc", ".DS_Store")


def resolves_to_itself(fs: FileSystem, path: Path) -> bool:
    """False for a symlink, including a dangling one (Windows raises instead of returning the path)."""
    try:
        return fs.resolve(path) == path
    except OSError:
        return False


def is_generated_dir(fs: FileSystem, directory: Path) -> bool:
    """A directory with a generated-output name is output unless it is a real Python package (has ``__init__.py``)."""
    return directory.name in GENERATED_DIRS and not fs.is_file(
        directory / "__init__.py"
    )


def is_generated(fs: FileSystem, root: Path, path: Path) -> bool:
    """Whether ``path`` is generated output below ``root`` (the root itself is always copied).

    Limitation: a namespace package (no ``__init__.py``) named like generated output, e.g. ``build``, is skipped.
    """
    parts = path.relative_to(root).parts
    if any(
        part in GENERATED_DIRS
        and is_generated_dir(fs, root.joinpath(*parts[: index + 1]))
        for index, part in enumerate(parts[:-1])
    ):
        return True
    name = parts[-1]
    if name == ".git" or any(
        fnmatch.fnmatchcase(name, pattern) for pattern in GENERATED_FILES
    ):
        return True  # `.git` may be a worktree or submodule pointer file as well as a directory
    return fs.is_dir(path) and is_generated_dir(fs, path)


def shipped_policy(fs: FileSystem, base: Path, path: Path) -> Policy | None:
    """The policy ``path`` defines (``arborescent.toml`` or ``pyproject.toml`` with ``[tool.arborescent]``), else ``None``."""
    if path.name == "pyproject.toml":
        try:
            tool = tomllib.loads(fs.read_text(path)).get("tool")
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            return None  # not ours to validate
        if not isinstance(tool, dict) or "arborescent" not in tool:
            return None
    elif path.name != "arborescent.toml":
        return None
    if not fs.is_file(path):
        return None
    try:
        return load_policy(fs, path)[0]
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, PolicyError) as exc:
        raise CatalogError(
            f"shipped policy {path.relative_to(base).as_posix()} is invalid: {exc}"
        ) from exc


def check_policy_sources(
    fs: FileSystem, base: Path, config: Path, policy: Policy
) -> None:
    for local in (policy, *(scope.policy for scope in policy.scopes)):
        for entry in (*local.sources, *local.source_roots):
            if not fs.exists(config.parent / entry):
                raise CatalogError(
                    f"shipped policy {config.relative_to(base).as_posix()} expects {entry!r}, which is absent after "
                    "extraction; a changed layout would change module identity"
                )


def check_tree(fs: FileSystem, base: Path) -> None:
    """No symlink may leave the tree (generated directories excepted); every shipped policy loads and finds its sources."""
    for path in fs.rglob(base, "*"):
        if is_generated(fs, base, path):
            continue
        if not resolves_to_itself(fs, path):
            raise CatalogError(f"extracted tree contains a symlink: {path}")
        policy = shipped_policy(fs, base, path)
        if policy is not None:
            check_policy_sources(fs, base, path, policy)


