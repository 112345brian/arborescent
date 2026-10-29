"""Generated engine configs honor annotated module rings."""

from __future__ import annotations

import json
import tomllib

from arborescent_helpers import mini_policy

from arborescent.config import policy_from_mapping
from arborescent.generate import generate
from arborescent.typecheckers import Pyright


def test_annotation_only_modules_appear_in_each_generated_ring_config() -> None:
    policy = mini_policy(classify={})
    names = ["app", "app.core", "app.cli"]
    files = generate(policy, names, [], arbor_index={
        "app.core": ("domain", None),
        "app.cli": ("composition", None),
    })

    tach = tomllib.loads(files["tach.toml"])
    assert [(module["path"], module["layer"]) for module in tach["modules"]] == [
        ("app.cli", "composition"), ("app.core", "domain"),
    ]
    contracts = tomllib.loads(files["arborescent-importlinter.toml"])["tool"]["importlinter"]["contracts"]
    domain_contract = next(contract for contract in contracts if contract["name"].startswith("domain ring imports only"))
    assert domain_contract["source_modules"] == ["app.core"]
    assert domain_contract["forbidden_modules"] == ["app.cli"]
    ruff = tomllib.loads(files["ruff.toml"])
    assert "**/.venv" in ruff["exclude"]
    ignores = ruff["lint"]["per-file-ignores"]
    assert ignores["**/app/cli.py"] == ["T20"]
    assert "**/app/core.py" not in ignores


def test_empty_annotation_index_preserves_all_generated_bytes() -> None:
    policy = mini_policy()
    names = ["app", "app.domain", "app.ports", "app.adapters", "app.main"]
    assert generate(policy, names, [], arbor_index={}) == generate(policy, names, [])


def _scoped_policy():
    return policy_from_mapping({
        "scopes": {
            "alpha": {
                "sources": ["libs/alpha/src/alpha"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain", "composition"],
                "classify": {"alpha.core": "domain", "alpha.cli": "composition"},
                "public": ["alpha.core"], "depends_on": ["beta"],
                "ring": {"composition": {"may_import": ["domain"]}},
            },
            "beta": {
                "sources": ["libs/beta/src/beta"], "source_roots": ["libs/beta/src"],
                "rings": ["domain", "adapters"],
                "classify": {"beta.api": "domain", "beta.private": "adapters"},
                "public": ["beta.api"],
                "ring": {"adapters": {"may_import": ["domain"]}},
            },
        },
    })


def _scoped_generation():
    names = ["alpha", "alpha.core", "alpha.cli", "beta", "beta.api", "beta.private"]
    owners = {
        "alpha": "alpha", "alpha.core": "alpha", "alpha.cli": "alpha",
        "beta": "beta", "beta.api": "beta", "beta.private": "beta",
    }
    return _scoped_policy(), names, owners


def test_tach_uses_namespaced_scope_layers() -> None:
    policy, names, owners = _scoped_generation()

    files = generate(policy, names, [], scope_by_module=owners)
    tach = tomllib.loads(files["tach.toml"])

    assert set(tach["layers"]) == {"alpha__domain", "alpha__composition", "beta__domain", "beta__adapters"}
    modules = {module["path"]: module for module in tach["modules"]}
    assert modules["alpha.core"]["layer"] == "alpha__domain"
    assert modules["alpha.cli"]["layer"] == "alpha__composition"
    assert {dependency["path"] for dependency in modules["alpha.cli"]["depends_on"]} == {
        "alpha.core", "beta.api",
    }


def test_import_linter_blocks_private_cross_scope_targets() -> None:
    policy, names, owners = _scoped_generation()

    files = generate(policy, names, [], scope_by_module=owners)
    contracts = tomllib.loads(files["arborescent-importlinter.toml"])["tool"]["importlinter"]["contracts"]
    boundary = next(contract for contract in contracts if contract["name"].startswith("alpha scope"))

    assert boundary["source_modules"] == ["alpha.cli", "alpha.core"]
    assert boundary["forbidden_modules"] == ["beta.private"]


def test_ruff_composition_ignore_is_scope_local() -> None:
    policy, names, owners = _scoped_generation()

    files = generate(policy, names, [], scope_by_module=owners)
    ignores = tomllib.loads(files["ruff.toml"])["lint"]["per-file-ignores"]

    assert ignores["**/alpha/cli.py"] == ["T20"]
    assert ignores["**/beta/private.py"] == ["T20"]
    assert "**/beta/api.py" not in ignores


def test_type_checker_includes_union_of_scope_sources() -> None:
    policy, names, owners = _scoped_generation()

    files = generate(policy, names, [Pyright()], scope_by_module=owners)
    checker = json.loads(files["pyrightconfig.json"])

    assert checker["include"] == ["libs/alpha/src/alpha", "libs/beta/src/beta"]
