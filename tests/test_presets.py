"""Presets: ``strict`` is the default and is exactly the behaviour Arborescent had before presets existed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError
from arborescent.generate import generate
from arborescent.presets import IO_MODULES, PRESETS
from arborescent.rules import check_modules

NAMES = ["app", "app.domain", "app.ports", "app.adapters", "app.main"]


def found_rules(root: Path, **overrides: Any) -> set[tuple[str, str]]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    return {(v.subject, v.rule) for v in check_modules(policy, scan.modules, scan.known, scan.entry_deps)}


def linter_text(**overrides: Any) -> str:
    return generate(mini_policy(**overrides), NAMES, [])["arborescent-importlinter.toml"]


# --- the default is strict, and strict is the old behaviour -------------------------------------------------------


def test_default_preset_is_strict() -> None:
    assert mini_policy().preset.name == "strict"
    assert mini_policy().preset is PRESETS["strict"]


def test_strict_restricts_plain_stdlib_too(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import json\n"})
    assert found_rules(repo) == {("app.domain", "external-import")}


def test_strict_import_linter_bans_the_io_modules_per_ring() -> None:
    text = linter_text()
    assert '"subprocess"' in text and '"sqlite3"' in text


# --- the three presets, side by side ------------------------------------------------------------------------------

# (source written to app/domain.py, rule expected per preset)
SCOPE_CASES = {
    "plain stdlib": ("import json\n", {"strict": True, "standard": False, "relaxed": False}),
    "io stdlib": ("import subprocess\n", {"strict": True, "standard": True, "relaxed": False}),
    "third party": ("import psutil\n", {"strict": True, "standard": True, "relaxed": True}),
}


@pytest.mark.parametrize("preset", sorted(PRESETS))
@pytest.mark.parametrize("case", sorted(SCOPE_CASES))
def test_external_scope_per_preset(repo: Path, preset: str, case: str) -> None:
    source, expected = SCOPE_CASES[case]
    write_files(repo, {"app/domain.py": source})
    fired = ("app.domain", "external-import") in found_rules(repo, preset=preset)
    assert fired is expected[preset]


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_os_branching_enforcement_per_preset(repo: Path, preset: str) -> None:
    write_files(repo, {"app/ports.py": "import sys\nsys.platform\n"})
    ring = ring_with(ports={"may_import": ["domain"], "external": ["sys"]})
    fired = ("app.ports", "os-branching") in found_rules(repo, preset=preset, ring=ring)
    assert fired is PRESETS[preset].os_branching


@pytest.mark.parametrize("preset", sorted(PRESETS))
def test_layering_is_enforced_by_every_preset(repo: Path, preset: str) -> None:
    write_files(repo, {"app/domain.py": "from . import adapters\n", "app/newcomer.py": "X = 1\n"})
    assert found_rules(repo, preset=preset) == {
        ("app.domain", "internal-import"),
        ("app.newcomer", "unclassified"),
        ("app.adapters, app.domain, app.ports", "circular-import"),
    }


def test_relaxed_import_linter_bans_only_declared_third_party() -> None:
    text = linter_text(preset="relaxed", dependencies={"psutil": "psutil"})
    assert '"subprocess"' not in text
    assert '"psutil"' in text


def test_relaxed_without_third_party_emits_no_empty_contract() -> None:
    assert "forbidden_modules = []" not in linter_text(preset="relaxed")


# --- the I/O module set is the policy's, not a constant buried in the generator -----------------------------------


def test_io_modules_can_be_replaced_and_emptied() -> None:
    assert '"sqlite3"' not in linter_text(io_modules=["subprocess"])
    assert '"subprocess"' in linter_text(io_modules=["subprocess"])
    assert '"subprocess"' not in linter_text(io_modules=[], dependencies={"psutil": "psutil"})


def test_replaced_io_modules_drive_the_own_check_too(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import sqlite3\n"})
    assert found_rules(repo, preset="standard") == {("app.domain", "external-import")}
    assert found_rules(repo, preset="standard", io_modules=["subprocess"]) == set()


def test_default_io_modules_are_the_documented_set() -> None:
    assert mini_policy().io_modules == IO_MODULES
    assert {"os", "subprocess", "socket", "sqlite3"} <= IO_MODULES


# --- bad input is reported, never repaired ------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Strict", "", "lenient", 3, None, ["strict"]])
def test_unknown_or_malformed_preset_is_an_error(bad: object) -> None:
    with pytest.raises(PolicyError, match="preset"):
        _ = mini_policy(preset=bad)


def test_unknown_preset_error_lists_the_valid_names() -> None:
    with pytest.raises(PolicyError) as err:
        _ = mini_policy(preset="lenient")
    assert all(name in str(err.value) for name in PRESETS)


@pytest.mark.parametrize("bad", ["subprocess", 3, [3], [""], None])
def test_malformed_io_modules_is_an_error(bad: object) -> None:
    with pytest.raises(PolicyError, match="io_modules"):
        _ = mini_policy(io_modules=bad)


def test_policy_without_a_preset_key_still_loads() -> None:
    assert policy_from_mapping(MINI).preset.name == "strict"


def test_optional_imports_land_in_the_tach_external_exclude() -> None:
    files = generate(mini_policy(optional_imports=["livekit", "sentry_sdk"]), NAMES, [])
    assert 'exclude = ["livekit", "sentry_sdk"]' in files["tach.toml"]
    assert "exclude = []" in generate(mini_policy(), NAMES, [])["tach.toml"]
