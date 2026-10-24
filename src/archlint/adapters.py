"""The real world: subprocesses and the file system, with a content-hash cache of per-file facts."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import Policy, ToolResult
from .facts import ModuleInfo, RawImport, SourceFacts, extract

__all__ = ["FsScanner", "ScanData", "SubprocessToolRunner"]

_PRUNE = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".archlint", ".mypy_cache", ".ruff_cache", ".tox"}
_CACHE_VERSION = 2


class SubprocessToolRunner:
    """``ToolRunner`` over ``subprocess.run``; a missing executable is a result, not an exception."""

    def available(self, executable: str) -> bool:
        return shutil.which(executable) is not None

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        started = time.perf_counter()
        try:
            done = subprocess.run(
                list(argv), cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                stdin=subprocess.DEVNULL, check=False,
            )  # fmt: skip
        except FileNotFoundError:
            return ToolResult(None, "", f"{argv[0]}: not installed", time.perf_counter() - started)
        except subprocess.TimeoutExpired:
            return ToolResult(-1, "", f"{argv[0]}: timed out after {timeout}s", time.perf_counter() - started)
        return ToolResult(done.returncode, done.stdout, done.stderr, time.perf_counter() - started)


@dataclass(frozen=True)
class ScanData:
    known: frozenset[str]
    modules: dict[str, ModuleInfo]
    entry_deps: dict[str, tuple[str, ...] | None]
    paths: dict[str, str]
    parsed: int = 0  # files actually parsed this run (cache misses); 0 on a fully warm run


def _facts_to_json(f: SourceFacts) -> list[Any]:
    imports = [[i.level, i.module, list(i.names), i.line, i.lazy, i.type_only] for i in f.imports]
    return [imports, [list(u) for u in f.os_uses], None if f.script_deps is None else list(f.script_deps)]


def _facts_from_json(raw: list[Any]) -> SourceFacts:
    imports = tuple(RawImport(i[0], i[1], tuple(i[2]), i[3], i[4], i[5]) for i in raw[0])
    os_uses = tuple((u[0], u[1]) for u in raw[1])
    return SourceFacts(imports, os_uses, None if raw[2] is None else tuple(raw[2]))


class FsScanner:
    """Walk the policy's sources, parse each file once, reuse facts for unchanged content."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self._cache_dir = cache_dir

    def uncached(self) -> FsScanner:
        return FsScanner(None)

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
        files = self._discover(policy, root)
        known = frozenset(m for m, _, _ in files)
        wanted = {m for m, _, rel in files if only is None or rel in only or m in policy.entry_scripts}
        modules: dict[str, ModuleInfo] = {}
        paths: dict[str, str] = {}
        parsed = 0
        dirty = False
        fresh: dict[str, list[Any]] = {}
        for module, is_pkg, rel in files:
            paths[module] = rel
            if module not in wanted:
                continue
            data = (root / rel).read_bytes()
            digest = hashlib.blake2b(data, digest_size=16).hexdigest()
            hit = cache.get(rel)
            if hit and hit[0] == digest:
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
        entry = {s: modules[s].facts.script_deps for s in policy.entry_scripts if s in modules}
        return ScanData(known, modules, entry, paths, parsed)

    @staticmethod
    def _discover(policy: Policy, root: Path) -> list[tuple[str, bool, str]]:
        roots = sorted((r.strip("/") for r in policy.source_roots), key=len, reverse=True)
        found: dict[str, tuple[str, bool, str]] = {}

        def excluded(rel: str, name: str) -> bool:
            return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in policy.exclude)

        def add(rel: str) -> None:
            base = next((r for r in roots if r in ("", ".") or rel.startswith(r + "/")), ".")
            sub = rel if base in ("", ".") else rel[len(base) + 1 :]
            parts = sub.removesuffix(".py").split("/")
            is_pkg = parts[-1] == "__init__"
            if is_pkg:
                parts = parts[:-1]
            if parts:
                found[".".join(parts)] = (".".join(parts), is_pkg, rel)

        for source in policy.sources:
            start = (root / source).resolve()
            if start.is_file():
                add(start.relative_to(root.resolve()).as_posix())
                continue
            for dirpath, dirnames, filenames in os.walk(start):
                here = Path(dirpath).relative_to(root.resolve()).as_posix()
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d not in _PRUNE and not d.startswith(".") and not excluded(f"{here}/{d}".lstrip("./"), d)
                )  # fmt: skip
                for f in sorted(filenames):
                    rel = f"{here}/{f}".removeprefix("./")
                    if f.endswith(".py") and not excluded(rel, f):
                        add(rel)
        return sorted(found.values())
