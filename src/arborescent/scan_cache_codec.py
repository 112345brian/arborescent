"""The scan cache's record layout: ``SourceFacts`` to JSON-ready lists and back, and the version that names the layout.

Pure, split out of ``scanner`` so the adapter holds only the cache file's reading and writing. ``CACHE_VERSION`` is bumped
by hand whenever the layout changes; ``cached_facts`` still reads a record of another layout as a miss, never a crash.
"""

from __future__ import annotations

from typing import Any

from .call_facts import FunctionCalls
from .definition_facts import ClassShape, TopLevelDef
from .effect_facts import EffectUse
from .facts import Forwarder, PurityFacts, PurityUnit, SourceFacts
from .import_syntax import RawImport

__all__ = ["CACHE_VERSION", "cached_facts", "facts_from_json", "facts_to_json"]

CACHE_VERSION = 25


def _purity_to_json(p: PurityFacts) -> list[Any]:
    units = [
        [u.name, u.line, u.end_line, u.body_lines, u.is_class, u.logic, [[line, kind] for line, kind in u.kinds], list(u.calls)]
        for u in p.units
    ]
    return [units, [[line, kind] for line, kind in p.import_time]]


def _purity_from_json(raw: list[Any]) -> PurityFacts:
    units = tuple(
        PurityUnit(u[0], u[1], u[2], u[3], u[4], u[5], tuple((item[0], item[1]) for item in u[6]), tuple(u[7])) for u in raw[0]
    )
    return PurityFacts(units, tuple((item[0], item[1]) for item in raw[1]))


def facts_to_json(f: SourceFacts) -> list[Any]:
    imports = [[i.level, i.module, list(i.names), i.line, i.lazy, i.type_only] for i in f.imports]
    effects = [[u.line, u.category, u.target, u.seam] for u in f.effect_uses]
    suppressions = [[line, kind] for line, kind in f.suppressions]
    top_level_defs = [[d.name, d.line, d.is_class, d.is_abstract] for d in f.top_level_defs]
    return [
        imports, effects, None if f.script_deps is None else list(f.script_deps),
        suppressions, list(f.discard_calls), top_level_defs, f.arbor_ring, f.arbor_error, list(f.file_refs), f.main_guard, f.arbor_role,
        [[line, value] for line, value in f.string_literals],
        [[w.name, w.line, w.level, w.module, w.imported, w.member] for w in f.forwarders],
        [list(item) for item in f.weak_port_returns],
        f.surface_size, f.surface_line, [list(item) for item in f.reexports],
        [[line, reason] for line, reason in f.deferred],
        [[line, name] for line, name in f.calls],
        list(f.result_defs), [[line, target] for line, target in f.bare_calls],
        [[c.name, c.line, list(c.calls)] for c in f.function_calls],
        f.module_doc,
        f.arbor_reason, [[line, target] for line, target in f.dynamic_imports],
        _purity_to_json(f.purity), f.untokenizable,
        [[c.name, c.line, list(c.bases), list(c.members), c.protocol, c.abstract] for c in f.classes],
    ]


def cached_facts(hit: list[Any]) -> SourceFacts | None:
    """The facts of a cache record, or ``None`` when the record does not fit this layout: a record is a miss, never a crash.

    The version number is bumped by hand, so a record written by another branch's layout can carry the current version."""
    try:
        return facts_from_json(hit[1])
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return None


def facts_from_json(raw: list[Any]) -> SourceFacts:
    imports = tuple(RawImport(i[0], i[1], tuple(i[2]), i[3], i[4], i[5]) for i in raw[0])
    effects = tuple(EffectUse(u[0], u[1], u[2], u[3]) for u in raw[1])
    suppressions = tuple((item[0], item[1]) for item in raw[3])
    top_level_defs = tuple(TopLevelDef(d[0], d[1], d[2], d[3]) for d in raw[5])
    return SourceFacts(
        imports, effects, None if raw[2] is None else tuple(raw[2]),
        suppressions, tuple(raw[4]), top_level_defs, raw[6], raw[7], raw[10], tuple(raw[8]), raw[9],
        tuple((item[0], item[1]) for item in raw[11]),
        tuple(Forwarder(*item) for item in raw[12]),
        tuple((item[0], item[1], item[2]) for item in raw[13]),
        raw[14], raw[15], tuple((i[0], i[1], i[2], i[3]) for i in raw[16]),
        tuple((item[0], item[1]) for item in raw[17]),
        tuple((item[0], item[1]) for item in raw[18]),
        tuple(raw[19]), tuple((item[0], item[1]) for item in raw[20]),
        tuple(FunctionCalls(c[0], c[1], tuple(c[2])) for c in raw[21]),
        raw[22], raw[23], tuple((item[0], item[1]) for item in raw[24]),
        _purity_from_json(raw[25]), raw[26],
        tuple(ClassShape(c[0], c[1], tuple(c[2]), tuple(c[3]), c[4], c[5]) for c in raw[27]),
    )
