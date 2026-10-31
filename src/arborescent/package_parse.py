"""Parse ``packages.toml`` entries and ``.arborescent-package.toml`` tables into the package vocabulary. Pure."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from .package_fields import (
    glob_pattern,
    optional,
    relative_path,
    required,
    strings,
    within,
)
from .package_model import (
    GIT_URL_PATTERN,
    HOME_KINDS,
    MANIFEST_NAME,
    AssetRule,
    CatalogDocument,
    CatalogError,
    ExtractedPath,
    ModuleRename,
    PackageDescriptor,
    PackageHome,
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
    _check_workspace_members(packages)
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
        extraction_status=str(value.get("extraction_status", "in-repository")),
        paths=tuple(paths),
        home=_home(value),
        public=strings(value.get("public", []), "public"),
        externals=_optional_strings(value, "externals"),
        module_renames=_module_renames(value),
    )


def _optional_strings(value: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    """``None`` when the key is absent (not annotated yet), the list, possibly empty, when it is set."""
    return None if key not in value else strings(value[key], key)


def _home(value: Mapping[str, Any]) -> PackageHome | None:
    raw: object = value.get("home")
    name = f"package {value.get('id', '?')} home"
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CatalogError(f"{name} must be a table")
    kind = required(raw, "kind")
    if kind not in HOME_KINDS:
        raise CatalogError(f"{name} kind must be one of {', '.join(HOME_KINDS)}: {kind!r}")
    repository, path = optional(raw, "repository"), optional(raw, "path")
    if kind == "repository":
        if repository is None:
            raise CatalogError(f"{name} of kind repository requires a repository")
        if re.fullmatch(GIT_URL_PATTERN, repository) is None:
            raise CatalogError(
                f"{name} repository must be a git URL (https://, ssh:// or git@host:path), not {repository!r}"
            )
        if path is not None:
            raise CatalogError(f"{name} of kind repository takes no path")
        return PackageHome(kind, repository=repository)
    if path is None:
        raise CatalogError(f"{name} of kind workspace-member requires a path")
    if repository is not None:
        raise CatalogError(f"{name} of kind workspace-member takes no repository")
    return PackageHome(kind, path=relative_path(path, f"{name} path").as_posix())


def _module_renames(value: Mapping[str, Any]) -> tuple[ModuleRename, ...]:
    raw: object = value.get("module_renames", [])
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise CatalogError(f"package {value.get('id', '?')} module_renames must be an array of tables")
    return tuple(ModuleRename(required(item, "from"), required(item, "to")) for item in raw)


def _check_workspace_members(packages: tuple[PackageDescriptor, ...]) -> None:
    """Two workspace members cannot share a folder or sit inside each other: each is its own distribution root."""
    members = [(PurePosixPath(p.home.path), p.id) for p in packages if p.home is not None and p.home.path is not None]
    for index, (path, owner) in enumerate(members):
        for other, other_owner in members[index + 1 :]:
            if within(path, other) or within(other, path):
                raise CatalogError(f"workspace members {owner!r} and {other_owner!r} share or nest folders ({path}, {other})")


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
