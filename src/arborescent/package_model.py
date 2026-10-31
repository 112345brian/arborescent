"""The package vocabulary: descriptors from ``packages.toml``, extracted-package manifests, and their error. Pure."""

from __future__ import annotations

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
# A home repository is used verbatim as a git source, so it must be a URL: https://, ssh:// or scp-style git@host:path.
GIT_URL_PATTERN = r"^(?:https://|ssh://)[^\s/]+[^\s]*$|^git@[^\s:@/]+:[^\s]+$"


@dataclass(frozen=True, slots=True)
class PackageHome:
    """Where a package lives after the migration: its own repository, or a member of a workspace in the same repository."""

    kind: str  # one of HOME_KINDS
    repository: str | None = None  # kind "repository": the new repository
    path: str | None = None  # kind "workspace-member": the member's folder, repository-relative


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
    extraction_status: str = "in-repository"
    paths: tuple[PackagePath, ...] = ()
    release: ReleasePolicy | None = None
    history: str | None = None
    unmanaged: bool = False  # explicitly outside release, pin and history policy
    home: PackageHome | None = None  # None: not annotated yet (a gap in the migration export)
    public: tuple[str, ...] = ()  # dotted module globs other packages may import; empty = no declared surface
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
    )


@dataclass(frozen=True, slots=True)
class AssetRule:
    """A non-Python file glob owned by a package; rules are tried in order and the first match wins."""

    pattern: str
    package: str


@dataclass(frozen=True, slots=True)
class CatalogDocument:
    """What ``packages.toml`` declares, parsed but not checked against the file system."""

    packages: tuple[PackageDescriptor, ...]
    assets: tuple[AssetRule, ...] = ()
