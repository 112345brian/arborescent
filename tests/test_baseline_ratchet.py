"""Growth has to be deliberate and auditable; v1 baselines remain readable."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, write_files

from arborescent import baseline
from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.cli import main
from arborescent.domain import ToolResult, Violation
from arborescent.engine import Options, Services, run_check
from arborescent.engines import default_engines
from arborescent.testing import FakeFileSystem, FakeToolRunner
from arborescent.typecheckers import all_checkers


def v(rule: str, subject: str) -> Violation:
    return Violation(rule, subject, "detail")


def filesystem() -> tuple[FakeFileSystem, Path]:
    fs = FakeFileSystem()
    path = Path("/repo/baseline.json")
    fs.mkdir(Path("/repo"))
    return fs, path


def write_policy(repo: Path) -> Path:
    write_files(
        repo,
        {
            "arborescent.toml": (
                'sources = ["app"]\nrings = ["domain", "ports", "adapters", "composition"]\n'
                '[groups]\npure = ["__future__"]\n[ring.domain]\nexternal = ["@pure"]\n'
                '[ring.ports]\nmay_import = ["domain"]\nexternal = ["@pure"]\n'
                '[ring.adapters]\nmay_import = ["domain", "ports"]\nexternal = ["@pure", "os", "psutil"]\nos_branching = true\n'
                '[ring.composition]\nmay_import = ["*"]\nexternal = ["@pure", "sys"]\n'
                '[classify]\n"app" = "domain"\n"app.domain" = "domain"\n"app.ports" = "ports"\n'
                '"app.adapters" = "adapters"\n"app.main" = "composition"\n'
            ),
        },
    )
    return repo / "arborescent.toml"


def test_existing_baseline_refuses_growth_without_writing() -> None:
    fs, path = filesystem()
    baseline.save(fs, path, [v("internal-import", "old")])
    before = fs.read_text(path)

    with pytest.raises(baseline.BaselineError, match="new violation"):
        baseline.record(fs, path, [v("internal-import", "old"), v("internal-import", "new")])

    assert fs.read_text(path) == before


def test_allow_growth_requires_reason_and_logs_exact_added_keys() -> None:
    fs, path = filesystem()
    baseline.save(fs, path, [v("internal-import", "old")])

    with pytest.raises(baseline.BaselineError, match="reason"):
        baseline.record(fs, path, [v("internal-import", "old"), v("internal-import", "new")], allow_growth=True)

    baseline.record(
        fs,
        path,
        [v("internal-import", "old"), v("internal-import", "new")],
        allow_growth=True,
        reason="adopt new rule",
    )
    document = fs.read_json(path)
    assert document["version"] == 2
    assert document["growth"] == [{"reason": "adopt new rule", "added": [v("internal-import", "new").key]}]


def test_adopt_records_one_unrepresented_family_and_refuses_existing_family() -> None:
    fs, path = filesystem()
    baseline.save(fs, path, [v("internal-import", "existing")])

    baseline.record(
        fs,
        path,
        [v("internal-import", "existing"), v("ruff-BLE001", "old")],
        adopt="ruff",
    )
    assert baseline.load(fs, path) == {v("internal-import", "existing").key, v("ruff-BLE001", "old").key}
    assert fs.read_json(path)["growth"] == [{"reason": "adopt ruff family", "added": [v("ruff-BLE001", "old").key]}]

    with pytest.raises(baseline.BaselineError, match="already contains"):
        baseline.record(
            fs,
            path,
            [v("internal-import", "existing"), v("ruff-BLE001", "old"), v("ruff-BLE001", "new")],
            adopt="ruff",
        )


@pytest.mark.parametrize("family,rule", [("suppressions", "suppression"), ("discard", "discard")])
def test_adopt_can_record_inventory_counter_families(family: str, rule: str) -> None:
    fs, path = filesystem()
    baseline.save(fs, path, [v("internal-import", "existing")])

    counter = Violation(rule, "app.module", "count#1")
    baseline.record(fs, path, [v("internal-import", "existing"), counter], adopt=family)

    assert counter.key in baseline.load(fs, path)


def test_adopt_coverage_includes_unclassified_and_noise_dir_code() -> None:
    fs, path = filesystem()
    baseline.save(fs, path, [v("internal-import", "existing")])

    baseline.record(
        fs,
        path,
        [v("internal-import", "existing"), v("unclassified", "scripts/build"), v("noise-dir-code", ".venv/x.py")],
        adopt="coverage",
    )
    assert baseline.load(fs, path) == {
        v("internal-import", "existing").key,
        v("unclassified", "scripts/build").key,
        v("noise-dir-code", ".venv/x.py").key,
    }


def test_version_one_baseline_loads_and_upgrades_when_written() -> None:
    fs, path = filesystem()
    original = v("internal-import", "old").key
    fs.write_json(path, {"version": 1, "violations": [original]})
    assert baseline.load(fs, path) == {original}

    baseline.record(fs, path, [v("internal-import", "old")], allow_growth=True, reason="upgrade")
    assert fs.read_json(path) == {"version": 2, "violations": [original], "growth": []}


def test_growth_log_must_account_for_every_new_key_since_ref() -> None:
    old = {"version": 1, "violations": ["rule|old|detail"]}
    valid = {
        "version": 2,
        "violations": ["rule|new|detail", "rule|old|detail"],
        "growth": [{"reason": "approved", "approved-by": "me", "added": ["rule|new|detail"]}],
    }
    unlogged = {**valid, "growth": []}
    incorrectly_logged = {**valid, "growth": [{"reason": "approved", "added": ["rule|other|detail"]}]}
    assert baseline.growth_consistent(valid, old)
    assert not baseline.growth_consistent(unlogged, old)
    assert not baseline.growth_consistent(incorrectly_logged, old)


def test_check_growth_against_reads_the_baseline_from_git(repo: Path) -> None:
    config = write_policy(repo)
    fs = LocalFileSystem()
    key = "rule|new|detail"
    fs.write_json(repo / "arborescent-baseline.json", {"version": 2, "violations": [key], "growth": [{"reason": "approved", "approved-by": "me", "added": [key]}]})
    runner = FakeToolRunner({"git"}, {"git": ToolResult(0, '{"version":1,"violations":[]}', "")})
    svc = Services(FsScanner(), runner, list(all_checkers().values()), fs, SystemClock(), default_engines())
    assert main(["--config", str(config), "generate"], svc) == 0

    args = ["--config", str(config), "check", "--no-tools", "--no-baseline", "--growth-against", "base"]
    assert main(args, svc) == 0
    fs.write_json(repo / "arborescent-baseline.json", {"version": 2, "violations": [key], "growth": []})
    assert main(args, svc) == 1


def test_baseline_command_refuses_growth_then_accepts_a_reason(repo: Path) -> None:
    config = write_policy(repo)
    fs = LocalFileSystem()
    svc = Services(FsScanner(), FakeToolRunner(), list(all_checkers().values()), fs, SystemClock(), default_engines())
    args = ["--config", str(config), "baseline"]
    assert main(args, svc) == 0
    baseline_path = repo / "arborescent-baseline.json"
    before = fs.read_text(baseline_path)

    write_files(repo, {"app/domain.py": "from . import adapters\n"})
    assert main(args, svc) == 2
    assert fs.read_text(baseline_path) == before
    assert main([*args, "--allow-growth", "--reason", "new architecture debt", "--approved-by", "#12"], svc) == 0
    assert fs.read_json(baseline_path)["growth"][0]["reason"] == "new architecture debt"
    assert fs.read_json(baseline_path)["growth"][0]["approved-by"] == "#12"


def test_baseline_adopt_runs_requested_findings_engine(repo: Path) -> None:
    config = write_policy(repo)
    fs = LocalFileSystem()
    runner = FakeToolRunner(
        {"ruff"},
        {"ruff": ToolResult(0, '[{"code":"BLE001","filename":"app/domain.py","message":"blind exception","location":{"row":1}}]', "")},
    )
    svc = Services(FsScanner(), runner, list(all_checkers().values()), fs, SystemClock(), default_engines())
    assert main(["--config", str(config), "baseline"], svc) == 0

    assert main(["--config", str(config), "baseline", "--adopt", "ruff"], svc) == 0

    assert any(call[0] == "ruff" for call in runner.calls)
    assert "ruff-BLE001|app/domain.py|blind exception|" in fs.read_text(repo / "arborescent-baseline.json")
    assert main(["--config", str(config), "baseline"], svc) == 0
    assert "ruff-BLE001|app/domain.py|blind exception|" in fs.read_text(repo / "arborescent-baseline.json")


def test_suppression_and_discard_counts_are_reported_and_ratcheted(repo: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    config = write_policy(repo)
    write_files(repo, {"app/domain.py": 'import os  # noqa: F401\ndiscard(task(), reason="best effort")\n'})
    fs = LocalFileSystem()
    svc = Services(FsScanner(), FakeToolRunner(), list(all_checkers().values()), fs, SystemClock(), default_engines())
    policy = mini_policy()

    first = run_check(policy, repo, svc, Options(tools=False))
    assert first.suppression_counts == {("app.domain", "noqa-coded"): 1}
    assert first.discard_counts == {"app.domain": 1}
    baseline.save(fs, repo / policy.baseline, first.violations)

    write_files(
        repo,
        {"app/domain.py": 'import os  # noqa: F401\n# type: ignore\ndiscard(task(), reason="best effort")\ndiscard(task(), reason="legacy")\n'},
    )
    grown = run_check(policy, repo, svc, Options(tools=False))
    assert grown.suppression_counts == {("app.domain", "noqa-coded"): 1, ("app.domain", "type-ignore-blanket"): 1}
    assert grown.discard_counts == {"app.domain": 2}
    assert len(grown.ratchet.new if grown.ratchet else []) == 2

    main(["--config", str(config), "check", "--no-tools"], svc)
    output = capsys.readouterr().out
    assert "suppression counts by module" in output
    assert "app.domain: noqa-coded=1, type-ignore-blanket=1" in output
    assert "discard calls by module" in output
    assert "app.domain: 2" in output


def test_prune_removes_stale_entries_without_adding_new_ones_or_touching_growth() -> None:
    fs, path = filesystem()
    baseline.record(fs, path, [v("internal-import", "a"), v("internal-import", "b")])
    baseline.record(fs, path, [v("internal-import", "a"), v("internal-import", "b"), v("internal-import", "c")], allow_growth=True, reason="why")
    growth_before = baseline.document(fs, path)["growth"]
    # "b" is gone; "n" is a brand-new violation that must NOT be recorded.
    removed = baseline.prune(fs, path, [v("internal-import", "a"), v("internal-import", "c"), v("internal-import", "n")])
    doc = baseline.document(fs, path)
    assert removed == 1
    assert sorted(str(k) for k in doc["violations"]) == sorted(v("internal-import", s).key for s in ("a", "c"))  # type: ignore[union-attr]
    assert doc["growth"] == growth_before


def test_prune_without_a_baseline_is_a_noop() -> None:
    fs, path = filesystem()
    assert baseline.prune(fs, path, [v("internal-import", "a")]) == 0
    assert not fs.is_file(path)


@pytest.mark.parametrize("approver", [None, "", "  ", 3])
def test_growth_entry_without_a_real_approved_by_is_inconsistent(approver: object) -> None:
    old = {"version": 2, "violations": [], "growth": []}
    entry: dict[str, object] = {"reason": "why", "added": ["k"]}
    if approver is not None:
        entry["approved-by"] = approver
    assert not baseline.growth_consistent({"version": 2, "violations": ["k"], "growth": [entry]}, old)


def test_baseline_growth_needs_approved_by(repo: Path) -> None:
    config = write_policy(repo)
    fs = LocalFileSystem()
    svc = Services(FsScanner(), FakeToolRunner(), list(all_checkers().values()), fs, SystemClock(), default_engines())
    args = ["--config", str(config), "baseline"]
    assert main(args, svc) == 0
    write_files(repo, {"app/domain.py": "from . import adapters\n"})
    before = (repo / "arborescent-baseline.json").read_text()
    assert main([*args, "--allow-growth", "--reason", "debt"], svc) == 2
    assert main([*args, "--allow-growth", "--reason", "debt", "--approved-by", " "], svc) == 2
    assert main([*args, "--approved-by", "me"], svc) == 2
    assert (repo / "arborescent-baseline.json").read_text() == before


def test_values_growth_requires_an_attributed_entry_for_each_raised_or_added_name() -> None:
    old = {"values": {"a": 3, "b": 5}, "growth": [{"key": "b", "reason": "r", "approved-by": "x"}]}
    entry = {"key": "a", "reason": "r", "approved-by": "me"}
    same = {"values": {"a": 3, "b": 5}, "growth": old["growth"]}
    assert baseline.values_growth_problems(same, old) == []
    assert baseline.values_growth_problems({**same, "values": {"a": 2, "b": 5}}, old) == []  # shrink needs nothing
    assert baseline.values_growth_problems({"values": {"a": 4, "b": 5}, "growth": old["growth"]}, old)  # silent raise
    assert baseline.values_growth_problems({"values": {"a": 4, "b": 5}, "growth": [*old["growth"], entry]}, old) == []
    assert baseline.values_growth_problems({"values": {"a": 3, "b": 5, "c": 0}, "growth": old["growth"]}, old)  # new name
    for bad in ({**entry, "approved-by": ""}, {**entry, "reason": " "}, {**entry, "key": "other"}):
        assert baseline.values_growth_problems({"values": {"a": 4, "b": 5}, "growth": [*old["growth"], bad]}, old)


def test_values_growth_edge_inputs() -> None:
    old = {"values": {"a": 1}, "growth": [{"key": "a", "reason": "r", "approved-by": "x"}]}
    assert baseline.values_growth_problems({"values": {"a": 1}, "growth": []}, old)  # log rewritten
    assert baseline.values_growth_problems({"values": {"a": "1"}, "growth": old["growth"]}, old)  # not a number
    assert baseline.values_growth_problems({"values": {"a": True}, "growth": old["growth"]}, old)
    assert baseline.values_growth_problems({"values": [], "growth": []}, old)
    assert baseline.values_growth_problems({"values": {"a": 1}}, {"values": {"a": 1}}) == []  # unchanged, no logs


def test_check_growth_against_covers_configured_ratchet_files(repo: Path) -> None:
    config = write_policy(repo)
    config.write_text('ratchet_files = ["budget.json"]\n' + config.read_text())
    fs = LocalFileSystem()
    fs.write_json(repo / "arborescent-baseline.json", {"version": 2, "violations": [], "growth": []})
    fs.write_json(repo / "budget.json", {"values": {"mods": 4}, "growth": []})
    git = ToolResult(0, '{"values":{"mods":3},"growth":[]}', "")
    svc = Services(FsScanner(), FakeToolRunner({"git"}, {"git": git}), list(all_checkers().values()), fs, SystemClock(), default_engines())
    args = ["--config", str(config), "check", "--no-tools", "--no-baseline", "--growth-against", "base"]
    assert main(args, svc) == 1
    fs.write_json(repo / "budget.json", {"values": {"mods": 4}, "growth": [{"key": "mods", "reason": "r", "approved-by": "me"}]})
    assert main(args, svc) == 0
    (repo / "budget.json").unlink()
    assert main(args, svc) == 1  # a missing file is a failure, not a pass


def test_stale_hint_points_at_prune_which_works_while_new_violations_exist(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_policy(repo)
    write_files(repo, {"app/domain.py": ""})
    fs = LocalFileSystem()
    svc = Services(FsScanner(), FakeToolRunner(), list(all_checkers().values()), fs, SystemClock(), default_engines())
    policy = mini_policy()
    fs.write_json(repo / policy.baseline, {"version": 2, "violations": [v("internal-import", "gone").key], "growth": []})

    main(["--config", str(config), "check", "--no-tools"], svc)
    stale_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("STALE")]
    assert stale_lines and all("arborescent baseline --prune" in line for line in stale_lines)
