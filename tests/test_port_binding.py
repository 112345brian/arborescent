"""``port_binding`` (#219): a declared port has at least one production implementor, and no more than the policy expects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, services, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.engine import blocking, run_own
from arborescent.facts import extract
from arborescent.selftest import plan, selftest

PORT = "app.ports.Clock"
HEADER = "from __future__ import annotations\n\n"
CLOCK = HEADER + "from typing import Protocol\n\n\nclass Clock(Protocol):\n    def now(self) -> float: ...\n"
REAL = "class SystemClock:\n    def now(self) -> float:\n        return 0.0\n"


def files(port: str = CLOCK, **modules: str) -> dict[str, str]:
    return {"app/ports.py": port, **{f"app/{name}.py": HEADER + body for name, body in modules.items()}}


def policy(*entries: dict[str, Any]) -> Any:
    return mini_policy(port_binding=list(entries) or [{"port": PORT}])


def run(root: Path, *entries: dict[str, Any], only: frozenset[str] | None = None) -> list[Violation]:
    found, _ = run_own(policy(*entries), root, services(), only, practices=False)
    return [v for v in found if v.rule == Rule.PORT_BINDING]


def details(found: list[Violation]) -> list[str]:
    return [v.detail for v in found]


def test_one_structural_implementor_is_clean(repo: Path) -> None:
    write_files(repo, files(adapters=REAL))
    assert run(repo) == []


def test_no_implementor_is_a_violation_that_can_be_baselined(repo: Path) -> None:
    write_files(repo, files(adapters="VALUE = 1\n"))
    found = run(repo)
    assert details(found) == ["declared port has no production implementor"]
    assert found[0].subject == PORT
    assert blocking(found) == found


def test_a_second_implementor_is_a_violation_naming_both(repo: Path) -> None:
    write_files(repo, files(adapters=REAL, main="class Other:\n    def now(self) -> float:\n        return 1.0\n"))
    assert details(run(repo)) == ["declared port has 2 production implementors, at most 1 expected: app.adapters.SystemClock, app.main.Other"]


def test_max_allows_a_deliberate_plugin_point_and_reason_is_reported(repo: Path) -> None:
    write_files(repo, files(adapters=REAL + "\n\nclass Second(SystemClock):\n    pass\n"))
    assert run(repo, {"port": PORT, "max": 2}) == []
    assert details(run(repo, {"port": PORT, "reason": "one clock only"}))[0].endswith(": one clock only")


@pytest.mark.parametrize(
    "body",
    [
        # explicit inheritance counts even when the class overrides nothing
        "from app.ports import Clock\n\n\nclass A(Clock):\n    pass\n",
        "from .ports import Clock\n\n\nclass A(Clock):\n    pass\n",
        "from . import ports\n\n\nclass A(ports.Clock):\n    pass\n",
        "import app.ports as p\n\n\nclass A(p.Clock):\n    pass\n",
        # members: an instance attribute, a class attribute, conditionally defined
        "class A:\n    def __init__(self) -> None:\n        self.now = lambda: 0.0\n",
        "class A:\n    now: object = None\n",
        "import sys\n\n\nclass A:\n    if sys.platform:\n        def now(self) -> float:\n            return 0.0\n",
        # a class made by a factory function is still a production class
        "def make():\n    class A:\n        def now(self) -> float:\n            return 0.0\n\n    return A\n",
    ],
)
def test_the_forms_that_count_as_one_implementor(repo: Path, body: str) -> None:
    write_files(repo, files(adapters=body))
    assert run(repo) == []


def test_members_are_inherited_from_a_first_party_base(repo: Path) -> None:
    write_files(repo, files(adapters="class Base:\n    def now(self) -> float:\n        return 0.0\n\n\nclass A(Base):\n    pass\n"))
    # the base is a structural implementor in its own right, so the subclass is the second one
    assert details(run(repo)) == ["declared port has 2 production implementors, at most 1 expected: app.adapters.A, app.adapters.Base"]


@pytest.mark.parametrize(
    "body",
    [
        "from typing import Protocol\n\nfrom app.ports import Clock\n\n\nclass Sub(Clock, Protocol):\n    def later(self) -> float: ...\n",
        "import abc\n\n\nclass A(abc.ABC):\n    def now(self) -> float:\n        return 0.0\n",
        "import abc\n\n\nclass A:\n    @abc.abstractmethod\n    def now(self) -> float: ...\n",
        "class FakeClock:\n    def now(self) -> float:\n        return 0.0\n",
        "class _StubClock:\n    def now(self) -> float:\n        return 0.0\n",
        "class MockClock:\n    def now(self) -> float:\n        return 0.0\n",
        "def now() -> float:\n    return 0.0\n",  # a module function is not a class
        "class A:\n    def later(self) -> float:\n        return 0.0\n",
    ],
)
def test_what_does_not_count(repo: Path, body: str) -> None:
    write_files(repo, files(adapters=body))
    assert details(run(repo)) == ["declared port has no production implementor"]


def test_a_testing_module_holds_doubles_and_exclude_names_more(repo: Path) -> None:
    write_files(repo, {**files(adapters=REAL, testing="class Manual:\n    def now(self) -> float:\n        return 1.0\n")})
    assert run(repo) == []
    write_files(repo, files(main="class Frozen:\n    def now(self) -> float:\n        return 2.0\n"))
    assert len(run(repo)) == 1
    assert run(repo, {"port": PORT, "exclude": ["app.main.Frozen"]}) == []
    assert run(repo, {"port": PORT, "exclude": ["app.main"]}) == []


def test_a_name_that_only_starts_like_a_double_still_counts(repo: Path) -> None:
    write_files(repo, files(adapters="class Stubborn:\n    def now(self) -> float:\n        return 0.0\n"))
    assert run(repo) == []


def test_a_structural_match_needs_every_member_including_those_of_parent_protocols(repo: Path) -> None:
    port = CLOCK + "\n\nclass Named(Clock, Protocol):\n    name: str\n"
    write_files(repo, files(port, adapters=REAL))
    assert details(run(repo, {"port": "app.ports.Named"})) == ["declared port has no production implementor"]
    write_files(repo, files(port, adapters=REAL + "    name = 'x'\n"))
    assert run(repo, {"port": "app.ports.Named"}) == []


def test_a_protocol_with_no_members_counts_only_explicit_subclasses(repo: Path) -> None:
    port = HEADER + "from typing import Protocol\n\n\nclass Marker(Protocol):\n    pass\n"
    write_files(repo, files(port, adapters=REAL))
    assert details(run(repo, {"port": "app.ports.Marker"})) == ["declared port has no production implementor"]


def test_an_abc_port_counts_only_explicit_subclasses(repo: Path) -> None:
    port = HEADER + "import abc\n\n\nclass Clock(abc.ABC):\n    @abc.abstractmethod\n    def now(self) -> float: ...\n"
    write_files(repo, files(port, adapters=REAL))
    assert details(run(repo)) == ["declared port has no production implementor"]
    write_files(repo, files(port, adapters="from app.ports import Clock\n\n\n" + REAL.replace("SystemClock", "SystemClock(Clock)")))
    assert run(repo) == []


def test_a_generic_protocol_and_a_reexported_port_resolve(repo: Path) -> None:
    port = HEADER + "from typing import Protocol, TypeVar\n\nT = TypeVar('T')\n\n\nclass Box(Protocol[T]):\n    def get(self) -> T: ...\n"
    write_files(repo, files(port, domain="from app.ports import Box\n\n__all__ = ['Box']\n", adapters="from app.domain import Box\n\n\nclass A(Box[int]):\n    pass\n"))
    assert run(repo, {"port": "app.ports.Box"}) == []
    assert run(repo, {"port": "app.domain.Box"}) == []


def test_an_inheritance_cycle_does_not_hang(repo: Path) -> None:
    write_files(repo, files(adapters="class A(B):\n    pass\n\n\nclass B(A):\n    pass\n"))
    assert details(run(repo)) == ["declared port has no production implementor"]


@pytest.mark.parametrize(
    ("port", "expected"),
    [
        ("app.nowhere.Clock", "`port_binding`: module 'app.nowhere' does not exist"),
        ("app.ports.Gone", "`port_binding`: no class 'app.ports.Gone' was found"),
        ("app.adapters.SystemClock", "`port_binding`: 'app.adapters.SystemClock' is not a Protocol or an abstract class"),
    ],
)
def test_a_stale_or_wrong_target_is_reported_not_ignored(repo: Path, port: str, expected: str) -> None:
    write_files(repo, files(adapters=REAL))
    assert details(run(repo, {"port": port})) == [expected]


def test_a_changed_only_run_does_not_judge_a_whole_repository_fact(repo: Path) -> None:
    write_files(repo, files(adapters=REAL))
    assert run(repo, only=frozenset({"app/ports.py"})) == []


def test_without_the_setting_nothing_is_checked(repo: Path) -> None:
    write_files(repo, files(adapters="VALUE = 1\n"))
    found, _ = run_own(mini_policy(), repo, services(), practices=False)
    assert not [v for v in found if v.rule == Rule.PORT_BINDING]


def test_each_scope_declares_its_own_ports_and_implementors_are_counted_repository_wide(repo: Path) -> None:
    write_files(repo, {
        "libs/a/src/alpha/__init__.py": "",
        "libs/a/src/alpha/ports.py": CLOCK,
        "libs/b/src/beta/__init__.py": "",
        "libs/b/src/beta/clock.py": REAL,
    })
    scoped = policy_from_mapping({"scopes": {
        "a": {"sources": ["libs/a/src/alpha"], "source_roots": ["libs/a/src"], "rings": ["core"], "classify": {"alpha*": "core"},
              "port_binding": [{"port": "alpha.ports.Clock"}]},
        "b": {"sources": ["libs/b/src/beta"], "source_roots": ["libs/b/src"], "rings": ["core"], "classify": {"beta*": "core"}},
    }})
    found, _ = run_own(scoped, repo, services(), practices=False)
    assert not [v for v in found if v.rule == Rule.PORT_BINDING]


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"port": ""},
        {"port": "Clock"},
        {"port": "app.ports.Clock\x00"},
        {"port": PORT, "max": 0},
        {"port": PORT, "max": True},
        {"port": PORT, "max": "2"},
        {"port": PORT, "exclude": "app"},
        {"port": PORT, "exclude": [""]},
        {"port": PORT, "reason": 1},
        {"port": PORT, "maximum": 2},
    ],
)
def test_a_malformed_entry_is_rejected(entry: dict[str, Any]) -> None:
    with pytest.raises(PolicyError, match="port_binding"):
        policy_from_mapping({**MINI, "port_binding": [entry]})


def test_a_port_listed_twice_or_a_non_array_is_rejected() -> None:
    for value in ([{"port": PORT}, {"port": PORT, "max": 2}], {"port": PORT}, [PORT]):
        with pytest.raises(PolicyError, match="port_binding"):
            policy_from_mapping({**MINI, "port_binding": value})


def test_class_facts_survive_the_scan_cache(repo: Path, tmp_path: Path) -> None:
    write_files(repo, files(adapters=REAL))
    svc = services(cache=tmp_path / "cache")
    first = run_own(policy(), repo, svc, practices=False)[1]
    second = run_own(policy(), repo, svc, practices=False)[1]
    assert second.parsed == 0
    assert second.modules["app.adapters"].facts.classes == first.modules["app.adapters"].facts.classes != ()


def test_class_facts_edge_inputs() -> None:
    assert extract("").classes == ()
    assert extract("class A:\n    pass\n").classes[0].members == ()
    nested = extract("class A:\n    class B:\n        def f(*args):\n            args.x = 1\n").classes
    assert [c.name for c in nested] == ["A", "A.B"]
    assert nested[1].members == ("f",)


@pytest.mark.parametrize("entry", [{"port": PORT}, {"port": PORT, "max": 3}, {"port": PORT, "exclude": ["app.main"]}])
def test_selftest_proves_the_rule_bites(repo: Path, entry: dict[str, Any]) -> None:
    write_files(repo, files(adapters=REAL))
    declared = mini_policy(port_binding=[entry])
    mutations, skipped = plan(declared, FsScanner().scan(declared, repo))
    assert [m for m in mutations if m.rule == Rule.PORT_BINDING], skipped
    report = selftest(declared, repo, services())
    assert any(item.startswith("[port-binding]") for item in report.killed)
    assert not any("port-binding" in item for item in report.survived)


def test_selftest_says_why_a_binding_has_no_probe(repo: Path) -> None:
    write_files(repo, files(adapters=REAL))
    declared = mini_policy(port_binding=[{"port": "app.ports.Gone"}, {"port": PORT, "exclude": ["app"]}])
    mutations, skipped = plan(declared, FsScanner().scan(declared, repo))
    assert not [m for m in mutations if m.rule == Rule.PORT_BINDING]
    assert len([reason for reason in skipped if reason.startswith("port_binding ")]) == 2
