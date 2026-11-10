"""``construct_only`` (#85): a class or factory may be called only from its owner modules and rings (the composition root)."""

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

TARGET = "app.adapters.Reader"
OWNED = [{"target": TARGET, "modules": ["app.main"], "reason": "only the composition root picks the reader"}]
DEFINES = "from __future__ import annotations\n\n\nclass Reader:\n    pass\n"


def files(**modules: str) -> dict[str, str]:
    return {"app/adapters.py": DEFINES, **{f"app/{name}.py": f"from __future__ import annotations\n\n{body}" for name, body in modules.items()}}


def run(root: Path, construct_only: list[dict[str, Any]] | None = None) -> list[Violation]:
    policy = mini_policy(construct_only=OWNED if construct_only is None else construct_only)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def outside(found: list[Violation]) -> list[tuple[str, int]]:
    return [(v.subject, v.line) for v in found if v.rule == Rule.CONSTRUCTION_OUTSIDE_SITE and v.line]


@pytest.mark.parametrize(
    "body",
    [
        "from app.adapters import Reader\n\nREADER = Reader()\n",
        "from app.adapters import Reader as R\n\nREADER = R()\n",
        "import app.adapters\n\nREADER = app.adapters.Reader()\n",
        "import app.adapters as a\n\nREADER = a.Reader()\n",
        "from app import adapters\n\nREADER = adapters.Reader()\n",
        "from . import adapters\n\nREADER = adapters.Reader()\n",
        "from .adapters import Reader\n\nREADER = Reader()\n",
    ],
)
def test_every_spelling_of_the_call_outside_the_owners_is_reported(repo: Path, body: str) -> None:
    write_files(repo, files(domain=body))
    found = run(repo)
    assert outside(found) == [("app.domain", 5)]
    detail = next(v.detail for v in found if v.rule == Rule.CONSTRUCTION_OUTSIDE_SITE)
    assert "ring domain" in detail and TARGET in detail and "app.main" in detail and "composition root" in detail


def test_the_owner_may_call_it(repo: Path) -> None:
    write_files(repo, files(main="from .adapters import Reader\n\nREADER = Reader()\n"))
    assert outside(run(repo)) == []


def test_naming_the_class_without_calling_it_is_not_construction(repo: Path) -> None:
    body = "from app.adapters import Reader\n\n\ndef accepts(reader: Reader) -> bool:\n    return isinstance(reader, Reader)\n"
    write_files(repo, files(domain=body))
    assert outside(run(repo)) == []


def test_a_same_named_class_that_is_not_imported_is_not_the_target(repo: Path) -> None:
    write_files(repo, files(domain="class Reader:\n    pass\n\n\nREADER = Reader()\n"))
    assert outside(run(repo)) == []


def test_a_method_call_on_a_value_is_not_construction(repo: Path) -> None:
    write_files(repo, files(domain="def f(reader: object) -> None:\n    reader.Reader()\n"))
    assert outside(run(repo)) == []


def test_the_defining_module_constructing_its_own_class_is_a_construction_site(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": DEFINES + "\n\ndef make() -> Reader:\n    return Reader()\n", "app/main.py": ""})
    assert outside(run(repo)) == [("app.adapters", 9)]


def test_a_lazy_call_inside_a_function_counts(repo: Path) -> None:
    write_files(repo, files(domain="def make() -> object:\n    from app.adapters import Reader\n\n    return Reader()\n"))
    assert outside(run(repo)) == [("app.domain", 6)]


def test_a_ring_restriction_applies_even_to_a_listed_module(repo: Path) -> None:
    write_files(repo, files(main="from .adapters import Reader\n\nREADER = Reader()\n"))
    found = run(repo, [{"target": TARGET, "modules": ["app.main"], "rings": ["adapters"]}])
    assert outside(found) == [("app.main", 5)]


def test_rings_alone_restrict_by_ring(repo: Path) -> None:
    body = "from .adapters import Reader\n\nREADER = Reader()\n"
    write_files(repo, files(main=body, ports=body))
    assert outside(run(repo, [{"target": TARGET, "rings": ["composition"]}])) == [("app.ports", 5)]


def test_an_owner_glob_or_a_target_module_that_matches_nothing_is_reported(repo: Path) -> None:
    write_files(repo, files(main=""))
    found = run(repo, [{"target": TARGET, "modules": ["app.main", "app.gone*"]}, {"target": "app.missing.Reader", "modules": ["app.main"]}])
    stale = [v.detail for v in found if v.rule == Rule.CONSTRUCTION_OUTSIDE_SITE and not v.line]
    assert len(stale) == 2 and any("app.gone*" in text for text in stale) and any("app.missing" in text for text in stale)


def test_a_violation_is_a_baseline_entry_like_any_other(repo: Path) -> None:
    write_files(repo, files(domain="from .adapters import Reader\n\nREADER = Reader()\n"))
    [violation] = [v for v in run(repo) if v.rule == Rule.CONSTRUCTION_OUTSIDE_SITE]
    assert violation.key.startswith("construction-outside-site|app.domain|")


@pytest.mark.parametrize(
    "value",
    ["x", [{"modules": ["app"]}], [{"target": "Reader", "modules": ["app"]}], [{"target": "app.has space", "modules": ["app"]}],
     [{"target": TARGET}], [{"target": TARGET, "modules": "app"}], [{"target": TARGET, "modules": [""]}], [{"target": TARGET, "rings": [1]}],
     [{"target": TARGET, "modules": ["a"], "reason": 1}]],
)
def test_the_setting_is_validated(value: object) -> None:
    with pytest.raises(PolicyError, match="construct_only"):
        policy_from_mapping({**MINI, "construct_only": value})


def test_it_is_off_by_default_and_parsed_into_entries() -> None:
    assert mini_policy().construct_only == ()
    [entry] = mini_policy(construct_only=OWNED).construct_only
    assert (entry.target, entry.modules, entry.rings, entry.reason) == (TARGET, ("app.main",), (), OWNED[0]["reason"])


def test_the_call_sites_are_recorded_for_imported_and_defined_names_only() -> None:
    source = (
        "import os\nfrom a.b import C\nfrom . import sib\nfrom ..up import D\n\n\nclass E:\n    pass\n\n\n"
        "C()\nsib.F()\nD()\nE()\nos.getcwd()\nlen([])\nself.G()\nx = C\n"
    )
    assert extract(source).calls == ((11, "a.b.C"), (12, ".sib.F"), (13, "..up.D"), (14, "E"), (15, "os.getcwd"))


def test_selftest_calls_each_target_from_a_module_that_does_not_own_it(repo: Path) -> None:
    write_files(repo, files(main=""))
    policy = mini_policy(construct_only=OWNED)
    mutations = plan(policy, FsScanner().scan(policy, repo))[0]
    [probe] = [m for m in mutations if m.rule == Rule.CONSTRUCTION_OUTSIDE_SITE]
    assert probe.file != "app/main.py" and TARGET in probe.append
