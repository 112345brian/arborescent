"""Source discovery and content-hash cached fact extraction."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import NOISE_DIRS, Policy
from .facts import EffectUse, ModuleInfo, RawImport, SourceFacts, extract
from .ports import ToolRunner
from .tool_runner import SubprocessToolRunner

_PRUNE = NOISE_DIRS
_CACHE_VERSION = 5
_VENDOR_NAMES = frozenset({"vendor", "vendored", "third_party", "site-packages"})


def _under_sources(rel: str, sources: Sequence[str]) -> bool:
    for source in sources:
        source = source.strip("/")
        if source in ("", ".") or rel == source or rel.startswith(source + "/"):
            return True
    return False



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


def _facts_to_json(f: SourceFacts) -> list[Any]:
    imports = [[i.level, i.module, list(i.names), i.line, i.lazy, i.type_only] for i in f.imports]
    effects = [[u.line, u.category, u.target] for u in f.effect_uses]
    suppressions = [[line, kind] for line, kind in f.suppressions]
    return [
        imports, effects, None if f.script_deps is None else list(f.script_deps),
        suppressions, list(f.discard_calls), f.arbor_ring, f.arbor_error,
    ]


def _facts_from_json(raw: list[Any]) -> SourceFacts:
    imports = tuple(RawImport(i[0], i[1], tuple(i[2]), i[3], i[4], i[5]) for i in raw[0])
    effects = tuple(EffectUse(u[0], u[1], u[2]) for u in raw[1])
    suppressions = tuple((item[0], item[1]) for item in raw[3])
    return SourceFacts(
        imports, effects, None if raw[2] is None else tuple(raw[2]),
        suppressions, tuple(raw[4]), raw[5], raw[6],
    )


class FsScanner:
    """Walk the policy's sources, parse each file once, reuse facts for unchanged content."""

    def __init__(self, cache_dir: Path | None = None, runner: ToolRunner | None = None) -> None:
        self._cache_dir = cache_dir
        self._runner = runner or SubprocessToolRunner()

    def uncached(self) -> FsScanner:
        return FsScanner(None, self._runner)

    def scan(self, policy: Policy, root: Path, only: frozenset[str] | None = None) -> ScanData:
        cache_file = self._cache_dir / "scan.json" if self._cache_dir else None
        cache: dict[str, list[Any]] = {}
        if cache_file and cache_file.is_file():
            try:
                loaded = json.loads(cache_file.read_text(encoding="utf-8"))
                if loaded.get("v") == _CACHE_VERSION:
                    cache = loaded["files"]
            except (OSError, ValueError, KeyError):
                cache = {}
        files, excluded, noise_files, unclassified, vendor_folders = self._discover(policy, root)
        known = frozenset(m for m, _, _ in files)
        annotations_enabled = "arbor-annotations" not in policy.disabled_tools
        arbor_index: dict[str, tuple[str | None, str | None]] = {}
        ring_names: set[str] = {ring.name for ring in policy.rings} if annotations_enabled else set()
        wanted = {
            m
            for m, _, rel in files
            if (only is None or rel in only or m in policy.entry_scripts)
            and (policy.ring_of(m) is not None or _under_sources(rel, policy.sources))
        }
        modules: dict[str, ModuleInfo] = {}
        paths: dict[str, str] = {}
        parsed = 0
        dirty = False
        fresh: dict[str, list[Any]] = {}
        for module, is_pkg, rel in files:
            paths[module] = rel
            if not annotations_enabled and module not in wanted:
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
                dirty = True
            modules[module] = ModuleInfo(module, rel, is_pkg, facts)
        if cache_file and (dirty or (only is None and len(fresh) != len(cache))):
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            merged = fresh if only is None else {**cache, **fresh}
            _ = cache_file.write_text(json.dumps({"v": _CACHE_VERSION, "files": merged}), encoding="utf-8")
        if annotations_enabled:
            # This pass covers every discovered file, including unchanged import
            # targets and modules outside both classify and sources.
            arbor_index = {
                module: (info.facts.arbor_ring, info.facts.arbor_error)
                for module, info in modules.items()
            }
            wanted.update(
                module for module, _, rel in files
                if arbor_index[module][0] in ring_names and arbor_index[module][1] is None
                and (only is None or rel in only or module in policy.entry_scripts)
            )
            modules = {module: info for module, info in modules.items() if module in wanted}
            unclassified = tuple(
                item for item in unclassified
                if arbor_index[item[0]][0] not in ring_names or arbor_index[item[0]][1] is not None
            )
        entry = {s: modules[s].facts.script_deps for s in policy.entry_scripts if s in modules}
        if only is not None:
            unclassified = tuple(item for item in unclassified if item[1] in only or item[0] in only)
            noise_files = tuple(path for path in noise_files if path in only)
        return ScanData(known, modules, entry, paths, parsed, excluded, noise_files, unclassified, vendor_folders, arbor_index)

    def _discover(
        self, policy: Policy, root: Path
    ) -> tuple[
        list[tuple[str, bool, str]],
        dict[str, int],
        tuple[str, ...],
        tuple[tuple[str, str], ...],
        tuple[tuple[str, str], ...],
    ]:
        roots = sorted((r.strip("/") for r in policy.source_roots), key=len, reverse=True)
        found: dict[str, tuple[str, bool, str]] = {}
        excluded: dict[str, int] = {}
        noise_files: list[str] = []
        unclassified: list[tuple[str, str]] = []
        vendors: dict[str, list[str]] = {}

        def excluded_by_policy(rel: str) -> bool:
            parts = rel.split("/")
            ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]
            return any(
                fnmatch.fnmatch(rel, pattern)
                or fnmatch.fnmatch(Path(rel).name, pattern)
                or any(fnmatch.fnmatch(parent, pattern) or fnmatch.fnmatch(parent.rsplit("/", 1)[-1], pattern) for parent in ancestors)
                for pattern in policy.exclude
            )

        def excluded_folder(rel: str) -> str:
            parts = rel.split("/")[:-1]
            for i in range(len(parts), 0, -1):
                candidate = "/".join(parts[:i])
                if any(
                    fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(parts[i - 1], pattern)
                    for pattern in policy.exclude
                ):
                    return candidate
            return Path(rel).parent.as_posix()

        def add(rel: str) -> None:
            base = next((r for r in roots if r in ("", ".") or rel.startswith(r + "/")), ".")
            sub = rel if base in ("", ".") else rel[len(base) + 1 :]
            parts = sub.removesuffix(".py").split("/")
            is_pkg = parts[-1] == "__init__"
            if is_pkg:
                parts = parts[:-1]
            if parts:
                module = ".".join(parts)
                found[module] = (module, is_pkg, rel)

        git_listing = (
            self._runner.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], root)
            if self._runner.available("git")
            else None
        )
        if git_listing is not None and git_listing.returncode == 0:
            candidates = git_listing.stdout.split("\0") if "\0" in git_listing.stdout else git_listing.stdout.splitlines()
            all_paths = []
            for candidate in candidates:
                rel = candidate.replace("\\", "/").removeprefix("./")
                if rel and (root / rel).is_file():
                    all_paths.append(rel)
            all_paths = sorted(set(all_paths))
            tracked = True
        else:
            all_paths = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in _PRUNE)
                here = Path(dirpath).relative_to(root).as_posix()
                all_paths.extend(f"{here}/{name}".removeprefix("./") for name in filenames)
            all_paths.sort()
            tracked = False

        def noise_parent(rel: str) -> bool:
            return tracked and any(part in _PRUNE for part in rel.split("/"))

        def vendor_parent(rel: str) -> str | None:
            parts = rel.split("/")[:-1]
            try:
                noise_index = next(i for i, part in enumerate(parts) if part in _PRUNE)
            except StopIteration:
                noise_index = len(parts)
            for i in range(noise_index - 1, -1, -1):
                if parts[i] in {"vendor", "vendored", "third_party", "site-packages"}:
                    candidate = "/".join(parts[: i + 1])
                    # A vendor folder inside another vendor folder is covered by the outer tree's fingerprint.
                    if any(part in {"vendor", "vendored", "third_party", "site-packages"} for part in parts[:i]):
                        continue
                    return candidate
            return None

        for rel in all_paths:
            if excluded_by_policy(rel):
                if rel.endswith(".py"):
                    folder = excluded_folder(rel)
                    excluded[folder] = excluded.get(folder, 0) + 1
                continue
            if noise_parent(rel):
                if rel.endswith(".py"):
                    noise_files.append(rel)
                continue
            vendor = vendor_parent(rel)
            if vendor is not None:
                vendors.setdefault(vendor, []).append(rel)

        vendor_folders: list[tuple[str, str]] = []
        for folder, contents in sorted(vendors.items()):
            digest = hashlib.blake2b(digest_size=16)
            for rel in sorted(contents):
                relative = rel[len(folder) + 1 :]
                data = (root / rel).read_bytes()
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
            vendor_folders.append((folder, digest.hexdigest()))
            py_count = sum(rel.endswith(".py") for rel in contents)
            if py_count:
                excluded[folder] = excluded.get(folder, 0) + py_count

        py_paths = [rel for rel in all_paths if rel.endswith(".py")]
        for rel in py_paths:
            if excluded_by_policy(rel) or noise_parent(rel) or vendor_parent(rel) is not None:
                continue
            add(rel)
            module = next((name for name, _, path in found.values() if path == rel), None)
            if module is not None and policy.ring_of(module) is None and not _under_sources(rel, policy.sources):
                unclassified.append((module, rel))

        return (
            sorted(found.values()),
            excluded,
            tuple(sorted(noise_files)),
            tuple(sorted(unclassified)),
            tuple(vendor_folders),
        )
