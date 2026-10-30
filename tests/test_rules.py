"""Each own-check rule, against a tiny clean repository that is then broken one way at a time."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import CLASSIFY, mini_policy, ring_with, write_files

from arborescent import baseline
from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import Policy, Rule, Violation
from arborescent.rules import RuleContext, check_modules


def run(root: Path, **overrides: Any) -> list[Violation]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def rules(found: list[Violation]) -> set[str]:
    return {v.rule for v in found}


def test_clean_repo_has_no_violations(repo: Path) -> None:
    assert run(repo) == []


def test_inner_ring_importing_outer_ring_fails(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from . import adapters\n"})
    found = run(repo)
    assert rules(found) == {"internal-import", "circular-import"}
    assert "app.adapters" in found[0].detail


def test_same_ring_import_cycle_fails_even_when_peer_imports_are_allowed(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from . import ports\n"})
    found = run(repo, classify={"app": "domain", "app.domain": "domain", "app.ports": "domain"})
    cycles = [violation for violation in found if violation.rule == "circular-import"]
    assert len(cycles) == 1
    assert cycles[0].subject == "app.domain, app.ports"


def test_module_forbid_imports_adds_a_finer_grained_exception(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": "import app.main\n"})
    found = run(repo, module_forbid_imports={"app.adapters": ["app.main"]})
    assert [(v.rule, v.subject) for v in found if v.rule == "internal-import"] == [("internal-import", "app.adapters")]
    assert "forbidden module app.main" in next(v for v in found if v.rule == "internal-import").detail


def test_lazy_import_inside_a_function_counts(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "def f() -> None:\n    import app.adapters\n"})
    assert rules(run(repo)) == {"internal-import", "circular-import"}


def test_type_checking_import_counts(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import adapters\n"})
    assert rules(run(repo)) == {"internal-import", "circular-import"}


def test_absolute_import_of_own_package_is_internal_not_external(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import app.adapters\n"})
    assert rules(run(repo)) == {"internal-import", "circular-import"}


def test_external_allow_list_is_per_ring(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import os\n", "app/adapters.py": "import psutil\n"})
    assert {(v.subject, v.rule) for v in run(repo)} == {("app.domain", "external-import")}


def test_lazy_external_import_counts(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "def f() -> None:\n    import requests\n"})
    assert rules(run(repo)) == {"external-import"}


def test_os_branching_outside_permitted_rings(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import sys\nsys.platform\n", "app/adapters.py": "import os\nos.name\n"})
    found = run(repo, ring=ring_with(ports={"may_import": ["domain"], "external": ["sys"]}))
    assert {(v.subject, v.rule) for v in found} == {("app.ports", "os-branching")}


def test_from_sys_import_platform_is_os_branching(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from sys import platform\n"})
    assert "os-branching" in rules(run(repo, ring=ring_with(domain={"external": ["sys"]})))


def test_aliased_os_branching_is_detected(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import os as operating_system\noperating_system.name\n"})
    assert "os-branching" in rules(run(repo, ring=ring_with(domain={"external": ["os"]})))


def test_non_outermost_wildcard_ring_is_a_violation_per_axis(repo: Path) -> None:
    found = run(
        repo,
        ring=ring_with(domain={"external": ["*"], "may_import": ["*"]}),
    )

    assert [(v.rule, v.subject, v.detail) for v in found] == [
        ("wildcard-ring", "domain", "external"),
        ("wildcard-ring", "domain", "may_import"),
    ]


def test_outermost_ring_is_exempt_from_wildcard_ring_rule(repo: Path) -> None:
    found = run(repo, ring=ring_with(composition={"external": ["*"], "may_import": ["*"]}))

    assert not [v for v in found if v.rule == "wildcard-ring"]


def test_adding_explicit_allowances_does_not_shrink_a_retained_wildcard(repo: Path) -> None:
    broad = run(repo, ring=ring_with(ports={"may_import": ["*"], "external": ["*"]}))
    narrowed = run(repo, ring=ring_with(ports={"may_import": ["*", "domain"], "external": ["*", "requests"]}))

    broad_keys = {v.key for v in broad if v.rule == "wildcard-ring"}
    narrowed_keys = {v.key for v in narrowed if v.rule == "wildcard-ring"}
    assert broad_keys == narrowed_keys == {
        "wildcard-ring|ports|external",
        "wildcard-ring|ports|may_import",
    }


def test_removing_wildcard_ring_policy_shrinks_the_baseline(repo: Path) -> None:
    before = run(repo, ring=ring_with(domain={"external": ["*"], "may_import": ["*"]}))
    after = run(repo, ring=ring_with(domain={"external": ["@pure", "os"], "may_import": []}))

    ratchet = baseline.apply(after, {finding.key for finding in before})
    assert ratchet.new == []
    assert ratchet.stale == ["wildcard-ring|domain|external", "wildcard-ring|domain|may_import"]


def test_unclassified_module_is_a_violation(repo: Path) -> None:
    write_files(repo, {"app/newcomer.py": "X = 1\n"})
    assert [(v.rule, v.subject) for v in run(repo)] == [("unclassified", "app.newcomer")]


def test_relative_import_above_the_root_is_reported(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "from ... import x\n"})
    assert "bad-import" in rules(run(repo))


def test_from_dot_import_names_submodules(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "from . import adapters\n"})  # reaches app.adapters, not just the package
    assert rules(run(repo)) == {"internal-import", "circular-import"}


ENTRY = {
    "sources": ["app", "run.py"],
    "entry_scripts": ["run"],
    "classify": {**CLASSIFY, "run": "composition"},
}


def test_entry_script_must_declare_every_third_party_package(repo: Path) -> None:
    write_files(
        repo,
        {
            "run.py": '# /// script\n# dependencies = ["requests"]\n# ///\nfrom app import main\n',
            "app/adapters.py": "import psutil\nimport requests\n",
        },
    )
    found = run(repo, **ENTRY, ring=ring_with(adapters={"may_import": ["domain", "ports"], "external": ["psutil", "requests"], "os_branching": True}))
    assert [(v.rule, v.subject) for v in found] == [("undeclared-entry-dependency", "run")]
    assert "psutil" in found[0].detail


def test_distribution_mapping_is_used_for_entry_scripts(repo: Path) -> None:
    write_files(
        repo,
        {"run.py": '# /// script\n# dependencies = ["lifecycle-runner"]\n# ///\n', "app/adapters.py": "import lifecycle_runner\n"},
    )
    found = run(
        repo, **ENTRY, dependencies={"lifecycle_runner": "lifecycle-runner"},
        ring=ring_with(adapters={"may_import": ["domain", "ports"], "external": ["lifecycle_runner"], "os_branching": True}),
    )  # fmt: skip
    assert found == []


def test_entry_script_without_metadata_block_fails(repo: Path) -> None:
    write_files(repo, {"run.py": "import sys\n"})
    assert [v.rule for v in run(repo, **ENTRY)] == ["undeclared-entry-dependency"]


def test_entry_script_metadata_is_read_from_a_file_with_crlf_line_endings(repo: Path) -> None:
    """Found by running Arborescent on DeepInterview: the scripts there are CRLF and the PEP 723 regex missed the block."""
    script = b'# /// script\r\n# dependencies = ["psutil"]\r\n# ///\r\nimport sys\r\n'
    (repo / "run.py").write_bytes(script)
    scan = FsScanner().scan(mini_policy(**ENTRY), repo)
    assert scan.entry_deps["run"] == ("psutil",)


@pytest.mark.parametrize("annotation", ['__arbor__ = 3', '__arbor__ = {"ring": "missing"}'])
def test_broken_annotation_keeps_all_classified_module_checks(repo: Path, annotation: str) -> None:
    source = "from . import adapters\nimport os, time, requests\nos.name\ntime.time()\n"
    write_files(repo, {"app/domain.py": source})
    before = run(repo)
    assert rules(before) == {
        "internal-import", "external-import", "os-branching", "effect-clock", "circular-import",
    }

    write_files(repo, {"app/domain.py": source + annotation + "\n"})
    after = run(repo)

    assert [v for v in after if v.rule != "invalid-arbor-annotation"] == before
    invalid = [v for v in after if v.rule == "invalid-arbor-annotation"]
    assert len(invalid) == 1
    assert invalid[0].subject == "app.domain"
    assert invalid[0].detail


def test_broken_unclassified_annotation_reports_both_problems(repo: Path) -> None:
    write_files(repo, {"app/newcomer.py": "__arbor__ = 3\n"})

    assert {(v.rule, v.subject) for v in run(repo)} == {
        ("invalid-arbor-annotation", "app.newcomer"), ("unclassified", "app.newcomer"),
    }


def test_valid_annotation_classifies_module_without_classify_match(repo: Path) -> None:
    write_files(repo, {"app/newcomer.py": '__arbor__ = {"ring": "domain"}\nimport os\n'})

    assert {(v.rule, v.subject) for v in run(repo)} == {("external-import", "app.newcomer")}


def test_internal_import_uses_target_annotation_in_changed_scan(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import app.ports\n", "app/ports.py": '__arbor__ = {"ring": "composition"}\n'})
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo, frozenset({"app/domain.py"}))

    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)

    assert [(v.rule, v.subject) for v in found] == [("internal-import", "app.domain")]
    assert "[composition]" in found[0].detail


def test_disabling_annotations_preserves_classify_checks(repo: Path) -> None:
    write_files(repo, {"app/domain.py": '__arbor__ = {"ring": "composition"}\nimport os\n', "app/newcomer.py": "__arbor__ = 3\n"})

    assert {(v.rule, v.subject) for v in run(repo, disabled_tools=["arbor-annotations"])} == {
        ("external-import", "app.domain"), ("unclassified", "app.newcomer"),
    }


def test_annotation_disagreeing_with_classify_reports_mismatch(repo: Path) -> None:
    write_files(repo, {"app/domain.py": '__arbor__ = {"ring": "ports"}\n'})

    found = run(repo)

    assert {(v.rule, v.subject) for v in found} == {("arbor-ring-mismatch", "app.domain")}
    mismatch = next(v for v in found if v.rule == "arbor-ring-mismatch")
    assert "ports" in mismatch.detail
    assert "domain" in mismatch.detail


def test_annotation_agreeing_with_classify_reports_no_mismatch(repo: Path) -> None:
    write_files(repo, {"app/domain.py": '__arbor__ = {"ring": "domain"}\n'})

    assert run(repo) == []


def test_invalid_annotation_is_not_also_reported_as_a_mismatch(repo: Path) -> None:
    write_files(repo, {"app/domain.py": '__arbor__ = {"ring": "missing"}\n'})

    assert rules(run(repo)) == {"invalid-arbor-annotation"}


def test_disabling_annotations_suppresses_the_mismatch_rule_too(repo: Path) -> None:
    write_files(repo, {"app/domain.py": '__arbor__ = {"ring": "ports"}\n'})

    assert run(repo, disabled_tools=["arbor-annotations"]) == []


def test_patch_target_uses_annotation_in_changed_scan(repo: Path) -> None:
    write_files(repo, {
        "app/main.py": '__arbor__ = {"ring": "domain"}\nVALUE = 1\n',
        "tests/test_main.py": 'from unittest.mock import patch\npatch("app.main.VALUE")\n',
    })
    policy = mini_policy(
        sources=["app", "tests"], source_roots=["."],
        classify={**CLASSIFY, "tests.*": "composition"},
        practices={"secrets-guard": "tests"},
        ring=ring_with(composition={"may_import": ["*"], "external": ["unittest"]}),
    )
    scan = FsScanner().scan(policy, repo, frozenset({"tests/test_main.py"}))

    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)

    assert [(v.rule, v.subject) for v in found] == [("effect-patch", "tests.test_main")]
    assert "[domain]" in found[0].detail


def _scope_boundary_policy(
    *, alpha_depends: list[str] | None = None, alpha_forbid: dict[str, list[str]] | None = None,
) -> Policy:
    return policy_from_mapping({
        "scopes": {
            "alpha": {
                "sources": ["libs/alpha/src"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain", "adapters"],
                "classify": {"core": "domain", "adapter": "adapters"},
                "public": ["core"], "depends_on": alpha_depends or [],
                "module_forbid_imports": alpha_forbid or {},
            },
            "beta": {
                "sources": ["libs/beta/src"], "source_roots": ["libs/beta/src"],
                "rings": ["domain", "adapters"],
                "classify": {"api": "domain", "private": "adapters"},
                "public": ["api"],
            },
        },
    })


def _scope_findings(policy: Policy, root: Path) -> list[Violation]:
    scan = FsScanner().scan(policy, root)
    return check_modules(
        policy, scan.modules, scan.known, scan.entry_deps,
        RuleContext(policy, scan.arbor_index, scan.scope_by_module),
    )


def test_cross_scope_import_requires_declared_dependency(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/core.py": "import api\n",
        "libs/beta/src/api.py": "VALUE = 1\n",
    })

    found = _scope_findings(_scope_boundary_policy(), tmp_path)

    assert [(item.rule, item.subject) for item in found if item.rule == Rule.SCOPE_IMPORT] == [("scope-import", "core")]


def test_cross_scope_import_requires_public_target(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/core.py": "import private\n",
        "libs/beta/src/private.py": "VALUE = 1\n",
    })

    found = _scope_findings(_scope_boundary_policy(alpha_depends=["beta"]), tmp_path)

    assert [(item.rule, item.subject) for item in found if item.rule == Rule.SCOPE_IMPORT] == [("scope-import", "core")]
    assert "not public" in next(item.detail for item in found if item.rule == Rule.SCOPE_IMPORT)


def test_public_cross_scope_import_is_allowed(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/core.py": "import api\n",
        "libs/beta/src/api.py": "VALUE = 1\n",
    })

    found = _scope_findings(_scope_boundary_policy(alpha_depends=["beta"]), tmp_path)

    assert not [item for item in found if item.rule == Rule.SCOPE_IMPORT]


def test_cross_scope_import_honours_module_forbid_patterns(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/core.py": "import api.deep\n",
        "libs/beta/src/api/__init__.py": "",
        "libs/beta/src/api/deep.py": "VALUE = 1\n",
    })

    found = _scope_findings(
        _scope_boundary_policy(alpha_depends=["beta"], alpha_forbid={"core": ["api.*"]}), tmp_path,
    )

    scope_findings = [item for item in found if item.rule == Rule.SCOPE_IMPORT]
    assert [(item.subject, item.rule) for item in scope_findings] == [("core", "scope-import")]
    assert "matches 'api.*'" in scope_findings[0].detail


def test_same_scope_rings_are_independent(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/core.py": "import adapter\n",
        "libs/alpha/src/adapter.py": "VALUE = 1\n",
        "libs/beta/src/api.py": "VALUE = 2\n",
    })

    found = _scope_findings(_scope_boundary_policy(), tmp_path)

    assert [(item.rule, item.subject) for item in found if item.rule == Rule.INTERNAL_IMPORT] == [("internal-import", "core")]


def test_same_name_rings_resolve_in_the_source_scope(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/core.py": '__arbor__ = {"ring": "adapters"}\n',
        "libs/beta/src/api.py": '__arbor__ = {"ring": "domain"}\n',
    })

    found = _scope_findings(_scope_boundary_policy(), tmp_path)

    assert not [item for item in found if item.rule == Rule.INVALID_ARBOR_ANNOTATION]


def test_annotation_findings_are_scope_local(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/core.py": '__arbor__ = {"ring": "parser"}\n',
        "libs/beta/src/api.py": '__arbor__ = {"ring": "domain"}\n',
    })

    found = _scope_findings(_scope_boundary_policy(), tmp_path)

    assert [(item.rule, item.subject) for item in found if item.rule == Rule.INVALID_ARBOR_ANNOTATION] == [
        ("invalid-arbor-annotation", "core"),
    ]


def test_public_import_leaking_a_forbidden_effect_is_flagged(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/core.py": "import api\n",
        "libs/beta/src/api.py": "import time\nVALUE = time.time()\n",
    })

    found = _scope_findings(_scope_boundary_policy(alpha_depends=["beta"]), tmp_path)

    leaks = [item for item in found if item.rule == Rule.SCOPE_EFFECT_LEAK]
    assert [(item.subject, item.rule) for item in leaks] == [("core", "scope-effect-leak")]
    assert "effect clock" in leaks[0].detail
    assert "api" in leaks[0].detail


def test_non_public_import_leaking_an_effect_is_not_flagged_by_the_effect_rule(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/core.py": "import private\n",
        "libs/beta/src/private.py": "import time\nVALUE = time.time()\n",
    })

    found = _scope_findings(_scope_boundary_policy(alpha_depends=["beta"]), tmp_path)

    assert not [item for item in found if item.rule == Rule.SCOPE_EFFECT_LEAK]
    assert any(item.rule == Rule.SCOPE_IMPORT for item in found)  # the plain boundary rule still catches this


def test_public_import_of_an_effect_the_importing_ring_allows_is_not_flagged(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/adapter.py": "import api\n",  # alpha's outermost ring forbids no effects
        "libs/beta/src/api.py": "import time\nVALUE = time.time()\n",
    })

    found = _scope_findings(_scope_boundary_policy(alpha_depends=["beta"]), tmp_path)

    assert not [item for item in found if item.rule == Rule.SCOPE_EFFECT_LEAK]
