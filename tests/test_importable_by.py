"""``importable_by`` (#111): the inverse of ``module_forbid_imports``. A listed module may be imported only by the modules its globs match."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import CLASSIFY, MINI, mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.rules import check_modules
from arborescent.selftest import plan

EXT_CLASSIFY = {"app.ext": "domain", "app.ext.rules": "domain"}  # inner ring: ring rules say nothing, so only importable_by speaks
EXT_FILES = {"app/ext/__init__.py": "", "app/ext/rules.py": "HINTS: int = 1\n"}


def run(root: Path, **overrides: Any) -> list[Violation]:
    policy = mini_policy(classify={**CLASSIFY, **EXT_CLASSIFY}, **overrides)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def restricted(found: list[Violation]) -> list[tuple[str, str]]:
    return [(v.subject, v.target or "") for v in found if v.rule == Rule.RESTRICTED_IMPORT]


def test_a_module_outside_the_allowlist_may_not_import_the_restricted_module(repo: Path) -> None:
    found = run(repo, importable_by={"app.domain": ["app.ports"]})
    assert restricted(found) == [("app.adapters", "app.domain")]  # ports is listed; adapters is not
    assert "importable only by ['app.ports']" in next(v for v in found if v.rule == Rule.RESTRICTED_IMPORT).detail


def test_the_allowlist_takes_globs_and_a_module_not_listed_is_unrestricted(repo: Path) -> None:
    assert restricted(run(repo, importable_by={"app.domain": ["app.p*", "app.adapters"]})) == []
    assert restricted(run(repo, importable_by={"app.ports": ["app.adapters"]})) == []  # nothing imports ports except adapters


def test_every_spelling_of_the_import_is_checked(repo: Path) -> None:
    for line in ("from app import domain", "from app.domain import VALUE", "import app.domain", "from . import domain"):
        write_files(repo, {"app/adapters.py": f"from __future__ import annotations\n\n{line}\n"})
        assert restricted(run(repo, importable_by={"app.domain": ["app.ports"]})) == [("app.adapters", "app.domain")], line


def test_a_restricted_package_covers_its_submodules_and_its_own_modules_may_import_each_other(repo: Path) -> None:
    write_files(repo, {**EXT_FILES, "app/ext/__init__.py": "from . import rules\n", "app/ports.py": "from app.ext import rules\n"})
    found = run(repo, importable_by={"app.ext": ["app.adapters"]})
    assert restricted(found) == [("app.ports", "app.ext.rules")]  # ext's own __init__ importing rules is fine


def test_all_matching_restrictions_must_allow_the_importer(repo: Path) -> None:
    write_files(repo, {**EXT_FILES, "app/adapters.py": "from app.ext import rules\n"})
    found = run(repo, importable_by={"app.ext": ["app.adapters"], "app.ext.rules": ["app.ports"]})
    assert restricted(found) == [("app.adapters", "app.ext.rules")]


def test_a_key_or_an_allowed_glob_that_matches_no_module_is_reported_so_the_list_cannot_go_stale(repo: Path) -> None:
    found = run(repo, importable_by={"app.gone": ["app.adapters"], "app.domain": ["app.ports", "app.adapters", "app.removed*"]})
    stale = [(v.subject, v.detail) for v in found if v.rule == Rule.RESTRICTED_IMPORT and not v.target]
    assert [subject for subject, _ in stale] == ["app.gone", "app.domain"]
    assert "matches no module" in stale[0][1] and "app.removed*" in stale[1][1]


@pytest.mark.parametrize("value", ["app.domain", {"app.domain": "app.ports"}, {"": ["app.ports"]}, {"app.domain": []}, {"app.domain": [""]}, {"app.domain": [1]}])
def test_the_setting_must_map_module_globs_to_non_empty_lists_of_globs(value: object) -> None:
    with pytest.raises(PolicyError, match="importable_by"):
        policy_from_mapping({**MINI, "importable_by": value})


def test_importable_by_is_a_scope_key_and_is_off_by_default() -> None:
    assert mini_policy().importable_by == ()
    assert mini_policy(importable_by={"app.domain": ["app.ports"]}).importable_by == (("app.domain", ("app.ports",)),)


def test_selftest_injects_a_disallowed_import_for_each_restriction(repo: Path) -> None:
    policy = mini_policy(importable_by={"app.domain": ["app.ports"]})
    mutations = plan(policy, FsScanner().scan(policy, repo))[0]
    assert [(m.rule, m.file) for m in mutations if m.rule == Rule.RESTRICTED_IMPORT] == [(Rule.RESTRICTED_IMPORT, "app/adapters.py")]
