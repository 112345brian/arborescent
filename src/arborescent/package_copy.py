"""Copying owned paths into an extraction destination: only regular files, never symlinks or generated output."""

from __future__ import annotations

from pathlib import Path

from .package_model import CatalogError
from .package_tree import is_generated
from .ports import FileSystem


def claim_destination(fs: FileSystem, destination: Path) -> None:
    """Create the destination; refuse one that already holds anything, so cleanup never deletes foreign files."""
    fs.mkdir(destination)
    if fs.rglob(destination, "*"):  # it appeared after the exists() check
        raise CatalogError(f"extraction destination is not empty: {destination}")


def _check_copyable(fs: FileSystem, root: Path, nested: Path) -> None:
    """Only regular files and directories inside the repository are copied."""
    try:
        resolved: Path | None = fs.resolve(nested)
    except (
        OSError
    ):  # e.g. a dangling link: Windows raises instead of returning the path
        resolved = None
    if resolved is None or resolved != nested or not resolved.is_relative_to(root):
        raise CatalogError(f"package extraction does not copy symlinks: {nested}")
    if not fs.is_dir(nested) and not fs.is_file(nested):
        raise CatalogError(
            f"package extraction does not copy special files: {nested}"
        )


def copy_owned_path(fs: FileSystem, root: Path, source: Path, target: Path) -> list[tuple[Path, Path]]:
    """Copy one owned path; return the (source file, copied file) pairs."""
    pairs: list[tuple[Path, Path]] = []
    resolved = fs.resolve(source)
    if resolved != source or not resolved.is_relative_to(root):
        raise CatalogError(
            f"package extraction does not copy symlinks or escaped paths: {source}"
        )
    if fs.is_dir(source):
        nested_paths = [
            nested
            for nested in fs.rglob(source, "*")
            if not is_generated(fs, source, nested)
        ]
        for nested in nested_paths:
            _check_copyable(fs, root, nested)
        fs.mkdir(target)
        for nested in nested_paths:
            nested_target = target / nested.relative_to(source)
            if fs.is_dir(nested):
                fs.mkdir(nested_target)
            else:
                if fs.exists(nested_target):
                    raise CatalogError(
                        f"multiple package paths map to extraction path {nested_target}"
                    )
                fs.mkdir(nested_target.parent)
                fs.write_bytes(nested_target, fs.read_bytes(nested))
                pairs.append((nested, nested_target))
    else:
        if fs.exists(target):
            raise CatalogError(
                f"multiple package paths map to extraction path {target}"
            )
        fs.mkdir(target.parent)
        fs.write_bytes(target, fs.read_bytes(source))
        pairs.append((source, target))
    return pairs
