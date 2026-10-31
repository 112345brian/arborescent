"""Parse ``packages.toml`` entries and ``.arborescent-package.toml`` tables into the package vocabulary. Pure."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .package_fields import glob_pattern, optional, relative_path, required, strings
from .package_model import (
    MANIFEST_NAME,
    AssetRule,
    CatalogDocument,
    CatalogError,
    ExtractedPath,
    PackageDescriptor,
    PackagePath,
    PackageSemantics,
)


def parse_catalog(data: Mapping[str, Any]) -> CatalogDocument:
    """Parse a decoded ``packages.toml``: the packages and the ordered ``[[assets]]`` rules. No file-system checks."""
    raw_packages = data.get("packages")
    if not isinstance(raw_packages, list) or not raw_packages:
        raise CatalogError("catalog must contain one or more [[packages]] entries")
    packages = tuple(parse_package(item, index) for index, item in enumerate(raw_packages))
    known = {package.id for package in packages}
    if len(known) != len(packages):
        raise CatalogError("package ids must be unique")
    raw_assets: object = data.get("assets", [])
    if not isinstance(raw_assets, list):
        raise CatalogError("assets must be an array of tables")
    rules: list[AssetRule] = []
    for index, item in enumerate(raw_assets):
        if not isinstance(item, dict):
            raise CatalogError(f"assets entry {index} must be a table")
        rule = AssetRule(glob_pattern(required(item, "pattern"), f"assets entry {index} pattern"), required(item, "package"))
        if rule.package not in known:
            raise CatalogError(f"assets pattern {rule.pattern!r} names unknown package {rule.package}")
        rules.append(rule)
    return CatalogDocument(packages, tuple(rules))


def parse_package(value: object, index: int) -> PackageDescriptor:
    if not isinstance(value, dict):
        raise CatalogError(f"packages entry {index} must be a table")
    raw_paths = value.get("paths", [])
    if not isinstance(raw_paths, list):
        raise CatalogError(
            f"package {value.get('id', index)} paths must be an array of tables"
        )
    paths: list[PackagePath] = []
    for item in raw_paths:
        if not isinstance(item, dict):
            raise CatalogError(
                f"package {value.get('id', index)} path entries must be tables"
            )
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict) or not all(
            isinstance(key, str) and isinstance(val, str)
            for key, val in metadata.items()
        ):
            raise CatalogError(
                f"package {value.get('id', index)} path metadata must map strings to strings"
            )
        paths.append(
            PackagePath(
                required(item, "path"),
                required(item, "role"),
                optional(item, "destination"),
                metadata,
            )
        )
    return PackageDescriptor(
        id=required(value, "id"),
        name=required(value, "name"),
        kind=required(value, "kind"),
        repository=required(value, "repository"),
        root=required(value, "root"),
        summary=required(value, "summary"),
        domain=optional(value, "domain"),
        capabilities=strings(value.get("capabilities", []), "capabilities"),
        depends_on=strings(value.get("depends_on", []), "depends_on"),
        public=strings(value.get("public", []), "public"),
        extraction_status=str(value.get("extraction_status", "in-repository")),
        paths=tuple(paths),
    )


def manifest_table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value: object = data.get(key)
    if not isinstance(value, dict):
        raise CatalogError(f"{MANIFEST_NAME} requires a [{key}] table")
    return value


def manifest_array(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value: object = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CatalogError(f"{MANIFEST_NAME} {key} must be an array of tables")
    return value


def manifest_semantics(value: Mapping[str, Any]) -> PackageSemantics:
    return PackageSemantics(
        required(value, "id"),
        required(value, "name"),
        required(value, "kind"),
        required(value, "summary"),
        optional(value, "domain"),
        strings(value.get("capabilities", []), "capabilities"),
        strings(value.get("depends_on", []), "depends_on"),
        strings(value.get("public", []), "public"),
    )


def manifest_path(value: Mapping[str, Any], known: set[str]) -> ExtractedPath:
    package_id = required(value, "package_id")
    if package_id not in known:
        raise CatalogError(
            f"path refers to package {package_id!r} that is not included"
        )
    metadata: object = value.get("metadata", {})
    if not isinstance(metadata, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in metadata.items()
    ):
        raise CatalogError("path metadata must map strings to strings")
    return ExtractedPath(
        package_id,
        required(value, "role"),
        dict(metadata),
        relative_path(required(value, "origin_path"), "origin path").as_posix(),
        required(value, "current_path"),
    )
