"""#216: per-entrypoint closure budgets, blame on the importing edge, and lazy-mount checks (all static)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files

from arborescent.cli import main
from arborescent.coupling import blame, import_chain, mount_cost, with_parent_packages
from arborescent.coupling_policy import LazyMount
from arborescent.domain import PolicyError


def graph(**edges: str) -> dict[str, frozenset[str]]:
    return {name: frozenset(targets.split()) for name, targets in edges.items()}


def test_blame_names_the_edge_that_alone_loads_a_subtree() -> None:
    found = blame(graph(app="a b", a="heavy", heavy="h1 h2", h1="", h2="", b=""), "app")
    assert [(item.target, item.importers, item.modules) for item in found[:3]] == [
        ("a", ("app",), 4),
        ("heavy", ("a",), 3),
        ("b", ("app",), 1),
    ]


def test_blame_lists_every_importer_that_must_be_cut() -> None:
    found = {item.target: item for item in blame(graph(app="a b", a="heavy", b="heavy", heavy="x", x=""), "app")}
    assert (found["heavy"].importers, found["heavy"].modules) == (("a", "b"), 2)


def test_an_importer_reached_only_through_the_target_is_not_an_edge_to_cut() -> None:
    found = {item.target: item for item in blame(graph(app="a", a="b", b="a"), "app")}
    assert (found["a"].importers, found["a"].modules) == (("app",), 2)


def test_blame_of_a_lone_or_unknown_entrypoint_is_empty() -> None:
    assert blame({}, "app") == []
    assert blame(graph(app="app"), "app") == []


def test_blame_handles_a_long_chain() -> None:
    chain = {f"m{n}": frozenset({f"m{n + 1}"}) for n in range(1500)}
    assert blame(chain, "m0")[0].modules == 1500


def test_import_chain_is_the_shortest_path_or_none() -> None:
    edges = graph(app="a b", a="c", c="cmd", b="cmd", cmd="")
    assert import_chain(edges, "app", "cmd") == ("app", "b", "cmd")
    assert import_chain(edges, "cmd", "app") is None
    assert import_chain(edges, "app", "app") == ("app",)


def test_importing_a_submodule_loads_its_parent_packages() -> None:
    edges = with_parent_packages(graph(app="p.q.r"), {"app", "p", "p.q", "p.q.r"})
    assert import_chain(edges, "app", "p") is not None
    assert import_chain(edges, "app", "p.q") is not None


def test_mount_cost_is_what_the_command_adds() -> None:
    edges = graph(app="shared", shared="", cmd="shared dep", dep="")
    assert mount_cost(edges, "app", "cmd") == 2


FILES = {
    "app/__init__.py": "",
    "app/cli.py": "from app import hub\n",
    "app/hub.py": "from app import dep\n",
    "app/dep.py": "",
    "app/backfill.py": "from app import hub, heavy\n\napp = None\n",
    "app/heavy.py": "",
}
BUDGET = '[coupling.closure_max]\n"app.cli" = {}\n'
MOUNTS = '[coupling.lazy_mounts]\nbackfill = "app.backfill:app"\n'


def repo(tmp_path: Path, extra: str = "", files: Mapping[str, str] | None = None, entrypoints: str = '["app.cli"]') -> list[str]:
    write_files(tmp_path, {**FILES, **(files or {})})
    config = tmp_path / "arborescent.toml"
    config.write_text(
        'sources = ["app"]\nrings = ["domain"]\n[ring.domain]\nexternal = ["*"]\n[classify]\n"app" = "domain"\n"app.*" = "domain"\n'
        f"[coupling]\nentrypoints = {entrypoints}\n" + extra,
        encoding="utf-8",
    )
    return ["--config", str(config), "coupling"]


def run(base: list[str], action: str) -> int:
    return main([*base, action], services())


def test_check_passes_within_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo(tmp_path, BUDGET.format(3) + MOUNTS)
    assert run(base, "baseline") == 0
    assert run(base, "check") == 0
    assert "FAIL " not in capsys.readouterr().out


def test_an_exceeded_budget_fails_even_when_baselined_and_blames_an_edge(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo(tmp_path, BUDGET.format(2))
    assert run(base, "baseline") == 0
    _ = capsys.readouterr()
    assert run(base, "check") == 1
    out = capsys.readouterr().out
    assert "startup budget: app.cli loads 3 project modules, budget 2" in out
    assert "app.cli -> app.hub pulls 2 module(s)" in out
    assert "(1 regression(s))" in out


def test_a_mounted_module_imported_at_top_level_fails_with_the_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo(tmp_path, MOUNTS, {"app/hub.py": "from app import backfill\n"})
    assert run(base, "baseline") == 0
    _ = capsys.readouterr()
    assert run(base, "check") == 1
    out = capsys.readouterr().out
    assert "lazy mount `backfill`: app.cli loads app.backfill at import time: app.cli -> app.hub -> app.backfill" in out


def test_a_parent_package_that_imports_a_mounted_module_is_caught(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo(tmp_path, MOUNTS, {"app/__init__.py": "from app import backfill\n"})
    assert run(base, "baseline") == 0
    assert run(base, "check") == 1
    assert "lazy mount `backfill`" in capsys.readouterr().out


def test_a_function_level_or_type_only_import_of_a_mounted_module_passes(tmp_path: Path) -> None:
    source = "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from app import backfill\n\n\ndef f() -> None:\n    from app import backfill\n"
    base = repo(tmp_path, MOUNTS, {"app/cli.py": source})
    assert run(base, "baseline") == 0
    assert run(base, "check") == 0


def test_a_mounted_module_may_itself_be_an_entrypoint(tmp_path: Path) -> None:
    base = repo(tmp_path, MOUNTS, entrypoints='["app.cli", "app.backfill"]')
    assert run(base, "baseline") == 0
    assert run(base, "check") == 0


def test_startup_reports_closure_budget_blame_and_mounts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo(tmp_path, BUDGET.format(2) + MOUNTS)
    assert run(base, "startup") == 0  # a report: `check` is what fails
    out = capsys.readouterr().out
    assert "app.cli: 3 project modules (budget 2, OVER)" in out
    assert "app.cli -> app.hub pulls 2 module(s)" in out
    assert "backfill -> app.backfill: +2 module(s) when run" in out  # backfill and heavy; hub and dep are shared


def test_startup_marks_an_eager_mount(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo(tmp_path, MOUNTS, {"app/cli.py": "from app import backfill\n"})
    assert run(base, "startup") == 0
    assert "backfill -> app.backfill: EAGER" in capsys.readouterr().out


def test_startup_without_entrypoints_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo(tmp_path, entrypoints="[]")
    assert run(base, "startup") == 0
    assert "no entrypoints declared" in capsys.readouterr().out


def test_a_mount_naming_a_missing_module_is_a_policy_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo(tmp_path, '[coupling.lazy_mounts]\nx = "app.gone:app"\n')
    assert run(base, "check") == 2
    assert "app.gone" in capsys.readouterr().err


def test_the_new_keys_are_read() -> None:
    policy = mini_policy(coupling={"entrypoints": ["app.main"], "closure_max": {"app.main": 4}, "lazy_mounts": {"b c": "app.adapters:x.y"}})
    assert policy.coupling.closure_max == (("app.main", 4),)
    assert policy.coupling.lazy_mounts == (LazyMount("b c", "app.adapters", "x.y"),)


@pytest.mark.parametrize(
    "table",
    [
        {"closure_max": []},
        {"closure_max": {"app.main": 0}},
        {"closure_max": {"app.main": True}},
        {"closure_max": {"app.other": 3}},  # not a declared entrypoint: would never be checked
        {"lazy_mounts": []},
        {"lazy_mounts": {"x": 1}},
        {"lazy_mounts": {"x": ""}},
        {"lazy_mounts": {"x": "app.adapters"}},
        {"lazy_mounts": {"x": ":app"}},
        {"lazy_mounts": {"x": "app.adapters:"}},
        {"lazy_mounts": {"x": "app.adapters:a:b"}},
        {"lazy_mounts": {" ": "app.adapters:a"}},
        {"lazy_mounts": {"x": "app\x00:a"}},
    ],
)
def test_malformed_startup_keys_are_refused(table: dict[str, object]) -> None:
    with pytest.raises(PolicyError):
        mini_policy(coupling={"entrypoints": ["app.main"], **table})


def test_lazy_mounts_without_entrypoints_are_refused() -> None:
    with pytest.raises(PolicyError, match="entrypoints"):
        mini_policy(coupling={"lazy_mounts": {"x": "app.adapters:a"}})
