"""The policy is compiled once into neutral contracts; each engine adapter, or one the caller supplies, spells them itself."""

from __future__ import annotations

import json
from dataclasses import dataclass

from arborescent_helpers import mini_policy

from arborescent.architecture_contracts import compile_contracts
from arborescent.domain import ArchitectureContracts
from arborescent.engines import ImportLinter, Tach
from arborescent.generate import GraphFacts, generate
from arborescent.ports import ContractRenderer

NAMES = ["app.domain", "app.ports", "app.adapters", "app.main"]


def test_the_contracts_say_who_may_not_import_whom_without_naming_a_tool() -> None:
    contracts = compile_contracts(mini_policy(), NAMES, {}, {}, None)
    assert contracts.root_packages == ("app",)
    by_name = {name: (sources, forbidden) for name, sources, forbidden in contracts.forbidden}
    sources, forbidden = by_name["domain ring imports only ['domain']"]
    assert sources == ["app.domain"] and set(forbidden) >= {"app.adapters", "app.main", "app.ports"}
    assert [(m.path, m.layer) for m in contracts.modules] == [
        ("app.adapters", "adapters"), ("app.domain", "domain"), ("app.main", "composition"), ("app.ports", "ports"),
    ]
    assert contracts.layers == ("composition", "adapters", "ports", "domain")


def test_both_engines_render_the_same_contracts_and_the_core_writes_neither() -> None:
    contracts = compile_contracts(mini_policy(), NAMES, {}, {}, None)
    assert "[[tool.importlinter.contracts]]" in ImportLinter().render_contracts(contracts)
    assert "[[modules]]" in Tach().render_contracts(contracts)
    assert ImportLinter().config_file != Tach().config_file


@dataclass(frozen=True)
class GraphAnalyzer:
    """A user-supplied analyzer: its own format, its own file, and the engines.py module never hears of it."""

    config_file: str = "analyzer.json"

    def render_contracts(self, contracts: ArchitectureContracts) -> str:
        return json.dumps({"layers": contracts.layers, "forbidden": [name for name, _, _ in contracts.forbidden]})


def test_a_renderer_the_caller_supplies_gets_a_file_beside_the_others() -> None:
    renderer = GraphAnalyzer()
    assert isinstance(renderer, ContractRenderer)
    files = generate(mini_policy(), GraphFacts(NAMES, {}, {}), [], [renderer, ImportLinter()])
    assert json.loads(files["analyzer.json"])["layers"] == ["composition", "adapters", "ports", "domain"]
    assert "arborescent-importlinter.toml" in files and "tach.toml" not in files


def test_no_renderer_means_no_engine_config_rather_than_a_silent_default() -> None:
    assert set(generate(mini_policy(), GraphFacts(NAMES, {}, {}), [], [])) == {"ruff.toml"}


def test_contracts_are_relative_to_the_repository_and_the_adapter_offsets_them() -> None:
    contracts = compile_contracts(mini_policy(output_dir="cfg/tools"), NAMES, {}, {}, None)
    assert contracts.config_dir == "cfg/tools" and contracts.source_roots == tuple(mini_policy().source_roots)
    assert 'source_roots = ["../../' in Tach().render_contracts(contracts)
