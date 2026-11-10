"""#161 stage 1: strict function-level admission (``admit_check``, ``arborescent admit``) and the ``ring_reason`` key."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, ring_with, services, write_files

from arborescent.adapters import FsScanner
import arborescent.admission_probes as probes
from arborescent.classify_audit import AdmissionOutcome, admission_warnings, admit_check
from arborescent.cli import main
from arborescent.facts import ModuleInfo, extract
from arborescent.rules import RuleContext
from arborescent.scanner import _facts_from_json, _facts_to_json

SRC = Path(__file__).resolve().parents[1] / "src" / "arborescent"
HEADER = "from __future__ import annotations\n\n"
CONFIG = (
    'sources = ["app"]\nrings = ["domain", "ports", "adapters", "composition"]\n'
    '[groups]\npure = ["__future__", "dataclasses", "typing", "collections"]\n'
    '[ring.domain]\nexternal = ["@pure"]\n[ring.ports]\nmay_import = ["domain"]\nexternal = ["@pure"]\n'
    '[ring.adapters]\nmay_import = ["domain", "ports"]\nexternal = ["@pure", "os", "importlib"]\n'
    '[ring.composition]\nmay_import = ["*"]\nexternal = ["*"]\n'
    '[classify]\n"app" = "domain"\n"app.domain" = "domain"\n"app.ports" = "ports"\n"app.adapters" = "adapters"\n"app.main" = "composition"\n'
)


def outcome(root: Path, source: str, ring: str, reason: str | None = None) -> AdmissionOutcome:
    write_files(root, {**MINI_FILES, "app/mod.py": source})
    policy = mini_policy(ring=ring_with(composition={"may_import": ["*"], "external": ["*"]}))
    scan = FsScanner().scan(policy, root)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    return admit_check(context, "app.mod", scan.modules["app.mod"], ring, sorted(scan.known), reason)


def refusals(root: Path, source: str, ring: str, reason: str | None = None) -> list[str]:
    return list(outcome(root, source, ring, reason).refusals)


# -- ring_reason in the facts --------------------------------------------------------------------------------------------------

def test_ring_reason_is_a_legal_key_and_is_trimmed() -> None:
    facts = extract('__arbor__ = {"ring": "adapters", "ring_reason": "  wraps os  "}\n')
    assert (facts.arbor_ring, facts.arbor_reason, facts.arbor_error) == ("adapters", "wraps os", None)


@pytest.mark.parametrize("value", ['""', '"   "', '"\\n\\t"', '"a\\x00b"', "1", "None", "['x']"])
def test_a_blank_nul_or_non_string_reason_is_an_invalid_annotation(value: str) -> None:
    facts = extract(f'__arbor__ = {{"ring": "adapters", "ring_reason": {value}}}\n')
    assert facts.arbor_error == '__arbor__."ring_reason" must be a non-blank string'
    assert facts.arbor_ring is None and facts.arbor_reason is None


def test_a_reason_alone_is_still_missing_a_ring() -> None:
    assert extract('__arbor__ = {"ring_reason": "why"}\n').arbor_error == '__arbor__ dictionary is missing "ring"'


def test_a_module_without_a_reason_has_none() -> None:
    assert extract('__arbor__ = {"ring": "domain"}\n').arbor_reason is None


def test_the_new_facts_survive_the_cache_round_trip() -> None:
    facts = extract('import importlib\n__arbor__ = {"ring": "domain", "ring_reason": "r"}\nimportlib.import_module("a.b")\n__import__(name)\n')
    assert _facts_from_json(_facts_to_json(facts)) == facts
    assert facts.dynamic_imports == ((3, "a.b"), (4, ""))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('import importlib\nimportlib.import_module("pkg.x")\n', ((2, "pkg.x"),)),
        ('from importlib import import_module as im\nim(name="pkg.x")\n', ((2, "pkg.x"),)),
        ('import importlib\nimportlib.import_module(x)\n', ((2, ""),)),
        ('import importlib\nimportlib.import_module(".rel", "pkg")\n', ((2, ""),)),
        ('import importlib\nimportlib.import_module()\n', ((2, ""),)),
        ('__import__("pkg")\n', ((1, "pkg"),)),
        ('def f(x):\n    return __import__(x)\n', ((2, ""),)),
        ('importlib = object()\nimportlib.other("pkg")\n', ()),
        ("", ()),
    ],
)
def test_dynamic_import_targets(source: str, expected: tuple[tuple[int, str], ...]) -> None:
    assert extract(source).dynamic_imports == expected


# -- admit_check ----------------------------------------------------------------------------------------------------------------

def test_a_module_that_fits_the_tightest_ring_is_admitted(tmp_path: Path) -> None:
    found = outcome(tmp_path, HEADER + "X: int = 1\n", "domain")
    assert found.admitted and found.refusals == () and found.notes == ()
    assert "externals ['__future__']" in found.evidence


def test_an_empty_module_fits_only_the_innermost_ring(tmp_path: Path) -> None:
    assert outcome(tmp_path, "", "domain").admitted
    [text] = refusals(tmp_path, "", "adapters")
    assert text.startswith("ring 'domain' also fits")


def test_a_tighter_ring_that_fits_refuses(tmp_path: Path) -> None:
    [text] = refusals(tmp_path, HEADER + "from . import domain\n\nX: int = domain.VALUE\n", "adapters")
    assert "ring 'domain' also fits" in text


def test_a_recorded_reason_excuses_a_tighter_ring_and_blank_does_not(tmp_path: Path) -> None:
    source = HEADER + "X: int = 1\n"
    assert outcome(tmp_path, source, "adapters", "wraps the OS later").admitted
    for blank in ("", "   ", "a\x00b"):
        assert not outcome(tmp_path, source, "adapters", blank).admitted


def test_the_files_own_reason_is_read(tmp_path: Path) -> None:
    source = HEADER + '__arbor__ = {"ring": "adapters", "ring_reason": "wraps the OS later"}\nX: int = 1\n'
    found = outcome(tmp_path, source, "adapters")
    assert found.admitted and found.notes == ()  # `domain` fits, so the recorded reason is doing its job


def test_a_reason_with_no_tighter_ring_to_excuse_is_reported_as_stale(tmp_path: Path) -> None:
    found = outcome(tmp_path, HEADER + "import os\n\nX = os.sep\n", "adapters", "needs os")
    assert found.admitted and len(found.notes) == 1 and "stale" in found.notes[0]


def test_facts_that_violate_the_ring_refuse(tmp_path: Path) -> None:
    [text] = refusals(tmp_path, HEADER + "import os\n\nX = os.sep\n", "domain")
    assert text.startswith("its facts violate ring 'domain'")


def test_a_lazy_import_violating_the_ring_refuses(tmp_path: Path) -> None:
    source = HEADER + "def f() -> int:\n    from . import adapters\n    return len(adapters.SEP)\n"
    assert any("violate ring 'ports'" in text for text in refusals(tmp_path, source, "ports"))


def test_a_type_checking_import_violating_the_ring_refuses(tmp_path: Path) -> None:
    source = HEADER + "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from . import adapters\n"
    assert any("violate ring 'ports'" in text for text in refusals(tmp_path, source, "ports"))


def test_a_literal_dynamic_import_is_an_edge(tmp_path: Path) -> None:
    source = HEADER + 'import importlib\n\nM = importlib.import_module("app.adapters")\n'
    texts = refusals(tmp_path, source, "ports", "r")
    assert any("violate ring 'ports'" in text for text in texts)


def test_a_literal_dynamic_import_to_an_allowed_ring_is_admitted(tmp_path: Path) -> None:
    source = HEADER + 'import importlib\n\nM = importlib.import_module("app.domain")\n'
    assert outcome(tmp_path, source, "composition").admitted


def test_a_non_literal_dynamic_import_is_unverifiable(tmp_path: Path) -> None:
    source = HEADER + "import importlib\n\n\ndef load(name: str) -> object:\n    return importlib.import_module(name)\n"
    assert any("non-literal target (line 7)" in text for text in refusals(tmp_path, source, "composition"))
    assert outcome(tmp_path, source, "composition", "load(name) imports plugins named in the config").admitted


def test_an_unknown_ring_refuses(tmp_path: Path) -> None:
    assert refusals(tmp_path, HEADER + "X: int = 1\n", "core")[0].startswith("unknown ring 'core'")


def test_every_reason_is_reported_together(tmp_path: Path) -> None:
    source = HEADER + "import importlib\nimport os\n\nX = os.sep\nM = importlib.import_module(os.sep)\n"
    assert len(refusals(tmp_path, source, "ports")) == 2  # the facts, and the unreadable dynamic import


def test_admission_warns_about_importers_that_cannot_reach_the_ring(tmp_path: Path) -> None:
    write_files(tmp_path, {**MINI_FILES, "app/mod.py": HEADER + "X: int = 1\n", "app/domain.py": HEADER + "from . import mod\n\nVALUE: int = mod.X\n"})
    policy = mini_policy()
    scan = FsScanner().scan(policy, tmp_path)
    context = RuleContext(policy, scan.arbor_index, scan.scope_by_module)
    found = admission_warnings(context, scan.modules, sorted(scan.known), "app.mod", "adapters")
    assert found == ("app.domain [domain] imports it and may not import ring 'adapters'",)
    assert admission_warnings(context, scan.modules, sorted(scan.known), "app.mod", "domain") == ()


# -- the command ----------------------------------------------------------------------------------------------------------------

def run_admit(root: Path, *args: str) -> int:
    (root / "arborescent.toml").write_text(CONFIG, encoding="utf-8")
    return main(["--config", str(root / "arborescent.toml"), "admit", *args], services())


def test_admit_is_a_dry_run_by_default(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {"app/mod.py": HEADER + "X: int = 1\n"})
    assert run_admit(repo, "app.mod", "--ring", "domain") == 0
    assert "would annotate app/mod.py (ring: domain)" in capsys.readouterr().out
    assert "__arbor__" not in (repo / "app/mod.py").read_text(encoding="utf-8")


def test_admit_yes_writes_the_annotation_and_a_valid_reason(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {"app/mod.py": HEADER + "X: int = 1\n"})
    assert run_admit(repo, "app.mod", "--ring", "adapters", "--reason", "wraps os later", "--yes") == 0
    facts = extract((repo / "app/mod.py").read_text(encoding="utf-8"))
    assert (facts.arbor_ring, facts.arbor_reason, facts.arbor_error) == ("adapters", "wraps os later", None)
    assert run_admit(repo, "app.mod", "--ring", "adapters") == 0  # a second run: admitted, already annotated
    assert "already annotated" in capsys.readouterr().out


def test_admit_refuses_with_exit_code_one_and_writes_nothing(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {"app/mod.py": HEADER + "X: int = 1\n"})
    assert run_admit(repo, "app.mod", "--ring", "adapters", "--yes") == 1
    assert "refused: ring 'domain' also fits" in capsys.readouterr().out
    assert "__arbor__" not in (repo / "app/mod.py").read_text(encoding="utf-8")


def test_admit_with_a_blank_reason_refuses_and_writes_nothing(repo: Path) -> None:
    write_files(repo, {"app/mod.py": HEADER + "X: int = 1\n"})
    assert run_admit(repo, "app.mod", "--ring", "adapters", "--reason", "  ", "--yes") == 1
    assert "__arbor__" not in (repo / "app/mod.py").read_text(encoding="utf-8")


def test_admit_with_a_nul_reason_never_writes_a_broken_annotation(repo: Path) -> None:
    write_files(repo, {"app/mod.py": HEADER + "import os\n\nX = os.sep\n"})
    assert run_admit(repo, "app.mod", "--ring", "adapters", "--reason", "a\x00b", "--yes") == 1
    assert "__arbor__" not in (repo / "app/mod.py").read_text(encoding="utf-8")


def test_admit_an_unknown_module_or_ring_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_admit(repo, "app.nope", "--ring", "domain") == 1
    assert "not a scanned module" in capsys.readouterr().out
    assert run_admit(repo, "app.domain", "--ring", "core") == 1
    assert "unknown ring 'core'" in capsys.readouterr().out


def test_admit_does_not_overwrite_an_annotation_for_another_ring(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {"app/mod.py": HEADER + '__arbor__ = {"ring": "ports"}\nX: int = 1\n'})
    assert run_admit(repo, "app.mod", "--ring", "domain", "--yes") == 1
    assert "annotated with ring 'ports'" in capsys.readouterr().out


def test_admit_reports_a_malformed_annotation(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, {"app/mod.py": HEADER + '__arbor__ = {"ring": "ports", "ring_reason": ""}\n'})
    assert run_admit(repo, "app.mod", "--ring", "ports") == 1
    assert "ring_reason" in capsys.readouterr().out


# -- a refused outcome cannot be silently dropped -----------------------------------------------------------------------------

def test_no_caller_discards_an_admission_outcome() -> None:
    callers: list[str] = []
    for path in SRC.glob("*.py"):
        if path.name == "classify_audit.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for stmt in ast.walk(tree):
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and getattr(stmt.value.func, "id", "") == "admit_check":
                pytest.fail(f"{path.name}:{stmt.lineno} discards the outcome of admit_check")
        if any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "admit_check" for n in ast.walk(tree)):
            callers.append(path.name)
    assert sorted(callers) == ["admission_probes.py", "cli_admit.py"]  # a new caller must be added to this scan on purpose


def test_the_result_discard_facts_see_a_dropped_outcome() -> None:
    assert "admit_check" in extract("from x import admit_check\nadmit_check()\n").bare_calls[0][1]
    assert extract("from x import admit_check\nr = admit_check()\n").bare_calls == ()


def test_the_legal_keys_are_documented() -> None:
    docs = (Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert "ring_reason" in docs and "arborescent admit" in docs


def test_info_is_a_module_info() -> None:
    assert ModuleInfo.__name__ == "ModuleInfo"


def test_the_selftest_admission_probes_pass_on_the_mini_policy(repo: Path) -> None:
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo)
    killed, survived, skipped = probes.admission_probes(policy, scan.arbor_index, scan.scope_by_module, scan.known)
    assert survived == [] and skipped == [] and len(killed) == 6


def test_a_broken_gate_would_survive_the_probes(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo)
    real = probes.admit_check
    monkeypatch.setattr(probes, "admit_check", lambda *a, **k: AdmissionOutcome(a[1], a[3], True, (), (), real(*a, **k).evidence))
    _, survived, _ = probes.admission_probes(policy, scan.arbor_index, scan.scope_by_module, scan.known)
    assert survived
