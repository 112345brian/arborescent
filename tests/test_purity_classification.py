"""``mixed-purity`` classification (#200): functions that read files, take locks or start processes are not listed as pure.

Each case is one of the functions the report listed as pure although it did I/O, built so that a mixed host module has
two clean pure functions, one seed effect (so the module is mixed whatever the function under test does) and the function
under test, whose body is long enough to be counted as pure logic if it were classified pure.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from test_mixed_purity import RINGS, pure_function, run

from arborescent.cli_purity import run_explain, run_purity
from arborescent.domain import Violation

PURE = pure_function("clean_a") + pure_function("clean_b")
SEED = "def seed() -> str:\n    return input()\n\n\n"
FSIO = '''\
import os


def save_text_atomic(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(text)
    os.replace(tmp, path)


def normalise(text: str) -> str:
    lines = text.splitlines()
    kept = [line.strip() for line in lines]
    joined = "\\n".join(kept)
    return joined
'''
BODY = "    a = 1\n    b = a + 1\n    c = b + 1\n    d = c + 1\n"


def parts(found: list[Violation]) -> tuple[list[str], list[str]]:
    """(effectful names, pure names) from the hint of the single mixed-purity finding for ``app.domain``."""
    mine = [v for v in found if v.subject == "app.domain"]
    assert len(mine) == 1, found
    hint = mine[0].hint
    effectful = hint.split("effectful: ")[1].split("; pure:")[0]
    pure = hint.split("; pure: ")[1].split("; imported by")[0]
    return effectful.split(", "), pure.split(", ")


def classify(repo: Path, function: str, header: str = "", extra: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    code = "from __future__ import annotations\n\n" + header + "\n\n" + function + "\n\n\n" + SEED + PURE
    return parts(run(repo, {"app/domain.py": code, "app/fsio.py": FSIO, **(extra or {})}))


def assert_effectful(repo: Path, function: str, header: str = "", extra: dict[str, str] | None = None) -> None:
    effectful, pure = classify(repo, function, header, extra)
    assert "target" in effectful and "target" not in pure, (effectful, pure)


def assert_pure(repo: Path, function: str, header: str = "", extra: dict[str, str] | None = None) -> None:
    effectful, pure = classify(repo, function, header, extra)
    assert "target" in pure and "target" not in effectful, (effectful, pure)


def test_method_call_on_a_module_level_path_constant(repo: Path) -> None:
    header = "from pathlib import Path\n\nREGISTRY_PATH = Path(__file__).parent / 'registry.json'"
    assert_effectful(repo, f"def target() -> str:\n{BODY}    return REGISTRY_PATH.read_text()", header)


def test_a_constant_built_from_another_constant_and_a_module_level_annotation(repo: Path) -> None:
    header = "from pathlib import Path\n\nROOT: Path = Path('.')\nDATA = ROOT / 'data'"
    assert_effectful(repo, f"def target() -> None:\n{BODY}    DATA.mkdir()", header)


def test_a_local_that_shadows_the_constant_is_not_the_constant(repo: Path) -> None:
    header = "from pathlib import Path\n\nREGISTRY_PATH = Path('registry.json')"
    assert_pure(repo, f"def target(REGISTRY_PATH: str) -> str:\n{BODY}    return REGISTRY_PATH.replace('a', 'b')", header)
    assert_pure(repo, f"def target() -> str:\n    REGISTRY_PATH = 'abc'\n{BODY}    return REGISTRY_PATH.replace('a', 'b')", header)


def test_a_non_path_module_constant_is_not_a_path(repo: Path) -> None:
    header = "from pathlib import Path\n\nNAME = 'registry.json'\nHERE = Path('.')"
    assert_pure(repo, f"def target() -> str:\n{BODY}    return NAME.replace('a', 'b')", header)


def test_a_path_parameter_stays_filesystem(repo: Path) -> None:
    assert_effectful(repo, f"def target(path: Path) -> str:\n{BODY}    return path.read_text()", "from pathlib import Path")


@pytest.mark.parametrize("header, call", [
    ("from .fsio import save_text_atomic", "save_text_atomic('x', 'y')"),
    ("from . import fsio", "fsio.save_text_atomic('x', 'y')"),
    ("from app import fsio", "fsio.save_text_atomic('x', 'y')"),
    ("from app.fsio import save_text_atomic as save", "save('x', 'y')"),
])
def test_a_call_into_a_filesystem_wrapper_makes_the_caller_effectful(repo: Path, header: str, call: str) -> None:
    assert_effectful(repo, f"def target() -> None:\n{BODY}    {call}", header)


def test_a_function_level_import_of_a_wrapper_counts(repo: Path) -> None:
    assert_effectful(repo, f"def target() -> None:\n    from .fsio import save_text_atomic\n{BODY}    save_text_atomic('x', 'y')")


def test_a_call_to_a_pure_function_of_another_module_stays_pure(repo: Path) -> None:
    assert_pure(repo, f"def target(text: str) -> str:\n{BODY}    return normalise(text)", "from .fsio import normalise")


def test_an_unknown_name_or_a_library_call_is_not_followed(repo: Path) -> None:
    assert_pure(repo, f"def target(text: str) -> str:\n{BODY}    return missing(text)", "from .fsio import missing")
    assert_pure(repo, f"def target(text: str) -> str:\n{BODY}    return json.dumps(text)", "import json")


def test_an_effect_two_hops_away_is_not_followed(repo: Path) -> None:
    middle = "from .fsio import save_text_atomic\n\n\ndef relay(text: str) -> str:\n    a = text\n    b = a\n    c = b\n    return c\n"
    assert_pure(repo, f"def target(text: str) -> str:\n{BODY}    return relay(text)", "from .middle import relay", {"app/middle.py": middle})


def test_a_class_is_not_followed_through_its_constructor(repo: Path) -> None:
    holder = "import os\n\n\nclass Store:\n    def __init__(self) -> None:\n        self.n = 1\n\n    def save(self) -> None:\n        os.remove('x')\n"
    assert_pure(repo, f"def target() -> int:\n{BODY}    return Store().n", "from .holder import Store", {"app/holder.py": holder})


LOCK = '''\
import fcntl
from contextlib import contextmanager


@contextmanager
def target(handle: object):
    a = 1
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield a
    finally:
        pass
'''


def test_a_context_manager_that_takes_a_file_lock_is_effectful(repo: Path) -> None:
    code = "from __future__ import annotations\n\n" + LOCK + "\n\n" + SEED + PURE
    effectful, pure = parts(run(repo, {"app/domain.py": code}))
    assert "target" in effectful and "target" not in pure


def test_flock_alone_is_filesystem(repo: Path) -> None:
    assert_effectful(repo, f"def target(handle: object) -> None:\n{BODY}    fcntl.flock(handle, fcntl.LOCK_EX)", "import fcntl")


def test_using_a_locking_context_manager_from_another_module(repo: Path) -> None:
    lock = "from contextlib import contextmanager\nimport fcntl\n\n\n@contextmanager\ndef file_lock(handle: object):\n    fcntl.flock(handle, 2)\n    yield\n"
    assert_effectful(
        repo, f"def target(handle: object) -> None:\n{BODY}    with file_lock(handle):\n        pass", "from .locks import file_lock", {"app/locks.py": lock},
    )


def test_starting_a_process(repo: Path) -> None:
    assert_effectful(repo, f"def target() -> None:\n{BODY}    subprocess.Popen(['x'])", "import subprocess")


def test_starting_a_process_behind_a_function_level_import(repo: Path) -> None:
    assert_effectful(repo, f"def target() -> None:\n    from subprocess import Popen\n{BODY}    Popen(['x'])")


# --- every report judges a module the same way; --explain gives the evidence ---------------------------------------------


def _wrapper_fixture(repo: Path) -> None:
    code = (
        "from __future__ import annotations\n\nfrom pathlib import Path\n\nfrom .fsio import save_text_atomic\n\nREGISTRY = Path('r.json')\n\n\n"
        f"def via_wrapper() -> None:\n{BODY}    save_text_atomic('x', 'y')\n\n\n"
        f"def via_constant() -> str:\n{BODY}    return REGISTRY.read_text()\n\n\n" + SEED + PURE
    )
    write_files(repo, {"app/__init__.py": "", "app/domain.py": code, "app/fsio.py": FSIO})


def test_the_purity_command_follows_calls_like_check(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _wrapper_fixture(repo)
    assert run_purity(mini_policy(ring=RINGS), repo, services(), as_json=True) == 0
    (row,) = [r for r in json.loads(capsys.readouterr().out) if r["module"] == "app.domain"]
    assert {"via_wrapper", "via_constant"} <= set(row["effectful"]) and not {"via_wrapper", "via_constant"} & set(row["pure"])


def test_explain_names_the_call_behind_each_label(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _wrapper_fixture(repo)
    assert run_explain(mini_policy(ring=RINGS), repo, services(), "app.domain") == 0
    out = capsys.readouterr().out
    assert "via_wrapper" in out and "app.fsio.save_text_atomic" in out and "calls into app.fsio" in out
    assert "via_constant" in out and "pathlib.Path.read_text" in out
    assert "seed" in out and "console" in out
    assert "clean_a" in out and "no effect found (pure)" in out


def test_explain_reports_an_unknown_module(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _wrapper_fixture(repo)
    assert run_explain(mini_policy(ring=RINGS), repo, services(), "app.missing") == 2
    assert "no scanned module" in capsys.readouterr().err


def test_every_caller_with_a_scan_passes_it_to_assess() -> None:
    """``assess`` without ``followed`` silently reads only the module's own text, so two reports could judge one module differently."""
    src = Path(__file__).resolve().parent.parent / "src" / "arborescent"
    needed = {"assess": 3, "assess_in_ring": 4}
    missing: list[str] = []
    for path in sorted(src.glob("*.py")):
        if path.name == "purity.py":
            continue  # `excluded_findings` has only the excluded modules, by design
        for node in ast.walk(ast.parse(path.read_text())):
            is_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in needed
            if is_call and len(node.args) + len(node.keywords) < needed[node.func.id]:  # type: ignore[attr-defined]
                missing.append(f"{path.name}:{node.lineno}")  # type: ignore[attr-defined]
    assert missing == []
