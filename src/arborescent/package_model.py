"""The package vocabulary: descriptors from ``packages.toml``, extracted-package manifests, and their error. Pure."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from .domain import PolicyError

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


PIN_KINDS = ("tag", "sha")  # how dependents pin the package
MERGE_KINDS = ("merge-commit",)  # squash or rebase would orphan pinned SHAs
HISTORY_KINDS = ("preserve", "fresh")  # whether the package's history is kept when it is split out


@dataclass(frozen=True, slots=True)
class ReleasePolicy:
    """Release stance of a package. Data for the tools that release and pin; Arborescent performs none of it."""

    pin: str | None = None
    merge: str | None = None
    tag_before_branch_delete: bool | None = None


HOME_KINDS = ("repository", "workspace-member")
_GIT_URL = re.compile(r"(?:https://|ssh://)[^\s/]+\S*|git@[^\s:@/]+:\S+")


def is_git_url(value: str) -> bool:
    """Whether ``value`` can be used directly as a git source: ``https://``, ``ssh://`` or scp-style ``git@host:path``.

    A classifier, not a validator: nothing else counts and no host is guessed (``org/name`` is a legitimate name, not a URL).
    """
    return _GIT_URL.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class PackageHome:
    """Where a package lives after the migration: its own repository, or a member of a workspace in the same repository."""

    kind: str  # one of HOME_KINDS
    repository: str | None = None  # kind "repository": any non-empty string naming the new repository
    path: str | None = None  # kind "workspace-member": the member's folder, repository-relative

    @property
    def repository_is_git_url(self) -> bool | None:
        """Derived, never annotated: whether a consumer can use ``repository`` as a git source (None without one)."""
        return None if self.repository is None else is_git_url(self.repository)


@dataclass(frozen=True, slots=True)
class ModuleRename:
    """A dotted-module prefix that changes name in the target (``app.core`` -> ``core``); the longest matching prefix wins."""

    source: str
    target: str


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
    public: tuple[str, ...] = ()  # dotted module globs other packages may import; empty = no declared surface
    extraction_status: str = "in-repository"
    paths: tuple[PackagePath, ...] = ()
    release: ReleasePolicy | None = None
    history: str | None = None
    unmanaged: bool = False  # explicitly outside release, pin and history policy
    vendored: bool = False  # a read-only copy of code maintained elsewhere: never edited here, so no owner-side policy
    home: PackageHome | None = None  # None: not annotated yet (a gap in the migration export)
    externals: tuple[str, ...] | None = None  # top-level external import names the package may use; None: not annotated
    module_renames: tuple[ModuleRename, ...] = ()


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
    public: tuple[str, ...] = ()


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
class Placement:
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
        package.public,
    )


# An ``[[assets]]`` owner that is no package: the repository itself (LICENSE, CI files, the lock file). Never extracted.
ROOT_OWNER = "@root"


@dataclass(frozen=True, slots=True)
class AssetRule:
    """A non-Python file glob owned by a package (or by ``@root``); rules are tried in order and the first match wins."""

    pattern: str
    package: str


@dataclass(frozen=True, slots=True)
class CatalogDocument:
    """What ``packages.toml`` declares, parsed but not checked against the file system."""

    packages: tuple[PackageDescriptor, ...]
    assets: tuple[AssetRule, ...] = ()
