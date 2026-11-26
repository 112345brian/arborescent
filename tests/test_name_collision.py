"""``name_collisions`` (#221): a field or local reuses the name of an unrelated type. An observation, never a failure."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, services, write_files

from arborescent.check_runner import run_check
from arborescent.config import policy_from_mapping
from arborescent.domain import INFORMATIONAL_RULES, PolicyError, Rule, Violation
from arborescent.engine import Options, blocking, run_own, write_generated

HEADER = "from __future__ import annotations\n\n"
TYPES = HEADER + "class Ratchet:\n    pass\n\n\nclass ArchitectureScope:\n    pass\n\n\nclass Spec:\n    pass\n"


def files(main: str, domain: str = TYPES) -> dict[str, str]:
    return {"app/domain.py": domain, "app/main.py": HEADER + main}


def run(root: Path, table: dict[str, Any] | None = None, only: frozenset[str] | None = None) -> list[Violation]:
    policy = mini_policy(name_collisions={} if table is None else table)
    found, _ = run_own(policy, root, services(), only, practices=False)
    return [v for v in found if v.rule == Rule.NAME_COLLISION]


def details(found: list[Violation]) -> list[str]:
    return [v.detail for v in found]


@pytest.mark.parametrize(
    ("body", "what"),
    [
        ("class A:\n    ratchet: int = 0\n", "field 'A.ratchet'"),
        ("class A:\n    _Ratchet: int\n", "field 'A._Ratchet'"),
        ("class A:\n    def __init__(self) -> None:\n        self.ratchet = 1\n", "field 'A.ratchet'"),
        ("class A:\n    def go(self) -> None:\n        self.ratchet: int = 1\n", "field 'A.ratchet'"),
        ("def f() -> None:\n    ratchet = []\n", "local 'f.ratchet'"),
        ("def f() -> None:\n    ratchet = (1,)\n", "local 'f.ratchet'"),
        ("class A:\n    ratchet: list[str]\n", "field 'A.ratchet'"),
        ("def f() -> None:\n    ratchet = {k for k in ()}\n", "local 'f.ratchet'"),
        ("def f() -> None:\n    ratchet: tuple[str, ...]\n", "local 'f.ratchet'"),
        ("async def f() -> None:\n    if True:\n        ratchet = 3\n", "local 'f.ratchet'"),
    ],
)
def test_a_name_that_is_evidently_something_else_is_noted(repo: Path, body: str, what: str) -> None:
    write_files(repo, files(body))
    found = run(repo)
    assert details(found) == [f"{what} shares its name with the type app.domain.Ratchet"]
    assert found[0].subject == "app.main" and found[0].line > 2


@pytest.mark.parametrize(
    "body",
    [
        # the annotation names the type, or a more specific type of the same word: the same concept
        "class A:\n    ratchet: Ratchet\n",
        "class A:\n    ratchet: Ratchet | None = None\n",
        "class A:\n    ratchet: 'list[Ratchet]'\n",
        "class A:\n    ratchet: domain.NamedRatchet\n",
        "class A:\n    ratchet: Any = 1\n",
        "class A:\n    ratchet: object\n",
        # no evidence either way
        "def f() -> None:\n    ratchet = load()\n",
        "def f(x) -> None:\n    ratchet = x.ratchet\n",
        "def f() -> None:\n    ratchet = None\n",
        "def f() -> None:\n    ratchet, other = 1, 2\n",
        # not a field or a local
        "ratchet = 1\n",
        "def ratchet() -> None:\n    pass\n",
        # a str-typed value is a reference to the thing by its name
        "class A:\n    ratchet: str\n",
        "class A:\n    ratchet: str | None = None\n",
        "class A:\n    ratchet: Optional['str'] = None\n",
        "def f(ratchet: str) -> None:\n    pass\n",
        "def f() -> None:\n    ratchet = 'by-name'\n",
        "def f() -> None:\n    ratchet = f'{1}'\n",
        "def ratchet(x: int) -> str:\n    return ''\n",
        "def f(ratchet: int) -> None:\n    pass\n",  # parameters and functions are read for declared words only
        "def ratchet(x: int) -> bool:\n    pass\n",
        "class A:\n    def __ratchet__(self, x: int) -> bool:\n        pass\n",
        "class A:\n    RATCHET = 'ratchet'\n",
        "class A:\n    ratchet = 'an enum member or class constant'\n",
        "def f() -> None:\n    RATCHET = 1\n",
        # not an exact name, and too short for the default minimum
        "class A:\n    ratchets: int\n    spec: int\n    scope: int\n",
        "def f(other) -> None:\n    other.ratchet = 1\n",
    ],
)
def test_the_narrow_definition_stays_quiet(repo: Path, body: str) -> None:
    write_files(repo, files(body))
    assert run(repo) == []


def test_underscores_and_case_are_ignored_for_compound_names(repo: Path) -> None:
    write_files(repo, files("class A:\n    architecture_scope: int\n"))
    assert details(run(repo)) == ["field 'A.architecture_scope' shares its name with the type app.domain.ArchitectureScope"]


def test_a_declared_word_matches_the_last_word_of_a_type(repo: Path) -> None:
    write_files(repo, files("class RatchetSpec:\n    scope: tuple[str, ...] = ()\n"))
    assert run(repo) == []
    assert details(run(repo, {"words": ["scope"]})) == ["field 'RatchetSpec.scope' shares its name with the type app.domain.ArchitectureScope"]
    write_files(repo, files("class RatchetSpec:\n    scope: ArchitectureScope\n"))
    assert run(repo, {"words": ["scope"]}) == []


@pytest.mark.parametrize(
    ("body", "what"),
    [
        ("def f(ratchet: int) -> None:\n    pass\n", "parameter 'f.ratchet'"),
        ("class A:\n    def go(self, *, ratchet: list[str]) -> None:\n        pass\n", "parameter 'A.go.ratchet'"),
        ("def ratchet(x: int) -> bool:\n    return True\n", "function 'ratchet'"),
        ("class A:\n    def _in_ratchet(self) -> tuple[str, ...]:\n        return ()\n", "function 'A._in_ratchet'"),
    ],
)
def test_parameters_and_functions_are_noted_for_a_declared_word(repo: Path, body: str, what: str) -> None:
    write_files(repo, files(body))
    assert run(repo) == []
    assert details(run(repo, {"words": ["ratchet"]})) == [f"{what} shares its name with the type app.domain.Ratchet"]


@pytest.mark.parametrize(
    "body",
    [
        "def f(ratchet: str) -> None:\n    pass\n",
        "def f(ratchet) -> None:\n    pass\n",
        "def f(ratchet: Any, *ratchets: int, **kw: int) -> None:\n    pass\n",
        "def f(ratchet: Ratchet) -> None:\n    pass\n",
        "def ratchet(x) -> None:\n    pass\n",
        "def ratchet(x: int) -> str:\n    return ''\n",
        "def ratchet(x: int) -> Ratchet:\n    pass\n",
        "def ratchet(spec: NamedRatchet) -> bool:\n    pass\n",
        "class A:\n    def __ratchet__(self, x: int) -> bool:\n        pass\n",
    ],
)
def test_a_declared_word_stays_quiet_without_evidence_of_another_concept(repo: Path, body: str) -> None:
    write_files(repo, files(body))
    assert run(repo, {"words": ["ratchet"]}) == []


def test_a_declared_word_matches_the_last_word_of_a_function_name(repo: Path) -> None:
    """The issue's second case: a helper and a parameter still saying `scope` after the field was renamed."""
    write_files(repo, files("def _in_scope(spec: RatchetSpec, module: str) -> bool:\n    return True\n"))
    assert run(repo) == []
    assert details(run(repo, {"words": ["scope"]})) == ["function '_in_scope' shares its name with the type app.domain.ArchitectureScope"]
    write_files(repo, files("def measure(scope: tuple[str, ...]) -> int:\n    return 0\n"))
    assert details(run(repo, {"words": ["scope"]})) == ["parameter 'measure.scope' shares its name with the type app.domain.ArchitectureScope"]
    concept = "def _in_scope(scope: ArchitectureScope) -> bool:\n    pass\n\n\ndef make_scope(x: int) -> ArchitectureScope:\n    pass\n"
    write_files(repo, files(concept))
    assert run(repo, {"words": ["scope"]}) == []
    write_files(repo, files("def _in_scope(spec: RatchetSpec) -> bool:\n    return True\n"))
    assert run(repo, {"words": ["scope"], "allow": [{"name": "in_scope", "reason": "on purpose"}]}) == []


def test_the_module_that_defines_the_type_is_left_alone(repo: Path) -> None:
    write_files(repo, files("VALUE = 1\n", TYPES + "\n\nclass A:\n    ratchet: int\n"))
    assert run(repo) == []


def test_private_types_are_not_vocabulary(repo: Path) -> None:
    write_files(repo, files("class A:\n    ratchet: int\n", HEADER + "class _Ratchet:\n    pass\n"))
    assert run(repo) == []


def test_min_length_is_configurable_and_a_name_is_reported_once_per_owner(repo: Path) -> None:
    write_files(repo, files("class A:\n    spec: int\n\n    def f(self) -> None:\n        self.spec = 1\n        self.spec = 2\n"))
    assert run(repo) == []
    assert [(v.detail, v.line) for v in run(repo, {"min_length": 4})] == [("field 'A.spec' shares its name with the type app.domain.Spec", 4)]


def test_an_allowed_name_is_not_reported(repo: Path) -> None:
    write_files(repo, files("class A:\n    ratchet: int\n"))
    assert run(repo, {"allow": [{"name": "Ratchet", "reason": "the same word on purpose"}]}) == []


def test_it_is_an_observation_that_never_fails_or_enters_a_baseline(repo: Path) -> None:
    write_files(repo, files("from . import domain\n\n\nclass A:\n    ratchet: int = len(str(domain))\n"))
    policy = mini_policy(name_collisions={})
    svc = services()
    write_generated(policy, repo, svc)
    report = run_check(policy, repo, svc, Options(tools=False))
    assert Rule.NAME_COLLISION in INFORMATIONAL_RULES
    noted = [v for v in report.notes if v.rule == Rule.NAME_COLLISION]
    assert len(noted) == 1
    assert "name_collisions.allow" in noted[0].render()
    assert report.ok and report.violations == []
    found, _ = run_own(policy, repo, svc, practices=False)
    assert not [v for v in blocking(found) if v.rule == Rule.NAME_COLLISION]


def test_off_unless_the_table_is_present_and_skipped_on_changed_only_runs(repo: Path) -> None:
    write_files(repo, files("class A:\n    ratchet: int\n"))
    found, _ = run_own(mini_policy(), repo, services(), practices=False)
    assert not [v for v in found if v.rule == Rule.NAME_COLLISION]
    assert run(repo, only=frozenset({"app/main.py"})) == []


def test_edge_sources(repo: Path) -> None:
    write_files(repo, files("class A:\n    ratchet: 'not [ valid'\n"))
    (repo / "app/adapters.py").write_bytes(b"\xef\xbb\xbfclass B:\n    ratchet: int\n")  # a byte-order mark
    (repo / "app/ports.py").write_text("", encoding="utf-8")
    assert [v.subject for v in run(repo)] == ["app.adapters"]  # an unreadable annotation is no evidence


def test_vocabulary_is_per_scope(repo: Path) -> None:
    write_files(repo, {
        "libs/a/src/alpha/__init__.py": "",
        "libs/a/src/alpha/types.py": "class Ratchet:\n    pass\n",
        "libs/b/src/beta/__init__.py": "",
        "libs/b/src/beta/use.py": "class A:\n    ratchet: int\n",
    })
    scoped = policy_from_mapping({"name_collisions": {}, "scopes": {
        name: {"sources": [f"libs/{key}/src/{name}"], "source_roots": [f"libs/{key}/src"], "rings": ["core"], "classify": {f"{name}*": "core"}}
        for key, name in (("a", "alpha"), ("b", "beta"))
    }})
    found, _ = run_own(scoped, repo, services(), practices=False)
    assert not [v for v in found if v.rule == Rule.NAME_COLLISION]
    assert scoped.name_collisions is not None


@pytest.mark.parametrize(
    "table",
    [
        [],
        {"min_length": 0},
        {"min_length": True},
        {"words": "scope"},
        {"words": [""]},
        {"allow": {}},
        {"allow": ["ratchet"]},
        {"allow": [{"name": "ratchet"}]},
        {"allow": [{"name": "ratchet", "reason": "  "}]},
        {"allow": [{"name": "", "reason": "why"}]},
        {"enabled": True},
    ],
)
def test_a_malformed_table_is_rejected(table: object) -> None:
    with pytest.raises(PolicyError, match="name_collisions"):
        policy_from_mapping({**MINI, "name_collisions": table})
