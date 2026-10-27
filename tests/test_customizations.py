from __future__ import annotations

import tomllib

from arborescent_helpers import MINI

from arborescent.cli import _print_rule_label
from arborescent.config import policy_from_mapping
from arborescent.generate import ruff


def test_ruff_overrides_compose_with_generated_rules() -> None:
    policy = policy_from_mapping({
        **MINI,
        "ruff": {
            "extend-select": ["UP"],
            "ignore": ["B008"],
            "per-file-ignores": {"tests/**": ["BLE001"]},
            "max-complexity": 12,
            "max-args": 8,
        },
    })

    generated = ruff(policy, ["app.domain", "app.main"])

    parsed = tomllib.loads(generated)
    assert {"BLE001", "UP"} <= set(parsed["lint"]["select"])
    assert parsed["lint"]["ignore"] == ["B008", "PIE807"]
    assert parsed["lint"]["per-file-ignores"]["tests/**"] == ["BLE001"]
    assert parsed["lint"]["mccabe"]["max-complexity"] == 12
    assert parsed["lint"]["pylint"]["max-args"] == 8


def test_untouched_configuration_is_standard() -> None:
    assert policy_from_mapping(MINI).customization_labels == ()


def test_check_label_names_default_and_custom_configuration(capsys) -> None:  # type: ignore[no-untyped-def]
    _print_rule_label(policy_from_mapping(MINI))
    assert capsys.readouterr().out == "standard rules (baseline v2)\n"

    _print_rule_label(policy_from_mapping({**MINI, "preset": "standard"}))
    assert capsys.readouterr().out == "customised rules: preset\n"
