"""Generated engine configs honor annotated module rings."""

from __future__ import annotations

import tomllib

from arborescent_helpers import mini_policy

from arborescent.generate import generate


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
    ignores = tomllib.loads(files["ruff.toml"])["lint"]["per-file-ignores"]
    assert ignores["**/app/cli.py"] == ["T20"]
    assert "**/app/core.py" not in ignores


def test_empty_annotation_index_preserves_all_generated_bytes() -> None:
    policy = mini_policy()
    names = ["app", "app.domain", "app.ports", "app.adapters", "app.main"]
    assert generate(policy, names, [], arbor_index={}) == generate(policy, names, [])
