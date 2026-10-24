"""Honest TODOs: behaviour the owner's brief asks for that this prototype does NOT do yet.

Each is `xfail(strict=True)`: when someone implements it the test starts passing and strict mode forces the marker
to be removed, so this file cannot silently go stale.
"""

from __future__ import annotations

import pytest

from archlint.generate import generate
from archlint.typecheckers import by_name
from archlint_helpers import mini_policy


def _files() -> dict[str, str]:
    return generate(mini_policy(), ["app.domain", "app.ports", "app.adapters", "app.main"], by_name(["pyright"]))


@pytest.mark.xfail(strict=True, reason="TODO: ruff is not wired; flake8-tidy-imports banned-api is global, not per ring")
def test_ruff_config_is_generated_where_ruff_can_express_a_rule() -> None:
    assert any(name.endswith("ruff.toml") for name in _files())


@pytest.mark.xfail(strict=True, reason="TODO: tach `[external]`/check-external against pyproject is not generated")
def test_tach_external_dependency_check_is_generated() -> None:
    assert "[external]" in _files()["tach.toml"]


@pytest.mark.xfail(strict=True, reason="TODO: module-level exceptions finer than a ring (e.g. bootstrap.adapters may not import launcher at all)")
def test_policy_supports_per_module_import_overrides() -> None:
    from archlint.domain import Policy

    assert hasattr(Policy, "module_overrides")


@pytest.mark.xfail(strict=True, reason="TODO: import-linter and tach are graph-wide; --changed still runs them on everything")
def test_changed_mode_restricts_import_linter_to_changed_files() -> None:
    import inspect

    from archlint import engine

    assert "changed" in inspect.getsource(engine._tool)  # type: ignore[attr-defined]  # noqa: SLF001
