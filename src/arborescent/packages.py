"""Package metadata and extraction use cases owned by Arborescent."""

from __future__ import annotations

import fnmatch
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .domain import NOISE_DIRS, PolicyError
from .ports import FileSystem

_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
# Directories and files below a declared package path that are local build output, not distributable source.
_GENERATED_DIRS = NOISE_DIRS | {"build", "dist"}
_GENERATED_FILES = ("*.pyc",)


class CatalogError(PolicyError):
    """Package metadata is invalid or unsafe to extract."""


@dataclass(frozen=True, slots=True)
class PackagePath:
    path: str
    role: str
    destination: str | None = None
    metadata: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class PackageDescriptor:
    id: str
    name: str
    kind: str
    repository: str
    root: str
    summary: str
    domain: str | None = None
    capabilities: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    extraction_status: str = "in-repository"
    paths: tuple[PackagePath, ...] = ()


class PackageCatalog:
    """Validate, inspect and extract repository package descriptors."""

    def __init__(
        self,
        fs: FileSystem,
        repository_root: Path,
        packages: Sequence[PackageDescriptor],
    ) -> None:
        self.fs = fs
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
        path = root / catalog_path
        try:
            data = tomllib.loads(fs.read_text(path))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            raise CatalogError(f"cannot read package catalog {path}: {exc}") from exc
        raw_packages = data.get("packages")
        if not isinstance(raw_packages, list) or not raw_packages:
            raise CatalogError("catalog must contain one or more [[packages]] entries")
        catalog = cls(
            fs,
            root,
            tuple(
                _parse_package(item, index) for index, item in enumerate(raw_packages)
            ),
        )
        catalog.validate()
        return catalog

    def get(self, package_id: str) -> PackageDescriptor:
        try:
            return self._by_id[package_id]
        except KeyError as exc:
            raise CatalogError(f"unknown package id: {package_id}") from exc

    def validate(self) -> None:
        if len(self._by_id) != len(self.packages):
            raise CatalogError("package ids must be unique")
        owners: list[tuple[PurePosixPath, str]] = []
        for package in self.packages:
            self._validate_package(package, owners)
        self._validate_dependencies()

    def _validate_package(
        self, package: PackageDescriptor, owners: list[tuple[PurePosixPath, str]]
    ) -> None:
        self._validate_package_header(package)
        self._validate_package_paths(package, owners)
        for dependency in package.depends_on:
            if dependency not in self._by_id:
                raise CatalogError(
                    f"package {package.id} depends on unknown package {dependency}"
                )

    def _validate_package_header(self, package: PackageDescriptor) -> None:
        if not _ID.fullmatch(package.id):
            raise CatalogError(
                f"package id must be a stable lowercase slug: {package.id!r}"
            )
        _relative_path(package.root, f"package {package.id} root")
        if not package.paths:
            raise CatalogError(
                f"package {package.id} must declare at least one owned path"
            )
        if not any(item.role == "source" for item in package.paths):
            raise CatalogError(f"package {package.id} must declare a source path")
        package_root = PurePosixPath(package.root)
        if not any(
            _within(PurePosixPath(item.path), package_root) for item in package.paths
        ):
            raise CatalogError(
                f"package {package.id} root must be covered by an owned path"
            )

    def _validate_package_paths(
        self, package: PackageDescriptor, owners: list[tuple[PurePosixPath, str]]
    ) -> None:
        targets: list[PurePosixPath] = []
        for item in package.paths:
            self._validate_owned_path(package, item, owners)
            target = self._extraction_target(package, item)
            if target == PurePosixPath(".arborescent-package.toml"):
                raise CatalogError(
                    "package paths cannot overwrite the extraction metadata manifest"
                )
            if target.parts and any(
                _within(target, prior) or _within(prior, target) for prior in targets
            ):
                raise CatalogError(
                    f"package {package.id} has overlapping extraction destinations at {target}"
                )
            if target.parts:
                targets.append(target)

    def _validate_owned_path(
        self,
        package: PackageDescriptor,
        item: PackagePath,
        owners: list[tuple[PurePosixPath, str]],
    ) -> None:
        source_path = _relative_path(item.path, f"package {package.id} path")
        if item.destination is not None:
            _relative_path(item.destination, f"package {package.id} destination")
        if not item.role.strip():
            raise CatalogError(f"package {package.id} path role cannot be empty")
        source = self.root.joinpath(*source_path.parts)
        if not self.fs.exists(source):
            raise CatalogError(f"package {package.id} path does not exist: {item.path}")
        resolved = self.fs.resolve(source)
        if resolved != source:
            raise CatalogError(
                f"package {package.id} path cannot be a symlink: {item.path}"
            )
        if not resolved.is_relative_to(self.root):
            raise CatalogError(f"path escapes repository root: {item.path}")
        for prior, owner in owners:
            if _within(source_path, prior) or _within(prior, source_path):
                text = (
                    "owned by more than one package"
                    if owner != package.id
                    else "overlaps another path"
                )
                raise CatalogError(f"path {item.path!r} {text} ({owner}, {package.id})")
        owners.append((source_path, package.id))
        if item.destination is not None:
            self._extraction_target(package, item)

    def _validate_dependencies(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(package_id: str) -> None:
            if package_id in visiting:
                raise CatalogError(f"package dependency cycle includes {package_id}")
            if package_id in visited:
                return
            visiting.add(package_id)
            for dependency in self.get(package_id).depends_on:
                visit(dependency)
            visiting.remove(package_id)
            visited.add(package_id)

        for package in self.packages:
            visit(package.id)

    def extract(
        self, package_id: str, destination: Path, *, include_dependencies: bool = False
    ) -> Path:
        """Copy owned paths to an absent destination and retain identity/path metadata."""
        package = self.get(package_id)
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
        self.fs.mkdir(destination)
        try:
            copied: set[str] = set()
            for descriptor in included:
                for item in descriptor.paths:
                    target_relative = self._extraction_target(descriptor, item)
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
                    source = self.root.joinpath(*PurePosixPath(item.path).parts)
                    target = (
                        destination.joinpath(*target_relative.parts)
                        if target_relative.parts
                        else destination
                    )
                    self._copy_owned_path(source, target)
            self.fs.write_text(
                destination / ".arborescent-package.toml",
                _render_manifest(package, included),
            )
        except Exception:
            self.fs.remove_tree(destination)
            raise
        return destination

    def _copy_owned_path(self, source: Path, target: Path) -> None:
        resolved = self.fs.resolve(source)
        if resolved != source or not resolved.is_relative_to(self.root):
            raise CatalogError(
                f"package extraction does not copy symlinks or escaped paths: {source}"
            )
        if self.fs.is_dir(source):
            nested_paths = [
                nested
                for nested in self.fs.rglob(source, "*")
                if not self._is_generated(source, nested)
            ]
            for nested in nested_paths:
                nested_resolved = self.fs.resolve(nested)
                if nested_resolved != nested or not nested_resolved.is_relative_to(
                    self.root
                ):
                    raise CatalogError(
                        f"package extraction does not copy symlinks: {nested}"
                    )
            self.fs.mkdir(target)
            for nested in nested_paths:
                nested_target = target / nested.relative_to(source)
                if self.fs.is_dir(nested):
                    self.fs.mkdir(nested_target)
                else:
                    if self.fs.exists(nested_target):
                        raise CatalogError(
                            f"multiple package paths map to extraction path {nested_target}"
                        )
                    self.fs.mkdir(nested_target.parent)
                    self.fs.write_bytes(nested_target, self.fs.read_bytes(nested))
        else:
            if self.fs.exists(target):
                raise CatalogError(
                    f"multiple package paths map to extraction path {target}"
                )
            self.fs.mkdir(target.parent)
            self.fs.write_bytes(target, self.fs.read_bytes(source))

    def _is_generated(self, root: Path, path: Path) -> bool:
        """Whether ``path`` is generated output below the declared ``root`` (the root itself is always copied)."""
        parts = path.relative_to(root).parts
        if _GENERATED_DIRS.intersection(parts[:-1]):
            return True
        name = parts[-1]
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in _GENERATED_FILES):
            return True
        return name in _GENERATED_DIRS and self.fs.is_dir(path)

    def _extraction_target(
        self, package: PackageDescriptor, item: PackagePath
    ) -> PurePosixPath:
        if item.destination is not None:
            return _relative_path(item.destination, f"package {package.id} destination")
        root = PurePosixPath(package.root)
        source = PurePosixPath(item.path)
        if not _within(source, root):
            raise CatalogError(
                f"package {package.id} path {item.path!r} is outside root {package.root!r}; set destination"
            )
        parts = source.parts[len(root.parts) :]
        return PurePosixPath(*parts) if parts else PurePosixPath()

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


def _parse_package(value: object, index: int) -> PackageDescriptor:
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
                _required(item, "path"),
                _required(item, "role"),
                _optional(item, "destination"),
                metadata,
            )
        )
    return PackageDescriptor(
        id=_required(value, "id"),
        name=_required(value, "name"),
        kind=_required(value, "kind"),
        repository=_required(value, "repository"),
        root=_required(value, "root"),
        summary=_required(value, "summary"),
        domain=_optional(value, "domain"),
        capabilities=_strings(value.get("capabilities", []), "capabilities"),
        depends_on=_strings(value.get("depends_on", []), "depends_on"),
        extraction_status=str(value.get("extraction_status", "in-repository")),
        paths=tuple(paths),
    )


def _required(value: Mapping[str, Any], key: str) -> str:
    item: object = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise CatalogError(f"package entry requires a non-empty {key}")
    return item.strip()


def _optional(value: Mapping[str, Any], key: str) -> str | None:
    item: object = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item.strip():
        raise CatalogError(f"{key} must be a non-empty string when set")
    return item.strip()


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CatalogError(f"{name} must be an array of non-empty strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CatalogError(f"{name} must be an array of non-empty strings")
        result.append(item.strip())
    return tuple(result)


def _relative_path(value: str, context: str) -> PurePosixPath:
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


def _within(path: PurePosixPath, parent: PurePosixPath) -> bool:
    return path == parent or parent in path.parents


def _render_manifest(
    package: PackageDescriptor, included: Sequence[PackageDescriptor]
) -> str:
    lines = [
        "[package]",
        f"id = {json.dumps(package.id)}",
        f"name = {json.dumps(package.name)}",
        f"kind = {json.dumps(package.kind)}",
        f"repository = {json.dumps(package.repository)}",
        f"source_root = {json.dumps(package.root)}",
        f"summary = {json.dumps(package.summary)}",
        'extraction_status = "extracted"',
        f"capabilities = {json.dumps(package.capabilities)}",
        f"depends_on = {json.dumps(package.depends_on)}",
        f"included_packages = {json.dumps([item.id for item in included])}",
        "",
    ]
    if package.domain is not None:
        lines.insert(-1, f"domain = {json.dumps(package.domain)}")
    for descriptor in included:
        for item in descriptor.paths:
            lines.extend(
                [
                    "[[paths]]",
                    f"package_id = {json.dumps(descriptor.id)}",
                    f"path = {json.dumps(item.path)}",
                    f"role = {json.dumps(item.role)}",
                    f"metadata = {_inline_table(item.metadata or {})}",
                ]
            )
            if item.destination is not None:
                lines.append(f"destination = {json.dumps(item.destination)}")
            lines.append("")
    return "\n".join(lines)


def _inline_table(value: Mapping[str, str]) -> str:
    return (
        "{ "
        + ", ".join(
            f"{json.dumps(key)} = {json.dumps(item)}" for key, item in value.items()
        )
        + " }"
        if value
        else "{}"
    )
