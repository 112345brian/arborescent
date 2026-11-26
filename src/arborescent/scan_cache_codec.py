"""The scan cache's record layout: ``SourceFacts`` to JSON-ready lists and back, and the version that names the layout.

Pure, split out of ``scanner`` so the adapter holds only the cache file's reading and writing. ``CACHE_VERSION`` is bumped
by hand whenever the layout changes; ``cached_facts`` still reads a record of another layout as a miss, never a crash.
"""

from __future__ import annotations

from typing import Any

from .call_facts import (
    CallShape,
    FlowCall,
    FlowEdge,
    FlowNode,
    FunctionCalls,
    FunctionSpan,
)
from .definition_facts import ClassMembers, ClassShape, Parameter, TopLevelDef
from .effect_facts import EffectUse, StateWrite
from .facts import (
    Forwarder,
    ImportCall,
    ImportTimeFacts,
    PurityFacts,
    PurityUnit,
    SeamFacts,
    SeamFrame,
    SeamParam,
    SeamUse,
    SharedState,
    SharedWrite,
    SourceFacts,
)
from .import_syntax import RawImport

__all__ = ["CACHE_VERSION", "cached_facts", "facts_from_json", "facts_to_json"]

CACHE_VERSION = 31


def _purity_to_json(p: PurityFacts) -> list[Any]:
    units = [
        [u.name, u.line, u.end_line, u.body_lines, u.is_class, u.logic, [[line, kind] for line, kind in u.kinds], list(u.calls)]
        for u in p.units
    ]
    return [units, [[line, kind] for line, kind in p.import_time], [list(call) for call in p.port_calls]]


def _purity_from_json(raw: list[Any]) -> PurityFacts:
    units = tuple(
        PurityUnit(u[0], u[1], u[2], u[3], u[4], u[5], tuple((item[0], item[1]) for item in u[6]), tuple(u[7])) for u in raw[0]
    )
    return PurityFacts(units, tuple((item[0], item[1]) for item in raw[1]), tuple((c[0], c[1], c[2], c[3]) for c in raw[2]))


def _import_time_from_json(raw: list[Any]) -> ImportTimeFacts:
    calls = tuple(ImportCall(c[0], c[1], c[2]) for c in raw[0])
    return ImportTimeFacts(calls, tuple((item[0], item[1], item[2]) for item in raw[1]), tuple((item[0], item[1]) for item in raw[2]))


def _seams_to_json(seams: SeamFacts) -> list[Any]:
    frames = [[f.name, f.parent, [[p.name, p.annotation, p.default] for p in f.params], f.is_class] for f in seams.frames]
    return [frames, [[u.frame, u.line, u.category, u.target, list(u.guards)] for u in seams.uses]]


def _seams_from_json(raw: list[Any]) -> SeamFacts:
    frames = tuple(SeamFrame(f[0], f[1], tuple(SeamParam(p[0], p[1], p[2]) for p in f[2]), f[3]) for f in raw[0])
    return SeamFacts(frames, tuple(SeamUse(u[0], u[1], u[2], u[3], tuple(u[4])) for u in raw[1]))


def _parameters_to_json(items: tuple[Parameter, ...]) -> list[Any]:
    return [[p.name, p.positional] for p in items]


def _parameters_from_json(raw: list[Any]) -> tuple[Parameter, ...]:
    return tuple(Parameter(p[0], p[1]) for p in raw)


def _calls_to_json(shapes: tuple[CallShape, ...]) -> list[Any]:
    return [[s.line, s.callee, s.positional, list(s.keywords), s.open_keywords, list(s.replaced)] for s in shapes]


def _calls_from_json(raw: list[Any]) -> tuple[CallShape, ...]:
    return tuple(CallShape(s[0], s[1], s[2], tuple(s[3]), s[4], tuple(s[5])) for s in raw)


def _classes_to_json(shapes: tuple[ClassShape, ...]) -> list[Any]:
    return [
        [c.name, list(c.bases), _parameters_to_json(c.fields), None if c.init is None else _parameters_to_json(c.init),
         c.is_dataclass, c.opaque, c.open_keywords, list(c.derived)]
        for c in shapes
    ]


def _classes_from_json(raw: list[Any]) -> tuple[ClassShape, ...]:
    return tuple(
        ClassShape(
            c[0], tuple(c[1]), _parameters_from_json(c[2]), None if c[3] is None else _parameters_from_json(c[3]),
            c[4], c[5], c[6], tuple(c[7]),
        )
        for c in raw
    )


def _flow_calls_to_json(calls: tuple[FlowCall, ...]) -> list[Any]:
    return [[c.line, c.text, c.resolved] for c in calls]


def _flow_calls_from_json(raw: list[Any]) -> tuple[FlowCall, ...]:
    return tuple(FlowCall(c[0], c[1], c[2]) for c in raw)


def _spans_to_json(spans: tuple[FunctionSpan, ...]) -> list[Any]:
    return [
        [s.name, s.line, s.end_line, s.start, [list(call) for call in s.method_calls], [
            [
                _flow_calls_to_json(n.calls), list(n.assigned),
                # most edges only name their target, and are written as that number
                [
                    e.target if e == FlowEdge(e.target) else [e.target, e.condition, e.truth, e.absent, _flow_calls_to_json(e.unless), e.line]
                    for e in n.edges
                ],
            ]
            for n in s.flow
        ]]
        for s in spans
    ]


def _spans_from_json(raw: list[Any]) -> tuple[FunctionSpan, ...]:
    return tuple(
        FunctionSpan(s[0], s[1], s[2], s[3], tuple((c[0], c[1]) for c in s[4]), tuple(
            FlowNode(
                _flow_calls_from_json(n[0]), tuple(n[1]),
                tuple(FlowEdge(e) if isinstance(e, int) else FlowEdge(e[0], e[1], e[2], e[3], _flow_calls_from_json(e[4]), e[5]) for e in n[2]),
            )
            for n in s[5]
        ))
        for s in raw
    )


def facts_to_json(f: SourceFacts) -> list[Any]:
    imports = [[i.level, i.module, list(i.names), i.line, i.lazy, i.type_only, i.end_line] for i in f.imports]
    effects = [[u.line, u.category, u.target, u.seam, u.at_import] for u in f.effect_uses]
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
        [[[c.line, c.callee, c.local] for c in f.import_time.calls], [list(item) for item in f.import_time.function_effects], [list(item) for item in f.import_time.function_writes]],
        _seams_to_json(f.seams),
        _spans_to_json(f.function_spans),
        [list(item) for item in f.companion_exempt],
        _calls_to_json(f.call_shapes), _classes_to_json(f.class_shapes),
        [[line, target] for line, target in f.private_attrs],
        [[line, target] for line, target in f.patches], [list(item) for item in f.from_calls],
        [[c.name, c.line, list(c.bases), list(c.members), c.protocol, c.abstract] for c in f.classes],
        [[w.line, w.name, w.function, list(w.held), list(w.decorators)] for w in f.state_writes], list(f.module_locks),
        [list(f.shared_state.containers), [[w.line, w.function, w.target] for w in f.shared_state.writes]],
        [list(item) for item in f.path_calls],
    ]


def cached_facts(hit: list[Any]) -> SourceFacts | None:
    """The facts of a cache record, or ``None`` when the record does not fit this layout: a record is a miss, never a crash.

    The version number is bumped by hand, so a record written by another branch's layout can carry the current version."""
    try:
        return facts_from_json(hit[1])
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return None


def facts_from_json(raw: list[Any]) -> SourceFacts:
    imports = tuple(RawImport(i[0], i[1], tuple(i[2]), i[3], i[4], i[5], i[6]) for i in raw[0])
    effects = tuple(EffectUse(u[0], u[1], u[2], u[3], u[4]) for u in raw[1])
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
        _purity_from_json(raw[25]), raw[26], _import_time_from_json(raw[27]), _seams_from_json(raw[28]),
        _spans_from_json(raw[29]),
        tuple((item[0], item[1]) for item in raw[30]),
        _calls_from_json(raw[31]), _classes_from_json(raw[32]),
        tuple((item[0], item[1]) for item in raw[33]),
        tuple((item[0], item[1]) for item in raw[34]), tuple((item[0], item[1], item[2]) for item in raw[35]),
        tuple(ClassMembers(c[0], c[1], tuple(c[2]), tuple(c[3]), c[4], c[5]) for c in raw[36]),
        tuple(StateWrite(w[0], w[1], w[2], tuple(w[3]), tuple(w[4])) for w in raw[37]), tuple(raw[38]),
        SharedState(tuple(raw[39][0]), tuple(SharedWrite(w[0], w[1], w[2]) for w in raw[39][1])),
        tuple((item[0], item[1]) for item in raw[40]),
    )
