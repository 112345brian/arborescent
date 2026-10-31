"""Read and validate an extracted tree's ``.arborescent-package.toml``, and write it after a migration library has extracted."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .package_fields import (
    current_location,
    extraction_target,
    is_slug,
    relative_path,
    required,
    strings,
)
from .package_model import (
    MANIFEST_NAME,
    MANIFEST_VERSION,
    CatalogDocument,
    CatalogError,
    ExtractedDependency,
    ExtractedPackage,
    ExtractedPath,
    PackageSemantics,
    Placement,
)
from .package_parse import (
    manifest_array,
    manifest_path,
    manifest_semantics,
    manifest_table,
)
from .package_render import render_manifest
from .package_tree import check_tree, resolves_to_itself, shipped_policy
from .ports import FileSystem


def write_extracted_manifest(
    fs: FileSystem, catalog: CatalogDocument, package_id: str, directory: Path, repository: str | None = None
) -> ExtractedPackage:
    """Record what was extracted into ``directory``, then read it back so a manifest that does not validate is an error.

    The files themselves were put there by a migration library; this is only Arborescent's own record of them (what the
    package is, where it came from, where each owned path now lives). ``repository`` names where the tree lives now: the
    flag, else the package's home repository, else the repository it came from.
    """
    package = next((item for item in catalog.packages if item.id == package_id), None)
    if package is None:
        raise CatalogError(f"unknown package {package_id!r}")
    home = package.home
    if repository is not None:
        if not repository.strip():
            raise CatalogError("repository must be a non-empty string when set")
        current = repository.strip()
    elif home is not None and home.repository is not None:
        current = home.repository
    else:
        current = package.repository
    placements = [
        Placement(package.id, item, relative.as_posix() if relative.parts else ".")
        for item in package.paths
        for relative in [extraction_target(package, item)]
    ]
    fs.mkdir(directory)
    fs.write_text(directory / MANIFEST_NAME, render_manifest(package, (package,), placements, current))
    return load_extracted_package(fs, directory)


def load_extracted_package(fs: FileSystem, directory: Path) -> ExtractedPackage:
    """Read and validate an extracted tree: current paths resolve inside it and nested policies keep their layout."""
    try:
        return _load_extracted_package(fs, directory)
    except OSError as exc:
        raise CatalogError(f"cannot read extracted package {directory}: {exc}") from exc


def _load_extracted_package(fs: FileSystem, directory: Path) -> ExtractedPackage:
    base = fs.resolve(directory)
    manifest_file = base / MANIFEST_NAME
    try:
        data = tomllib.loads(fs.read_text(manifest_file))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(f"cannot read {manifest_file}: {exc}") from exc
    if data.get("manifest_version") != MANIFEST_VERSION:
        raise CatalogError(
            f"{MANIFEST_NAME} must declare manifest_version = {MANIFEST_VERSION}"
        )
    package = manifest_semantics(manifest_table(data, "package"))
    origin = manifest_table(data, "origin")
    location = manifest_table(data, "location")
    extraction = manifest_table(data, "extraction")
    dependencies = tuple(
        ExtractedDependency(
            manifest_semantics(item),
            required(item, "origin_repository"),
            required(item, "origin_root"),
            required(item, "current_root"),
        )
        for item in manifest_array(data, "dependencies")
    )
    included = strings(extraction.get("included_packages"), "included_packages")
    known = _check_identities(package, dependencies, included, extraction)
    roots = {
        package.id: current_location(required(location, "root"), "location root")
    }
    for dependency in dependencies:
        roots[dependency.semantics.id] = current_location(
            dependency.current_root, f"package {dependency.semantics.id} current root"
        )
    paths = tuple(
        manifest_path(item, known) for item in manifest_array(data, "paths")
    )
    _check_layout(fs, base, paths, roots)
    result = ExtractedPackage(
        package,
        required(origin, "repository"),
        required(origin, "root"),
        required(origin, "extraction_status"),
        required(location, "repository"),
        required(location, "root"),
        required(extraction, "status"),
        included,
        dependencies,
        paths,
    )
    for item in paths:
        _check_nested_policy(fs, base, roots[item.package_id], item)
    return result


def _check_identities(
    package: PackageSemantics,
    dependencies: Sequence[ExtractedDependency],
    included: Sequence[str],
    extraction: Mapping[str, Any],
) -> set[str]:
    known = {package.id, *(item.semantics.id for item in dependencies)}
    if set(included) != known or len(known) != len(dependencies) + 1:
        raise CatalogError(
            "included_packages does not match the package and its dependencies"
        )
    for identity in known:
        if not is_slug(identity):
            raise CatalogError(
                f"package id {identity!r} is not a stable lowercase slug"
            )
    if required(extraction, "status") != "extracted":
        raise CatalogError("extraction status must be 'extracted'")
    return known


def _check_layout(
    fs: FileSystem,
    base: Path,
    paths: Sequence[ExtractedPath],
    roots: Mapping[str, PurePosixPath],
) -> None:
    if not paths:
        raise CatalogError(f"{MANIFEST_NAME} declares no paths")
    for item in paths:
        _check_current_path(fs, base, item)
        current = current_location(item.current_path, "current path")
        if not current.is_relative_to(roots[item.package_id]):
            raise CatalogError(
                f"current path {item.current_path!r} lies outside package {item.package_id}'s current root"
            )
    check_tree(fs, base)


def _check_current_path(fs: FileSystem, base: Path, item: ExtractedPath) -> None:
    relative = current_location(
        item.current_path, f"package {item.package_id} current path"
    )
    target = base.joinpath(*relative.parts)
    if not fs.exists(target) or not resolves_to_itself(fs, target):
        raise CatalogError(
            f"current path {item.current_path!r} of package {item.package_id} does not resolve inside the extracted tree"
        )


def _check_nested_policy(
    fs: FileSystem, base: Path, root: PurePosixPath, item: ExtractedPath
) -> None:
    """A declared ``arborescent_config`` must be shipped and be a policy file (its content is checked with the tree)."""
    declared = item.metadata.get("arborescent_config")
    if declared is None:
        return
    relative = root / relative_path(
        declared, f"package {item.package_id} arborescent_config"
    )
    config = base.joinpath(*relative.parts)
    if not fs.is_file(config):
        raise CatalogError(
            f"arborescent_config {declared!r} of package {item.package_id} is missing from the extraction; declare it as a package path"
        )
    if shipped_policy(fs, base, config) is None:
        raise CatalogError(
            f"arborescent_config {declared!r} of package {item.package_id} is not an Arborescent policy"
        )
