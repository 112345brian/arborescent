"""``port_binding.shadow`` (#219): a function reaching an effect category outside the port's one implementation. An observation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, services, write_files

from arborescent.check_runner import run_check
from arborescent.config import policy_from_mapping
from arborescent.domain import INFORMATIONAL_RULES, PolicyError, Rule, Violation
from arborescent.engine import Options, blocking, run_own, write_generated

PORT = "app.ports.Clock"
HEADER = "from __future__ import annotations\n\n"
CLOCK = HEADER + "from typing import Protocol\n\n\nclass Clock(Protocol):\n    def now(self) -> float: ...\n"
REAL = "import time\n\n\nclass SystemClock:\n    def now(self) -> float:\n        return time.perf_counter()\n"
OTHER_ROUTE = "import time\n\n\ndef f() -> float:\n    return time.monotonic()\n"


def files(**modules: str) -> dict[str, str]:
    return {"app/ports.py": CLOCK, **{f"app/{name}.py": HEADER + body for name, body in {"adapters": REAL, **modules}.items()}}


def entry(**changes: Any) -> dict[str, Any]:
    return {"port": PORT, "shadow": ["monotonic"], **changes}


def run(root: Path, only: frozenset[str] | None = None, **changes: Any) -> list[Violation]:
    found, _ = run_own(mini_policy(port_binding=[entry(**changes)]), root, services(), only, practices=False)
    return [v for v in found if v.rule == Rule.PORT_SHADOW]


def reached(found: list[Violation]) -> list[str]:
    return [v.detail.split(" outside ")[0] for v in found]


def test_the_one_implementation_alone_is_quiet(repo: Path) -> None:
    write_files(repo, files(main="def go(clock) -> float:\n    return clock.now()\n"))
    assert run(repo) == []


def test_a_function_taking_another_route_to_the_same_category_is_noted_but_never_fails(repo: Path) -> None:
    write_files(repo, files(main="import time\n\n\ndef elapsed() -> float:\n    return time.monotonic()\n"))
    found = run(repo)
    assert [v.detail for v in found] == [
        f"elapsed() uses monotonic (time.monotonic) outside app.adapters.SystemClock, the one implementation of {PORT}"
    ]
    assert found[0].subject == "app.main" and found[0].line == 7
    assert Rule.PORT_SHADOW in INFORMATIONAL_RULES
    assert not [v for v in blocking(found) if v.rule == Rule.PORT_SHADOW]
    assert "shadow_allow" in found[0].render()


def test_a_free_function_beside_the_adapter_is_noted_but_what_the_adapter_calls_is_its_own(repo: Path) -> None:
    body = (
        "import time\n\n\ndef _read() -> float:\n    return _deeper()\n\n\ndef _deeper() -> float:\n    return time.perf_counter()\n\n\n"
        "class _Source:\n    def get(self) -> float:\n        return time.monotonic()\n\n\n"
        "class Base:\n    def raw(self) -> float:\n        return time.perf_counter_ns()\n\n\n"
        "class SystemClock(Base):\n    def now(self) -> float:\n        return _read() + _Source().get() + self._own()\n\n"
        "    def _own(self) -> float:\n        return time.perf_counter()\n\n\n"
        "def get_now() -> float:\n    return time.perf_counter()\n"
    )
    write_files(repo, files(adapters=body))
    assert reached(run(repo, exclude=["app.adapters.Base"])) == ["get_now() uses monotonic (time.perf_counter)"]


def test_methods_module_level_code_and_one_note_per_function_and_category(repo: Path) -> None:
    main = (
        "import time\n\nSTART = time.monotonic()\n\n\nclass Job:\n    def run(self) -> float:\n"
        "        def inner() -> float:\n            return time.monotonic()\n\n        return inner() + time.perf_counter()\n"
    )
    write_files(repo, files(main=main))
    assert reached(run(repo)) == [
        "Job.run() uses monotonic (time.monotonic, time.perf_counter)",
        "module-level code uses monotonic (time.monotonic)",
    ]


@pytest.mark.parametrize(
    "body",
    [
        "import time\n\n\ndef f(clock=time.monotonic) -> float:\n    return clock()\n",  # an injection seam is the sanctioned shape
        "import time\n\n\ndef f() -> float:\n    return time.time()\n",  # another category (`clock`, not `monotonic`)
        "def f(time) -> float:\n    return 0.0\n",
        "",
    ],
)
def test_what_is_not_a_shadow(repo: Path, body: str) -> None:
    write_files(repo, files(main=body))
    assert run(repo) == []


def test_an_exception_class_or_constant_reaches_nothing(repo: Path) -> None:
    tool = "import subprocess\n\n\nclass SystemClock:\n    def now(self) -> float:\n        return subprocess.run([]).returncode\n"
    main = "import subprocess\n\n\ndef f() -> object:\n    return (subprocess.TimeoutExpired, subprocess.DEVNULL, subprocess.CalledProcessError)\n"
    write_files(repo, files(adapters=tool, main=main))
    assert run(repo, shadow=["process"]) == []


def test_shadow_allow_exclude_and_test_doubles(repo: Path) -> None:
    use = "import time\n\n\ndef f() -> float:\n    return time.monotonic()\n\n\ndef g() -> float:\n    return time.monotonic()\n"
    write_files(repo, files(main=use, testing=use, test_it=use, fakes=use))
    assert {v.subject for v in run(repo)} == {"app.main"}
    assert run(repo, shadow_allow=["app.main"]) == []
    assert [d.split("(")[0] for d in reached(run(repo, shadow_allow=["app.main.f"]))] == ["g"]
    assert run(repo, exclude=["app.main"]) == []


def test_a_category_the_adapter_does_not_use_is_reported_not_silently_ignored(repo: Path) -> None:
    write_files(repo, files(main="import socket\n\n\ndef f() -> object:\n    return socket.socket()\n"))
    assert [v.detail for v in run(repo, shadow=["network"])] == [
        "`shadow` lists network, but app.adapters.SystemClock uses no network effect the scan can see: nothing to compare"
    ]


@pytest.mark.parametrize("adapters", ["VALUE = 1\n", REAL + "\n\nclass Second(SystemClock):\n    pass\n"])
def test_without_exactly_one_implementation_only_the_binding_rule_speaks(repo: Path, adapters: str) -> None:
    write_files(repo, files(adapters=adapters, main=OTHER_ROUTE))
    assert run(repo) == []


def test_off_unless_shadow_is_listed_and_skipped_on_changed_only_runs(repo: Path) -> None:
    write_files(repo, files(main=OTHER_ROUTE))
    assert run(repo, shadow=[]) == []
    assert run(repo, only=frozenset({"app/main.py"})) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"shadow": "monotonic"},
        {"shadow": [""]},
        {"shadow": ["time"]},
        {"shadow": ["patch"]},
        {"shadow": True},
        {"shadow": [1]},
        {"max": 2},
        {"shadow_allow": "app"},
        {"shadow_allow": [""]},
        {"shadow": [], "shadow_allow": ["app"]},
    ],
)
def test_a_malformed_entry_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(PolicyError, match="port_binding"):
        policy_from_mapping({**MINI, "port_binding": [entry(**changes)]})


def test_check_reports_it_as_a_note_and_stays_ok(repo: Path) -> None:
    write_files(repo, files(main=OTHER_ROUTE))
    policy = mini_policy(port_binding=[entry()])
    svc = services()
    write_generated(policy, repo, svc)
    report = run_check(policy, repo, svc, Options(tools=False))
    assert [v.subject for v in report.notes if v.rule == Rule.PORT_SHADOW] == ["app.main"]
    assert not [v for v in report.violations if v.rule == Rule.PORT_SHADOW]


def test_an_adapter_made_by_a_factory_function_owns_what_the_factory_holds(repo: Path) -> None:
    body = (
        "import time\n\n\ndef make() -> object:\n    class SystemClock:\n        def now(self) -> float:\n"
        "            return time.perf_counter()\n\n    return SystemClock()\n"
    )
    write_files(repo, files(adapters=body))
    assert run(repo) == []
