"""Which discovered path is which module, and which paths are enforced, unclassified, excluded, noise or vendored.

Pure rules over relative paths and the policy, split out of ``scanner`` so the adapter holds only the effectful steps
(listing files, reading and fingerprinting them, the cache file); this module decides what those paths mean.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path

from .declaration_facts import DEV_TOOL_ROLE
from .domain import NOISE_DIRS, ArchitecturePolicy, Policy, PolicyError
from .facts import ModuleInfo
from .path_exclusion import excluded_by_policy, matches_exclusion

__all__ = [
    "PRUNE",
    "add_discovered",
    "annotated_modules",
    "entry_scripts",
    "is_enforced_source",
    "is_selected",
    "is_skipped",
    "is_unclassified",
    "misplaced_dev_tools",
    "policies",
    "sift_paths",
    "still_unclassified",
]

PRUNE = NOISE_DIRS
_VENDOR_NAMES = frozenset({"vendor", "vendored", "third_party", "site-packages"})


def _under_sources(rel: str, sources: Sequence[str]) -> bool:
    for source in sources:
        source = source.strip("/")
        if source in ("", ".") or rel == source or rel.startswith(source + "/"):
            return True
    return False


def policies(policy: Policy) -> tuple[ArchitecturePolicy, ...]:
    return tuple(scope.policy for scope in policy.scopes) if policy.scopes else (policy,)


def entry_scripts(policy: Policy) -> tuple[str, ...]:
    return tuple(script for local_policy in policies(policy) for script in local_policy.entry_scripts)


def is_enforced_source(policy: Policy, module: str, rel: str) -> bool:
    if not policy.scopes:
        return policy.ring_of(module) is not None or _under_sources(rel, policy.sources)
    scope = policy.scope_for_path(rel)
    return scope is not None and (
        scope.policy.ring_of(module) is not None or _under_sources(rel, scope.policy.sources)
    )


def _module_identity(policy: Policy, rel: str) -> tuple[str | None, bool]:
    scope = policy.scope_for_path(rel) if policy.scopes else None
    roots = scope.policy.source_roots if scope else policy.source_roots
    ordered_roots = sorted((root.strip("/") for root in roots), key=len, reverse=True)
    base = next((root for root in ordered_roots if root in ("", ".") or rel.startswith(root + "/")), ".")
    subpath = rel if base in ("", ".") else rel[len(base) + 1 :]
    parts = subpath.removesuffix(".py").split("/")
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return (".".join(parts) if parts else None), is_package


def add_discovered(found: dict[str, tuple[str, bool, str]], policy: Policy, rel: str) -> None:
    module, is_package = _module_identity(policy, rel)
    if module is None:
        return
    previous = found.get(module)
    if previous is not None and previous[2] != rel:
        previous_scope = policy.scope_for_path(previous[2]) if policy.scopes else None
        scope = policy.scope_for_path(rel) if policy.scopes else None
        previous_name = previous_scope.name if previous_scope else "unowned"
        scope_name = scope.name if scope else "unowned"
        raise PolicyError(
            f"duplicate module name {module!r} in scopes {previous_name!r} ({previous[2]}) "
            f"and {scope_name!r} ({rel})"
        )
    found[module] = (module, is_package, rel)


def is_unclassified(policy: Policy, module: str, rel: str) -> bool:
    if policy.is_dev_tool_path(rel):
        return False
    scope = policy.scope_for_path(rel) if policy.scopes else None
    if policy.scopes and scope is None:
        return True
    if scope is not None:
        return scope.policy.ring_of(module) is None and not _under_sources(rel, scope.policy.sources)
    return policy.ring_of(module) is None and not _under_sources(rel, policy.sources)


def misplaced_dev_tools(
    files: Sequence[tuple[str, bool, str]], modules: Mapping[str, ModuleInfo], dev_tool_modules: Collection[str],
) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(
        (module, rel) for module, _, rel in files
        if module in modules and modules[module].facts.arbor_role == DEV_TOOL_ROLE and module not in dev_tool_modules
    ))


def _excluded_folder(rel: str, patterns: Sequence[str]) -> str:
    parts = rel.split("/")[:-1]
    for index in range(len(parts), 0, -1):
        candidate = "/".join(parts[:index])
        if any(matches_exclusion(candidate, pattern) for pattern in patterns):
            return candidate
    return Path(rel).parent.as_posix()


def _noise_parent(rel: str, tracked: bool) -> bool:
    return tracked and any(part in PRUNE for part in rel.split("/"))


def _vendor_parent(rel: str) -> str | None:
    parts = rel.split("/")[:-1]
    noise_index = next((index for index, part in enumerate(parts) if part in PRUNE), len(parts))
    for index in range(noise_index - 1, -1, -1):
        if parts[index] in _VENDOR_NAMES:
            if any(part in _VENDOR_NAMES for part in parts[:index]):
                continue
            return "/".join(parts[: index + 1])
    return None


def _skipped_vendor(rel: str, policy: Policy) -> str | None:
    """The auto-excluded vendor folder holding ``rel``, unless vendor scanning (global or per ``vendor_roots``) covers it."""
    vendor = _vendor_parent(rel)
    if vendor is None or policy.scan_vendors:
        return None
    if any(rel.startswith(root.rstrip("/") + "/") for root in policy.scan_vendor_paths):
        return None
    return vendor


def is_selected(only: frozenset[str] | None, entry_scripts: Collection[str], module: str, rel: str) -> bool:
    return only is None or rel in only or module in entry_scripts


def annotated_modules(
    files: list[tuple[str, bool, str]],
    arbor_index: dict[str, tuple[str | None, str | None]],
    ring_names: set[str],
    selected: Callable[[str, str], bool],
) -> set[str]:
    """Modules whose valid ``__arbor__`` ring is a policy ring and that the run selected."""
    return {
        module for module, _, rel in files
        if arbor_index[module][0] in ring_names and arbor_index[module][1] is None and selected(module, rel)
    }


def still_unclassified(
    policy: Policy,
    unclassified: tuple[tuple[str, str], ...],
    arbor_index: dict[str, tuple[str | None, str | None]],
    ring_names: set[str],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        item for item in unclassified
        if (policy.scopes and policy.scope_for_path(item[1]) is None)
        or arbor_index[item[0]][0] not in ring_names or arbor_index[item[0]][1] is not None
    )


def is_skipped(rel: str, policy: Policy, tracked: bool, excluded_paths: tuple[str, ...]) -> bool:
    return excluded_by_policy(rel, excluded_paths) or _noise_parent(rel, tracked) or _skipped_vendor(rel, policy) is not None


def sift_paths(
    all_paths: list[str], tracked: bool, policy: Policy, excluded_paths: tuple[str, ...]
) -> tuple[dict[str, int], list[str], dict[str, list[str]]]:
    """Count the Python files each excluded folder hides, collect noise files, and group the skipped vendor folders."""
    excluded: dict[str, int] = {}
    noise_files: list[str] = []
    vendors: dict[str, list[str]] = {}
    for rel in all_paths:
        is_python = rel.endswith(".py")
        if excluded_by_policy(rel, excluded_paths):
            if is_python:
                folder = _excluded_folder(rel, excluded_paths)
                excluded[folder] = excluded.get(folder, 0) + 1
        elif _noise_parent(rel, tracked):
            if is_python:
                noise_files.append(rel)
        elif (vendor := _skipped_vendor(rel, policy)) is not None:
            vendors.setdefault(vendor, []).append(rel)
    return excluded, noise_files, vendors
