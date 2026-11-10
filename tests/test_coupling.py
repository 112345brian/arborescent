"""#148: fan-in, entrypoint closure, ambient hubs, the coupling ratchet and the cost of a new module."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files

from arborescent import coupling_ratchet as ratchet
from arborescent.cli import main
from arborescent.coupling import (
    NewModule,
    accessor_surface,
    ambient_hubs,
    cost_of_new_modules,
    entrypoint_closure,
    fan_in,
    import_edges,
    reach,
)
from arborescent.coupling_policy import CouplingPolicy
from arborescent.domain import PolicyError
from arborescent.facts import ModuleInfo, extract


def build(sources: Mapping[str, str]) -> dict[str, ModuleInfo]:
    return {name: ModuleInfo(name, name + ".py", False, extract(text)) for name, text in sources.items()}


GRAPH = {
    "app": "import a\nimport b\n",
    "a": "import c\n\ndef f() -> None:\n    import d\n",
    "b": "import c\n",
    "c": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import e\n",
    "d": "import c\n",
    "e": "",
}


def edges():
    modules = build(GRAPH)
    return modules, import_edges(modules, frozenset(modules))


def test_an_import_has_one_kind_and_the_strongest_wins() -> None:
    modules = build({"x": "import y\n\ndef f() -> None:\n    import y\n    import z\n", "y": "", "z": ""})
    found = import_edges(modules, frozenset(modules))
    assert found.runtime["x"] == {"y"}
    assert found.lazy["x"] == {"z"}


def test_fan_in_counts_distinct_importing_modules_by_kind() -> None:
    modules, found = edges()
    rows = {row.module: row for row in fan_in(found, modules)}
    assert (rows["c"].direct, rows["c"].runtime) == (3, 3)  # a, b, d
    assert (rows["d"].direct, rows["d"].runtime) == (1, 0)  # only a function-level import
    assert (rows["e"].direct, rows["e"].runtime) == (1, 0)  # only a TYPE_CHECKING import
    assert rows["c"].transitive == 4  # a, b, d and app (through a and b); d is a module-level importer of c


def test_a_module_is_never_its_own_importer() -> None:
    modules = build({"x": "import x\n"})
    assert fan_in(import_edges(modules, frozenset(modules)), modules)[0].direct == 0


def test_closure_follows_module_level_imports_and_counts_the_rest_separately() -> None:
    _, found = edges()
    closure = entrypoint_closure(found, "app")
    assert closure.modules == {"app", "a", "b", "c"}
    assert closure.lazy_only == {"d"}
    assert closure.type_only == {"e"}


def test_a_module_reached_both_ways_is_in_the_closure_not_the_lazy_count() -> None:
    modules = build({"app": "import a\nimport b\n", "a": "", "b": "def f() -> None:\n    import a\n"})
    closure = entrypoint_closure(import_edges(modules, frozenset(modules)), "app")
    assert closure.lazy_only == frozenset()


def test_reach_includes_the_start_and_stops_on_cycles() -> None:
    graph = {"a": frozenset({"b"}), "b": frozenset({"a"})}
    assert reach(graph, ["a"]) == {"a", "b"}


ACCESSORS = (
    "import os\n\n"
    "def home() -> str:\n    return os.environ['HOME']\n\n"
    "def cwd() -> str:\n    return os.environ.get('PWD', '')\n\n"
    "def pure(x: int) -> int:\n    return x\n\n"
    "def _private() -> str:\n    return os.environ.get('PWD', '')\n"
)


def test_accessor_surface_counts_public_definitions_that_use_an_effect() -> None:
    accessors, public = accessor_surface(build({"m": ACCESSORS})["m"])
    assert accessors == ("home", "cwd")
    assert public == 3


def test_a_hub_needs_both_many_importers_and_an_effectful_surface() -> None:
    sources = {"cfg": ACCESSORS, "calc": "def pure(x: int) -> int:\n    return x\n"}
    sources |= {f"u{n}": "import cfg\nimport calc\n" for n in range(3)}
    modules = build(sources)
    rows = fan_in(import_edges(modules, frozenset(modules)), modules)
    assert [hub.module for hub in ambient_hubs(rows, modules, 3)] == ["cfg"]
    assert ambient_hubs(rows, modules, 4) == []


def test_cost_of_new_modules_adds_the_new_ones_and_any_closure_they_pull_in() -> None:
    _, found = edges()
    closures = [entrypoint_closure(found, "app"), entrypoint_closure(found, "b")]
    cost = cost_of_new_modules(closures, found, NewModule("b", ("d",), 3))
    assert cost == {"app": 3 + 1, "b": 3 + 1}  # d is in neither closure
    assert cost_of_new_modules(closures, found, NewModule("a", (), 2)) == {"app": 2}  # b's closure does not hold a


BASELINE = {ratchet.FAN_IN: {"hub": 10}, ratchet.CLOSURE: {"main": 40}}


def counts(hub: int = 10, closure: int = 40, extra: Mapping[str, int] | None = None) -> ratchet.Counts:
    return {ratchet.FAN_IN: {"hub": hub, **(extra or {})}, ratchet.CLOSURE: {"main": closure}}


def test_regressions_name_growth_and_entries_the_baseline_lacks() -> None:
    assert ratchet.regressions(counts(), BASELINE) == []
    assert ratchet.regressions(counts(hub=11), BASELINE) == ["fan-in: hub grew from 10 to 11"]
    assert ratchet.regressions(counts(closure=41), BASELINE) == ["closure: main grew from 40 to 41"]
    assert ratchet.regressions(counts(extra={"new": 9}), BASELINE) == ["fan-in: new is not in the baseline (9)"]


def test_shrinking_is_reported_as_lowerable_not_as_a_failure() -> None:
    assert ratchet.regressions(counts(hub=3), BASELINE) == []
    assert ratchet.lowerable(counts(hub=3), BASELINE) == ["fan-in: hub is 3, baseline 10"]
    assert ratchet.lowerable({ratchet.FAN_IN: {}, ratchet.CLOSURE: {"main": 40}}, BASELINE) == ["fan-in: hub is 0, baseline 10"]


def test_the_first_baseline_records_the_current_state_without_a_growth_entry() -> None:
    assert ratchet.next_document(None, counts(), False, None) == {**counts(), "growth": []}


def test_growth_needs_the_flag_and_a_reason_and_is_logged() -> None:
    existing = (BASELINE, [])
    with pytest.raises(ValueError, match="would grow"):
        ratchet.next_document(existing, counts(hub=12), False, None)
    with pytest.raises(ValueError, match="needs --reason"):
        ratchet.next_document(existing, counts(hub=12), True, "  ")
    document = ratchet.next_document(existing, counts(hub=12), True, "split planned")
    assert document["growth"] == [{"reason": "split planned", "added": ["fan-in: hub grew from 10 to 12"]}]
    assert document[ratchet.FAN_IN] == {"hub": 12}


def test_a_lower_count_is_written_without_growth() -> None:
    document = ratchet.next_document((BASELINE, [{"reason": "old", "added": []}]), counts(hub=7), False, None)
    assert document[ratchet.FAN_IN] == {"hub": 7}
    assert document["growth"] == [{"reason": "old", "added": []}]


@pytest.mark.parametrize(
    "document",
    [[], {"fan-in": []}, {"fan-in": {"a": "x"}}, {"closure": {1: 2}}, {"growth": {}}],
)
def test_a_malformed_baseline_is_refused(document: object) -> None:
    with pytest.raises(ValueError, match="coupling baseline"):
        ratchet.parse(document)


def test_parse_reads_what_next_document_writes() -> None:
    document = ratchet.next_document(None, counts(), False, None)
    assert ratchet.parse(document) == (counts(), [])


def test_the_policy_table_is_validated() -> None:
    assert mini_policy().coupling == CouplingPolicy()
    policy = mini_policy(coupling={"entrypoints": ["app.main"], "hub_fan_in": 3, "fan_in_max": {"domain": 5}, "baseline": "c.json"})
    assert policy.coupling == CouplingPolicy(("app.main",), (("domain", 5),), 3, "c.json")
    for bad in ({"hub_fan_in": 0}, {"fan_in_max": {"domain": "x"}}, {"entrypoints": "app"}, {"baseline": ""}, {"nope": 1}):
        with pytest.raises(PolicyError):
            mini_policy(coupling=bad)


def repo(tmp_path: Path, entrypoints: str = '["app.main"]') -> Path:
    write_files(tmp_path, MINI_FILES)
    config = tmp_path / "arborescent.toml"
    config.write_text(
        'sources = ["app"]\nrings = ["domain", "ports", "adapters", "composition"]\n'
        '[groups]\npure = ["__future__"]\n[ring.domain]\nexternal = ["@pure"]\n'
        '[ring.ports]\nmay_import = ["domain"]\nexternal = ["@pure"]\n'
        '[ring.adapters]\nmay_import = ["domain", "ports"]\nexternal = ["@pure", "os"]\nos_branching = true\n'
        '[ring.composition]\nmay_import = ["*"]\nexternal = ["@pure", "sys"]\n'
        '[classify]\n"app" = "domain"\n"app.domain" = "domain"\n"app.ports" = "ports"\n"app.adapters" = "adapters"\n"app.main" = "composition"\n'
        f"[coupling]\nentrypoints = {entrypoints}\nhub_fan_in = 2\n[coupling.fan_in_max]\ndomain = 1\n",
        encoding="utf-8",
        newline="\n",
    )
    return config


def run(config: Path, *args: str) -> int:
    return main(["--config", str(config), "coupling", *args], services())


def test_report_prints_fan_in_closure_and_flags_a_ring_cap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(repo(tmp_path), "report") == 0
    out = capsys.readouterr().out
    assert "app.domain: direct 2, module-level 2, transitive" in out
    assert "OVER CAP 1 for ring domain" in out
    assert "app.main: 4 (function-level only +0, type-only +0)" in out


def test_report_json_carries_the_same_numbers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(repo(tmp_path), "report", "--json") == 0
    out = capsys.readouterr().out
    assert '"entrypoint": "app.main"' in out
    assert '"modules": 4' in out


def test_baseline_then_check_then_a_new_import_fails_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = repo(tmp_path)
    assert run(config, "check") == 2  # no baseline yet: an error, not a pass
    assert run(config, "baseline") == 0
    assert run(config, "check") == 0
    (tmp_path / "app" / "main.py").write_text(MINI_FILES["app/main.py"] + "from . import ports\n", encoding="utf-8")
    capsys.readouterr()
    assert run(config, "check") == 1
    assert "FAIL   fan-in: app.ports is not in the baseline (2)" in capsys.readouterr().out  # a new hub, though the closure is unchanged
    (tmp_path / "app" / "extra.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "main.py").write_text(MINI_FILES["app/main.py"] + "from . import extra\n", encoding="utf-8")
    assert run(config, "check") == 1
    assert "FAIL   closure: app.main grew from 4 to 5" in capsys.readouterr().out


def test_cost_reports_per_entrypoint_growth(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = repo(tmp_path)
    assert run(config, "cost", "app.adapters", "--modules", "2") == 0
    assert "app.main: closure 4 -> 6 (+2)" in capsys.readouterr().out
    assert run(config, "cost", "app.nope") == 2


def test_an_undeclared_entrypoint_module_is_a_policy_error(tmp_path: Path) -> None:
    assert run(repo(tmp_path, '["app.missing"]'), "report") == 2
