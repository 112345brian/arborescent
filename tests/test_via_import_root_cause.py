"""effect-via-import groups by root cause: the last edge of the taint path plus the effect kind (#198)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, ring_with, write_files

from arborescent import baseline
from arborescent.adapters import FsScanner
from arborescent.cli_report import _print_findings
from arborescent.domain import Violation, root_cause_hint
from arborescent.engine import CheckReport
from arborescent.rules import check_modules
from arborescent.testing import FakeFileSystem

PORTS_DOMAIN = "effect-via-import|env|app.ports->app.domain"
DOMAIN = "effect-via-import|env|app.domain"


def _run(root: Path, **overrides: Any) -> list[Violation]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    return [v for v in found if v.rule == "effect-via-import"]


def _chain(repo: Path) -> list[Violation]:
    """domain reads the environment; ports imports it; adapters imports ports; main imports adapters."""
    write_files(repo, {
        "app/domain.py": "import os\ndef read():\n    return os.getenv('MODE')\n",
        "app/ports.py": "from . import domain\n",
        "app/adapters.py": "from . import ports\n",
        "app/main.py": "from . import adapters\n",
    })
    forbid = {"external": [], "forbid": ["env"]}
    ring = ring_with(
        domain={"external": ["os"], "forbid": ["env"]},
        ports={"may_import": ["domain"], **forbid},
        adapters={"may_import": ["domain", "ports"], **forbid},
        composition={"may_import": ["*"], **forbid},
    )
    return _run(repo, ring=ring)


def test_identity_keys_are_unchanged_and_the_cause_is_not_part_of_them(repo: Path) -> None:
    found = {v.subject: v for v in _chain(repo)}
    assert found["app.ports"].key == (
        "effect-via-import|app.ports|[ports] imports 'app.domain'; effect env reaches it via app.domain; inject a port instead"
    )
    assert found["app.adapters"].key.startswith("effect-via-import|app.adapters|")


def test_importers_through_the_same_last_edge_share_one_cause(repo: Path) -> None:
    found = {v.subject: v.root_cause for v in _chain(repo)}
    assert found == {"app.ports": DOMAIN, "app.adapters": PORTS_DOMAIN, "app.main": PORTS_DOMAIN}


def test_a_cause_names_the_effect_kind(repo: Path) -> None:
    write_files(repo, {
        "app/domain.py": "import os, time\nos.getenv('MODE')\ntime.time()\n",
        "app/ports.py": "from . import domain\n",
    })
    ring = ring_with(domain={"external": ["os", "time"], "forbid": []}, ports={"may_import": ["domain"], "external": [], "forbid": ["env", "clock"]})
    assert sorted(v.root_cause for v in _run(repo, ring=ring) if v.subject == "app.ports") == ["effect-via-import|clock|app.domain", "effect-via-import|env|app.domain"]


def test_a_workspace_baselined_by_root_cause_holds_one_entry_per_cause(repo: Path) -> None:
    found = _chain(repo)
    fs, path = FakeFileSystem(), Path("/repo/baseline.json")
    fs.mkdir(Path("/repo"))
    baseline.record(fs, path, found, adopt="effects", by_root_cause=True)
    doc = fs.read_json(path)
    assert doc["violations"] == []
    assert doc["root_causes"] == {DOMAIN: ["app.ports"], PORTS_DOMAIN: ["app.adapters", "app.main"]}

    covered = baseline.apply(found, baseline.load(fs, path), root_causes=baseline.load_root_causes(fs, path))
    assert covered.ok and covered.tolerated == 3


def test_a_new_importer_of_a_covered_edge_is_growth_and_fixing_the_edge_goes_stale(repo: Path) -> None:
    found = _chain(repo)
    recorded = {DOMAIN: {"app.ports"}, PORTS_DOMAIN: {"app.adapters"}}  # app.main arrived after the baseline
    grown = baseline.apply(found, set(), root_causes=recorded)
    assert [v.subject for v in grown.new] == ["app.main"]

    fixed = baseline.apply([v for v in found if v.root_cause != PORTS_DOMAIN], set(), root_causes=recorded)
    assert fixed.new == [] and len(fixed.stale) == 1 and fixed.stale[0].startswith(PORTS_DOMAIN)


def test_the_summary_counts_findings_and_causes(repo: Path) -> None:
    found = _chain(repo)
    assert baseline.rule_summary(found, found) == "by rule: effect-via-import 3 / 2 root causes (3 new)"
    assert baseline.cause_count(found) == 2


def test_by_root_cause_view_ranks_the_edges_and_prints_the_fix(capsys: pytest.CaptureFixture[str]) -> None:
    many = [Violation("effect-via-import", f"app.m{i}", f"d{i}", root_cause=PORTS_DOMAIN) for i in range(3)]
    one = [Violation("effect-via-import", "app.solo", "d", root_cause=DOMAIN)]
    report = CheckReport(violations=[*one, *many])

    class Args:
        no_baseline = True
        by_root_cause = True

    _print_findings(Args(), report)  # type: ignore[arg-type]
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("CAUSE") and PORTS_DOMAIN in lines[0] and "3 module(s)" in lines[0]
    assert lines[1] == (
        "         fix: inject a port at app.ports (so it stops importing app.domain), or add app.domain to "
        "`effects.sanctioned` if it is a capability seam, to clear 3 findings"
    )
    assert DOMAIN in lines[2] and "to clear 1 finding" in lines[3]


@pytest.mark.parametrize(
    ("cause", "expected"),
    [
        ("effect-unsanctioned-import|app.env_reader", "list app.env_reader in `effects.sanctioned`"),
        ("lazy-import|psutil", "import psutil at module level"),
        ("effect-via-import|env|app.domain", "add app.domain to `effects.sanctioned`"),
        ("something-else|x", ""),
        ("effect-via-import|malformed", ""),
        ("", ""),
    ],
)
def test_root_cause_hints(cause: str, expected: str) -> None:
    assert expected in root_cause_hint(cause, 2)
    if not expected:
        assert root_cause_hint(cause, 2) == ""
