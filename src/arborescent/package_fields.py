"""Field and path helpers shared by the package catalog and manifest readers. Pure."""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from .package_model import CatalogError, PackageDescriptor, PackagePath

_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


def is_slug(value: str) -> bool:
    """A stable lowercase package id (``core``, ``web-ui``)."""
    return _ID.fullmatch(value) is not None


def required(value: Mapping[str, Any], key: str) -> str:
    item: object = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise CatalogError(f"package entry requires a non-empty {key}")
    return item.strip()


def optional(value: Mapping[str, Any], key: str) -> str | None:
    item: object = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise CatalogError(f"{key} must be a non-empty string when set")
    return item.strip()


def strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CatalogError(f"{name} must be an array of non-empty strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CatalogError(f"{name} must be an array of non-empty strings")
        result.append(item.strip())
    return tuple(result)


def relative_path(value: str, context: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
    ):
        raise CatalogError(
            f"{context} must be a normalized repository-relative path: {value!r}"
        )
    return path


def within(path: PurePosixPath, parent: PurePosixPath) -> bool:
    return path == parent or parent in path.parents


def current_location(value: str, context: str) -> PurePosixPath:
    """A location inside the extracted tree; ``.`` is the tree root itself."""
    return PurePosixPath() if value == "." else relative_path(value, context)


def glob_pattern(value: str, context: str) -> str:
    """A repository-relative glob; ``..``, absolute paths and backslashes are refused like any other path."""
    relative_path(value.replace("*", "x").replace("?", "x").replace("[", "x"), context)
    return value


def glob_match(pattern: str, path: str) -> bool:
    """Match a posix path: ``*`` and ``?`` stay inside one folder, ``**`` spans folders (a trailing ``**`` needs one name)."""
    return _match(tuple(pattern.split("/")), tuple(path.split("/")))


def _match(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        if not rest:
            return bool(parts)
        return any(_match(rest, parts[skip:]) for skip in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _match(rest, parts[1:])


def extraction_target(package: PackageDescriptor, item: PackagePath) -> PurePosixPath:
    """Where an owned path lands inside an extracted tree (``destination``, else its place below the package root)."""
    if item.destination is not None:
        return relative_path(item.destination, f"package {package.id} destination")
    root = PurePosixPath(package.root)
    source = PurePosixPath(item.path)
    if not within(source, root):
        raise CatalogError(
            f"package {package.id} path {item.path!r} is outside root {package.root!r}; set destination"
        )
    parts = source.parts[len(root.parts) :]
    return PurePosixPath(*parts) if parts else PurePosixPath()
