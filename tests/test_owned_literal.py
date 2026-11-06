"""``owned_literal`` (#112): a string such as an environment variable name may be spelled only by its owner modules and rings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.facts import extract
from arborescent.rules import check_modules
from arborescent.selftest import plan

NAME = "APP_CONFIG"
OWNED = [{"literal": NAME, "modules": ["app.adapters"], "reason": "the process protocol"}]


def spell(*modules: str) -> dict[str, str]:
    return {f"app/{module}.py": f'from __future__ import annotations\n\nVAR: str = "{NAME}"\n' for module in modules}


def run(root: Path, owned: list[dict[str, Any]] | None = None) -> list[Violation]:
    policy = mini_policy(owned_literal=OWNED if owned is None else owned)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def outside(found: list[Violation]) -> list[tuple[str, int]]:
    return [(v.subject, v.line) for v in found if v.rule == Rule.LITERAL_OUTSIDE_OWNER and v.line]


def test_a_module_that_does_not_own_the_literal_may_not_spell_it(repo: Path) -> None:
    write_files(repo, spell("adapters", "domain"))
    found = run(repo)
    assert outside(found) == [("app.domain", 3)]
    detail = next(v.detail for v in found if v.rule == Rule.LITERAL_OUTSIDE_OWNER)
    assert "ring domain" in detail and "the process protocol" in detail and "app.adapters" in detail


def test_the_owner_may_spell_it_and_a_different_string_is_free(repo: Path) -> None:
    write_files(repo, {**spell("adapters"), "app/domain.py": 'X: str = "OTHER"\n'})
    assert outside(run(repo)) == []


def test_a_ring_restriction_applies_even_to_a_listed_module(repo: Path) -> None:
    write_files(repo, spell("adapters"))
    found = run(repo, [{"literal": NAME, "modules": ["app.adapters"], "rings": ["infrastructure"]}])
    assert outside(found) == [("app.adapters", 3)]


def test_rings_alone_restrict_by_ring(repo: Path) -> None:
    write_files(repo, spell("adapters", "ports"))
    assert outside(run(repo, [{"literal": NAME, "rings": ["adapters"]}])) == [("app.ports", 3)]


def test_a_docstring_is_prose_not_a_spelling(repo: Path) -> None:
    write_files(repo, {"app/domain.py": f'"""{NAME}"""\n\ndef f() -> None:\n    """{NAME}"""\n'})
    assert outside(run(repo)) == []
    assert extract(f'"""{NAME}"""\nA = "{NAME}"\n').string_literals == ((2, NAME),)


def test_an_owner_glob_that_matches_no_module_is_reported(repo: Path) -> None:
    found = run(repo, [{"literal": NAME, "modules": ["app.adapters", "app.gone*"]}])
    [stale] = [v for v in found if v.rule == Rule.LITERAL_OUTSIDE_OWNER and not v.line]
    assert "app.gone*" in stale.detail


@pytest.mark.parametrize(
    "value",
    ["x", [{"modules": ["app"]}], [{"literal": "has space", "modules": ["app"]}], [{"literal": NAME}], [{"literal": NAME, "modules": "app"}],
     [{"literal": NAME, "modules": [""]}], [{"literal": NAME, "rings": [1]}], [{"literal": NAME, "modules": ["a"], "reason": 1}]],
)
def test_the_setting_is_validated(value: object) -> None:
    with pytest.raises(PolicyError, match="owned_literal"):
        policy_from_mapping({**MINI, "owned_literal": value})


def test_it_is_off_by_default_and_parsed_into_entries() -> None:
    assert mini_policy().owned_literals == ()
    [entry] = mini_policy(owned_literal=OWNED).owned_literals
    assert (entry.literal, entry.modules, entry.rings, entry.reason) == (NAME, ("app.adapters",), (), "the process protocol")


def test_selftest_spells_each_literal_in_a_module_that_does_not_own_it(repo: Path) -> None:
    policy = mini_policy(owned_literal=OWNED)
    mutations = plan(policy, FsScanner().scan(policy, repo))[0]
    [probe] = [m for m in mutations if m.rule == Rule.LITERAL_OUTSIDE_OWNER]
    assert probe.file != "app/adapters.py" and NAME in probe.append
