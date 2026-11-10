from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule
from arborescent.facts import extract
from arborescent.rules import check_modules

NAMES = ["A", "B", "C", "D", "E", "F"]
DOMAIN = "from __future__ import annotations\n\n" + "".join(f"{n}: int = 1\n" for n in NAMES)
RINGS_LIMIT = ring_with(composition={"may_import": ["*"], "external": ["@pure", "sys"], "max_reexport_ratio": 0.5})


def run(root: Path, main: str, rings: dict[str, Any] | None = None) -> list[Any]:
    write_files(root, {"app/domain.py": DOMAIN, "app/main.py": main})
    policy = mini_policy(ring=rings or RINGS_LIMIT)
    scan = FsScanner().scan(policy, root)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    return [v for v in found if v.rule == Rule.REEXPORT_RATIO]


def test_mostly_reexported_surface_is_flagged_with_a_stable_baseline_key(repo: Path) -> None:
    found = run(repo, "from .domain import A, B, C, D, E\nlocal = 1\n")
    assert [v.subject for v in found] == ["app.main"]
    assert "5 of" not in found[0].detail  # counts live in the hint: the baseline key must not churn as names come and go
    assert "5 of 6" in found[0].hint


def test_all_declares_the_surface(repo: Path) -> None:
    code = "from .domain import A, B, C, D, E, F\n\ndef f() -> None: ...\n\n__all__ = ['A', 'B', 'C', 'D', 'E', 'F']\n"
    found = run(repo, code)
    assert len(found) == 1 and found[0].line == 5
    local_heavy = run(repo, "from .domain import A\n\n" + "".join(f"def f{i}() -> None: ...\n" for i in range(6)) + "__all__ = ['A', 'f0', 'f1', 'f2', 'f3', 'f4']\n")
    assert local_heavy == []


def test_a_ring_without_a_limit_is_exempt(repo: Path) -> None:
    assert run(repo, "from .domain import A, B, C, D, E, F\n", ring_with()) == []


def test_below_the_minimum_surface_is_not_judged(repo: Path) -> None:
    assert run(repo, "from .domain import A, B\n") == []
    assert run(repo, "") == []


def test_third_party_and_stdlib_imports_are_not_internal_reexports(repo: Path) -> None:
    assert run(repo, "from sys import argv, path, modules, platform, version, exit\n") == []


def test_alias_and_module_imports_count(repo: Path) -> None:
    code = "from .domain import A as a1, B as b1, C as c1\nfrom . import domain as d1\nimport app.domain as d2\n"
    assert len(run(repo, code)) == 1


def test_private_names_and_dunders_and_future_do_not_count(repo: Path) -> None:
    code = "from __future__ import annotations\nfrom .domain import A as _a, B as _b, C as _c, D as _d, E as _e, F as _f\nx = 1\n"
    assert run(repo, code) == []


def test_dynamic_all_falls_back_to_bindings_and_augmented_all_accumulates() -> None:
    dynamic = extract("from a import x\n__all__ = [n for n in dir()]\n")
    assert dynamic.surface_size == 1
    grown = extract("from a import x\n__all__ = ['x']\n__all__ += ['y']\ny = 1\n")
    assert grown.surface_size == 2 and [r[0] for r in grown.reexports] == ["x"]
    assert extract("").surface_size == 0
    assert extract("__all__ = ['ghost']\n").reexports == ()


def test_threshold_is_validated() -> None:
    for bad in (True, "x", 1.5, -0.1):
        with pytest.raises(PolicyError):
            policy_from_mapping({**mini_policy.__globals__["MINI"], "ring": ring_with(domain={"external": [], "max_reexport_ratio": bad})})
    assert mini_policy().ring("domain").max_reexport_ratio is None
