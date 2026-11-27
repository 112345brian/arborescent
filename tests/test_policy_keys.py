"""The policy root and each scope table hold only known keys: a typo such as `rign` must fail, not silently enforce less."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, MINI_FILES, mini_policy, services, write_files

from arborescent.cli import main
from arborescent.config import (
    _ROOT_TABLE_KEYS,
    _SCOPE_TABLE_KEYS,
    load_policy,
    policy_from_mapping,
)
from arborescent.domain import PolicyError
from arborescent.engine import Options, child_arbor_runs
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.package_model import CatalogError
from arborescent.package_tree import shipped_policy
from arborescent.tool_runner import SubprocessToolRunner

TYPO = """\
rings = ["domain"]
forbid_circular_import = false

[classify]
"*" = "domain"
"""
SCOPE: dict[str, Any] = {"sources": ["a"], "source_roots": ["."], "rings": ["domain"]}
REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("typo", "meant"),
    [
        ("rign", "ring"),
        ("clasify", "classify"),
        ("source_root", "source_roots"),
        ("forbid-circular-imports", "forbid_circular_imports"),
        ("mixedpurity", "mixed_purity"),
        ("Exclude", "exclude"),
        ("scope", "scopes"),
        ("lazy_imports", "lazy_import"),
        ("vendor_root", "vendor_roots"),
    ],
)
def test_a_misspelt_root_key_is_rejected_with_the_closest_one(typo: str, meant: str) -> None:
    with pytest.raises(PolicyError) as error:
        mini_policy(**{typo: {}})

    assert f"unknown key in the policy root: {typo!r} (did you mean {meant!r}?)" in str(error.value)


def test_unrelated_keys_are_all_named_with_the_known_ones_and_no_guess() -> None:
    with pytest.raises(PolicyError) as error:
        mini_policy(zzzzzzzzzzzz=1, qqqqqqqqqqqq=2)

    message = str(error.value)
    assert "'qqqqqqqqqqqq', 'zzzzzzzzzzzz'; known keys: " in message
    assert "did you mean" not in message


@pytest.mark.parametrize("key", ["", 1, None, "ri\0ngs_and_more"])
def test_odd_keys_are_policy_errors_not_crashes(key: Any) -> None:
    with pytest.raises(PolicyError, match="unknown key in the policy root"):
        policy_from_mapping({**MINI, key: []})


def test_a_misspelt_key_in_a_scope_is_rejected() -> None:
    with pytest.raises(PolicyError, match=re.escape("unknown key in [scopes.alpha]: 'rign' (did you mean 'ring'?)")):
        policy_from_mapping({"scopes": {"alpha": {**SCOPE, "rign": {}}}})


@pytest.mark.parametrize("key", ["python", "exclude", "mixed_purity", "lazy_import", "unguarded_module_state"])
def test_a_root_setting_inside_a_scope_is_rejected_and_says_where_it_goes(key: str) -> None:
    with pytest.raises(PolicyError, match=re.escape(f"{key!r} (a root setting: set it outside `scopes`)")):
        policy_from_mapping({"scopes": {"alpha": {**SCOPE, key: []}}})


def test_a_misspelt_root_key_beside_scopes_is_rejected() -> None:
    with pytest.raises(PolicyError, match="did you mean 'exclude'"):
        policy_from_mapping({"excludes": [], "scopes": {"alpha": SCOPE}})


def test_an_architecture_key_beside_scopes_keeps_its_own_message() -> None:
    with pytest.raises(PolicyError, match="root-level architecture keys cannot be combined"):
        policy_from_mapping({"rings": ["a"], "scopes": {"alpha": SCOPE}})


class _Recording(Mapping[str, Any]):
    """A table that remembers every key anything asks it for, present or not."""

    def __init__(self, data: Mapping[str, Any], seen: set[str]) -> None:
        self.data, self.seen = data, seen

    def __getitem__(self, key: str) -> Any:
        self.seen.add(key)
        return self.data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)


def test_every_key_a_reader_asks_for_is_accepted() -> None:
    """Fails when a new setting is read but not listed, which would make it unusable."""
    root: set[str] = set()
    scoped_root: set[str] = set()
    scope: set[str] = set()

    _ = policy_from_mapping(_Recording(MINI, root))
    _ = policy_from_mapping(_Recording({"scopes": {"alpha": _Recording(SCOPE, scope)}}, scoped_root))

    assert root - _ROOT_TABLE_KEYS == set()
    assert scoped_root - _ROOT_TABLE_KEYS == set()
    assert scope - _SCOPE_TABLE_KEYS == set()
    assert len(root) > 40  # the recorder does see the reads


def test_every_documented_key_is_accepted() -> None:
    text = (REPOSITORY / "docs" / "configuration.md").read_text(encoding="utf-8")

    def first_column(start: str, end: str) -> set[str]:
        section = text.split(start, 1)[1].split(end, 1)[0]
        rows = (line.split("|")[1] for line in section.splitlines() if line.startswith("| `"))
        return {name for row in rows for name in re.findall(r"`([a-z_]+)`", row)}

    top = first_column("## Single-scope top-level keys", "## Multi-scope configuration")
    scoped = first_column("## Multi-scope configuration", "## Nested package arbors")
    subtables = set(re.findall(r"^\[\[?tool\.arborescent\.([a-z_]+)", text, re.MULTILINE))

    assert top and top - _ROOT_TABLE_KEYS == set()
    assert scoped and scoped - _SCOPE_TABLE_KEYS == set()
    assert subtables and subtables - _ROOT_TABLE_KEYS == set()


# Keys that only `load_policy` reads.


def test_the_keys_load_policy_expands_are_accepted(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "arborescent.toml": 'classify_from = "tach.toml"\nvendor_roots = ["vendor"]\nlibrary_roots = ["libs"]\n',
        "tach.toml": 'layers = ["domain"]\n',
        "vendor/one/__init__.py": "",
        "libs/two/__init__.py": "",
    })
    _ = load_policy(LocalFileSystem(), tmp_path / "arborescent.toml")

    write_files(tmp_path, {
        "arborescent.toml": '[scopes.a]\nsources = ["."]\nsource_roots = ["."]\nclassify_from = "tach.toml"\nrings = ["domain"]\n',
    })
    _ = load_policy(LocalFileSystem(), tmp_path / "arborescent.toml")


# Each caller of `load_policy`, and what it does with the new refusal.


def test_the_command_line_reports_it_and_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {"arborescent.toml": TYPO})

    assert main(["--config", str(tmp_path / "arborescent.toml"), "check"]) == 2
    assert "did you mean 'forbid_circular_imports'" in capsys.readouterr().err


def test_bench_reports_it_and_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {"arborescent.toml": TYPO})

    assert main(["bench", str(tmp_path)]) == 2
    assert "did you mean 'forbid_circular_imports'" in capsys.readouterr().err


def test_a_shipped_policy_with_it_is_invalid(tmp_path: Path) -> None:
    write_files(tmp_path, {"arborescent.toml": TYPO})

    with pytest.raises(CatalogError, match="did you mean 'forbid_circular_imports'"):
        _ = shipped_policy(LocalFileSystem(), tmp_path, tmp_path / "arborescent.toml")


def test_a_child_arbor_with_it_fails_the_parent_run(tmp_path: Path) -> None:
    write_files(tmp_path, {**MINI_FILES, "child/arborescent.toml": TYPO})
    runner = SubprocessToolRunner(tmp_path)

    (run,) = child_arbor_runs(mini_policy(children=["child"]), tmp_path, services(runner), Options())

    assert run.status == "fail"
    assert "did you mean 'forbid_circular_imports'" in "\n".join(run.lines)
