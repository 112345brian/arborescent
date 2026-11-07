"""#98: function-level call-graph taint, a sharper companion to #103's module-level ``effect-via-import``.

``effect-via-import`` already tells you a module imports something effectful, at module granularity: if any
function anywhere in the imported module uses an I/O primitive, every importer is "tainted", whether or not it
ever calls that code. This module instead asks, per function: does *this* function call an I/O primitive, or
call a function (in this file or another) that does, through an actual, resolvable call expression?

Two stages, kept separate on purpose (see the module docstring on why ``classify_audit`` keeps its own
whole-repository pass distinct from the per-file scan):

1. :func:`_extract_file` — per file, cached by content hash in its own file (``taint.json``, alongside the
   scanner's ``scan.json``, same cache directory) rather than the scanner's cache: it depends only on
   ``(module, is_package, text, known)`` plus the active seed list, not on anything ring- or policy-shaped, so
   sharing a cache file with ``SourceFacts`` would mean invalidating on changes that do not affect this pass.
2. :func:`compute_taint` — whole repository: builds the call graph across files and iterates it to a fixed
   point. This is the "own cache, not per-file" pass the issue asks for; nothing here is baselined, and it is
   skipped under ``--changed`` by the caller, the same way ``classify_audit.audit_classification`` is.

What is and is not followed, by design (see "Non-goals" in #98):

- A call through an object of unknown static type (``self._fs.exists()``, a parameter, a return value) is not
  followed: there is no type inference here, so the receiver's class is unknown. This is the same limit
  ``effect-via-import`` already documents for ``helper.do_it()``.
- ``getattr``, decorators, and callbacks (a function passed as a value and invoked later through another name)
  are not followed: a function that is only ever handed to someone else is not marked tainted by that alone.
- Inheritance is not resolved: ``self.method()`` resolves only to a method defined directly on the enclosing
  class, not one inherited from a base class in the same or another module.
- Only module-level functions and the methods of module-level classes are tracked; a nested ``def`` inside a
  function, or a class defined inside a function, is invisible to this pass (and so is anything it calls).
- Constructor tracking (``x = C(); x.method()``) is a simple, same-function, left-to-right approximation: it
  sees a plain ``name = Call(...)`` and remembers what ``name`` was built from until the next time ``name`` is
  rebound; it does not follow the variable across branches, loops bodies that assign conditionally, or into
  another function.
- ``Path(...).read_text()`` and similar are real I/O but are a method call on the *result* of a call, not on an
  import alias; recognising them needs a type, which this module does not have. They are a known false
  negative, the same one ``facts._path_io_use`` documents for the module-level effect scan.
- Lazy (function-local) imports are not read as aliases; only module-level ``import``/``from ... import`` lines
  bind a name this pass will follow.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .facts import ModuleInfo, effect_category_of
from .presets import DEFAULT_TAINT_SEEDS

__all__ = ["DEFAULT_TAINT_SEEDS", "TaintResult", "TaintedFunction", "compute_taint"]

_CACHE_VERSION = 1

# Real I/O, but not a stdlib-wide `effect_category_of` match: only these specific submodules/functions count.
# `urllib.parse`, `http.HTTPStatus` and the rest of `urllib`/`http` are not network I/O (see the module docstring
# of facts.py's effect scan for the same rule applied to filesystem APIs).
_NETWORK_PREFIXES = ("urllib.request.", "http.client.", "http.server.", "socket.")
_NETWORK_EXACT = frozenset({"urllib.request.urlopen", "http.client.HTTPConnection", "http.client.HTTPSConnection"})

_SCOPE_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _is_network(target: str) -> bool:
    return target in _NETWORK_EXACT or target.startswith(_NETWORK_PREFIXES)


def _io_category(target: str, seeds: frozenset[str]) -> str | None:
    if "network" in seeds and _is_network(target):
        return "network"
    category = effect_category_of(target)
    return category if category is not None and category in seeds else None


def _dotted(node: ast.expr, aliases: Mapping[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value, aliases)
        return f"{parent}.{node.attr}" if parent is not None else None
    return None


def _body_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Every node in ``func``'s own scope, in source order, not descending into a nested def/class/lambda."""
    out: list[ast.AST] = []

    def walk(node: ast.AST) -> None:
        out.append(node)
        if isinstance(node, _SCOPE_BOUNDARIES):
            return
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in func.body:
        walk(stmt)
    return out


@dataclass(frozen=True)
class _CallCandidate:
    """An unresolved call: either ``module.name(...)`` (``cls`` is ``None``) or ``module.cls.name(...)``."""

    module: str
    name: str
    cls: str | None = None


@dataclass(frozen=True)
class _FuncInfo:
    line: int
    direct_hits: tuple[tuple[str, str], ...]  # (category, target) pairs matched directly in this function's body
    candidates: tuple[_CallCandidate, ...]


_NO_FUNCS: Mapping[str, _FuncInfo] = dict[str, _FuncInfo]()


@dataclass(frozen=True)
class _FileFacts:
    module: str
    funcs: Mapping[str, _FuncInfo]
    classes: Mapping[str, Mapping[str, _FuncInfo]]
    symbol_aliases: Mapping[str, tuple[str, str]]  # local name -> (origin module, origin name)
    module_aliases: Mapping[str, str]  # local name -> dotted module path


@dataclass(frozen=True)
class TaintedFunction:
    """One function the taint pass could show a path to an I/O primitive for."""

    module: str
    qualname: str  # "name" for a module-level function, "Class.method" for a method
    line: int
    chain: tuple[str, ...]  # func ids ("module:qualname"), itself first, down to the one with the direct hit


@dataclass(frozen=True)
class TaintResult:
    tainted: Mapping[str, TaintedFunction] = field(default_factory=dict)  # func id -> details, sorted by func id
    by_module: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # module -> sorted tainted qualnames


def _relative_base(module: str, is_package: bool, level: int, submodule: str | None) -> str | None:
    parts = module.split(".")
    pkg = parts if is_package else parts[:-1]
    if level - 1 > len(pkg):
        return None
    base = pkg[: len(pkg) - (level - 1)]
    if submodule is not None:
        base = base + submodule.split(".")
    return ".".join(base) if base else None


@dataclass(frozen=True)
class _FileContext:
    """Everything about one file's own module-level bindings that resolving a call inside it needs."""

    module: str
    symbol_aliases: Mapping[str, tuple[str, str]]  # local name -> (origin module, origin name)
    module_aliases: Mapping[str, str]  # local name -> dotted module path
    local_funcs: Collection[str]
    local_classes: Collection[str]
    known: Collection[str]

    @property
    def merged_aliases(self) -> dict[str, str]:
        """Every bound name as one dotted-string alias table, the shape ``_dotted`` wants."""
        return {**self.module_aliases, **{n: f"{m}.{o}" for n, (m, o) in self.symbol_aliases.items()}}


def _import_bindings(node: ast.Import, module_aliases: dict[str, str]) -> None:
    for alias in node.names:
        top = alias.name.split(".")[0]
        local = alias.asname if alias.asname is not None else top
        module_aliases[local] = alias.name if alias.asname is not None else top


def _import_from_bindings(
    node: ast.ImportFrom, base: str | None, known: Collection[str],
    symbol_aliases: dict[str, tuple[str, str]], module_aliases: dict[str, str],
) -> None:
    if base is None:
        return
    for alias in node.names:
        if alias.name == "*":
            continue
        local = alias.asname or alias.name
        candidate = f"{base}.{alias.name}"
        if candidate in known:
            module_aliases[local] = candidate
        else:
            symbol_aliases[local] = (base, alias.name)


def _module_bindings(module: str, is_package: bool, tree: ast.Module, known: Collection[str]) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """Module-level import bindings only: ``symbol_aliases`` (name read off a module) and ``module_aliases``
    (name bound to a module itself, which a later attribute call can resolve through)."""
    symbol_aliases: dict[str, tuple[str, str]] = {}
    module_aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            _import_bindings(node, module_aliases)
        elif isinstance(node, ast.ImportFrom):
            base = node.module if node.level == 0 else _relative_base(module, is_package, node.level, node.module)
            _import_from_bindings(node, base, known, symbol_aliases, module_aliases)
    return symbol_aliases, module_aliases


def _resolve_symbol_ref(name: str, ctx: _FileContext) -> tuple[str, str] | None:
    if name in ctx.local_funcs or name in ctx.local_classes:
        return (ctx.module, name)
    return ctx.symbol_aliases.get(name)


def _attribute_call_target(obj: ast.expr, attr: str, owning_class: str | None, var_classes: Mapping[str, tuple[str, str]], ctx: _FileContext) -> _CallCandidate | None:
    if isinstance(obj, ast.Name) and obj.id == "self" and owning_class is not None:
        return _CallCandidate(ctx.module, attr, cls=owning_class)
    if isinstance(obj, ast.Name) and obj.id in var_classes:
        ref_module, ref_name = var_classes[obj.id]
        return _CallCandidate(ref_module, attr, cls=ref_name)
    if isinstance(obj, ast.Name) and ctx.module_aliases.get(obj.id) in ctx.known:
        return _CallCandidate(ctx.module_aliases[obj.id], attr)
    if isinstance(obj, ast.Attribute):
        dotted = _dotted(obj, ctx.merged_aliases)
        if dotted is not None and dotted in ctx.known:
            return _CallCandidate(dotted, attr)
    return None


def _call_target(call: ast.Call, owning_class: str | None, var_classes: Mapping[str, tuple[str, str]], ctx: _FileContext) -> _CallCandidate | None:
    func = call.func
    if isinstance(func, ast.Name):
        ref = _resolve_symbol_ref(func.id, ctx)
        return None if ref is None else _CallCandidate(ref[0], ref[1])
    if isinstance(func, ast.Attribute):
        return _attribute_call_target(func.value, func.attr, owning_class, var_classes, ctx)
    return None


def _bound_names(node: ast.AST) -> list[str]:
    """Plain ``Name`` targets a statement rebinds, for invalidating stale constructor tracking."""
    if isinstance(node, ast.Assign):
        return [n.id for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)]
    if isinstance(node, ast.AugAssign | ast.For) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, ast.With):
        return [item.optional_vars.id for item in node.items if isinstance(item.optional_vars, ast.Name)]
    return []


def _simple_constructor_assign(node: ast.Assign, ctx: _FileContext) -> tuple[str, tuple[str, str]] | None:
    if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name) or not isinstance(node.value, ast.Call):
        return None
    func = node.value.func
    if not isinstance(func, ast.Name):
        return None
    ref = _resolve_symbol_ref(func.id, ctx)
    return None if ref is None else (node.targets[0].id, ref)


def _track_constructor(node: ast.AST, ctx: _FileContext, var_classes: dict[str, tuple[str, str]]) -> None:
    assigned = _simple_constructor_assign(node, ctx) if isinstance(node, ast.Assign) else None
    if assigned is not None:
        var_classes[assigned[0]] = assigned[1]
        return
    for name in _bound_names(node):
        var_classes.pop(name, None)


def _direct_hit(node: ast.Call, ctx: _FileContext, seeds: frozenset[str]) -> tuple[str, str] | None:
    dotted = _dotted(node.func, ctx.merged_aliases)
    category = _io_category(dotted, seeds) if dotted is not None else None
    return None if category is None or dotted is None else (category, dotted)


def _analyze_function(func: ast.FunctionDef | ast.AsyncFunctionDef, owning_class: str | None, ctx: _FileContext, seeds: frozenset[str]) -> _FuncInfo:
    var_classes: dict[str, tuple[str, str]] = {}
    hits: list[tuple[str, str]] = []
    candidates: list[_CallCandidate] = []
    for node in _body_nodes(func):
        _track_constructor(node, ctx, var_classes)
        if not isinstance(node, ast.Call):
            continue
        hit = _direct_hit(node, ctx, seeds)
        if hit is not None:
            hits.append(hit)
        target = _call_target(node, owning_class, var_classes, ctx)
        if target is not None:
            candidates.append(target)
    return _FuncInfo(func.lineno, tuple(sorted(set(hits))), tuple(candidates))


def _extract_file(module: str, is_package: bool, text: str, known: Collection[str], seeds: frozenset[str]) -> _FileFacts | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    symbol_aliases, module_aliases = _module_bindings(module, is_package, tree, known)
    local_funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    local_classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    ctx = _FileContext(module, symbol_aliases, module_aliases, local_funcs, local_classes, known)
    funcs = {
        n.name: _analyze_function(n, None, ctx, seeds)
        for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    classes = {
        n.name: {
            m.name: _analyze_function(m, n.name, ctx, seeds)
            for m in n.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        for n in tree.body if isinstance(n, ast.ClassDef)
    }
    return _FileFacts(module, funcs, classes, symbol_aliases, module_aliases)


def _func_info_to_json(fi: _FuncInfo) -> list[Any]:
    return [fi.line, [[category, target] for category, target in fi.direct_hits], [[c.module, c.name, c.cls] for c in fi.candidates]]


def _func_info_from_json(raw: list[Any]) -> _FuncInfo:
    hits = tuple((category, target) for category, target in raw[1])
    candidates = tuple(_CallCandidate(c[0], c[1], c[2]) for c in raw[2])
    return _FuncInfo(raw[0], hits, candidates)


def _file_facts_to_json(ff: _FileFacts) -> list[Any]:
    return [
        ff.module,
        {name: _func_info_to_json(fi) for name, fi in ff.funcs.items()},
        {cls: {name: _func_info_to_json(fi) for name, fi in methods.items()} for cls, methods in ff.classes.items()},
        {name: [origin[0], origin[1]] for name, origin in ff.symbol_aliases.items()},
        dict(ff.module_aliases),
    ]


def _file_facts_from_json(raw: list[Any]) -> _FileFacts:
    funcs = {name: _func_info_from_json(fi) for name, fi in raw[1].items()}
    classes = {cls: {name: _func_info_from_json(fi) for name, fi in methods.items()} for cls, methods in raw[2].items()}
    symbol_aliases = {name: (origin[0], origin[1]) for name, origin in raw[3].items()}
    return _FileFacts(raw[0], funcs, classes, symbol_aliases, dict(raw[4]))


def _load_file_cache(cache_file: Path | None, seeds_key: str) -> dict[str, list[Any]]:
    """Content-hash cache of per-file facts, keyed by module; wholesale-invalidated if the seed list changed."""
    if not (cache_file and cache_file.is_file()):
        return {}
    try:
        loaded = json.loads(cache_file.read_text(encoding="utf-8"))
        if loaded.get("v") != _CACHE_VERSION or loaded.get("seeds") != seeds_key:
            return {}
        return loaded["files"]
    except (OSError, ValueError, KeyError):
        return {}


def _store_file_cache(cache_file: Path | None, seeds_key: str, fresh: dict[str, list[Any]]) -> None:
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"v": _CACHE_VERSION, "seeds": seeds_key, "files": fresh}), encoding="utf-8")


def _extract_files_cached(
    modules: Mapping[str, ModuleInfo], sources: Mapping[str, str], known: Collection[str],
    seeds: frozenset[str], cache: Mapping[str, list[Any]],
) -> tuple[dict[str, _FileFacts], dict[str, list[Any]]]:
    """Reuse a module's cached facts when its text digest still matches; re-extract (and re-cache) otherwise."""
    files: dict[str, _FileFacts] = {}
    fresh: dict[str, list[Any]] = {}
    for name, info in modules.items():
        text = sources.get(name)
        if text is None:
            continue
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        hit = cache.get(name)
        ff = _file_facts_from_json(hit[1]) if hit is not None and hit[0] == digest else _extract_file(name, info.is_package, text, known, seeds)
        if ff is not None:
            files[name] = ff
            fresh[name] = [digest, _file_facts_to_json(ff)]
    return files, fresh


@dataclass(frozen=True)
class _ModuleIndex:
    funcs: Mapping[str, str]  # name -> func id
    classes: Mapping[str, Mapping[str, str]]  # class -> method -> func id


def _build_index(files: Mapping[str, _FileFacts]) -> dict[str, _ModuleIndex]:
    return {
        module: _ModuleIndex(
            {name: f"{module}:{name}" for name in ff.funcs},
            {cls: {m: f"{module}:{cls}.{m}" for m in methods} for cls, methods in ff.classes.items()},
        )
        for module, ff in files.items()
    }


def _resolve_symbol(
    files: Mapping[str, _FileFacts], index: Mapping[str, _ModuleIndex], module: str, name: str, visited: frozenset[tuple[str, str]],
) -> str | None:
    key = (module, name)
    if key in visited:
        return None
    idx = index.get(module)
    if idx is not None:
        if name in idx.funcs:
            return idx.funcs[name]
        if name in idx.classes:
            return idx.classes[name].get("__init__")
    ff = files.get(module)
    alias = ff.symbol_aliases.get(name) if ff is not None else None
    return None if alias is None else _resolve_symbol(files, index, alias[0], alias[1], visited | {key})


def _resolve_class(
    files: Mapping[str, _FileFacts], index: Mapping[str, _ModuleIndex], module: str, cls: str, visited: frozenset[tuple[str, str]],
) -> tuple[str, str] | None:
    key = (module, cls)
    if key in visited:
        return None
    idx = index.get(module)
    if idx is not None and cls in idx.classes:
        return (module, cls)
    ff = files.get(module)
    alias = ff.symbol_aliases.get(cls) if ff is not None else None
    return None if alias is None else _resolve_class(files, index, alias[0], alias[1], visited | {key})


_NO_METHODS: Mapping[str, str] = dict[str, str]()


def _resolve_candidate(files: Mapping[str, _FileFacts], index: Mapping[str, _ModuleIndex], candidate: _CallCandidate) -> str | None:
    if candidate.cls is None:
        return _resolve_symbol(files, index, candidate.module, candidate.name, frozenset())
    resolved = _resolve_class(files, index, candidate.module, candidate.cls, frozenset())
    if resolved is None:
        return None
    idx = index.get(resolved[0])
    if idx is None:
        return None
    return idx.classes.get(resolved[1], _NO_METHODS).get(candidate.name)


def _build_graph(
    files: Mapping[str, _FileFacts], index: Mapping[str, _ModuleIndex],
) -> tuple[dict[str, frozenset[str]], dict[str, tuple[tuple[str, str], ...]]]:
    graph: dict[str, frozenset[str]] = {}
    direct: dict[str, tuple[tuple[str, str], ...]] = {}

    def record(fid: str, info: _FuncInfo) -> None:
        resolved = {t for c in info.candidates if (t := _resolve_candidate(files, index, c)) is not None and t != fid}
        graph[fid] = frozenset(resolved)
        direct[fid] = info.direct_hits

    for module, ff in files.items():
        module_idx = index[module]
        for name, info in ff.funcs.items():
            record(module_idx.funcs[name], info)
        for cls, methods in ff.classes.items():
            for name, info in methods.items():
                record(module_idx.classes[cls][name], info)
    return graph, direct


def _propagate(graph: Mapping[str, frozenset[str]], direct: Mapping[str, tuple[tuple[str, str], ...]]) -> dict[str, tuple[str, ...]]:
    memo: dict[str, tuple[str, ...] | None] = {}
    visiting: set[str] = set()

    def compute(fid: str) -> tuple[str, ...] | None:
        if fid in memo:
            return memo[fid]
        if fid in visiting:
            return None
        visiting.add(fid)
        result: tuple[str, ...] | None = (fid,) if direct.get(fid, ()) else None
        if result is None:
            for target in sorted(graph.get(fid, frozenset())):
                sub = compute(target)
                if sub is not None:
                    result = (fid, *sub)
                    break
        visiting.discard(fid)
        memo[fid] = result
        return result

    for fid in sorted(graph):
        compute(fid)
    return {fid: chain for fid, chain in memo.items() if chain is not None}


def _line_of(files: Mapping[str, _FileFacts], module: str, qualname: str) -> int:
    ff = files.get(module)
    if ff is None:
        return 0
    if "." not in qualname:
        info = ff.funcs.get(qualname)
        return info.line if info is not None else 0
    cls, _, method = qualname.partition(".")
    info = ff.classes.get(cls, _NO_FUNCS).get(method)
    return info.line if info is not None else 0


def compute_taint(
    modules: Mapping[str, ModuleInfo], sources: Mapping[str, str], known: Collection[str], seeds: Collection[str] = DEFAULT_TAINT_SEEDS,
    cache_file: Path | None = None,
) -> TaintResult:
    """Whole-repository pass: which functions call an I/O primitive directly, or call (through a resolvable
    expression) a function that does, iterated to a fixed point. ``sources`` is the file text for every module
    in ``modules`` this pass can read; a module missing from it, or one that fails to parse, contributes no
    facts and no edges (it is simply not in the call graph), never an error. ``cache_file``, when given, skips
    re-parsing and re-walking a module's AST when its text has not changed since the last run.
    """
    seed_set = frozenset(seeds)
    seeds_key = ",".join(sorted(seed_set))
    cache = _load_file_cache(cache_file, seeds_key)
    files, fresh = _extract_files_cached(modules, sources, known, seed_set, cache)
    _store_file_cache(cache_file, seeds_key, fresh)
    index = _build_index(files)
    graph, direct = _build_graph(files, index)
    tainted = _propagate(graph, direct)
    details: dict[str, TaintedFunction] = {}
    by_module: dict[str, list[str]] = {}
    for fid, chain in tainted.items():
        module, qualname = fid.split(":", 1)
        details[fid] = TaintedFunction(module, qualname, _line_of(files, module, qualname), chain)
        by_module.setdefault(module, []).append(qualname)
    return TaintResult(
        tainted=dict(sorted(details.items())),
        by_module={module: tuple(sorted(names)) for module, names in sorted(by_module.items())},
    )
