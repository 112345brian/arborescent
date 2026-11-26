"""``private_access`` (#218): a module reaching for another module's leading-underscore name or submodule.

Static and syntactic: ``from m import _x`` and ``m._x`` through an import, on first-party modules only. Attributes of objects
(``self._x``, ``Class._x``, a namedtuple's ``_replace``), dunders, a bare ``_``, ``getattr`` and shadowed names are not reported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, MINI_FILES, mini_policy, services, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.facts import extract
from arborescent.rules import check_modules
from arborescent.scan_cache_codec import facts_from_json, facts_to_json
from arborescent.selftest import plan, selftest

OWNER = "from __future__ import annotations\n\n_secret: int = 1\n\n\ndef _helper() -> int:\n    return _secret\n\n\nclass Thing:\n    _attr: int = 1\n"
SETTING: dict[str, Any] = {}
CLASSIFY = {"app*": "composition"}


def run(root: Path, files: dict[str, str], setting: dict[str, Any] | None = SETTING, **overrides: Any) -> list[Violation]:
    write_files(root, {"app/__init__.py": "", "app/domain.py": OWNER, **files})
    extra = {} if setting is None else {"private_access": setting}
    policy = mini_policy(classify=CLASSIFY, **extra, **overrides)
    scan = FsScanner().scan(policy, root)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    return [v for v in found if v.rule == Rule.PRIVATE_ACCESS]


def targets(found: list[Violation]) -> list[tuple[str, str]]:
    return sorted((v.subject, v.detail.split("uses ", 1)[1].split(",", 1)[0]) for v in found)


@pytest.mark.parametrize(
    "source",
    [
        "from app.domain import _helper\n",
        "from .domain import _helper as helper\n",
        "from app.domain import Thing, _helper\n",
        "import app.domain\n\nX = app.domain._helper()\n",
        "import app.domain as d\n\nX = d._helper\n",
        "from app import domain\n\nX = domain._helper()\n",
        "from . import domain\n\n\ndef f() -> int:\n    return domain._helper()\n",
        "def f() -> int:\n    from app.domain import _helper\n\n    return _helper()\n",
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from app.domain import _helper\n",
        "from app import domain\n\ndomain._helper = None\n",
    ],
)
def test_each_way_of_reaching_a_private_name_is_reported(repo: Path, source: str) -> None:
    assert targets(run(repo, {"app/other.py": source})) == [("app.other", "app.domain._helper")]


def test_the_finding_names_the_owner_and_the_line_once_per_name(repo: Path) -> None:
    source = "from app import domain\n\nA = domain._helper()\nB = domain._helper()\nC = domain._secret\n"
    found = run(repo, {"app/other.py": source})
    assert targets(found) == [("app.other", "app.domain._helper"), ("app.other", "app.domain._secret")]
    assert sorted(v.line for v in found) == [3, 5]
    assert all("private to app.domain" in v.detail for v in found)


@pytest.mark.parametrize(
    "source",
    [
        "from app.domain import Thing\n\nX = Thing._attr\n",  # an object's attribute, not a module's
        "from app import domain\n\nX = domain.__name__, domain.__doc__, domain.Thing._attr\n",
        "import os\nfrom collections import namedtuple\n\nos._exit(0)\nX = namedtuple('X', 'a')._make([1])\n",  # not first-party
        "from app.domain import __doc__\n",
        "from app.domain import Thing as _Thing\nimport app.domain as _d\n\nX = _Thing, _d.Thing\n",  # a private alias of a public thing
        "class A:\n    def f(self) -> None:\n        self._x = 1\n",
        "from app import domain\n\n\ndef f(domain: object) -> object:\n    return domain._helper\n",  # shadowed
        "from app import domain\n\nX = getattr(domain, '_helper')\n",
        "from app.domain import *\n",
        "",
    ],
)
def test_what_is_not_a_cross_module_private_access(repo: Path, source: str) -> None:
    assert run(repo, {"app/other.py": source}) == []


def test_a_bare_underscore_is_not_private(repo: Path) -> None:
    assert run(repo, {"app/i18n.py": "def _(s: str) -> str:\n    return s\n", "app/other.py": "from app.i18n import _\n"}) == []


def test_a_module_may_use_its_own_names_and_a_package_its_submodules(repo: Path) -> None:
    files = {
        "app/pkg/__init__.py": "_shared = 1\nfrom . import _impl\nfrom ._impl import run\n",
        "app/pkg/_impl.py": "from app.pkg import _shared\n\n\ndef run() -> None: ...\n",
        "app/pkg/user.py": "from . import _impl\nfrom ._impl import run\nimport app.pkg._impl\n\nX = app.pkg._impl.run\n",
        "app/domain.py": "from app import domain\n\n_x = 1\nY = domain._x\n",
    }
    assert run(repo, files) == []


@pytest.mark.parametrize(
    "source",
    ["from app.pkg import _impl\n", "from app.pkg._impl import run\n", "import app.pkg._impl\n", "from .pkg._impl import run\n"],
)
def test_a_private_submodule_is_private_to_its_package(repo: Path, source: str) -> None:
    files = {"app/pkg/__init__.py": "", "app/pkg/_impl.py": "def run() -> None: ...\n", "app/other.py": source}
    assert targets(run(repo, files)) == [("app.other", "app.pkg._impl")]


def test_it_is_off_by_default(repo: Path) -> None:
    assert run(repo, {"app/other.py": "from app.domain import _helper\n"}, setting=None) == []
    assert mini_policy().private_access is None


def test_test_modules_are_exempt_by_default_and_exempt_replaces_the_default(repo: Path) -> None:
    use = "from app.domain import _helper\n"
    files = {"app/tests/__init__.py": "", "app/tests/test_domain.py": use, "app/test_other.py": use, "app/conftest.py": use}
    assert run(repo, files) == []
    assert len(run(repo, files, {"exempt": []})) == 3
    assert [v.subject for v in run(repo, files, {"exempt": ["app.tests", "app.conftest"]})] == ["app.test_other"]


def test_rings_limit_which_modules_are_checked_and_allow_names_a_sanctioned_target(repo: Path) -> None:
    files = {"app/other.py": "from app.domain import _helper, _secret\n"}
    assert run(repo, files, {"rings": ["domain"]}) == []
    assert len(run(repo, files, {"rings": ["composition"]})) == 2
    assert targets(run(repo, files, {"allow": ["app.domain._secret"]})) == [("app.other", "app.domain._helper")]
    assert run(repo, files, {"allow": ["app.domain"]}) == []


@pytest.mark.parametrize(
    "value",
    [True, [], {"rings": "domain"}, {"exempt": [""]}, {"allow": [1]}, {"other": []}, {"rings": ["nope"]}],
)
def test_a_malformed_table_is_a_policy_error(value: Any) -> None:
    with pytest.raises(PolicyError, match="private_access"):
        policy_from_mapping({**MINI, "private_access": value})


def test_the_facts_survive_the_cache() -> None:
    facts = extract("from app import domain\nfrom . import x\n\nA = domain._a\nB = x._b\n")
    assert facts.private_attrs == ((4, "app.domain._a"), (5, ".x._b"))
    assert facts_from_json(facts_to_json(facts)).private_attrs == facts.private_attrs


def test_a_relative_import_above_the_root_is_not_resolved(repo: Path) -> None:
    assert run(repo, {"app/other.py": "from .... import _x\n"}) == []


def test_selftest_probes_the_rule_when_enabled(repo: Path) -> None:
    write_files(repo, MINI_FILES)
    off = mini_policy()
    assert Rule.PRIVATE_ACCESS not in {m.rule for m in plan(off, FsScanner().scan(off, repo))[0]}
    policy = mini_policy(private_access={})
    assert Rule.PRIVATE_ACCESS in {m.rule for m in plan(policy, FsScanner().scan(policy, repo))[0]}
    report = selftest(policy, repo, services())
    assert any(entry.startswith("[private-cross-module-access]") for entry in report.killed)
    assert not any(entry.startswith("[private-cross-module-access]") for entry in report.survived)


def test_selftest_says_so_when_no_module_can_be_probed(repo: Path) -> None:
    write_files(repo, MINI_FILES)
    policy = mini_policy(private_access={"exempt": ["app"]})
    mutations, skipped = plan(policy, FsScanner().scan(policy, repo))
    assert Rule.PRIVATE_ACCESS not in {m.rule for m in mutations}
    assert any(entry.startswith("private-cross-module-access:") for entry in skipped)
