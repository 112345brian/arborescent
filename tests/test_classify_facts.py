"""#90: a module whose facts contradict its `[classify]` ring is noted (information only), on small synthetic repositories."""

from __future__ import annotations

from pathlib import Path

from arborescent_helpers import MINI, mini_policy, services, write_files

from arborescent.adapters import FsScanner
from arborescent.domain import INFORMATIONAL_RULES, Policy, Rule, Violation
from arborescent.engine import Options, blocking, run_check, run_own
from arborescent.facts import extract

OPTS = Options(tools=False, use_baseline=False)
CLASSIFY = {**MINI["classify"], "app.adapters_*": "adapters", "app.ingest": "adapters", "app.tool": "domain", "app.pure": "adapters"}


def policy(**overrides: object) -> Policy:
    return mini_policy(**{"classify": CLASSIFY, **overrides})


def notes(root: Path, **overrides: object) -> list[Violation]:
    found, _ = run_own(policy(**overrides), root, services(), practices=False)
    return [v for v in found if v.rule == Rule.CLASSIFY_FACTS]


def one(root: Path) -> Violation:
    [note] = notes(root)
    return note


# -- the fact ------------------------------------------------------------------------------------------------------------

def test_a_top_level_main_guard_is_a_line_fact() -> None:
    assert extract('def f() -> None: ...\n\nif __name__ == "__main__":\n    f()\n').main_guard == 3
    assert extract('if "__main__" == __name__:\n    pass\n').main_guard == 1


def test_only_a_top_level_comparison_with_main_is_a_guard() -> None:
    assert extract("x = 1\n").main_guard == 0
    assert extract('def f() -> None:\n    if __name__ == "__main__":\n        pass\n').main_guard == 0
    assert extract('if __name__ == "other":\n    pass\n').main_guard == 0
    assert extract('x = "__main__"\n').main_guard == 0


# -- signal 1: the facts fit a more inner ring -----------------------------------------------------------------------------

PURE = "from __future__ import annotations\n\nfrom . import domain\n\n\ndef double() -> int:\n    return domain.VALUE * 2\n"


def test_a_module_that_passes_an_inner_rings_rules_names_the_innermost_such_ring(repo: Path) -> None:
    write_files(repo, {"app/pure.py": PURE})
    note = one(repo)
    assert (note.rule, note.subject) == (Rule.CLASSIFY_FACTS, "app.pure")
    assert note.detail.startswith("[adapters] could sit in ring 'domain'")


def test_the_note_is_stable_when_lines_move(repo: Path) -> None:
    write_files(repo, {"app/pure.py": PURE})
    first = one(repo).key
    write_files(repo, {"app/pure.py": "\n\n" + PURE})
    assert one(repo).key == first


def test_an_outward_import_keeps_a_module_where_it_is(repo: Path) -> None:
    write_files(repo, {"app/pure.py": PURE + "\nimport os\n\nSEP = os.sep\n"})
    assert notes(repo) == []


def test_an_effect_the_inner_ring_forbids_keeps_a_module_where_it_is(repo: Path) -> None:
    write_files(repo, {"app/pure.py": PURE + "\nimport os\n\nHOME = os.environ\n"})
    assert notes(repo, ring={**MINI["ring"], "domain": {"external": ["@pure", "os"]}}) == []


def test_importing_a_peer_in_the_same_ring_keeps_a_module_where_it_is(repo: Path) -> None:
    write_files(repo, {"app/pure.py": PURE + "\nfrom . import adapters\n\nSEP = adapters.SEP\n"})
    assert notes(repo) == []


def test_a_module_with_no_definitions_is_not_judged(repo: Path) -> None:
    write_files(repo, {"app/pure.py": "VALUE: int = 1\n"})
    assert notes(repo) == []


def test_a_module_declaring_an_abstract_seam_is_not_judged(repo: Path) -> None:
    write_files(repo, {"app/pure.py": "from typing import Protocol\n\n\nclass Store(Protocol):\n    def get(self) -> int: ...\n"})
    assert notes(repo) == []


def test_a_program_is_not_judged_by_the_inner_ring_signal(repo: Path) -> None:
    write_files(repo, {"app/pure.py": PURE + '\nif __name__ == "__main__":\n    double()\n'})
    assert [n for n in notes(repo) if "could sit in ring" in n.detail] == []


def test_the_innermost_ring_is_never_judged(repo: Path) -> None:
    write_files(repo, {"app/domain.py": PURE.replace("from . import domain\n\n", "")})
    assert notes(repo) == []


def test_an_entry_script_is_not_judged(repo: Path) -> None:
    write_files(repo, {"app/pure.py": PURE})
    assert notes(repo, entry_scripts=["app.pure"]) == []


# -- signal 2: a leaf that only wires peers to inner code -------------------------------------------------------------------

WIRE = (
    "from __future__ import annotations\n\nimport os\n\nfrom . import adapters_a, adapters_b, domain\n\n\n"
    "def run() -> int:\n    return adapters_a.A + adapters_b.B + domain.VALUE + len(os.sep)\n"
)
ADAPTERS = {
    "app/adapters_a.py": "import os\n\nA: int = len(os.sep)\n\n\nclass One:\n    pass\n",
    "app/adapters_b.py": "import os\n\nB: int = len(os.sep)\n\n\nclass Two:\n    pass\n",
}


def test_a_class_free_leaf_wiring_peers_to_inner_code_is_composition_shaped(repo: Path) -> None:
    write_files(repo, {**ADAPTERS, "app/ingest.py": WIRE})
    note = one(repo)
    assert note.subject == "app.ingest"
    assert "composition" in note.detail and "app.adapters_a" in note.detail and "app.adapters_b" in note.detail


def test_a_module_something_imports_is_not_a_leaf(repo: Path) -> None:
    write_files(repo, {**ADAPTERS, "app/ingest.py": WIRE, "app/main.py": "from . import ingest\n\nRUN = ingest.run\n"})
    assert notes(repo) == []


def test_a_class_defined_in_the_module_is_not_wiring(repo: Path) -> None:
    write_files(repo, {**ADAPTERS, "app/ingest.py": WIRE + "\n\nclass Own:\n    pass\n"})
    assert notes(repo) == []


def test_one_peer_is_not_enough(repo: Path) -> None:
    only = WIRE.replace("adapters_a, adapters_b, ", "adapters_a, ").replace(" + adapters_b.B", "")
    write_files(repo, {**ADAPTERS, "app/ingest.py": only})
    assert notes(repo) == []


def test_a_module_already_in_the_outermost_ring_is_not_judged(repo: Path) -> None:
    write_files(repo, {**ADAPTERS, "app/ingest.py": WIRE})
    found = notes(repo, classify={**CLASSIFY, "app.ingest": "composition"})
    assert [n for n in found if "shape of ring" in n.detail] == []


# -- signal 3: a program that other modules import ---------------------------------------------------------------------------

TOOL = 'def classify(x: int) -> int:\n    return x\n\n\nif __name__ == "__main__":\n    classify(1)\n'


def test_a_module_that_is_both_run_and_imported_mixes_a_program_with_a_library(repo: Path) -> None:
    write_files(repo, {"app/tool.py": TOOL, "app/ports.py": "from . import tool\n\nF = tool.classify\n"})
    note = one(repo)
    assert note.subject == "app.tool"
    assert "__main__" in note.detail and "app.ports" in note.detail


def test_a_program_nothing_imports_is_not_mixed(repo: Path) -> None:
    write_files(repo, {"app/tool.py": TOOL})
    assert notes(repo) == []


def test_a_library_without_a_main_guard_is_not_mixed(repo: Path) -> None:
    write_files(repo, {"app/tool.py": TOOL.split("\n\n\nif")[0] + "\n", "app/ports.py": "from . import tool\n\nF = tool.classify\n"})
    assert notes(repo) == []


# -- information only ---------------------------------------------------------------------------------------------------------

def test_the_rule_is_information_that_never_blocks_or_enters_the_baseline(repo: Path) -> None:
    assert Rule.CLASSIFY_FACTS in INFORMATIONAL_RULES
    write_files(repo, {"app/pure.py": PURE})
    found, _ = run_own(policy(), repo, services(), practices=False)
    assert blocking(found) == []
    report = run_check(policy(), repo, services(), OPTS)
    assert report.ok and report.violations == []
    assert [n.subject for n in report.notes if n.rule == Rule.CLASSIFY_FACTS] == ["app.pure"]


def test_a_clean_repository_has_no_note(repo: Path) -> None:
    assert notes(repo) == []
    assert FsScanner().scan(policy(), repo).modules  # the synthetic repository really was scanned


def test_a_program_in_the_ring_that_may_import_every_ring_is_where_programs_belong(repo: Path) -> None:
    """Dogfood: `python -m app` imports `app.cli.main` and `cli` has a guard; composition is the right home for both."""
    write_files(repo, {"app/main.py": TOOL, "app/ports.py": "from . import main\n\nF = main.classify\n"})
    assert [n for n in notes(repo) if n.subject == "app.main"] == []
