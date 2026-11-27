"""A `[ring.<name>]` table holds only ring keys: a typo such as `trait` must fail, not silently enforce less (#222)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import (
    MINI_FILES,
    RINGS,
    mini_policy,
    ring_with,
    services,
    write_files,
)

from arborescent.cli import main
from arborescent.config import load_policy, policy_from_mapping
from arborescent.domain import PolicyError
from arborescent.engine import Options, child_arbor_runs
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.package_model import CatalogError
from arborescent.package_tree import shipped_policy
from arborescent.tool_runner import SubprocessToolRunner

TYPO = """\
rings = ["domain"]

[ring.domain]
trait = ["no-env"]

[classify]
"*" = "domain"
"""


@pytest.mark.parametrize(
    ("typo", "meant"),
    [
        ("trait", "traits"),
        ("trait_s", "traits"),
        ("Traits", "traits"),
        ("may_imports", "may_import"),
        ("externals", "external"),
        ("max_reexport_ration", "max_reexport_ratio"),
    ],
)
def test_a_misspelt_key_is_rejected_with_the_closest_one(typo: str, meant: str) -> None:
    with pytest.raises(PolicyError) as error:
        mini_policy(ring=ring_with(domain={typo: []}))

    assert f"unknown key in [ring.domain]: {typo!r} (did you mean {meant!r}?)" in str(error.value)


def test_an_unrelated_key_lists_the_known_ones_without_a_guess() -> None:
    with pytest.raises(PolicyError) as error:
        mini_policy(ring=ring_with(domain={"colour": "red"}))

    assert "'colour'; known keys: may_import, external" in str(error.value)
    assert "did you mean" not in str(error.value)


@pytest.mark.parametrize("key", ["", 1, None, "for\0bid_everything_here"])
def test_odd_keys_are_policy_errors_not_crashes(key: object) -> None:
    with pytest.raises(PolicyError, match="unknown key"):
        mini_policy(ring={**RINGS, "domain": {key: [], "zzz": []}})


def test_every_known_key_is_accepted_and_so_is_an_empty_table() -> None:
    full: dict[str, Any] = {
        "may_import": [], "external": ["sys"], "traits": ["no-env", "no-env-at-import", "require-abstract"], "max_reexport_ratio": 0.5,
    }

    mini_policy(ring={**RINGS, "domain": full})
    mini_policy(ring={**RINGS, "domain": {}})
    mini_policy(ring={})


@pytest.mark.parametrize("table", [5, "forbid", ["forbid"], True])
def test_a_ring_entry_that_is_not_a_table_is_a_policy_error(table: object) -> None:
    with pytest.raises(PolicyError, match=r"\[ring\.domain\] must be a table"):
        mini_policy(ring={**RINGS, "domain": table})


@pytest.mark.parametrize("ring", [5, "domain", ["domain"]])
def test_a_ring_setting_that_is_not_a_table_is_a_policy_error(ring: object) -> None:
    with pytest.raises(PolicyError, match="`ring` must hold one table per ring"):
        mini_policy(ring=ring)


def test_a_scoped_policy_rejects_the_typo_too() -> None:
    scope = {"sources": ["a"], "source_roots": ["."], "rings": ["domain"], "ring": {"domain": {"trait": []}}}

    with pytest.raises(PolicyError, match="did you mean 'traits'"):
        policy_from_mapping({"scopes": {"alpha": scope}})


@pytest.mark.parametrize(
    ("ring_setting", "refusal"),
    [
        ('[ring.domain]\ntrait = ["no-env"]\n', "did you mean 'traits'"),
        ("[ring]\ndomain = 3\n", r"\[ring\.domain\] must be a table"),
        ("ring = 3\n", "`ring` must hold one table per ring"),
    ],
)
def test_tables_merged_with_tach_layers_are_checked(tmp_path: Path, ring_setting: str, refusal: str) -> None:
    write_files(tmp_path, {  # `rings` comes from tach's layers
        "arborescent.toml": f'classify_from = "tach.toml"\n{ring_setting}',
        "tach.toml": 'layers = ["domain"]\n',
    })

    with pytest.raises(PolicyError, match=refusal):
        load_policy(LocalFileSystem(), tmp_path / "arborescent.toml")


def test_a_scope_with_classify_from_is_checked(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "arborescent.toml": (
            '[scopes.a]\nsources = ["."]\nsource_roots = ["."]\nrings = ["domain"]\nclassify_from = "tach.toml"\n'
            '[scopes.a.ring.domain]\ntrait = []\n'
        ),
        "tach.toml": 'layers = ["domain"]\n',
    })

    with pytest.raises(PolicyError, match="did you mean 'traits'"):
        load_policy(LocalFileSystem(), tmp_path / "arborescent.toml")


# Each caller of `load_policy`, and what it does with the new refusal.


def test_the_command_line_reports_it_and_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {"arborescent.toml": TYPO})

    assert main(["--config", str(tmp_path / "arborescent.toml"), "check"]) == 2
    assert "did you mean 'traits'" in capsys.readouterr().err


def test_bench_reports_it_and_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {"arborescent.toml": TYPO})

    assert main(["bench", str(tmp_path)]) == 2
    assert "did you mean 'traits'" in capsys.readouterr().err


def test_a_shipped_policy_with_it_is_invalid(tmp_path: Path) -> None:
    write_files(tmp_path, {"arborescent.toml": TYPO})

    with pytest.raises(CatalogError, match="did you mean 'traits'"):
        shipped_policy(LocalFileSystem(), tmp_path, tmp_path / "arborescent.toml")


def test_a_child_arbor_with_it_fails_the_parent_run(tmp_path: Path) -> None:
    write_files(tmp_path, {**MINI_FILES, "child/arborescent.toml": TYPO})

    runner = SubprocessToolRunner(tmp_path)

    (run,) = child_arbor_runs(mini_policy(children=["child"]), tmp_path, services(runner), Options())

    assert run.status == "fail"
    assert "did you mean 'traits'" in "\n".join(run.lines)
