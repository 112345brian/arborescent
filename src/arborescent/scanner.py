"""Source discovery and content-hash cached fact extraction."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import NOISE_DIRS, Policy, PolicyError
from .facts import (
    DEV_TOOL_ROLE,
    EffectUse,
    Forwarder,
    ModuleInfo,
    RawImport,
    SourceFacts,
    TopLevelDef,
    extract,
)
from .path_exclusion import excluded_by_policy
from .ports import ToolRunner
from .tool_runner import SubprocessToolRunner

_PRUNE = NOISE_DIRS
_CACHE_VERSION = 15
_VENDOR_NAMES = frozenset({"vendor", "vendored", "third_party", "site-packages"})


def _under_sources(rel: str, sources: Sequence[str]) -> bool:
    for source in sources:
        source = source.strip("/")
        if source in ("", ".") or rel == source or rel.startswith(source + "/"):
            return True
    return False


def _policies(policy: Policy) -> tuple[Policy, ...]:
    return tuple(scope.policy for scope in policy.scopes) if policy.scopes else (policy,)


def _entry_scripts(policy: Policy) -> tuple[str, ...]:
    return tuple(script for local_policy in _policies(policy) for script in local_policy.entry_scripts)


def _is_enforced_source(policy: Policy, module: str, rel: str) -> bool:
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


def _add_discovered(found: dict[str, tuple[str, bool, str]], policy: Policy, rel: str) -> None:
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


def _is_unclassified(policy: Policy, module: str, rel: str) -> bool:
    if policy.is_dev_tool_path(rel):
        return False
    scope = policy.scope_for_path(rel) if policy.scopes else None
    if policy.scopes and scope is None:
        return True
    if scope is not None:
        return scope.policy.ring_of(module) is None and not _under_sources(rel, scope.policy.sources)
    return policy.ring_of(module) is None and not _under_sources(rel, policy.sources)


def _misplaced_dev_tools(
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
        if any(fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(parts[index - 1], pattern) for pattern in patterns):
            return candidate
    return Path(rel).parent.as_posix()


def _listed_paths(root: Path, runner: ToolRunner) -> tuple[list[str], bool]:
    listing = (
        runner.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], root)
        if runner.available("git") else None
    )
    if listing is not None and listing.returncode == 0:
        candidates = listing.stdout.split("\0") if "\0" in listing.stdout else listing.stdout.splitlines()
        paths = [path for item in candidates if (path := item.replace("\\", "/").removeprefix("./")) and (root / path).is_file()]
        return sorted(set(paths)), True
    paths = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _PRUNE)
        here = Path(directory).relative_to(root).as_posix()
        paths.extend(f"{here}/{name}".removeprefix("./") for name in filenames)
    return sorted(paths), False


def _noise_parent(rel: str, tracked: bool) -> bool:
    return tracked and any(part in _PRUNE for part in rel.split("/"))


def _vendor_parent(rel: str) -> str | None:
    parts = rel.split("/")[:-1]
    noise_index = next((index for index, part in enumerate(parts) if part in _PRUNE), len(parts))
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


def _fingerprint_vendors(vendors: dict[str, list[str]], root: Path, excluded: dict[str, int]) -> list[tuple[str, str]]:
    summaries: list[tuple[str, str]] = []
    for folder, contents in sorted(vendors.items()):
        digest = hashlib.blake2b(digest_size=16)
        for rel in sorted(contents):
            relative = rel[len(folder) + 1 :]
            data = (root / rel).read_bytes()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        summaries.append((folder, digest.hexdigest()))
        python_count = sum(rel.endswith(".py") for rel in contents)
        if python_count:
            excluded[folder] = excluded.get(folder, 0) + python_count
    return summaries



@dataclass(frozen=True)
class ScanData:
    known: frozenset[str]
    modules: dict[str, ModuleInfo]
    entry_deps: dict[str, tuple[str, ...] | None]
    paths: dict[str, str]
    parsed: int = 0  # files actually parsed this run (cache misses); 0 on a fully warm run
    excluded: dict[str, int] = field(default_factory=lambda: {})
    noise_files: tuple[str, ...] = ()
    unclassified: tuple[tuple[str, str], ...] = ()  # (module, path)
    vendor_folders: tuple[tuple[str, str], ...] = ()  # (folder, content fingerprint)
    arbor_index: dict[str, tuple[str | None, str | None]] = field(default_factory=lambda: {})
    scope_by_module: dict[str, str] = field(default_factory=lambda: {})
    dev_tool_modules: frozenset[str] = frozenset()  # modules below a `dev_tools` path
    misplaced_dev_tools: tuple[tuple[str, str], ...] = ()  # (module, path): annotated as dev tooling, outside every `dev_tools` path


def _load_cache(cache_file: Path | None) -> dict[str, list[Any]]:
    if not (cache_file and cache_file.is_file()):
        return {}
    try:
        loaded = json.loads(cache_file.read_text(encoding="utf-8"))
        return loaded["files"] if loaded.get("v") == _CACHE_VERSION else {}
    except (OSError, ValueError, KeyError):
        return {}


def _facts_to_json(f: SourceFacts) -> list[Any]:
    imports = [[i.level, i.module, list(i.names), i.line, i.lazy, i.type_only] for i in f.imports]
    effects = [[u.line, u.category, u.target, u.seam] for u in f.effect_uses]
    suppressions = [[line, kind] for line, kind in f.suppressions]
    top_level_defs = [[d.name, d.line, d.is_class, d.is_abstract] for d in f.top_level_defs]
    return [
        imports, effects, None if f.script_deps is None else list(f.script_deps),
        suppressions, list(f.discard_calls), top_level_defs, f.arbor_ring, f.arbor_error, list(f.file_refs), f.main_guard, f.arbor_role,
        [[line, value] for line, value in f.string_literals],
        [[w.name, w.line, w.level, w.module, w.imported, w.member] for w in f.forwarders],
    ]


def _facts_from_json(raw: list[Any]) -> SourceFacts:
    imports = tuple(RawImport(i[0], i[1], tuple(i[2]), i[3], i[4], i[5]) for i in raw[0])
    effects = tuple(EffectUse(u[0], u[1], u[2], u[3]) for u in raw[1])
    suppressions = tuple((item[0], item[1]) for item in raw[3])
    top_level_defs = tuple(TopLevelDef(d[0], d[1], d[2], d[3]) for d in raw[5])
    return SourceFacts(
        imports, effects, None if raw[2] is None else tuple(raw[2]),
        suppressions, tuple(raw[4]), top_level_defs, raw[6], raw[7], raw[10], tuple(raw[8]), raw[9],
        tuple((item[0], item[1]) for item in raw[11]),
        tuple(Forwarder(*item) for item in raw[12]),
    )


def _is_selected(only: frozenset[str] | None, entry_scripts: Collection[str], module: str, rel: str) -> bool:
    return only is None or rel in only or module in entry_scripts


def _parse_files(
    root: Path,
    files: list[tuple[str, bool, str]],
    cache: dict[str, list[Any]],
    only_wanted: set[str] | None,
) -> tuple[dict[str, ModuleInfo], dict[str, str], dict[str, list[Any]], int]:
    """Parse (or load from the cache) every discovered file, or only the wanted ones when `only_wanted` is given."""
    modules: dict[str, ModuleInfo] = {}
    paths: dict[str, str] = {}
    fresh: dict[str, list[Any]] = {}
    parsed = 0
    for module, is_pkg, rel in files:
        paths[module] = rel
        if only_wanted is not None and module not in only_wanted:
            continue
        data = (root / rel).read_bytes()
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        hit = cache.get(rel)
        if hit is not None and hit[0] == digest:
            facts = _facts_from_json(hit[1])
            fresh[rel] = hit
        else:
            facts = extract(data.decode("utf-8-sig"), rel)
            fresh[rel] = [digest, _facts_to_json(facts)]
            parsed += 1
        modules[module] = ModuleInfo(module, rel, is_pkg, facts)
    return modules, paths, fresh, parsed


def _store_cache(
    cache_file: Path | None, cache: dict[str, list[Any]], fresh: dict[str, list[Any]], only: frozenset[str] | None, parsed: int
) -> None:
    if cache_file and (parsed > 0 or (only is None and len(fresh) != len(cache))):
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        merged = fresh if only is None else {**cache, **fresh}
        _ = cache_file.write_text(json.dumps({"v": _CACHE_VERSION, "files": merged}), encoding="utf-8")


def _annotated_modules(
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


def _still_unclassified(
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


class FsScanner:
    """Walk the policy's sources, parse each file once, reuse facts for unchanged content."""

    def __init__(self, cache_dir: Path | None = None, runner: ToolRunner | None = None) -> None:
        self._cache_dir = cache_dir
        self._runner = runner or SubprocessToolRunner()

    @property
    def cache_dir(self) -> Path | None:
        """Where this scanner's own cache lives, or None when caching is off; other passes keep a sibling file here."""
        return self._cache_dir

    def uncached(self) -> FsScanner:
        return FsScanner(None, self._runner)

    def scan(self, policy: Policy, root: Path, only: frozenset[str] | None = None) -> ScanData:
        cache_file = self._cache_dir / "scan.json" if self._cache_dir else None
        cache = _load_cache(cache_file)
        files, excluded, noise_files, unclassified, vendor_folders = self._discover(policy, root)
        known = frozenset(m for m, _, _ in files)
        scope_by_module = {
            module: scope.name
            for module, _, rel in files
            if (scope := policy.scope_for_path(rel)) is not None
        }
        annotations_enabled = "arbor-annotations" not in policy.disabled_tools
        arbor_index: dict[str, tuple[str | None, str | None]] = {}
        ring_names: set[str] = {
            ring.name for local_policy in _policies(policy) for ring in local_policy.rings
        } if annotations_enabled else set()
        entry_scripts = _entry_scripts(policy)
        wanted = {
            m
            for m, _, rel in files
            if _is_selected(only, entry_scripts, m, rel) and _is_enforced_source(policy, m, rel)
        }
        dev_tool_modules = frozenset(m for m, _, rel in files if policy.is_dev_tool_path(rel))
        wanted |= {m for m, _, rel in files if m in dev_tool_modules and (only is None or rel in only)}
        modules, paths, fresh, parsed = _parse_files(root, files, cache, None if annotations_enabled else wanted)
        _store_cache(cache_file, cache, fresh, only, parsed)
        misplaced = _misplaced_dev_tools(files, modules, dev_tool_modules) if annotations_enabled else ()
        if annotations_enabled:
            # This pass covers every discovered file, including unchanged import
            # targets and modules outside both classify and sources.
            arbor_index = {
                module: (info.facts.arbor_ring, info.facts.arbor_error)
                for module, info in modules.items()
            }
            wanted |= _annotated_modules(files, arbor_index, ring_names, lambda module, rel: _is_selected(only, entry_scripts, module, rel))
            modules = {module: info for module, info in modules.items() if module in wanted}
            unclassified = _still_unclassified(policy, unclassified, arbor_index, ring_names)
        entry = {s: modules[s].facts.script_deps for s in entry_scripts if s in modules}
        if only is not None:
            unclassified = tuple(item for item in unclassified if item[1] in only or item[0] in only)
            noise_files = tuple(path for path in noise_files if path in only)
        return ScanData(
            known, modules, entry, paths, parsed, excluded, noise_files, unclassified,
            vendor_folders, arbor_index, scope_by_module, dev_tool_modules, misplaced,
        )

    def _discover(
        self, policy: Policy, root: Path
    ) -> tuple[
        list[tuple[str, bool, str]],
        dict[str, int],
        tuple[str, ...],
        tuple[tuple[str, str], ...],
        tuple[tuple[str, str], ...],
    ]:
        all_paths, tracked = _listed_paths(root, self._runner)
        excluded_paths = (*policy.exclude, *_child_policy_paths(root, policy))
        excluded, noise_files, vendors = _sift_paths(all_paths, tracked, policy, excluded_paths)

        vendor_folders = _fingerprint_vendors(vendors, root, excluded)

        kept = [
            rel for rel in all_paths
            if rel.endswith(".py") and not _is_skipped(rel, policy, tracked, excluded_paths)
        ]
        found: dict[str, tuple[str, bool, str]] = {}
        unclassified: list[tuple[str, str]] = []
        for rel in kept:
            _add_discovered(found, policy, rel)
            module = next((name for name, _, path in found.values() if path == rel), None)
            if module is not None and _is_unclassified(policy, module, rel):
                unclassified.append((module, rel))

        return (
            sorted(found.values()),
            excluded,
            tuple(sorted(noise_files)),
            tuple(sorted(unclassified)),
            tuple(vendor_folders),
        )


def _is_skipped(rel: str, policy: Policy, tracked: bool, excluded_paths: tuple[str, ...]) -> bool:
    return excluded_by_policy(rel, excluded_paths) or _noise_parent(rel, tracked) or _skipped_vendor(rel, policy) is not None


def _sift_paths(
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


def _child_policy_paths(root: Path, policy: Policy) -> tuple[str, ...]:
    if policy.child_policy_mode == "parent":
        return ()
    paths: list[str] = []
    for child in policy.children:
        child_root = root / child.path
        if (child_root / "arborescent.toml").is_file():
            paths.append(child.path)
            continue
        pyproject = child_root / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            paths.append(child.path)  # the child check reports its malformed config
        else:
            if isinstance(data.get("tool"), dict) and "arborescent" in data["tool"]:
                paths.append(child.path)
    return tuple(paths)
