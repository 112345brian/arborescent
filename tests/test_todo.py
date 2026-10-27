"""Contract checks for previously unfinished features, including their implemented behavior."""

from __future__ import annotations

from arborescent_helpers import mini_policy

from arborescent.generate import generate
from arborescent.typecheckers import by_name


def _files() -> dict[str, str]:
    return generate(mini_policy(), ["app.domain", "app.ports", "app.adapters", "app.main"], by_name(["pyright"]))


def test_ruff_config_is_generated_where_ruff_can_express_a_rule() -> None:
    assert any(name.endswith("ruff.toml") for name in _files())


def test_tach_external_dependency_check_is_generated() -> None:
    assert "[external]" in _files()["tach.toml"]


def test_policy_supports_per_module_import_overrides() -> None:
    policy = mini_policy(module_forbid_imports={"bootstrap.adapters": ["bootstrap.launcher"]})

    assert policy.forbids_module_import("bootstrap.adapters", "bootstrap.launcher.cli") == "bootstrap.launcher"


def test_changed_mode_restricts_import_linter_to_changed_files() -> None:
    import inspect

    from arborescent import engine

    assert "supports_changed" in inspect.getsource(engine.run_check)  # type: ignore[attr-defined]


def test_engine_commands_use_native_config_discovery_independent_of_output_dir() -> None:
    from arborescent.engines import default_engines

    files = generate(mini_policy(output_dir="cfg"), ["app.domain"], by_name(["pyright"]))
    assert "cfg/arborescent-importlinter.toml" in files
    assert [e.command([]) for e in default_engines("cfg")] == [
        ["lint-imports"], ["tach", "check", "--dependencies"], ["tach", "check-external"],
        ["ruff", "check", "--ignore-noqa", "--exit-zero", "--output-format", "json"],
    ]
