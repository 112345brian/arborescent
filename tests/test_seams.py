"""Defects found at the seams after five independent pieces of work were merged (review of the combined branch)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from archlint_helpers import MINI, write_files
from test_engine import services

from archlint import practices, selftest
from archlint.adapters import FsScanner, LocalFileSystem, SystemClock
from archlint.bench import bench_one
from archlint.cli import main
from archlint.config import policy_from_mapping
from archlint.domain import NOISE_DIRS, PolicyError
from archlint.engine import Services
from archlint.ports import Engine, TypeChecker
from archlint.scanner import _PRUNE
from archlint.testing import FakeToolRunner

TOML = 'sources = ["app"]\nrings = ["domain"]\n[ring.domain]\nexternal = []\n[classify]\n"app" = "domain"\n'


def mapping(**overrides: Any) -> dict[str, Any]:
    return {**MINI, **overrides}


# --- `python` ------------------------------------------------------------------------------------------------------


def test_a_patch_version_is_read_as_its_minor_version() -> None:
    assert policy_from_mapping(mapping(python="3.12.1")).python == "3.12"


def test_an_unquoted_toml_3_10_is_an_error_not_a_silent_3_1() -> None:
    """TOML reads ``python = 3.10`` as the float 3.1."""
    with pytest.raises(PolicyError, match=r"'3\.1'"):
        _ = policy_from_mapping(mapping(python=3.10))


@pytest.mark.parametrize("version", ["3.10", "3.15", "abc", ""])
def test_an_unsupported_version_is_a_policy_error_naming_the_supported_ones(version: str) -> None:
    with pytest.raises(PolicyError, match=r"3\.11"):
        _ = policy_from_mapping(mapping(python=version))


# --- `type_checkers` -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [[], "pyright", [1], [""], None])
def test_type_checkers_must_be_a_non_empty_list_of_names(bad: object) -> None:
    with pytest.raises(PolicyError, match="type_checkers"):
        _ = policy_from_mapping(mapping(type_checkers=bad))


def test_generate_with_an_empty_type_checkers_list_is_a_policy_error_not_a_crash(tmp_path: Path) -> None:
    write_files(tmp_path, {"archlint.toml": "type_checkers = []\n" + TOML, "app/__init__.py": ""})
    assert main(["--config", str(tmp_path / "archlint.toml"), "generate"], services()) == 2


# --- bench ---------------------------------------------------------------------------------------------------------


def test_the_bench_command_reports_a_bad_policy_instead_of_a_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {"archlint.toml": 'python = "2.7"\n' + TOML, "app/__init__.py": ""})
    assert main(["bench", str(tmp_path)]) == 2
    assert "2.7" in capsys.readouterr().err


def test_bench_checkers_are_validated_before_anything_is_timed(tmp_path: Path) -> None:
    write_files(tmp_path, {"archlint.toml": TOML, "app/__init__.py": ""})  # the policy itself lists only pyright
    timed: list[str] = []

    class Spy(FakeToolRunner):
        def run(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            timed.append("run")
            return super().run(*args, **kwargs)

    with pytest.raises(PolicyError, match="pyrite"):
        _ = bench_one(tmp_path, ["pyrite"], lambda root: dataclasses.replace(services(Spy()), scanner=FsScanner(root / ".archlint")))
    assert timed == []


# --- Services ------------------------------------------------------------------------------------------------------


def test_services_cannot_be_built_without_saying_which_engines_run() -> None:
    """A forgotten ``engines`` used to mean: import-linter and tach silently skipped, ``check`` still green."""
    with pytest.raises(TypeError):
        _ = Services(FsScanner(), FakeToolRunner(), [], LocalFileSystem(), SystemClock())  # type: ignore[call-arg]


# --- one port for "an external checker" ------------------------------------------------------------------------------


def test_a_type_checker_is_an_engine_that_also_renders_its_config() -> None:
    assert Engine in TypeChecker.__mro__


# --- the folders every walker ignores --------------------------------------------------------------------------------


def test_walkers_share_one_core_set_of_ignored_folders() -> None:
    assert {"__pycache__", ".git", ".venv", "venv", "node_modules", ".archlint"} <= NOISE_DIRS
    assert NOISE_DIRS <= _PRUNE
    assert NOISE_DIRS <= practices._NOISE  # type: ignore[attr-defined]
    assert set(selftest._SKIP_COPY) >= NOISE_DIRS  # type: ignore[attr-defined]
    assert {"build", "dist"} <= practices._NOISE  # type: ignore[attr-defined]
    assert "build" not in _PRUNE  # a package named `build` is source, not noise
