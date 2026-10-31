"""The package catalog use cases: load and validate ``packages.toml``, and extract a package (path copy; frozen)."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from .package_copy import claim_destination, copy_owned_path
from .package_manifest import load_extracted_package
from .package_model import (
    MANIFEST_NAME,
    AssetRule,
    CatalogDocument,
    CatalogError,
    PackageDescriptor,
    Placement,
    semantics_of,
)
from .package_parse import parse_catalog
from .package_render import render_manifest
from .package_tree import check_module_identity
from .package_validation import extraction_target, validate_catalog
from .ports import FileSystem

# The deliberate public surface of this module; the vocabulary and the readers live in the ``package_*`` modules.
__all__ = ["MANIFEST_NAME", "CatalogError", "PackageCatalog", "load_extracted_package", "read_catalog", "semantics_of"]


def read_catalog(fs: FileSystem, root: Path, catalog_path: str = "packages.toml") -> CatalogDocument:
    """Read and parse ``packages.toml`` without checking it against the file system (``PackageCatalog.load`` does)."""
    path = root / catalog_path
    try:
        data = tomllib.loads(fs.read_text(path))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(f"cannot read package catalog {path}: {exc}") from exc
    return parse_catalog(data)


class PackageCatalog:
    """Validate, inspect and extract repository package descriptors."""

    def __init__(
        self,
        fs: FileSystem,
        repository_root: Path,
        packages: Sequence[PackageDescriptor],
        assets: Sequence[AssetRule] = (),
    ) -> None:
        self.fs = fs
        self.assets = tuple(assets)
        self.root = fs.resolve(repository_root)
        self.packages = tuple(packages)
        self._by_id = {package.id: package for package in self.packages}

    @classmethod
    def load(
        cls,
        fs: FileSystem,
        repository_root: Path,
        catalog_path: str = "packages.toml",
    ) -> PackageCatalog:
        root = fs.resolve(repository_root)
        document = read_catalog(fs, root, catalog_path)
        catalog = cls(fs, root, document.packages, document.assets)
        catalog.validate()
        return catalog

    def get(self, package_id: str) -> PackageDescriptor:
        try:
            return self._by_id[package_id]
        except KeyError as exc:
            raise CatalogError(f"unknown package id: {package_id}") from exc

    def validate(self) -> None:
        validate_catalog(self.fs, self.root, self.packages)

    def extract(
        self,
        package_id: str,
        destination: Path,
        *,
        include_dependencies: bool = False,
        repository: str | None = None,
    ) -> Path:
        """Copy owned paths to an absent destination; semantics stay stable, locations are recorded explicitly.

        ``repository`` names the repository the extracted tree will live in; it defaults to the origin repository.
        """
        package = self.get(package_id)
        current_repository = _current_repository(repository, package)
        included = (
            self._dependency_closure(package) if include_dependencies else (package,)
        )
        self.fs.mkdir(destination.parent)
        destination = self.fs.resolve(destination.parent) / destination.name
        if self.fs.exists(destination):
            raise CatalogError(f"extraction destination already exists: {destination}")
        for descriptor in included:
            for item in descriptor.paths:
                source = self.fs.resolve(self.root / item.path)
                if destination.is_relative_to(source):
                    raise CatalogError(
                        f"destination overlaps package source: {destination}"
                    )
        claim_destination(self.fs, destination)
        try:
            copied: set[str] = set()
            placements: list[Placement] = []
            pairs: list[tuple[Path, Path]] = []
            for descriptor in included:
                for item in descriptor.paths:
                    target_relative = extraction_target(descriptor, item)
                    if descriptor.id != package.id:
                        target_relative = PurePosixPath(
                            "dependencies", descriptor.id
                        ) / (target_relative or PurePosixPath())
                    key = target_relative.as_posix() if target_relative.parts else "."
                    if key in copied:
                        raise CatalogError(
                            f"multiple package paths map to extraction path {key!r}"
                        )
                    copied.add(key)
                    placements.append(Placement(descriptor.id, item, key))
                    source = self.root.joinpath(*PurePosixPath(item.path).parts)
                    target = (
                        destination.joinpath(*target_relative.parts)
                        if target_relative.parts
                        else destination
                    )
                    pairs += copy_owned_path(self.fs, self.root, source, target)
            self.fs.write_text(
                destination / MANIFEST_NAME,
                render_manifest(package, included, placements, current_repository),
            )
            load_extracted_package(self.fs, destination)
            check_module_identity(self.fs, pairs)
        except Exception:
            self.fs.remove_tree(destination)
            raise
        return destination

    def _dependency_closure(
        self, package: PackageDescriptor
    ) -> tuple[PackageDescriptor, ...]:
        ordered: list[PackageDescriptor] = []
        seen: set[str] = set()

        def visit(current: PackageDescriptor) -> None:
            if current.id in seen:
                return
            seen.add(current.id)
            for dependency in current.depends_on:
                visit(self.get(dependency))
            ordered.append(current)

        visit(package)
        return tuple(ordered)


def _current_repository(repository: str | None, package: PackageDescriptor) -> str:
    if repository is None:
        return package.repository
    if not repository.strip():
        raise CatalogError("repository must be a non-empty string when set")
    return repository.strip()
