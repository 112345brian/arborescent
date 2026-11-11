"""``arborescent purity`` ranking (#183): blast radius first, grouped per ring, ``--top`` and ``--json``; presentation only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from test_mixed_purity import HYDRATION, RINGS, allow

from arborescent.adapters import FsScanner
from arborescent.cli import main
from arborescent.cli_purity import run_purity
from arborescent.rules import check_modules

LEAF = HYDRATION


def _fixture(repo: Path) -> None:
    """``app.adapters`` is alphabetically first and has the fewest importers (none); ``app.domain`` has two."""
    write_files(repo, {
        "app/__init__.py": "", "app/adapters.py": LEAF, "app/domain.py": HYDRATION,
        "app/ports.py": "from . import domain\n", "app/main.py": "from . import domain\n",
    })


def _json(repo: Path, capsys: pytest.CaptureFixture[str], top: int | None = None, **overrides: object) -> list[dict[str, object]]:
    extra = {} if top is None else {"top": top}  # the pins run without `top`
    assert run_purity(mini_policy(ring=RINGS, **overrides), repo, services(), as_json=True, **extra) == 0
    rows: list[dict[str, object]] = json.loads(capsys.readouterr().out)
    return rows


# --- pins: what the command did before this change -------------------------------------------------------------------


def test_pin_json_rows_keep_their_current_fields(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture(repo)
    rows = _json(repo, capsys)
    assert {"module", "kinds", "effectful", "pure", "line", "importers", "ring", "allowed"} <= set(rows[0])
    assert isinstance(rows, list)


def test_pin_check_findings_are_not_changed_by_ranking(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture(repo)
    policy = mini_policy(ring=RINGS)

    def findings() -> list[tuple[object, str, str, int]]:
        scan = FsScanner().scan(policy, repo)
        found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
        return [(v.rule, v.subject, v.detail, v.line) for v in found]

    before = findings()
    _json(repo, capsys, top=1)
    after = findings()
    assert before == after and any(row[0].value == "mixed-purity" for row in before)


# --- ranking ---------------------------------------------------------------------------------------------------------


def test_the_alphabetically_first_module_with_the_fewest_importers_ranks_last(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture(repo)
    rows = _json(repo, capsys)
    assert [r["module"] for r in rows] == ["app.domain", "app.adapters"]
    assert [r["importers"] for r in rows] == [2, 0]


def _mixed(pure: int) -> str:
    """A module with ``pure`` substantial pure functions and one effect."""
    body = "".join(f"def f{i}(x: int) -> int:\n    y = x + {i}\n    z = y * 2\n    w = z - 1\n    return w\n\n\n" for i in range(pure))
    return "from __future__ import annotations\n\n" + body + "def read(path: str) -> str:\n    with open(path) as handle:\n        return handle.read()\n"


def test_ties_break_on_pure_units_then_module_name(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {
        "app/__init__.py": "", "app/zeta.py": _mixed(2), "app/alpha.py": _mixed(2), "app/mid.py": _mixed(4), "app/bare.py": _mixed(3),
    })
    classify = {"app": "domain", **{f"app.{name}": "domain" for name in ("zeta", "alpha", "mid", "bare")}}
    rows = _json(repo, capsys, classify=classify)
    assert [r["importers"] for r in rows] == [0, 0, 0, 0]
    assert [r["module"] for r in rows] == ["app.mid", "app.bare", "app.alpha", "app.zeta"]


def test_top_limits_the_listing_but_not_the_count(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture(repo)
    assert [r["module"] for r in _json(repo, capsys, top=1)] == ["app.domain"]
    assert run_purity(mini_policy(ring=RINGS), repo, services(), as_json=False, top=1) == 0
    out = capsys.readouterr().out
    assert "app.domain" in out and "app.adapters" not in out
    assert "showing 1 of 2 mixed module(s)" in out


def test_top_larger_than_the_count_shows_everything(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture(repo)
    assert len(_json(repo, capsys, top=500)) == 2
    assert run_purity(mini_policy(ring=RINGS), repo, services(), as_json=False, top=500) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "2 mixed module(s)"


def test_zero_findings_with_top(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {"app/__init__.py": "", "app/domain.py": "from __future__ import annotations\n"})
    assert _json(repo, capsys, top=3) == []
    assert run_purity(mini_policy(ring=RINGS), repo, services(), as_json=False, top=3) == 0
    assert capsys.readouterr().out.strip() == "no mixed modules"


@pytest.mark.parametrize("top", [0, -1])
def test_a_non_positive_top_is_an_error(repo: Path, capsys: pytest.CaptureFixture[str], top: int) -> None:
    _fixture(repo)
    assert run_purity(mini_policy(ring=RINGS), repo, services(), as_json=False, top=top) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "--top" in captured.err


def test_the_text_listing_groups_by_ring_with_counts(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _fixture(repo)
    policy = mini_policy(ring=RINGS, mixed_purity=allow({"module": "app.adapters", "reason": "ranked, but allowed"}))
    assert run_purity(policy, repo, services(), as_json=False) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "[domain] 1 mixed module(s)" and out[2].lstrip().startswith("app.domain:")
    assert out[3] == "[adapters] 1 mixed module(s)" and "(allowed)" in out[4]
    assert "imported by 2 modules" in out[2] and "effects:" in out[2] and "pure:" in out[2]


def test_top_cli_flag_rejects_non_positive_values() -> None:
    for bad in ("0", "-2", "x"):
        with pytest.raises(SystemExit) as caught:
            main(["purity", "--top", bad])
        assert caught.value.code == 2
