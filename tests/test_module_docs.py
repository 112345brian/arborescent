"""``[tool.arborescent.docs]`` (#110): a module has a docstring, and the docstring says the ring the policy enforces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI, mini_policy, services, write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.facts import extract
from arborescent.rules import check_modules
from arborescent.selftest import plan, selftest

BOTH = {"module_docstring": True, "module_names_ring": True}
DOCS_RULES = (Rule.MISSING_MODULE_DOCSTRING, Rule.DOCSTRING_RING_MISMATCH)


def quiet() -> dict[str, str]:
    """The mini repo's modules, each documented and naming its ring, so a test changes only the module it is about."""
    rings = {"domain": "domain", "ports": "ports", "adapters": "adapters", "main": "composition"}
    return {f"app/{name}.py": f'"""The {ring} ring."""\n\nVALUE: int = 1\n' for name, ring in rings.items()} | {"app/__init__.py": '"""The package, in the domain ring."""\n'}


def run(root: Path, docs: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    policy = mini_policy(docs=BOTH if docs is None else docs)
    scan = FsScanner().scan(policy, root)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    return [(v.rule, v.subject) for v in found if v.rule in DOCS_RULES]


def findings(root: Path, docs: dict[str, Any] | None = None) -> list[Violation]:
    policy = mini_policy(docs=BOTH if docs is None else docs)
    scan = FsScanner().scan(policy, root)
    return [v for v in check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index) if v.rule in DOCS_RULES]


def test_a_module_without_a_docstring_is_reported(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": "VALUE: int = 1\n"})
    assert (Rule.MISSING_MODULE_DOCSTRING, "app.domain") in run(repo)


@pytest.mark.parametrize("doc", ['""""""', '"""   \n  """'])
def test_a_blank_docstring_is_a_missing_one(repo: Path, doc: str) -> None:
    write_files(repo, {**quiet(), "app/domain.py": f"{doc}\n\nVALUE: int = 1\n"})
    assert run(repo, {"module_docstring": True}) == [(Rule.MISSING_MODULE_DOCSTRING, "app.domain")]


def test_a_comment_or_string_that_is_not_first_is_not_a_docstring(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": '# the domain ring\nVALUE: int = 1\n"""not a docstring"""\n'})
    assert run(repo, {"module_docstring": True}) == [(Rule.MISSING_MODULE_DOCSTRING, "app.domain")]


def test_a_docstring_that_names_the_ring_passes(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": '"""Pure values. Ring: Domain."""\n\nVALUE: int = 1\n'})
    assert run(repo) == []


def test_a_docstring_that_names_a_different_ring_is_a_mismatch(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": '"""Wiring that lives in the adapters ring."""\n\nVALUE: int = 1\n'})
    [found] = findings(repo)
    assert (found.rule, found.subject) == (Rule.DOCSTRING_RING_MISMATCH, "app.domain")
    assert "domain" in found.detail and "adapters" in found.detail


def test_a_docstring_that_names_no_ring_is_a_mismatch(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": '"""Pure values."""\n\nVALUE: int = 1\n'})
    [found] = findings(repo)
    assert found.rule == Rule.DOCSTRING_RING_MISMATCH and "names no ring" in found.detail


def test_the_ring_is_matched_as_a_whole_word_not_a_prefix(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": '"""Subdomains and domains of values."""\n\nVALUE: int = 1\n'})
    assert run(repo) == [(Rule.DOCSTRING_RING_MISMATCH, "app.domain")]


def test_a_missing_docstring_is_not_also_a_mismatch(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": "VALUE: int = 1\n"})
    assert [rule for rule, subject in run(repo) if subject == "app.domain"] == [Rule.MISSING_MODULE_DOCSTRING]


def test_an_unclassified_module_has_no_ring_to_name(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/stray.py": '"""Something."""\n'})
    assert [rule for rule, subject in run(repo) if subject == "app.stray"] == []


def test_the_effective_ring_comes_from_the_annotation_when_there_is_one(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/stray.py": '"""Lives in the ports ring."""\n\n__arbor__ = {"ring": "ports"}\n'})
    assert [rule for rule, subject in run(repo) if subject == "app.stray"] == []


def test_each_rule_is_switched_on_separately(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": '"""Pure."""\n', "app/ports.py": "PORT: int = 1\n"})
    assert run(repo, {"module_docstring": True}) == [(Rule.MISSING_MODULE_DOCSTRING, "app.ports")]
    assert run(repo, {"module_names_ring": True}) == [(Rule.DOCSTRING_RING_MISMATCH, "app.domain")]


def test_exclude_skips_matching_modules(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": "VALUE: int = 1\n", "app/ports.py": "PORT: int = 1\n"})
    found = run(repo, {**BOTH, "exclude": ["app.domain"]})
    assert "app.domain" not in {subject for _, subject in found} and "app.ports" in {subject for _, subject in found}


def test_the_rules_are_off_by_default(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": "VALUE: int = 1\n"})
    policy = mini_policy()
    assert (policy.docs.module_docstring, policy.docs.module_names_ring, policy.docs.exclude) == (False, False, ())
    scan = FsScanner().scan(policy, repo)
    assert not [v for v in check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index) if v.rule in DOCS_RULES]


def test_the_baseline_key_is_stable_and_names_the_module(repo: Path) -> None:
    write_files(repo, {**quiet(), "app/domain.py": "VALUE: int = 1\n"})
    [found] = [v for v in findings(repo) if v.subject == "app.domain"]
    assert found.key.startswith("missing-module-docstring|app.domain|")


def test_the_docstring_is_read_into_the_facts_and_survives_the_scan_cache(repo: Path, tmp_path: Path) -> None:
    assert extract('"""Hello.\n\nRing: domain."""\nX = 1\n').module_doc == "Hello.\n\nRing: domain."
    assert extract("X = 1\n").module_doc is None
    write_files(repo, {**quiet(), "app/domain.py": '"""Cached ring domain."""\n'})
    cache = tmp_path / "cache.json"
    policy = mini_policy()
    first = FsScanner(cache).scan(policy, repo).modules["app.domain"].facts.module_doc
    second = FsScanner(cache).scan(policy, repo).modules["app.domain"].facts.module_doc
    assert first == second == "Cached ring domain."


@pytest.mark.parametrize(
    "value",
    ["x", {"module_docstring": "yes"}, {"module_names_ring": 1}, {"bogus": True}, {"exclude": "app"}, {"exclude": [""]}, {"exclude": [1]}],
)
def test_the_setting_is_validated(value: object) -> None:
    with pytest.raises(PolicyError, match="docs"):
        policy_from_mapping({**MINI, "docs": value})


def test_it_is_parsed_into_the_policy() -> None:
    docs = mini_policy(docs={**BOTH, "exclude": ["tests*"]}).docs
    assert (docs.module_docstring, docs.module_names_ring, docs.exclude) == (True, True, ("tests*",))


def test_selftest_probes_both_rules_with_new_files(repo: Path) -> None:
    policy = mini_policy(docs=BOTH)
    mutations = plan(policy, FsScanner().scan(policy, repo))[0]
    by_rule = {m.rule: m for m in mutations if m.rule in DOCS_RULES}
    assert set(by_rule) == set(DOCS_RULES)
    assert all(m.new_file and "\\" not in m.file for m in by_rule.values())
    assert by_rule[Rule.MISSING_MODULE_DOCSTRING].content.lstrip().startswith(('"""', "'''")) is False
    assert '"""' in by_rule[Rule.DOCSTRING_RING_MISMATCH].content


def test_selftest_kills_both_probes(repo: Path) -> None:
    write_files(repo, quiet())
    policy = mini_policy(docs=BOTH)
    report = selftest(policy, repo, services())
    assert not [entry for entry in report.survived if any(f"[{rule}]" in entry for rule in DOCS_RULES)]
    assert {rule for rule in DOCS_RULES if any(entry.startswith(f"[{rule}]") for entry in report.killed)} == set(DOCS_RULES)


def test_no_probe_without_the_setting(repo: Path) -> None:
    policy = mini_policy()
    assert not [m for m in plan(policy, FsScanner().scan(policy, repo))[0] if m.rule in DOCS_RULES]
