"""``mixed-purity`` ring filter, weak kinds, memoised functions and calls through ports (#195)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from test_mixed_purity import HYDRATION, RINGS, pure_function, run

from arborescent.adapters import FsScanner
from arborescent.cli_purity import run_purity
from arborescent.config import PolicyError
from arborescent.domain import INFORMATIONAL_RULES, Rule
from arborescent.rules import check_modules
from arborescent.selftest import selftest

PURE = pure_function("clean_a") + pure_function("clean_b")
BODY = "    a = 1\n    b = a + 1\n    c = b + 1\n    d = c + 1\n"
PRINTER = "def go() -> None:\n    print('x')\n\n\n"
LOGGER = "import logging\n\nLOG = logging.getLogger(__name__)\n\n\ndef note() -> None:\n    LOG.info('x')\n\n\n"


def rules_for(repo: Path, files: dict[str, str], rule: Rule = Rule.MIXED_PURITY, **extra: object) -> list[tuple[str, str]]:
    shutil.rmtree(repo / "app", ignore_errors=True)
    write_files(repo, {"app/__init__.py": "", **files})
    policy = mini_policy(ring=RINGS, **extra)
    scan = FsScanner().scan(policy, repo)
    return [(v.rule.value, v.subject) for v in check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index) if v.rule == rule]


# --- ring filter ---------------------------------------------------------------------------------------------------------


def test_rings_limits_the_rule_to_the_listed_rings(repo: Path) -> None:
    files = {"app/domain.py": PURE + PRINTER, "app/adapters.py": PURE + PRINTER}
    assert {s for _, s in rules_for(repo, files)} == {"app.domain", "app.adapters"}
    assert {s for _, s in rules_for(repo, files, mixed_purity={"rings": ["domain"]})} == {"app.domain"}


def test_skip_unrestricted_rings_skips_rings_that_forbid_nothing(repo: Path) -> None:
    files = {"app/domain.py": PURE + PRINTER, "app/main.py": PURE + PRINTER}  # main is the outermost ring: nothing forbidden
    policy = mini_policy(ring=RINGS)
    assert not policy.forbidden_effects("composition") and policy.forbidden_effects("domain")
    assert {s for _, s in rules_for(repo, files)} == {"app.domain", "app.main"}
    assert {s for _, s in rules_for(repo, files, mixed_purity={"skip_unrestricted_rings": True})} == {"app.domain"}


def test_an_unknown_ring_name_is_a_policy_error(repo: Path) -> None:
    with pytest.raises(PolicyError, match="rings"):
        mini_policy(ring=RINGS, mixed_purity={"rings": ["nowhere"]})


def test_the_purity_command_honours_the_ring_filter(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {"app/__init__.py": "", "app/domain.py": PURE + PRINTER, "app/adapters.py": PURE + PRINTER})
    assert run_purity(mini_policy(ring=RINGS, mixed_purity={"rings": ["domain"]}), repo, services(), as_json=True) == 0
    assert [row["module"] for row in json.loads(capsys.readouterr().out)] == ["app.domain"]


def test_selftest_still_kills_the_rule_mutants_under_a_ring_filter(repo: Path) -> None:
    write_files(repo, {"app/__init__.py": "", "app/domain.py": PURE, "app/adapters.py": PURE, "app/ports.py": PURE, "app/main.py": PURE})
    report = selftest(mini_policy(ring=RINGS, mixed_purity={"rings": ["adapters"]}), repo, services())
    assert [m for m in report.survived if "mixed-purity" in str(m)] == []
    report = selftest(mini_policy(ring=RINGS, mixed_purity={"exclude_rings": ["domain"]}), repo, services())
    assert [m for m in report.survived if "mixed-purity" in str(m)] == []


# --- weak kinds ----------------------------------------------------------------------------------------------------------


def test_default_is_unchanged_weak_only_modules_are_mixed(repo: Path) -> None:
    assert [s for _, s in rules_for(repo, {"app/domain.py": PURE + LOGGER})] == ["app.domain"]


def test_weak_only_modules_move_to_the_informational_rule(repo: Path) -> None:
    weak = {"weak_kinds": ["console", "logging", "memo"]}
    files = {"app/domain.py": PURE + LOGGER, "app/adapters.py": PURE + PRINTER}
    assert rules_for(repo, files, mixed_purity=weak) == []
    assert {s for _, s in rules_for(repo, files, Rule.MIXED_PURITY_WEAK, mixed_purity=weak)} == {"app.domain", "app.adapters"}


def test_a_module_with_a_strong_kind_stays_mixed(repo: Path) -> None:
    strong = "import os\n\n\ndef listing(path: str) -> list[str]:\n    return os.listdir(path)\n\n\n"
    weak = {"weak_kinds": ["console", "logging", "memo"]}
    found = rules_for(repo, {"app/domain.py": PURE + LOGGER + strong}, mixed_purity=weak)
    assert [s for _, s in found] == ["app.domain"]
    assert rules_for(repo, {"app/domain.py": PURE + LOGGER + strong}, Rule.MIXED_PURITY_WEAK, mixed_purity=weak) == []


def test_weak_kinds_must_be_known_kinds(repo: Path) -> None:
    with pytest.raises(PolicyError, match="weak_kinds"):
        mini_policy(ring=RINGS, mixed_purity={"weak_kinds": ["nonsense"]})


def test_a_weak_note_never_fails_and_is_never_baselined() -> None:
    assert Rule.MIXED_PURITY_WEAK in INFORMATIONAL_RULES


def test_the_purity_command_marks_weak_rows(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {"app/__init__.py": "", "app/domain.py": PURE + LOGGER, "app/adapters.py": HYDRATION})
    policy = mini_policy(ring=RINGS, mixed_purity={"weak_kinds": ["logging"]})
    assert run_purity(policy, repo, services(), as_json=True) == 0
    rows = {row["module"]: row for row in json.loads(capsys.readouterr().out)}
    assert rows["app.domain"]["weak"] is True and rows["app.adapters"]["weak"] is False


# --- memoised pure functions ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("decorator, header", [
    ("@functools.lru_cache(maxsize=None)", "import functools"),
    ("@functools.cache", "import functools"),
    ("@cache", "from functools import cache"),
])
def test_a_memoised_pure_function_is_not_effectful(repo: Path, decorator: str, header: str) -> None:
    code = f"{header}\n\n\n{decorator}\ndef compile_it(x: int) -> int:\n{BODY}    return x + d\n\n\n" + PURE + PRINTER
    (mixed,) = run(repo, {"app/domain.py": code})
    assert "compile_it" in mixed.hint.split("; pure: ")[1] and "memo" not in mixed.hint.split("; pure:")[0]


def test_a_module_level_cache_that_is_written_is_still_memo(repo: Path) -> None:
    code = "_CACHE: dict[int, int] = {}\n\n\ndef go(x: int) -> int:\n    _CACHE[x] = x\n    return x\n\n\n" + PURE
    (mixed,) = run(repo, {"app/domain.py": code})
    assert "memo" in mixed.hint


def test_a_memoised_function_that_also_has_an_effect_is_effectful(repo: Path) -> None:
    code = f"import functools\n\n\n@functools.lru_cache\ndef load(x: int) -> str:\n{BODY}    return open('f').read()\n\n\n" + PURE
    (mixed,) = run(repo, {"app/domain.py": code})
    assert "load" in mixed.hint.split("; pure:")[0] and "filesystem" in mixed.hint


# --- calls through injected ports ----------------------------------------------------------------------------------------

PORTS = "from typing import Protocol\n\n\nclass Reader(Protocol):\n    def read(self, path: str) -> str: ...\n"


def _loader(annotation: str = "Reader", header: str = "from .ports import Reader") -> str:
    return f"{header}\n\n\ndef load(reader: {annotation}, path: str) -> str:\n{BODY}    return reader.read(path)\n\n\n"


def test_default_a_loader_that_calls_a_port_is_pure(repo: Path) -> None:
    assert run(repo, {"app/ports.py": PORTS, "app/domain.py": _loader() + PURE + PRINTER})[0].hint.count("load") == 1
    (mixed,) = run(repo, {"app/ports.py": PORTS, "app/domain.py": _loader() + PURE + PRINTER})
    assert "load" in mixed.hint.split("; pure: ")[1]


@pytest.mark.parametrize("annotation, header", [
    ("Reader", "from .ports import Reader"),
    ("Reader | None", "from .ports import Reader"),
    ("'Reader'", "from .ports import Reader"),
    ("ports.Reader", "from . import ports"),
    ("Reader", "from app.ports import Reader"),
])
def test_ambient_patterns_match_a_call_on_a_parameter_typed_with_a_port(repo: Path, annotation: str, header: str) -> None:
    files = {"app/ports.py": PORTS, "app/domain.py": _loader(annotation, header) + PURE + PRINTER}
    (mixed,) = run(repo, files, mixed_purity={"ambient": ["app.ports.Reader.*"]})
    effectful = mixed.hint.split("effectful: ")[1].split("; pure:")[0]
    assert "load" in effectful and "ambient" in mixed.hint


def test_a_local_that_shadows_the_parameter_is_not_the_port(repo: Path) -> None:
    code = (
        "from .ports import Reader\n\n\ndef load(reader: Reader, path: str) -> str:\n"
        f"{BODY}    reader = path\n    return reader.upper()\n\n\n" + PURE + PRINTER
    )
    (mixed,) = run(repo, {"app/ports.py": PORTS, "app/domain.py": code}, mixed_purity={"ambient": ["app.ports.Reader.*"]})
    assert "load" in mixed.hint.split("; pure: ")[1]


def test_an_unrelated_annotation_is_not_matched(repo: Path) -> None:
    code = _loader("str", "") + PURE + PRINTER
    (mixed,) = run(repo, {"app/domain.py": code}, mixed_purity={"ambient": ["app.ports.Reader.*"]})
    assert "load" in mixed.hint.split("; pure: ")[1]
