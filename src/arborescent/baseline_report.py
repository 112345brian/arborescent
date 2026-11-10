"""``arborescent baseline report`` (#146): what a baseline is made of, so it can be lowered where one fix clears many entries.

Pure analysis of an already-parsed baseline document. Baselines take three shapes (a list of ``rule|subject|detail`` keys,
a ``{module: count}`` counter, a ``{name: {fields}}`` registry); ``parse_entries`` reads each into one ``Entry`` list, and
the rest clusters those entries by rule, module family and target and flags any one that holds a large share.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import cast

__all__ = ["DEFAULT_DEPTH", "DEFAULT_THRESHOLD", "DIMENSIONS", "MIN_FLAG_COUNT", "Analysis", "Cluster", "Entry", "analyse", "family", "parse_entries"]

DEFAULT_DEPTH = 2
DEFAULT_THRESHOLD = 0.25
MIN_FLAG_COUNT = 3  # a cluster of one or two entries is never "a missing abstraction", whatever its share
_META_KEYS = frozenset({"version", "growth", "$schema"})
_SUBJECT_FIELDS = ("module", "path", "subject", "file")
_COUNT_FIELDS = ("count", "n", "total")
DIMENSIONS = ("rule", "family", "module", "target")
CONCENTRATION_DIMENSIONS = ("family", "module", "target")  # a rule is not an abstraction that can be missing
_TARGET = re.compile(r"`([^`]+)`")


@dataclass(frozen=True)
class Entry:
    """One baselined thing. ``weight`` is its count: 1 for a violation key, the counter's value for a ``{module: count}`` entry."""

    ident: str
    rule: str
    subject: str
    target: str
    weight: int


@dataclass(frozen=True)
class Cluster:
    key: str
    count: int
    share: float


@dataclass(frozen=True)
class Analysis:
    total: int  # summed weights
    entries: int
    clusters: dict[str, list[Cluster]]  # dimension -> the top clusters
    concentration: list[tuple[str, Cluster]]  # (dimension, cluster) holding more than the threshold


def _mapping(value: object) -> dict[str, object] | None:
    return cast("dict[str, object]", value) if isinstance(value, dict) else None


def parse_entries(data: object) -> tuple[str, list[Entry]]:
    """Read a baseline document of any known shape into ``(shape, entries)``; ``ValueError`` names an unknown one."""
    document = _mapping(data)
    if document is not None and "violations" in document:
        data, document = document["violations"], _mapping(document["violations"])
    if isinstance(data, list):
        return "keys", [_from_key(item) for item in cast("list[object]", data)]
    if document is None:
        raise ValueError(f"unsupported baseline shape: {type(data).__name__}")
    items = [(str(name), value) for name, value in document.items() if name not in _META_KEYS]
    shapes = {"records" if _mapping(value) is not None else "counts" for _, value in items}
    return ("mixed" if len(shapes) > 1 else next(iter(shapes), "counts")), [_from_value(n, v) for n, v in items]


def _from_key(item: object) -> Entry:
    if not isinstance(item, str):
        raise ValueError(f"baseline list entries must be strings, not {type(item).__name__}")
    rule, _, rest = item.partition("|")
    subject, _, detail = rest.partition("|")
    found = _TARGET.search(detail)
    return Entry(item, rule if rest else "", subject if rest else item, found.group(1) if found else "", 1)


def _from_value(name: str, value: object) -> Entry:
    if isinstance(value, bool):
        raise ValueError(f"baseline entry {name!r}: a boolean is neither a count nor a record")
    if isinstance(value, int):
        return Entry(name, "", name, "", value)
    fields = _mapping(value)
    if fields is None:
        raise ValueError(f"baseline entry {name!r}: expected a count or a record, not {type(value).__name__}")
    return Entry(name, _text(fields, ("rule",)), _text(fields, _SUBJECT_FIELDS) or name, _text(fields, ("target",)), _weight(fields))


def _text(fields: dict[str, object], names: Iterable[str]) -> str:
    return next((str(fields[n]) for n in names if isinstance(fields.get(n), str) and fields[n]), "")


def _weight(fields: dict[str, object]) -> int:
    for n in _COUNT_FIELDS:
        count = fields.get(n)
        if isinstance(count, int) and not isinstance(count, bool):
            return count
    return 1


def family(subject: str, depth: int) -> str:
    """The first ``depth`` segments of a dotted module name or a ``/`` path; a shorter subject is its own family."""
    sep = "/" if "/" in subject else "."
    return sep.join(subject.split(sep)[:depth]) or subject


def _clusters(weights: Counter[str], total: int, top: int) -> list[Cluster]:
    ranked = sorted(weights.items(), key=lambda item: (-item[1], item[0]))
    return [Cluster(key, count, count / total) for key, count in ranked[:top]]


def analyse(entries: list[Entry], depth: int, top: int, threshold: float) -> Analysis:
    """Cluster by rule, family and target, and flag every module, family or target holding more than ``threshold`` of the total."""
    total = sum(e.weight for e in entries)
    weights: dict[str, Counter[str]] = {name: Counter() for name in DIMENSIONS}
    for e in entries:
        weights["rule"][e.rule or "(none)"] += e.weight
        weights["family"][family(e.subject, depth)] += e.weight
        weights["module"][e.subject] += e.weight
        if e.target:
            weights["target"][e.target] += e.weight
    if total <= 0:
        return Analysis(total, len(entries), {name: [] for name in DIMENSIONS}, [])
    flagged = [
        (name, c)
        for name in CONCENTRATION_DIMENSIONS
        for c in _clusters(weights[name], total, len(weights[name]))
        if c.share > threshold and c.count >= MIN_FLAG_COUNT
    ]
    return Analysis(total, len(entries), {name: _clusters(w, total, top) for name, w in weights.items()}, flagged)
