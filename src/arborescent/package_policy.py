"""Release, pin and history policy annotations per package (#56): parsing, validation and the missing-policy gap. Pure.

These are decisions the code cannot show: how dependents pin a package, whether its history is kept, which merge style
keeps pinned SHAs reachable. Arborescent records and checks them; the tools that release, pin or rewrite history
receive them as arguments. ``packages.toml`` example::

    [[packages]]
    id = "core"
    history = "preserve"            # or "fresh"
    [packages.release]
    pin = "tag"                     # or "sha"
    merge = "merge-commit"          # the only value: squash or rebase would orphan pinned SHAs
    tag_before_branch_delete = true

A package that has neither a policy nor ``unmanaged = true`` is a named gap (``package-release-policy-missing``).
"""

from __future__ import annotations

from collections.abc import Mapping

from .domain import Rule, Violation
from .package_model import (
    HISTORY_KINDS,
    MERGE_KINDS,
    PIN_KINDS,
    CatalogDocument,
    CatalogError,
    PackageHome,
    ReleasePolicy,
)

__all__ = ["check_member_policy", "is_workspace_member", "parse_policy", "policy_violations"]

_RELEASE_KEYS = ("pin", "merge", "tag_before_branch_delete")


def is_workspace_member(home: PackageHome | None) -> bool:
    """A workspace member stays in the repository, so it has no history of its own to keep and no pin to declare."""
    return home is not None and home.kind == "workspace-member"


def check_member_policy(home: PackageHome | None, release: ReleasePolicy | None, history: str | None, package: str) -> None:
    """A workspace member cannot declare release or history policy: the export carries none for it, so it would be lost."""
    if is_workspace_member(home) and (release is not None or history is not None):
        raise CatalogError(
            f"package {package!r} is a workspace member, which has no release or history of its own; "
            "remove its release/history policy or give it its own repository home"
        )


def _choice(value: object, kinds: tuple[str, ...], name: str, package: str) -> str:
    if not isinstance(value, str) or value not in kinds:
        raise CatalogError(f"package {package!r} {name} must be one of {', '.join(map(repr, kinds))}, not {value!r}")
    return value


def parse_policy(value: Mapping[str, object], package: str) -> tuple[ReleasePolicy | None, str | None, bool]:
    """The ``release`` table, ``history`` and ``unmanaged`` of one package entry; unknown ``release`` keys are refused."""
    release = _release(value.get("release"), package)
    history = None if "history" not in value else _choice(value["history"], HISTORY_KINDS, "history", package)
    unmanaged = False
    if "unmanaged" in value:
        if value["unmanaged"] is not True:
            raise CatalogError(f"package {package!r} unmanaged must be true when set; omit it to declare a policy")
        unmanaged = True
        if release is not None or history is not None:
            raise CatalogError(f"package {package!r} is unmanaged and also declares a release or history policy")
    return release, history, unmanaged


def _release(raw: object, package: str) -> ReleasePolicy | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CatalogError(f"package {package!r} release must be a table")
    unknown = sorted(set(raw) - set(_RELEASE_KEYS))
    if unknown:
        raise CatalogError(
            f"package {package!r} release has unknown key(s) {', '.join(map(repr, unknown))}; "
            f"allowed: {', '.join(_RELEASE_KEYS)}"
        )
    tag_first: object = raw.get("tag_before_branch_delete")
    if tag_first is not None and not isinstance(tag_first, bool):
        raise CatalogError(f"package {package!r} release.tag_before_branch_delete must be true or false")
    return ReleasePolicy(
        pin=None if "pin" not in raw else _choice(raw["pin"], PIN_KINDS, "release.pin", package),
        merge=None if "merge" not in raw else _choice(raw["merge"], MERGE_KINDS, "release.merge", package),
        tag_before_branch_delete=tag_first,
    )


def policy_violations(catalog: CatalogDocument) -> list[Violation]:
    """One finding per package that declares neither a policy nor ``unmanaged = true``; workspace members are exempt."""
    return [
        Violation(
            Rule.PACKAGE_RELEASE_POLICY_MISSING, package.id,
            f"package {package.id!r} declares no release or history policy and is not marked unmanaged",
        )
        for package in catalog.packages
        if package.release is None and package.history is None and not package.unmanaged
        and not is_workspace_member(package.home)
    ]
