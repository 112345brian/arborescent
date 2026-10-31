"""Structural validation of a package catalog: ids, owned paths, symlinks, extraction destinations, dependencies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from .package_fields import extraction_target, is_slug, relative_path, within
from .package_model import MANIFEST_NAME, CatalogError, PackageDescriptor, PackagePath
from .ports import FileSystem


def validate_catalog(fs: FileSystem, root: Path, packages: Sequence[PackageDescriptor]) -> None:
    by_id = {package.id: package for package in packages}
    if len(by_id) != len(packages):
        raise CatalogError("package ids must be unique")
    owners: list[tuple[PurePosixPath, str]] = []
    for package in packages:
        _validate_package(fs, root, package, by_id, owners)
    _validate_dependencies(by_id, packages)


def _validate_package(
    fs: FileSystem,
    root: Path,
    package: PackageDescriptor,
    by_id: Mapping[str, PackageDescriptor],
    owners: list[tuple[PurePosixPath, str]],
) -> None:
    _validate_package_header(package)
    _validate_package_paths(fs, root, package, owners)
    for dependency in package.depends_on:
        if dependency not in by_id:
            raise CatalogError(
                f"package {package.id} depends on unknown package {dependency}"
            )


def _validate_package_header(package: PackageDescriptor) -> None:
    if not is_slug(package.id):
        raise CatalogError(
            f"package id must be a stable lowercase slug: {package.id!r}"
        )
    relative_path(package.root, f"package {package.id} root")
    if not package.paths:
        raise CatalogError(
            f"package {package.id} must declare at least one owned path"
        )
    if not any(item.role == "source" for item in package.paths):
        raise CatalogError(f"package {package.id} must declare a source path")
    package_root = PurePosixPath(package.root)
    if not any(
        within(PurePosixPath(item.path), package_root) for item in package.paths
    ):
        raise CatalogError(
            f"package {package.id} root must be covered by an owned path"
        )


def _validate_package_paths(
    fs: FileSystem, root: Path, package: PackageDescriptor, owners: list[tuple[PurePosixPath, str]]
) -> None:
    targets: list[PurePosixPath] = []
    for item in package.paths:
        _validate_owned_path(fs, root, package, item, owners)
        target = extraction_target(package, item)
        if target == PurePosixPath(MANIFEST_NAME):
            raise CatalogError(
                "package paths cannot overwrite the extraction metadata manifest"
            )
        if target.parts and any(
            within(target, prior) or within(prior, target) for prior in targets
        ):
            raise CatalogError(
                f"package {package.id} has overlapping extraction destinations at {target}"
            )
        if target.parts:
            targets.append(target)


def _validate_owned_path(
    fs: FileSystem,
    root: Path,
    package: PackageDescriptor,
    item: PackagePath,
    owners: list[tuple[PurePosixPath, str]],
) -> None:
    source_path = relative_path(item.path, f"package {package.id} path")
    if item.destination is not None:
        relative_path(item.destination, f"package {package.id} destination")
    if not item.role.strip():
        raise CatalogError(f"package {package.id} path role cannot be empty")
    source = root.joinpath(*source_path.parts)
    if not fs.exists(source):
        raise CatalogError(f"package {package.id} path does not exist: {item.path}")
    resolved = fs.resolve(source)
    if resolved != source:
        raise CatalogError(
            f"package {package.id} path cannot be a symlink: {item.path}"
        )
    if not resolved.is_relative_to(root):
        raise CatalogError(f"path escapes repository root: {item.path}")
    for prior, owner in owners:
        if within(source_path, prior) or within(prior, source_path):
            text = (
                "owned by more than one package"
                if owner != package.id
                else "overlaps another path"
            )
            raise CatalogError(f"path {item.path!r} {text} ({owner}, {package.id})")
    owners.append((source_path, package.id))
    if item.destination is not None:
        extraction_target(package, item)


def _validate_dependencies(
    by_id: Mapping[str, PackageDescriptor], packages: Sequence[PackageDescriptor]
) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(package_id: str) -> None:
        if package_id in visiting:
            raise CatalogError(f"package dependency cycle includes {package_id}")
        if package_id in visited:
            return
        visiting.add(package_id)
        for dependency in by_id[package_id].depends_on:
            visit(dependency)
        visiting.remove(package_id)
        visited.add(package_id)

    for package in packages:
        visit(package.id)
