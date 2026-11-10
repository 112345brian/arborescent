"""Import cycles with edge kinds, a feedback edge set and the Protocol owner (#149)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from test_baseline_ratchet import write_policy

from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.cli import main
from arborescent.engine import Options, Services, run_check
from arborescent.engines import default_engines
from arborescent.import_cycles import (
    EdgeKind,
    ImportEdge,
    cyclic_components,
    find_cycles,
    import_edges,
)
from arborescent.rules import RuleContext
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers

# domain -> adapters is function-level; adapters -> ports -> domain is import-time: one known cycle of three modules.
KNOWN_CYCLE = {
    "app/domain.py": "def f() -> None:\n    from . import adapters\n",
    "app/ports.py": "from . import domain\n",
    "app/adapters.py": "from . import ports\n",
}


def _edges(repo: Path, files: dict[str, str]) -> list[ImportEdge]:
    write_files(repo, files)
    scan = FsScanner().scan(mini_policy(), repo)
    context = RuleContext(mini_policy(), scan.arbor_index, scan.scope_by_module)
    return import_edges(scan.modules, scan.known, context.scope_name)


def _edge(source: str, target: str, kind: EdgeKind = EdgeKind.EAGER) -> ImportEdge:
    return ImportEdge(source, target, kind, (1,))


def test_a_synthetic_project_with_a_known_cycle(tmp_path: Path) -> None:
    edges = _edges(tmp_path, {"app/__init__.py": "", **KNOWN_CYCLE})
    kinds = {(e.source, e.target): e.kind for e in edges}
    assert kinds == {
        ("app.domain", "app.adapters"): EdgeKind.LAZY,
        ("app.ports", "app.domain"): EdgeKind.EAGER,
        ("app.adapters", "app.ports"): EdgeKind.EAGER,
    }
    (cycle,) = find_cycles(edges, {"app.domain": 0, "app.ports": 1, "app.adapters": 2})
    assert cycle.modules == ("app.adapters", "app.domain", "app.ports")
    assert len(cycle.edges) == 3
    assert len(cycle.cut) == 1  # one cut breaks a three-cycle
    assert [(e.source, e.target) for e in cycle.cycle_breaking] == [("app.domain", "app.adapters")]
    assert cycle.protocol_owner == "app.domain"


def test_removing_the_cut_edges_leaves_the_component_acyclic(tmp_path: Path) -> None:
    edges = _edges(tmp_path, {"app/__init__.py": "", **KNOWN_CYCLE})
    (cycle,) = find_cycles(edges, {})
    rest = [e for e in edges if e not in cycle.cut]
    assert cyclic_components({e.source: {e2.target for e2 in rest if e2.source == e.source} for e in rest}) == []


def test_an_acyclic_project_has_no_cycles(repo: Path) -> None:
    scan = FsScanner().scan(mini_policy(), repo)
    context = RuleContext(mini_policy(), scan.arbor_index, scan.scope_by_module)
    assert find_cycles(import_edges(scan.modules, scan.known, context.scope_name), {}) == []


def test_empty_input_has_no_cycles() -> None:
    assert find_cycles([], {}) == []
    assert cyclic_components({}) == []


def test_a_self_import_is_a_one_module_cycle_that_must_be_cut() -> None:
    (cycle,) = find_cycles([_edge("a", "a", EdgeKind.LAZY)], {})
    assert cycle.modules == ("a",)
    assert cycle.cut == (_edge("a", "a", EdgeKind.LAZY),)
    assert cycle.protocol_owner == "a"


def test_two_overlapping_cycles_need_one_shared_cut() -> None:
    # a -> b -> a and a -> c -> a share only module a; cutting the two edges into a is not needed if a -> b and a -> c go.
    edges = [_edge("a", "b"), _edge("b", "a"), _edge("a", "c"), _edge("c", "a")]
    (cycle,) = find_cycles(edges, {})
    assert cycle.modules == ("a", "b", "c")
    assert len(cycle.cut) == 2
    rest = [e for e in edges if e not in cycle.cut]
    assert cyclic_components({e.source: {x.target for x in rest if x.source == e.source} for e in rest}) == []


def test_separate_components_are_reported_largest_first() -> None:
    edges = [_edge("x", "y"), _edge("y", "x"), _edge("a", "b"), _edge("b", "c"), _edge("c", "a"), _edge("c", "z")]
    cycles = find_cycles(edges, {})
    assert [c.modules for c in cycles] == [("a", "b", "c"), ("x", "y")]


def test_a_function_level_edge_outside_every_cycle_breaks_nothing() -> None:
    edges = [_edge("a", "b", EdgeKind.LAZY), _edge("b", "c")]
    assert find_cycles(edges, {}) == []


def test_a_function_level_edge_on_a_cycle_of_other_deferred_edges_is_not_the_one_that_breaks_it() -> None:
    # a -> b and b -> a are both function-level: neither is closed by import-time edges alone.
    edges = [_edge("a", "b", EdgeKind.LAZY), _edge("b", "a", EdgeKind.LAZY)]
    (cycle,) = find_cycles(edges, {})
    assert cycle.cycle_breaking == ()
    assert len(cycle.cut) == 1


def test_a_type_only_edge_closes_a_cycle_but_never_counts_as_a_function_level_breaker() -> None:
    edges = [_edge("a", "b"), _edge("b", "a", EdgeKind.TYPE_ONLY)]
    (cycle,) = find_cycles(edges, {})
    assert cycle.cycle_breaking == ()
    assert len(cycle.cut) == 1


def test_an_edge_made_by_several_imports_takes_the_strongest_kind(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "",
        "app/domain.py": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from . import ports\n\n\ndef f() -> None:\n    from . import ports\n",
        "app/ports.py": "from . import domain\n",
    }
    edges = {(e.source, e.target): e for e in _edges(tmp_path, files)}
    edge = edges["app.domain", "app.ports"]
    assert edge.kind is EdgeKind.LAZY  # function-level beats type-only
    assert edge.lines == (4, 8)


def test_the_owner_falls_back_to_the_most_imported_then_the_name() -> None:
    edges = [_edge("a", "c"), _edge("b", "c"), _edge("c", "a"), _edge("c", "b")]
    (cycle,) = find_cycles(edges, {})
    assert cycle.protocol_owner == "c"  # imported by both others
    (tied,) = find_cycles([_edge("m", "n"), _edge("n", "m")], {})
    assert tied.protocol_owner == "m"


def test_a_module_with_no_ring_sorts_after_one_with_a_ring() -> None:
    (cycle,) = find_cycles([_edge("a", "b"), _edge("b", "a")], {"b": 0})
    assert cycle.protocol_owner == "b"


def test_a_large_component_does_not_exhaust_the_stack() -> None:
    size = 3000
    edges = [_edge(f"m{i}", f"m{(i + 1) % size}") for i in range(size)]
    (cycle,) = find_cycles(edges, {})
    assert len(cycle.modules) == size
    assert len(cycle.cut) == 1


def _svc() -> Services:
    return Services(FsScanner(), FakeToolRunner(), list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines())


def test_the_cycles_command_prints_the_report(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_policy(repo)
    write_files(repo, KNOWN_CYCLE)
    assert main(["--config", str(config), "cycles"], _svc()) == 0
    out = capsys.readouterr().out
    assert "1 import cycle(s)" in out
    assert "cycle 1: 3 module(s): app.adapters, app.domain, app.ports" in out
    assert "app.domain -> app.adapters [function-level] line 2" in out
    assert "function-level imports that exist to break this cycle" in out
    assert "invert with a Protocol owned by app.domain" in out
    assert "function-level import edges: 1, 1 on a cycle (1 exist to break one), 0 outside every cycle" in out


def test_the_cycles_command_on_a_clean_project(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_policy(repo)
    assert main(["--config", str(config), "cycles"], _svc()) == 0
    assert capsys.readouterr().out.strip() == "no import cycles (0 function-level import edge(s), none on a cycle)"


def test_the_report_and_the_circular_import_rule_agree_on_the_components(tmp_path: Path) -> None:
    write_files(tmp_path, {"app/__init__.py": "", **KNOWN_CYCLE})
    report = run_check(mini_policy(), tmp_path, services(), Options(tools=False))
    flagged = {v.subject for v in report.violations if v.rule == "circular-import"}
    edges = _edges(tmp_path, {})
    assert flagged == {", ".join(c.modules) for c in find_cycles(edges, {})}
