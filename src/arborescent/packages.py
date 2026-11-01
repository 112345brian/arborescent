"""The package catalog use cases: load and validate ``packages.toml``. Extraction is not here: a migration library extracts."""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from pathlib import Path

from .package_manifest import load_extracted_package
from .package_model import (
    MANIFEST_NAME,
    AssetRule,
    CatalogDocument,
    CatalogError,
    PackageDescriptor,
    semantics_of,
)
from .package_parse import parse_catalog
from .package_validation import validate_catalog
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
