"""``baseline report`` (#146): every baseline shape is read, clustered and flagged, and the burn-down comes from git history."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from arborescent_helpers import write_files

from arborescent import baseline_history, baseline_report
from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.cli import main
from arborescent.domain import ToolResult
from arborescent.engine import Services
from arborescent.engines import default_engines
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers

KEYS = [
    "internal-import|app.core.a|import `app.db` is forbidden|1",
    "internal-import|app.core.b|import `app.db` is forbidden|2",
    "internal-import|app.core.c|import `app.db` is forbidden|3",
    "ruff-T201|app.cli.x|`print` found|4",
]


def test_key_list_is_split_into_rule_subject_and_target() -> None:
    shape, entries = baseline_report.parse_entries({"version": 2, "violations": KEYS, "growth": []})
    assert shape == "keys"
    assert [(e.rule, e.subject, e.target, e.weight) for e in entries][:2] == [
        ("internal-import", "app.core.a", "app.db", 1),
        ("internal-import", "app.core.b", "app.db", 1),
    ]
    assert baseline_report.parse_entries(KEYS)[0] == "keys"  # a bare list reads the same


def test_counter_shape_weights_each_module_by_its_count_and_skips_meta_keys() -> None:
    shape, entries = baseline_report.parse_entries({"version": 1, "app.core.a": 7, "app.cli.x": 3})
    assert shape == "counts"
    assert {e.subject: e.weight for e in entries} == {"app.core.a": 7, "app.cli.x": 3}


def test_registry_shape_reads_fields_and_defaults_the_rest() -> None:
    data = {
        "legacy_a": {"rule": "dead-code", "module": "app.core.a", "target": "app.old", "count": 4},
        "legacy_b": {"note": "no fields we know"},
    }
    shape, entries = baseline_report.parse_entries(data)
    assert shape == "records"
    by_ident = {e.ident: e for e in entries}
    assert (by_ident["legacy_a"].rule, by_ident["legacy_a"].subject, by_ident["legacy_a"].weight) == ("dead-code", "app.core.a", 4)
    assert (by_ident["legacy_b"].subject, by_ident["legacy_b"].weight) == ("legacy_b", 1)


@pytest.mark.parametrize("bad", [42, "text", [1, 2], {"a": "x"}, {"a": True}])
def test_unknown_shapes_are_refused_by_name(bad: object) -> None:
    with pytest.raises(ValueError, match="baseline"):
        baseline_report.parse_entries(bad)


def test_empty_baselines_report_nothing_without_dividing_by_zero() -> None:
    empties: list[object] = [[], {}, {"version": 2, "violations": []}, {"a": 0}]
    for data in empties:
        analysis = baseline_report.analyse(baseline_report.parse_entries(data)[1], 2, 5, 0.25)
        assert analysis.total <= 0
        assert analysis.concentration == []


def test_clusters_are_ranked_with_their_share_of_the_total() -> None:
    analysis = baseline_report.analyse(baseline_report.parse_entries(KEYS)[1], 2, 10, 0.25)
    assert [(c.key, c.count, c.share) for c in analysis.clusters["rule"]] == [("internal-import", 3, 0.75), ("ruff-T201", 1, 0.25)]
    assert [(c.key, c.count) for c in analysis.clusters["family"]] == [("app.core", 3), ("app.cli", 1)]
    assert (analysis.clusters["target"][0].key, analysis.clusters["target"][0].count) == ("app.db", 3)


def test_family_depth_applies_to_dotted_names_and_paths() -> None:
    assert baseline_report.family("a.b.c.d", 2) == "a.b"
    assert baseline_report.family("src/pkg/mod.py", 2) == "src/pkg"
    assert baseline_report.family("solo", 3) == "solo"


def test_concentration_flags_family_and_target_above_the_threshold_only() -> None:
    entries = baseline_report.parse_entries(KEYS)[1]
    flagged = {(name, c.key) for name, c in baseline_report.analyse(entries, 2, 10, 0.25).concentration}
    assert flagged == {("family", "app.core"), ("target", "app.db")}
    assert baseline_report.analyse(entries, 2, 10, 0.75).concentration == []  # 75% is not above 75%


def test_a_cluster_below_the_minimum_count_is_never_flagged() -> None:
    entries = baseline_report.parse_entries(["r|app.a|d|1", "r|app.a|d|2"])[1]
    assert baseline_report.analyse(entries, 2, 10, 0.1).concentration == []


@dataclass
class ScriptedGit:
    """Answers ``git log`` and ``git show <sha>:<path>`` from a list of ``(sha, date, baseline text)``, newest first."""

    history: list[tuple[str, str, str]]
    calls: list[list[str]] = field(default_factory=lambda: [])

    def available(self, executable: str) -> bool:
        return executable == "git"

    def run(self, argv: Sequence[str], cwd: Path, timeout: float = 600.0) -> ToolResult:
        self.calls.append(list(argv))
        if argv[1] == "log":
            return ToolResult(0, "".join(f"{sha} {day}\n" for sha, day, _ in self.history), "")
        wanted = argv[2].split(":", 1)[0]
        text = next(text for sha, _, text in self.history if sha == wanted)
        return ToolResult(0, text, "")


def doc(*keys: str) -> str:
    return json.dumps({"version": 2, "violations": list(keys), "growth": []})


def test_burn_down_counts_additions_and_removals_per_month() -> None:
    git = ScriptedGit([("c3", "2026-11-02", doc("a|x|1")), ("c2", "2026-10-20", doc("a|x|1", "a|y|2", "a|z|3")), ("c1", "2026-10-01", doc("a|x|1", "a|y|2"))])
    result = baseline_history.burndown(git, Path("/repo"), "bl.json", "month", 50)
    assert isinstance(result, baseline_history.Burndown)
    assert [(p.label, p.added, p.removed, p.net, p.total) for p in result.periods] == [("2026-10", 3, 0, 3, 3), ("2026-11", 0, 2, -2, 1)]
    assert not result.only_grows
    assert not result.truncated


def test_burn_down_reads_counter_baselines_by_weight() -> None:
    git = ScriptedGit([("c2", "2026-02-01", json.dumps({"m": 2})), ("c1", "2026-01-01", json.dumps({"m": 5}))])
    result = baseline_history.burndown(git, Path("/repo"), "bl.json", "year", 50)
    assert isinstance(result, baseline_history.Burndown)
    assert [(p.label, p.added, p.removed, p.total) for p in result.periods] == [("2026", 5, 3, 2)]


def test_a_baseline_that_only_grows_is_flagged() -> None:
    git = ScriptedGit([("c2", "2026-02-01", doc("a|x|1", "a|y|2")), ("c1", "2026-01-01", doc("a|x|1"))])
    result = baseline_history.burndown(git, Path("/repo"), "bl.json", "month", 50)
    assert isinstance(result, baseline_history.Burndown)
    assert result.only_grows


def test_the_history_limit_seeds_from_the_oldest_revision_read() -> None:
    git = ScriptedGit([("c3", "2026-03-01", doc("a|x|1", "a|y|2")), ("c2", "2026-02-01", doc("a|x|1")), ("c1", "2026-01-01", doc())])
    result = baseline_history.burndown(git, Path("/repo"), "bl.json", "month", 2)
    assert isinstance(result, baseline_history.Burndown)
    assert result.truncated
    assert [(p.label, p.added, p.total) for p in result.periods] == [("2026-02", 0, 1), ("2026-03", 1, 2)]


def test_no_history_is_a_note_not_a_failure() -> None:
    untracked = baseline_history.burndown(ScriptedGit([]), Path("/repo"), "bl.json", "month", 10)
    assert isinstance(untracked, str)
    assert "not tracked" in untracked
    no_git = baseline_history.burndown(FakeToolRunner(), Path("/repo"), "bl.json", "month", 10)
    assert isinstance(no_git, str)
    assert no_git.startswith("no git history")


def test_unreadable_revisions_are_skipped() -> None:
    git = ScriptedGit([("c2", "2026-02-01", doc("a|x|1")), ("c1", "2026-01-01", "not json")])
    result = baseline_history.burndown(git, Path("/repo"), "bl.json", "month", 10)
    assert isinstance(result, baseline_history.Burndown)
    assert result.commits == 1


def project(tmp_path: Path, baseline: str) -> Path:
    write_files(
        tmp_path,
        {
            "arborescent.toml": 'sources = ["app"]\nrings = ["domain"]\n[ring.domain]\nexternal = []\n[classify]\n"app" = "domain"\n',
            "app/__init__.py": "",
            "arborescent-baseline.json": baseline,
        },
    )
    return tmp_path / "arborescent.toml"


def services(runner: ScriptedGit | FakeToolRunner) -> Services:
    return Services(FsScanner(), runner, list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines())


def test_command_prints_json_for_the_policy_baseline(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = project(tmp_path, doc(*KEYS))
    git = ScriptedGit([("c1", "2026-01-05", doc(*KEYS))])
    assert main(["--config", str(config), "baseline", "report", "--json"], services(git)) == 0
    report = json.loads(capsys.readouterr().out)["files"][0]
    assert (report["path"], report["shape"], report["total"]) == ("arborescent-baseline.json", "keys", 4)
    assert report["clusters"]["rule"][0] == {"key": "internal-import", "count": 3, "share": 0.75}
    assert {(c["dimension"], c["key"]) for c in report["concentration"]} == {("family", "app.core"), ("target", "app.db")}
    assert report["burn_down"]["periods"] == [{"period": "2026-01", "added": 4, "removed": 0, "net": 4, "total": 4}]
    assert report["burn_down"]["only_grows"] is True


def test_command_prints_text_and_skips_git_when_asked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = project(tmp_path, doc(*KEYS))
    git = ScriptedGit([])
    assert main(["--config", str(config), "baseline", "report", "--no-history"], services(git)) == 0
    out = capsys.readouterr().out
    assert "4 total" in out
    assert "CONCENTRATED family app.core" in out
    assert "burn-down" not in out
    assert git.calls == []


def test_command_reports_several_files_and_refuses_a_missing_or_unknown_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = project(tmp_path, doc(*KEYS))
    write_files(tmp_path, {"counts.json": json.dumps({"app.x": 9})})
    svc = services(FakeToolRunner())
    assert main(["--config", str(config), "baseline", "report", "--json", "--no-history", "arborescent-baseline.json", "counts.json"], svc) == 0
    files = json.loads(capsys.readouterr().out)["files"]
    assert [(f["path"], f["shape"], f["total"]) for f in files] == [("arborescent-baseline.json", "keys", 4), ("counts.json", "counts", 9)]
    assert main(["--config", str(config), "baseline", "report", "missing.json"], svc) == 2
    write_files(tmp_path, {"weird.json": "42"})
    assert main(["--config", str(config), "baseline", "report", "weird.json"], svc) == 2
    assert main(["--config", str(config), "baseline", "report", "--depth", "0"], svc) == 2


def test_the_plain_baseline_command_still_records(tmp_path: Path) -> None:
    config = project(tmp_path, doc())
    assert main(["--config", str(config), "baseline"], services(FakeToolRunner())) == 0
