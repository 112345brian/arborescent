"""#67: the annotation layer never depends on the driver, and the driver respects the rings.

The constraint lives in Arborescent's own policy (``module_forbid_imports``) so ``check`` enforces it and ``selftest``
probes it. These tests fail if the policy entry, the probe, or the rule behind them is removed.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.config import load_policy
from arborescent.domain import Policy, Rule
from arborescent.driver_filter_repo import FilterRepo
from arborescent.engine import Services, run_own
from arborescent.module_forbid_probes import module_forbid_mutations
from arborescent.selftest import plan
from arborescent.testing import FakeToolRunner

ROOT = Path(__file__).resolve().parents[1]
DRIVER = {"arborescent.driver_request", "arborescent.driver_port", "arborescent.driver_plan", "arborescent.driver_filter_repo", "arborescent.cli_migrate"}
ANNOTATION = {
    "arborescent.package_model", "arborescent.package_fields", "arborescent.package_parse", "arborescent.package_render",
    "arborescent.path_exclusion", "arborescent.package_assignment", "arborescent.migration_arguments", "arborescent.migration_json",
    "arborescent.packages", "arborescent.package_tree", "arborescent.package_manifest", "arborescent.package_validation",
    "arborescent.package_copy", "arborescent.package_gaps", "arborescent.package_probes", "arborescent.migration_export",
}


def own_policy() -> Policy:
    return load_policy(LocalFileSystem(), ROOT / "pyproject.toml")[0]


@pytest.mark.parametrize("module", sorted(ANNOTATION))
def test_every_annotation_module_is_forbidden_from_importing_every_driver_module(module: str) -> None:
    assert own_policy().forbidden_imports(module) >= DRIVER


def test_every_listed_module_exists_so_the_lists_cannot_rot() -> None:
    scan = FsScanner().scan(own_policy(), ROOT)
    assert scan.known >= (ANNOTATION | DRIVER)


def test_the_policy_rule_bites_on_the_real_source(tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    target = copy / "src" / "arborescent"
    target.mkdir(parents=True)
    _ = (copy / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    for source in (ROOT / "src" / "arborescent").glob("*.py"):
        _ = (target / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    victim = target / "package_gaps.py"
    _ = victim.write_text(victim.read_text(encoding="utf-8") + "\nimport arborescent.driver_plan\n", encoding="utf-8")
    policy = load_policy(LocalFileSystem(), copy / "pyproject.toml")[0]
    found, _ = run_own(policy, copy, Services(FsScanner(), FakeToolRunner(), [], LocalFileSystem(), SystemClock(), []), practices=False)
    assert any(v.rule == Rule.INTERNAL_IMPORT and v.subject == "arborescent.package_gaps" and "driver_plan" in v.detail for v in found)


def test_selftest_probes_the_module_forbid_rule_and_removing_the_policy_entry_removes_the_probe() -> None:
    policy = own_policy()
    scan = FsScanner().scan(policy, ROOT)
    mutations, _ = plan(policy, scan)
    probes = [m for m in mutations if "module_forbid_imports" in m.label]
    assert probes and all(m.rule == Rule.INTERNAL_IMPORT for m in probes)
    assert len(probes) == len(policy.module_forbid_imports)  # one probe per configured pattern
    stripped = replace(policy, module_forbid_imports=())
    assert not [m for m in plan(stripped, scan)[0] if "module_forbid_imports" in m.label]


def test_the_probe_picks_a_matching_host_and_a_known_target_per_pattern() -> None:
    policy = replace(own_policy(), module_forbid_imports=(("arborescent.package_*", ("arborescent.driver_plan",)),))
    scan = FsScanner().scan(policy, ROOT)
    mutations, skipped = module_forbid_mutations(policy, scan.paths, scan.known)
    assert skipped == []
    assert len(mutations) == 1
    assert mutations[0].append == "import arborescent.driver_plan"
    assert Path(mutations[0].file).name.startswith("package_")


def test_a_pattern_with_no_matching_host_or_target_is_reported_as_skipped_not_silently_passed() -> None:
    policy = replace(own_policy(), module_forbid_imports=(("arborescent.nothing_*", ("arborescent.driver_plan",)), ("arborescent.package_*", ("arborescent.nope",))))
    scan = FsScanner().scan(policy, ROOT)
    mutations, skipped = module_forbid_mutations(policy, scan.paths, scan.known)
    assert mutations == []
    assert len(skipped) == 2


def test_the_adapter_never_imports_the_application_ring() -> None:
    source = (ROOT / "src" / "arborescent" / "driver_filter_repo.py").read_text(encoding="utf-8")
    assert "driver_plan" not in source and "migration_export" not in source
    assert FilterRepo.__module__ == "arborescent.driver_filter_repo"
