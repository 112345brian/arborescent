"""Resolve imported names to project modules for architecture and runtime checks.

Syntax extraction lives in `import_syntax` and dynamic import recognition lives in `call_facts`. Their original
import paths remain available here for callers, while the scanner and checks use the owning modules directly.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from .call_facts import dotted_name, dynamic_imports
from .import_syntax import RawImport, import_aliases, raw_imports

__all__ = [
    "RawImport", "dotted_name", "dynamic_imports", "import_aliases", "raw_imports", "resolve_runtime_targets", "resolve_targets",
]


def resolve_targets(
    module: str, is_package: bool, imp: RawImport, known: Collection[str], tops: Collection[str]
) -> tuple[list[str], list[str], str | None]:
    """Split one import into (internal module names, external top-level names, error).

    Internal names are dotted modules from ``known``; ``from . import a, b`` yields the submodules ``a`` and ``b``
    when they are modules, and ``from .m import x`` yields ``m``.

    ``from pkg import sub`` is an edge to ``pkg.sub`` alone, as ``from pkg.sub import x`` is; ``pkg`` is added only when some
    imported name is not a submodule (it is read off the package) or is ``*``. That is the question a ring check asks.
    """
    base, external, error = _import_base(module, is_package, imp, tops)
    return ([], external, error) if base is None else (_import_edges(base, imp, known, runtime_init=False), [], None)


def resolve_runtime_targets(
    module: str, is_package: bool, imp: RawImport, known: Collection[str], tops: Collection[str]
) -> tuple[list[str], list[str], str | None]:
    """Like ``resolve_targets``, but for what actually runs: importing ``pkg.sub`` also runs ``pkg/__init__``.

    Package splitting must carry that file with the modules that import its submodules, so ``from pkg import sub`` keeps the
    edge to ``pkg`` next to the one to ``pkg.sub``."""
    base, external, error = _import_base(module, is_package, imp, tops)
    return ([], external, error) if base is None else (_import_edges(base, imp, known, runtime_init=True), [], None)


def _import_base(module: str, is_package: bool, imp: RawImport, tops: Collection[str]) -> tuple[list[str] | None, list[str], str | None]:
    """The dotted path an import names, or (None, external names, error) when it is not first-party or cannot be resolved."""
    parts = module.split(".")
    pkg = parts if is_package else parts[:-1]
    if imp.level:
        if imp.level - 1 > len(pkg):
            return None, [], f"relative import climbs above the source root ({'.' * imp.level}{imp.module})"
        return pkg[: len(pkg) - (imp.level - 1)] + (imp.module.split(".") if imp.module else []), [], None
    base = imp.module.split(".")
    if base[0] not in tops:
        return None, [base[0]] if base[0] else [], None
    return base, [], None


def _import_edges(base: list[str], imp: RawImport, known: Collection[str], *, runtime_init: bool) -> list[str]:
    if not imp.names:  # plain `import a.b.c`
        return [_longest_known(base, known)]
    subs = [".".join([*base, n]) for n in imp.names if n != "*" and ".".join([*base, n]) in known]
    head = [_longest_known(base, known)] if base else []
    if runtime_init:
        return subs if not imp.module and subs else [*head, *subs]
    all_submodules = bool(subs) and len(subs) == len(imp.names)  # every name is a submodule: nothing is read off the package itself
    return ([] if all_submodules else head) + subs


def _longest_known(parts: Iterable[str], known: Collection[str]) -> str:
    items = list(parts)
    while items and ".".join(items) not in known:
        items.pop()
    return ".".join(items)
