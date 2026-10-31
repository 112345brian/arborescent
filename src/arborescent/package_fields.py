"""Field and path helpers shared by the package catalog and manifest readers. Pure."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from .package_model import CatalogError

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
