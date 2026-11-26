"""Source discovery and content-hash cached fact extraction: the effectful half (listing, reading and fingerprinting files, the cache file).

What each path means lives in ``source_discovery`` and the cache record layout in ``scan_cache_codec``; both are pure."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .declaration_facts import DEV_TOOL_ROLE
from .domain import Policy
from .facts import ModuleInfo, SourceFacts, extract, link_shared_state
from .path_exclusion import excluded_by_policy
from .ports import ToolRunner
from .scan_cache_codec import CACHE_VERSION, cached_facts, facts_to_json
from .source_discovery import (
    PRUNE,
    add_discovered,
    annotated_modules,
    entry_scripts,
    is_enforced_source,
    is_selected,
    is_skipped,
    is_unclassified,
    misplaced_dev_tools,
    patch_test_files,
    policies,
    sift_paths,
    still_unclassified,
)
from .tool_runner import SubprocessToolRunner


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
        dirnames[:] = sorted(name for name in dirnames if name not in PRUNE)
        here = Path(directory).relative_to(root).as_posix()
        paths.extend(f"{here}/{name}".removeprefix("./") for name in filenames)
    return sorted(paths), False


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
    excluded_modules: dict[str, ModuleInfo] = field(default_factory=lambda: {})  # files under `exclude`, parsed only for `mixed-purity` (#173)
    test_facts: dict[str, SourceFacts] = field(default_factory=lambda: {})  # path -> facts of the `unreachable-patch` tests folder (#226)


def _load_cache(cache_file: Path | None) -> dict[str, list[Any]]:
    if not (cache_file and cache_file.is_file()):
        return {}
    try:
        loaded = json.loads(cache_file.read_text(encoding="utf-8"))
        return loaded["files"] if loaded.get("v") == CACHE_VERSION else {}
    except (OSError, ValueError, KeyError):
        return {}


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
        cached = cached_facts(hit) if hit is not None and hit[0] == digest else None
        if cached is not None and hit is not None:
            facts = cached
            fresh[rel] = hit
        else:
            facts = extract(data.decode("utf-8-sig"), rel)
            fresh[rel] = [digest, facts_to_json(facts)]
            parsed += 1
        modules[module] = ModuleInfo(module, rel, is_pkg, facts)
    return modules, paths, fresh, parsed


def _store_cache(
    cache_file: Path | None, cache: dict[str, list[Any]], fresh: dict[str, list[Any]], only: frozenset[str] | None, parsed: int
) -> None:
    if cache_file and (parsed > 0 or (only is None and len(fresh) != len(cache))):
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        merged = fresh if only is None else {**cache, **fresh}
        _ = cache_file.write_text(json.dumps({"v": CACHE_VERSION, "files": merged}), encoding="utf-8")


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
        files, excluded, noise_files, unclassified, vendor_folders, excluded_files = self._discover(policy, root)
        known = frozenset(m for m, _, _ in files)
        scope_by_module = {
            module: scope.name
            for module, _, rel in files
            if (scope := policy.scope_for_path(rel)) is not None
        }
        annotations_enabled = "arbor-annotations" not in policy.disabled_tools
        arbor_index: dict[str, tuple[str | None, str | None]] = {}
        ring_names: set[str] = {
            ring.name for local_policy in policies(policy) for ring in local_policy.rings
        } if annotations_enabled else set()
        scripts = entry_scripts(policy)
        wanted = {
            m
            for m, _, rel in files
            if is_selected(only, scripts, m, rel) and is_enforced_source(policy, m, rel)
        }
        dev_tool_modules = frozenset(m for m, _, rel in files if policy.is_dev_tool_path(rel))
        wanted |= {m for m, _, rel in files if m in dev_tool_modules and (only is None or rel in only)}
        modules, paths, fresh, parsed = _parse_files(root, files, cache, None if annotations_enabled else wanted)
        modules = link_shared_state(modules)  # before `--changed` narrows the set: the container's module may be unchanged (#194)
        excluded_modules, extra_fresh, extra_parsed = (
            _excluded_modules(root, excluded_files, cache) if only is None and policy.mixed_purity.enabled and policy.mixed_purity.report_excluded else ({}, {}, 0)
        )
        test_facts, test_fresh, test_parsed = (
            self._test_facts(policy, root, modules, {**cache, **fresh, **extra_fresh}) if only is None else ({}, {}, 0)
        )
        _store_cache(cache_file, cache, {**fresh, **extra_fresh, **test_fresh}, only, parsed + extra_parsed + test_parsed)
        misplaced: tuple[tuple[str, str], ...] = ()
        if annotations_enabled:
            tool_annotations = {name for name, info in modules.items() if info.facts.arbor_role == DEV_TOOL_ROLE}
            misplaced = misplaced_dev_tools(files, tool_annotations, dev_tool_modules)
            # This pass covers every discovered file, including unchanged import
            # targets and modules outside both classify and sources.
            arbor_index = {
                module: (info.facts.arbor_ring, info.facts.arbor_error)
                for module, info in modules.items()
            }
            wanted |= annotated_modules(files, arbor_index, ring_names, lambda module, rel: is_selected(only, scripts, module, rel))
            modules = {module: info for module, info in modules.items() if module in wanted}
            unclassified = still_unclassified(policy, unclassified, arbor_index, ring_names)
        entry = {s: modules[s].facts.script_deps for s in scripts if s in modules}
        if only is not None:
            unclassified = tuple(item for item in unclassified if item[1] in only or item[0] in only)
            noise_files = tuple(path for path in noise_files if path in only)
        return ScanData(
            known, modules, entry, paths, parsed, excluded, noise_files, unclassified,
            vendor_folders, arbor_index, scope_by_module, dev_tool_modules, misplaced, excluded_modules, test_facts,
        )

    def _test_facts(
        self, policy: Policy, root: Path, modules: dict[str, ModuleInfo], cache: dict[str, list[Any]],
    ) -> tuple[dict[str, SourceFacts], dict[str, list[Any]], int]:
        """Facts of every file under the `unreachable-patch` tests folder: reused when scanned, else parsed once and cached."""
        if not policy.practices.get("unreachable-patch"):
            return {}, {}, 0
        wanted = patch_test_files(policy, _listed_paths(root, self._runner)[0])
        scanned = {info.path: info.facts for info in modules.values()}
        others, fresh, parsed = _excluded_modules(root, tuple(rel for rel in wanted if rel not in scanned), cache)
        facts = {info.path: info.facts for info in others.values()}
        facts.update({rel: scanned[rel] for rel in wanted if rel in scanned})
        return facts, fresh, parsed

    def _discover(
        self, policy: Policy, root: Path
    ) -> tuple[
        list[tuple[str, bool, str]],
        dict[str, int],
        tuple[str, ...],
        tuple[tuple[str, str], ...],
        tuple[tuple[str, str], ...],
        tuple[str, ...],
    ]:
        all_paths, tracked = _listed_paths(root, self._runner)
        child_paths = _child_policy_paths(root, policy)
        excluded_paths = (*policy.exclude, *child_paths)
        excluded, noise_files, vendors = sift_paths(all_paths, tracked, policy, excluded_paths)
        excluded_files = tuple(sorted(  # what `exclude` hides; a child package's own policy owns its source, so it is not "excluded"
            rel for rel in all_paths
            if rel.endswith(".py") and excluded_by_policy(rel, policy.exclude) and not excluded_by_policy(rel, child_paths)
        ))

        vendor_folders = _fingerprint_vendors(vendors, root, excluded)

        kept = [
            rel for rel in all_paths
            if rel.endswith(".py") and not is_skipped(rel, policy, tracked, excluded_paths)
        ]
        found: dict[str, tuple[str, bool, str]] = {}
        unclassified: list[tuple[str, str]] = []
        for rel in kept:
            add_discovered(found, policy, rel)
            module = next((name for name, _, path in found.values() if path == rel), None)
            if module is not None and is_unclassified(policy, module, rel):
                unclassified.append((module, rel))

        return (
            sorted(found.values()),
            excluded,
            tuple(sorted(noise_files)),
            tuple(sorted(unclassified)),
            tuple(vendor_folders),
            excluded_files,
        )


def _excluded_modules(
    root: Path, rels: tuple[str, ...], cache: dict[str, list[Any]],
) -> tuple[dict[str, ModuleInfo], dict[str, list[Any]], int]:
    """Facts for files outside the scan: what the policy excludes, so ``mixed-purity`` can say what the exclusion hides (#173),
    and the tests ``unreachable-patch`` reads (#226).

    They are never ruled on, only assessed; a file that does not parse is skipped, since excluded folders hold fixtures
    and generated code on purpose. Same content-hash cache as the scanned files, keyed by path."""
    modules: dict[str, ModuleInfo] = {}
    fresh: dict[str, list[Any]] = {}
    parsed = 0
    for rel in rels:
        try:
            data = (root / rel).read_bytes()
        except OSError:
            continue
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        hit = cache.get(rel)
        cached = cached_facts(hit) if hit is not None and hit[0] == digest else None
        if cached is not None and hit is not None:
            facts = cached
            fresh[rel] = hit
        else:
            try:
                facts = extract(data.decode("utf-8-sig"), rel)
            except (SyntaxError, ValueError, RecursionError):
                continue
            fresh[rel] = [digest, facts_to_json(facts)]
            parsed += 1
        parts = rel[:-3].split("/")
        is_package = parts[-1] == "__init__"
        name = ".".join(parts[:-1] if is_package else parts) or "__init__"
        modules[name] = ModuleInfo(name, rel, is_package, facts)
    return modules, fresh, parsed


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
