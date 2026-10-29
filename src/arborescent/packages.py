"""Package metadata and extraction use cases owned by Arborescent."""

from __future__ import annotations

import fnmatch
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .config import load_policy
from .domain import NOISE_DIRS, Policy, PolicyError
from .ports import FileSystem

_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
# Directories and files below a declared package path that are local build output, not distributable source.
_GENERATED_DIRS = NOISE_DIRS | {"build", "dist"}
_GENERATED_FILES = ("*.pyc",)
MANIFEST_NAME = ".arborescent-package.toml"
MANIFEST_VERSION = 2


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


@dataclass(frozen=True, slots=True)
class PackageSemantics:
    """Stable meaning of a package: identical in the catalog and in every extraction of it."""

    id: str
    name: str
    kind: str
    summary: str
    domain: str | None = None
    capabilities: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractedDependency:
    semantics: PackageSemantics
    origin_repository: str
    origin_root: str
    current_root: str


@dataclass(frozen=True, slots=True)
class ExtractedPath:
    package_id: str
    role: str
    metadata: Mapping[str, str]
    origin_path: str  # coordinate in the origin repository, kept for traceability
    current_path: str  # coordinate inside the extracted tree


@dataclass(frozen=True, slots=True)
class ExtractedPackage:
    """A validated ``.arborescent-package.toml``: semantics, origin and current location, and operation state."""

    semantics: PackageSemantics
    origin_repository: str
    origin_root: str
    origin_status: str
    current_repository: str
    current_root: str
    status: str
    included_packages: tuple[str, ...]
    dependencies: tuple[ExtractedDependency, ...]
    paths: tuple[ExtractedPath, ...]


@dataclass(frozen=True, slots=True)
class _Placement:
    package_id: str
    item: PackagePath
    current_path: str


def semantics_of(package: PackageDescriptor) -> PackageSemantics:
    return PackageSemantics(
        package.id,
        package.name,
        package.kind,
        package.summary,
        package.domain,
        package.capabilities,
        package.depends_on,
    )


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
        self.fs.mkdir(destination)
        try:
            copied: set[str] = set()
            placements: list[_Placement] = []
            pairs: list[tuple[Path, Path]] = []
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
                    placements.append(_Placement(descriptor.id, item, key))
                    source = self.root.joinpath(*PurePosixPath(item.path).parts)
                    target = (
                        destination.joinpath(*target_relative.parts)
                        if target_relative.parts
                        else destination
                    )
                    pairs += self._copy_owned_path(source, target)
            self.fs.write_text(
                destination / MANIFEST_NAME,
                _render_manifest(package, included, placements, current_repository),
            )
            load_extracted_package(self.fs, destination)
            _check_module_identity(self.fs, pairs)
        except Exception:
            self.fs.remove_tree(destination)
            raise
        return destination

    def _copy_owned_path(self, source: Path, target: Path) -> list[tuple[Path, Path]]:
        """Copy one owned path; return the (source file, copied file) pairs."""
        pairs: list[tuple[Path, Path]] = []
        resolved = self.fs.resolve(source)
        if resolved != source or not resolved.is_relative_to(self.root):
            raise CatalogError(
                f"package extraction does not copy symlinks or escaped paths: {source}"
            )
        if self.fs.is_dir(source):
            nested_paths = [
                nested
                for nested in self.fs.rglob(source, "*")
                if not _is_generated(self.fs, source, nested)
            ]
            for nested in nested_paths:
                try:
                    nested_resolved = self.fs.resolve(nested)
                except (
                    OSError
                ):  # e.g. a dangling link: Windows raises instead of returning the path
                    nested_resolved = None
                if (
                    nested_resolved is None
                    or nested_resolved != nested
                    or not nested_resolved.is_relative_to(self.root)
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
                    pairs.append((nested, nested_target))
        else:
            if self.fs.exists(target):
                raise CatalogError(
                    f"multiple package paths map to extraction path {target}"
                )
            self.fs.mkdir(target.parent)
            self.fs.write_bytes(target, self.fs.read_bytes(source))
            pairs.append((source, target))
        return pairs

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
    package: PackageDescriptor,
    included: Sequence[PackageDescriptor],
    placements: Sequence[_Placement],
    repository: str,
) -> str:
    lines = [f"manifest_version = {MANIFEST_VERSION}", ""]
    lines += ["[package]", *_semantic_lines(package), ""]
    lines += [
        "[origin]",
        f"repository = {_toml_str(package.repository)}",
        f"root = {_toml_str(package.root)}",
        f"extraction_status = {_toml_str(package.extraction_status)}",
        "",
        "[location]",
        f"repository = {_toml_str(repository)}",
        'root = "."',
        "",
        "[extraction]",
        'status = "extracted"',
        f"included_packages = {_toml_list([item.id for item in included])}",
        "",
    ]
    for descriptor in included:
        if descriptor.id == package.id:
            continue
        lines += [
            "[[dependencies]]",
            *_semantic_lines(descriptor),
            f"origin_repository = {_toml_str(descriptor.repository)}",
            f"origin_root = {_toml_str(descriptor.root)}",
            f"current_root = {_toml_str(f'dependencies/{descriptor.id}')}",
            "",
        ]
    for placement in placements:
        lines += [
            "[[paths]]",
            f"package_id = {_toml_str(placement.package_id)}",
            f"role = {_toml_str(placement.item.role)}",
            f"metadata = {_inline_table(placement.item.metadata or {})}",
            f"origin_path = {_toml_str(placement.item.path)}",
            f"current_path = {_toml_str(placement.current_path)}",
            "",
        ]
    return "\n".join(lines)


def _semantic_lines(package: PackageDescriptor) -> list[str]:
    lines = [
        f"id = {_toml_str(package.id)}",
        f"name = {_toml_str(package.name)}",
        f"kind = {_toml_str(package.kind)}",
    ]
    if package.domain is not None:
        lines.append(f"domain = {_toml_str(package.domain)}")
    lines += [
        f"summary = {_toml_str(package.summary)}",
        f"capabilities = {_toml_list(package.capabilities)}",
        f"depends_on = {_toml_list(package.depends_on)}",
    ]
    return lines


def load_extracted_package(fs: FileSystem, directory: Path) -> ExtractedPackage:
    """Read and validate an extracted tree: current paths resolve inside it and nested policies keep their layout."""
    base = fs.resolve(directory)
    manifest_path = base / MANIFEST_NAME
    try:
        data = tomllib.loads(fs.read_text(manifest_path))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise CatalogError(f"cannot read {manifest_path}: {exc}") from exc
    if data.get("manifest_version") != MANIFEST_VERSION:
        raise CatalogError(
            f"{MANIFEST_NAME} must declare manifest_version = {MANIFEST_VERSION}"
        )
    package = _manifest_semantics(_manifest_table(data, "package"))
    origin = _manifest_table(data, "origin")
    location = _manifest_table(data, "location")
    extraction = _manifest_table(data, "extraction")
    dependencies = tuple(
        ExtractedDependency(
            _manifest_semantics(item),
            _required(item, "origin_repository"),
            _required(item, "origin_root"),
            _required(item, "current_root"),
        )
        for item in _manifest_array(data, "dependencies")
    )
    included = _strings(extraction.get("included_packages"), "included_packages")
    known = {package.id, *(item.semantics.id for item in dependencies)}
    if set(included) != known:
        raise CatalogError(
            "included_packages does not match the package and its dependencies"
        )
    roots = {
        package.id: _current_location(_required(location, "root"), "location root")
    }
    for dependency in dependencies:
        roots[dependency.semantics.id] = _current_location(
            dependency.current_root, f"package {dependency.semantics.id} current root"
        )
    paths = tuple(
        _manifest_path(item, known) for item in _manifest_array(data, "paths")
    )
    for item in paths:
        _check_current_path(fs, base, item)
    _check_tree(fs, base)
    result = ExtractedPackage(
        package,
        _required(origin, "repository"),
        _required(origin, "root"),
        _required(origin, "extraction_status"),
        _required(location, "repository"),
        _required(location, "root"),
        _required(extraction, "status"),
        included,
        dependencies,
        paths,
    )
    for item in paths:
        _check_nested_policy(fs, base, roots[item.package_id], item)
    return result


def _is_generated(fs: FileSystem, root: Path, path: Path) -> bool:
    """Whether ``path`` is generated output below ``root`` (the root itself is always copied)."""
    parts = path.relative_to(root).parts
    if _GENERATED_DIRS.intersection(parts[:-1]):
        return True
    name = parts[-1]
    if any(fnmatch.fnmatchcase(name, pattern) for pattern in _GENERATED_FILES):
        return True
    return name in _GENERATED_DIRS and fs.is_dir(path)


def _current_repository(repository: str | None, package: PackageDescriptor) -> str:
    if repository is None:
        return package.repository
    if not repository.strip():
        raise CatalogError("repository must be a non-empty string when set")
    return repository.strip()


def _current_location(value: str, context: str) -> PurePosixPath:
    """A location inside the extracted tree; ``.`` is the tree root itself."""
    return PurePosixPath() if value == "." else _relative_path(value, context)


def _manifest_table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value: object = data.get(key)
    if not isinstance(value, dict):
        raise CatalogError(f"{MANIFEST_NAME} requires a [{key}] table")
    return value


def _manifest_array(data: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value: object = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CatalogError(f"{MANIFEST_NAME} {key} must be an array of tables")
    return value


def _manifest_semantics(value: Mapping[str, Any]) -> PackageSemantics:
    return PackageSemantics(
        _required(value, "id"),
        _required(value, "name"),
        _required(value, "kind"),
        _required(value, "summary"),
        _optional(value, "domain"),
        _strings(value.get("capabilities", []), "capabilities"),
        _strings(value.get("depends_on", []), "depends_on"),
    )


def _manifest_path(value: Mapping[str, Any], known: set[str]) -> ExtractedPath:
    package_id = _required(value, "package_id")
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
        _required(value, "role"),
        dict(metadata),
        _required(value, "origin_path"),
        _required(value, "current_path"),
    )


def _check_current_path(fs: FileSystem, base: Path, item: ExtractedPath) -> None:
    relative = _current_location(
        item.current_path, f"package {item.package_id} current path"
    )
    target = base.joinpath(*relative.parts)
    if not fs.exists(target) or fs.resolve(target) != target:
        raise CatalogError(
            f"current path {item.current_path!r} of package {item.package_id} does not resolve inside the extracted tree"
        )


def _check_nested_policy(
    fs: FileSystem, base: Path, root: PurePosixPath, item: ExtractedPath
) -> None:
    """A declared nested ``arborescent.toml`` must load and still find its sources where it expects them."""
    declared = item.metadata.get("arborescent_config")
    if declared is None:
        return
    relative = root / _relative_path(
        declared, f"package {item.package_id} arborescent_config"
    )
    config = base.joinpath(*relative.parts)
    if not fs.is_file(config):
        raise CatalogError(
            f"arborescent_config {declared!r} of package {item.package_id} is missing from the extraction; declare it as a package path"
        )
    try:
        policy, _ = load_policy(fs, config)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, PolicyError) as exc:
        raise CatalogError(
            f"nested policy {relative.as_posix()} is invalid: {exc}"
        ) from exc
    for local in (policy, *(scope.policy for scope in policy.scopes)):
        for entry in (*local.sources, *local.source_roots):
            if not fs.exists(config.parent / entry):
                raise CatalogError(
                    f"nested policy {relative.as_posix()} expects {entry!r}, which is absent after extraction; "
                    "a changed layout would change module identity"
                )


def _toml_str(value: str) -> str:
    """A TOML basic string. ``json.dumps`` is not one: it writes non-BMP characters as invalid surrogate pairs."""
    escapes = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}
    return (
        '"'
        + "".join(
            escapes.get(char)
            or (
                f"\\u{ord(char):04x}" if ord(char) < 0x20 or ord(char) == 0x7F else char
            )
            for char in value
        )
        + '"'
    )


def _toml_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_str(value) for value in values) + "]"


def _inline_table(value: Mapping[str, str]) -> str:
    return (
        "{ "
        + ", ".join(
            f"{_toml_str(key)} = {_toml_str(item)}" for key, item in value.items()
        )
        + " }"
        if value
        else "{}"
    )


def _check_tree(fs: FileSystem, base: Path) -> None:
    """No symlink may leave the tree (generated directories excepted), and every shipped policy must load."""
    for path in fs.rglob(base, "*"):
        if _is_generated(fs, base, path):
            continue
        if fs.resolve(path) != path:
            raise CatalogError(f"extracted tree contains a symlink: {path}")
        if path.name == "arborescent.toml" and fs.is_file(path):
            try:
                _ = load_policy(fs, path)
            except (
                OSError,
                tomllib.TOMLDecodeError,
                UnicodeDecodeError,
                PolicyError,
            ) as exc:
                raise CatalogError(
                    f"shipped policy {path.relative_to(base).as_posix()} is invalid: {exc}"
                ) from exc


def _depth(path: PurePosixPath) -> int:
    return len(path.parts)


def _module_name(file: Path, config_dir: Path, policy: Policy) -> str | None:
    """The dotted module name ``policy`` derives for ``file``, or ``None`` when the file is outside its sources."""
    if file.suffix != ".py" or not file.is_relative_to(config_dir):
        return None
    relative = PurePosixPath(file.relative_to(config_dir).as_posix())
    for local in (policy, *(scope.policy for scope in policy.scopes)):
        if not any(
            _within(relative, PurePosixPath(source)) for source in local.sources
        ):
            continue
        roots = [
            PurePosixPath(root)
            for root in local.source_roots
            if _within(relative, PurePosixPath(root))
        ]
        if not roots:
            return None
        deepest = max(roots, key=_depth)
        parts = list(relative.relative_to(deepest).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    return None


def _check_module_identity(fs: FileSystem, pairs: Sequence[tuple[Path, Path]]) -> None:
    """Extraction must not rename a module a shipped policy governs or move it out of that policy's sources.

    Files newly visible to the policy (a ``sources = "."`` policy now seeing moved tests) only add modules.
    """
    origin_configs = {
        target: source for source, target in pairs if target.name == "arborescent.toml"
    }
    for config, origin_config in origin_configs.items():
        policy, _ = load_policy(fs, config)
        changed = sorted(
            f"{before} -> {after or '(outside sources)'}"
            for source, target in pairs
            if target.is_relative_to(config.parent)
            and (before := _module_name(source, origin_config.parent, policy))
            is not None
            and before != (after := _module_name(target, config.parent, policy))
        )
        if changed:
            raise CatalogError(
                f"extraction would change module identity under {config.name}: {'; '.join(changed[:5])}"
                + (" ..." if len(changed) > 5 else "")
            )
